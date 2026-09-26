"""Commandes `mervio worker` et `mervio worker healthcheck` (Mission 004.3.6).

Couche MINCE: aucune logique de worker, de cycle de vie ni de sante ici.

    mervio worker                lit WorkerSettings, configure les logs, installe SIGTERM/SIGINT,
                                 puis delegue tout a workers.runtime.WorkerRuntime;
                                 le code de sortie est celui du runtime:
                                 0 arret propre, 1 erreur interne, 2 configuration/identite refusee,
                                 3 base injoignable, 4 schema non migre, 5 arret force
    mervio worker healthcheck    lit le fichier de sante (observability.health.check_health),
                                 sans base ni reseau; 0 sain, 1 non sain (convention HEALTHCHECK
                                 des conteneurs: jamais d'autre code, meme configuration invalide)
    mervio worker
        resolve-customer-ref     identite lue sur STDIN -> `c1:<32 hex>` sur STDOUT, et rien
                                 d'autre (004.4.5 E6, D-062); ne persiste rien, ne met rien en
                                 file, n'ecrit aucun audit, ne journalise rien. Codes: 0 resolu,
                                 1 erreur interne, 2 configuration ou resolution refusee,
                                 3 base injoignable, 4 schema non migre

Le runtime importe la persistance (psycopg): il n'est importe qu'a l'execution de la
commande, pour que la CLI d'analyse reste utilisable sans l'extra `persistence`.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Callable, Optional

from ..observability.health import check_health
from ..observability.logging import configure, get_event_logger
from ..settings import HealthCheckSettings, SettingsError, WorkerSettings

#: Codes du runtime, repris ici pour les commandes qui ne le chargent pas (`EXIT_CONFIG` quand le
#: runtime n'est meme pas importable, les autres pour `resolve-customer-ref`).
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CONFIG = 2
EXIT_DATABASE = 3
EXIT_SCHEMA = 4
HEALTHY = 0
UNHEALTHY = 1
_DATABASE_DRIVERS = ("psycopg", "psycopg_binary", "psycopg_c")


def add_worker_parser(sub) -> None:
    worker = sub.add_parser(
        "worker", help="lance le processus worker (configuration par variables MERVIO_*)",
        description="Processus worker longue duree. Configuration: variables d'environnement MERVIO_* "
                    "(voir mervio.settings). Codes de sortie: 0 arret propre, 1 erreur interne, "
                    "2 configuration ou identite refusee, 3 base injoignable, 4 schema non migre, "
                    "5 arret force.")
    commands = worker.add_subparsers(dest="worker_command")
    check = commands.add_parser(
        "healthcheck", help="verdict de sante du worker d'apres son fichier (sans base)",
        description="Lit MERVIO_WORKER_HEALTH_FILE et rend un verdict. Code 0 sain, 1 non sain.")
    check.add_argument("--ready", action="store_true",
                       help="controle de disponibilite (ready/busy) au lieu du controle de vie")
    check.add_argument("--health-file", dest="health_file",
                       help="chemin ABSOLU du fichier de sante (remplace MERVIO_WORKER_HEALTH_FILE)")
    check.add_argument("--json", action="store_true", help="sortie JSON sur une ligne")

    resolve = commands.add_parser(
        "resolve-customer-ref",
        help="reference client canonique d'une identite lue sur STDIN (jamais en argument)",
        description="Resout une identite client en `customer_ref` canonique (`c1:<32 hex>`), "
                    "localement au worker et HORS FILE (D-062). L'identite est lue sur STDIN et "
                    "n'est JAMAIS acceptee en argument: un argument est lisible par `ps` et "
                    "conserve par l'historique du shell. stdout ne porte que la reference; rien "
                    "n'est ecrit en base, en file, en audit, en log ni sur disque. "
                    "Enchainer ensuite: mervio admin job enqueue-redact --customer-ref <reference>.",
        epilog="Codes de sortie: 0 resolu, 1 erreur interne, 2 configuration ou resolution "
               "refusee, 3 base injoignable, 4 schema non migre.")
    resolve.add_argument("--org", required=True, metavar="UUID",
                         help="organisation dans laquelle resoudre (la reference lui est propre)")
    resolve.add_argument("--as", dest="actor", required=True, metavar="SUJET",
                         help="sujet d'identite de l'humain membre au nom de qui le sel est lu "
                              "(il doit exister; rang analyst au moins, impose par la revision 0011)")


def cmd_worker(args) -> int:
    command = getattr(args, "worker_command", None)
    if command == "healthcheck":
        return cmd_healthcheck(args)
    if command == "resolve-customer-ref":
        return cmd_resolve_customer_ref(args)
    return run_worker()


# -- processus -------------------------------------------------------------------------------

def _refusal_logger(stream):
    """Logs JSON par defaut: la configuration n'est pas lisible, la sortie doit rester machine."""
    configure(format="json", level=logging.INFO, service="worker", stream=stream)
    return get_event_logger("worker")


def run_worker(*, settings_loader: Optional[Callable[[], WorkerSettings]] = None, stream=None,
               registry=None, **runtime_options) -> int:
    """Point d'entree du processus. `registry` et `runtime_options` ne servent qu'aux tests."""
    stream = sys.stderr if stream is None else stream
    try:
        from ..workers.runtime import WorkerRuntime, configure_process_logging
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] not in _DATABASE_DRIVERS:
            raise
        _refusal_logger(stream).error("worker.dependency_missing", dependency="psycopg",
                                      remedy="installer l'extra mervio[persistence]", exit_code=EXIT_CONFIG)
        return EXIT_CONFIG
    try:
        settings = (settings_loader or WorkerSettings.from_env)()
    except SettingsError as error:
        # variables et regles seulement: SettingsError ne porte jamais de valeur
        _refusal_logger(stream).error(
            "worker.config_invalid", exit_code=EXIT_CONFIG,
            problems=[{"variable": name, "rule": rule} for name, rule in error.problems])
        return EXIT_CONFIG
    configure_process_logging(settings, stream=stream)
    runtime = WorkerRuntime(settings, registry=registry, **runtime_options)
    restore = runtime.install_signal_handlers()
    try:
        return runtime.run().exit_code
    finally:
        restore()


# -- resolution identite -> customer_ref (004.4.5 E6, D-062) ---------------------------------

#: Classe de refus -> code de sortie. TOUS les refus de resolution partagent `EXIT_CONFIG`: la
#: valeur de sortie ne doit pas devenir l'oracle que les messages evitent d'etre (D-062).
_EXIT_BY_KIND = {"refused": EXIT_CONFIG, "config": EXIT_CONFIG, "database": EXIT_DATABASE,
                 "schema": EXIT_SCHEMA, "internal": EXIT_INTERNAL}


def cmd_resolve_customer_ref(args, *, environ=None, stdin=None, stdout=None, stderr=None) -> int:
    """Identite sur STDIN -> `c1:<32 hex>` sur STDOUT. Rien d'autre, jamais (D-062).

    Couche MINCE, comme le reste de ce module: le cadrage de l'entree, la connexion et la
    derivation appartiennent a `workers.resolution`. Ici, seulement les flux, la traduction des
    refus en codes de sortie, et le silence.

    STDOUT EST UN CONTRAT: en cas de succes, exactement la reference et un saut de ligne; en cas
    de refus, RIEN. Un operateur capture cette sortie pour la passer a
    `mervio admin job enqueue-redact --customer-ref`; de la prose, un en-tete ou un message
    d'erreur sur stdout y deviendrait une reference invalide.

    Aucune journalisation n'est configuree: un log de resolution -- meme sans l'identite -- ferait
    de stderr la trace de l'exercice d'une capacite de reidentification, que D-062 laisse
    explicitement au conteneur et au systeme, et pas au produit.
    """
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        from ..workers import resolution
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] not in _DATABASE_DRIVERS:
            raise
        print("resolution refusee: dependency_missing", file=stderr)
        return EXIT_CONFIG
    try:
        settings = resolution.load_settings(environ)
        reference = resolution.resolve(
            settings, organization=args.org, actor_subject=args.actor,
            identity_bytes=_read_identity(stdin, resolution.MAX_IDENTITY_BYTES))
    except resolution.ResolutionRefused as refused:
        # un CODE, jamais une valeur fournie; et JAMAIS rien sur stdout
        print(f"resolution refusee: {refused.code}", file=stderr)
        return _EXIT_BY_KIND.get(refused.kind, EXIT_INTERNAL)
    print(reference, file=stdout)  # le SEUL ecrit sur stdout de toute la commande
    return EXIT_OK


def _read_identity(stream, limit: int) -> bytes:
    """Lit STDIN, borne a `limit` + 1 octets: UN de plus que ce que `parse_identity` accepte, pour
    qu'une entree trop longue soit REFUSEE et jamais tronquee en silence -- resoudre le prefixe
    d'une identite donnerait une reference valide, mais pour quelqu'un d'autre."""
    raw = stream.read(limit + 1)
    return raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8")


# -- sante -----------------------------------------------------------------------------------

def _print_verdict(document: dict, as_json: bool, stream) -> None:
    if as_json:
        print(json.dumps(document, sort_keys=True), file=stream)
        return
    status = "healthy" if document["healthy"] else "unhealthy"
    print(f"{status} check={document['check']} state={document['state'] or '-'} reason={document['reason']}",
          file=stream)


def cmd_healthcheck(args, *, environ=None, now: Optional[float] = None, stream=None) -> int:
    stream = sys.stdout if stream is None else stream
    check = "readiness" if args.ready else "liveness"
    try:
        settings = HealthCheckSettings.from_env(environ)
    except SettingsError as error:
        _print_verdict({"check": check, "healthy": False, "live": False, "ready": False, "state": None,
                        "reason": "config", "problems": [name for name, _ in error.problems]}, args.json, stream)
        return UNHEALTHY
    path = settings.health_file
    if args.health_file is not None:
        path = Path(args.health_file)
        if not path.is_absolute():
            _print_verdict({"check": check, "healthy": False, "live": False, "ready": False, "state": None,
                            "reason": "config", "problems": ["--health-file"]}, args.json, stream)
            return UNHEALTHY
    verdict = check_health(path, max_age_seconds=settings.health_max_age_seconds,
                           degraded_grace_seconds=settings.degraded_grace_seconds, now=now)
    healthy = verdict.ready if args.ready else verdict.live
    _print_verdict({"check": check, "healthy": healthy, "live": verdict.live, "ready": verdict.ready,
                    "state": verdict.state, "reason": verdict.reason}, args.json, stream)
    return HEALTHY if healthy else UNHEALTHY


__all__ = ["add_worker_parser", "cmd_healthcheck", "cmd_resolve_customer_ref", "cmd_worker", "run_worker"]
