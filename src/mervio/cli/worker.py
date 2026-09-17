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

#: Code du runtime pour une configuration refusee, repris ici quand le runtime n'est pas importable.
EXIT_CONFIG = 2
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


def cmd_worker(args) -> int:
    if getattr(args, "worker_command", None) == "healthcheck":
        return cmd_healthcheck(args)
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


__all__ = ["add_worker_parser", "cmd_healthcheck", "cmd_worker", "run_worker"]
