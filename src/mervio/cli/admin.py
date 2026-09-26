"""Commandes `mervio admin ...` (Mission 004.3.7): provisionnement, inspection, demonstration.

Couche MINCE: arguments -> `mervio.admin.operations` -> affichage. Aucune regle ici.

    mervio admin user ensure        --subject S
    mervio admin org create|list|show
    mervio admin member add|list
    mervio admin store create|list
    mervio admin connection create
    mervio admin service authorize|revoke|list
    mervio admin object upload      --store T --kind KIND --file CHEMIN
    mervio admin job enqueue-import|enqueue-analysis|enqueue-purge|enqueue-redact|list|show|cancel|stats
    mervio admin audit list
    mervio admin demo provision     --owner S --data-dir D [--service ROLE]

Identite: `--as <sujet>` nomme l'humain qui agit. Il doit exister, et chaque operation relit en
base son appartenance et son role dans l'organisation `--org`: l'identifiant d'organisation seul
n'accorde rien. Connexion: `MERVIO_DATABASE_URL` (ou `..._FILE`), role applicatif ordinaire;
un superutilisateur ou un role BYPASSRLS est refuse.

Sortie: texte `cle=valeur` par defaut, ou `--json` (un objet sur une ligne, sur stdout, y compris
pour une erreur). Codes: voir `mervio.admin.errors` (0 succes, 1 interne, 2 usage/configuration,
3 base injoignable, 4 schema non migre, 5 introuvable, 6 refuse, 7 conflit).

La persistance (psycopg) n'est importee qu'a l'execution d'une commande: la CLI d'analyse reste
utilisable sans l'extra `persistence`.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Dict
from uuid import UUID

from ..admin.errors import EXIT_INTERNAL, EXIT_OK, AdminError, ConfigurationRefused
from ..settings import AdminSettings, SettingsError

_DATABASE_DRIVERS = ("psycopg", "psycopg_binary", "psycopg_c")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
JOB_TYPES = ("import", "analysis", "purge", "redact_customer")
ASSIGNABLE_ROLES = ("admin", "analyst", "viewer")
#: Types de source acceptes. Recopies ici VOLONTAIREMENT: la CLI reste importable sans l'extra
#: `persistence` (barriere verifiee par tests/test_persistence_boundaries.py). Definition
#: canonique: `mervio.persistence.raw_objects.SOURCE_KINDS` et la contrainte de la revision 0013.
SOURCE_KINDS = ("shopify_orders", "shopify_products", "stripe", "google_ads")

EPILOG = ("Codes de sortie: 0 succes, 1 erreur interne, 2 usage ou configuration invalide, "
          "3 base injoignable, 4 schema non migre, 5 introuvable, 6 refuse, 7 conflit. "
          "Connexion: MERVIO_DATABASE_URL ou MERVIO_DATABASE_URL_FILE (role applicatif).")


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("identifiant UUID attendu") from None


def _bounded(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("entier attendu") from None
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"entre {minimum} et {maximum}")
        return number
    return parse


# =============================================================================================
# Analyseur
# =============================================================================================

def add_admin_parser(sub) -> None:
    admin = sub.add_parser(
        "admin", help="administration operateur (provisionnement, inspection, demonstration)",
        description="Administration operateur de Mervio. Jamais exposee en HTTP. Chaque operation "
                    "d'organisation est executee AU NOM de l'humain --as, dont l'appartenance et le role "
                    "sont verifies en base.", epilog=EPILOG)
    groups = admin.add_subparsers(dest="admin_group", required=True, metavar="GROUPE")

    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--json", action="store_true", help="sortie JSON sur une ligne (stdout)")
    actor = argparse.ArgumentParser(add_help=False, parents=[output])
    actor.add_argument("--as", dest="actor", required=True, metavar="SUJET",
                       help="sujet d'identite de l'humain qui agit (doit exister)")
    scoped = argparse.ArgumentParser(add_help=False, parents=[actor])
    scoped.add_argument("--org", required=True, type=_uuid, metavar="UUID", help="organisation")
    idempotent = argparse.ArgumentParser(add_help=False)
    idempotent.add_argument("--priority", type=_bounded(-100, 100), default=0, help="priorite (-100 a 100)")
    idempotent.add_argument("--idempotency-key", dest="idempotency_key", metavar="CLE",
                            help="rejouer la commande renvoie le meme travail")

    def leaf(group, name, handler, help_text, parents):
        parser = group.add_parser(name, help=help_text, description=help_text, parents=parents, epilog=EPILOG)
        parser.set_defaults(admin_handler=handler)
        return parser

    def group(name, help_text):
        parser = groups.add_parser(name, help=help_text, description=help_text)
        return parser.add_subparsers(dest="admin_action", required=True, metavar="ACTION")

    users = group("user", "identites humaines (sujet OIDC opaque, sans PII)")
    parser = leaf(users, "ensure", "user_ensure", "cree l'identite si elle manque (idempotent)", [output])
    parser.add_argument("--subject", required=True, metavar="SUJET")

    orgs = group("org", "organisations")
    parser = leaf(orgs, "create", "org_create", "cree une organisation dont --as est proprietaire "
                  "(idempotent par proprietaire et nom)", [actor])
    parser.add_argument("--name", required=True)
    leaf(orgs, "list", "org_list", "organisations dont --as est membre", [actor])
    leaf(orgs, "show", "org_show", "etat operationnel d'une organisation (boutiques, file, services)", [scoped])

    members = group("member", "membres d'une organisation (proprietaire)")
    parser = leaf(members, "add", "member_add", "ajoute un humain existant (idempotent si meme role)", [scoped])
    parser.add_argument("--subject", required=True, metavar="SUJET")
    parser.add_argument("--role", required=True, choices=ASSIGNABLE_ROLES)
    leaf(members, "list", "member_list", "membres et roles", [scoped])

    store_group = group("store", "boutiques")
    parser = leaf(store_group, "create", "store_create", "cree une boutique (idempotent par nom)", [scoped])
    parser.add_argument("--name", required=True)
    parser.add_argument("--currency", metavar="ISO4217", help="devise, ex. EUR")
    leaf(store_group, "list", "store_list", "boutiques de l'organisation", [scoped])

    connections = group("connection", "connexions de donnees (CSV)")
    parser = leaf(connections, "create", "connection_create",
                  "cree une connexion CSV (idempotent par libelle actif)", [scoped])
    parser.add_argument("--store", required=True, type=_uuid, metavar="UUID")
    parser.add_argument("--label", required=True)

    services = group("service", "autorisations des workers (proprietaire)")
    for name, handler, text in (("authorize", "service_authorize", "autorise un worker (idempotent)"),
                                ("revoke", "service_revoke", "revoque l'autorisation active (effet immediat)")):
        parser = leaf(services, name, handler, text, [scoped])
        parser.add_argument("--service", required=True, metavar="ROLE",
                            help="role PostgreSQL du worker (principal service:<role>)")
    parser = leaf(services, "list", "service_list", "autorisations de l'organisation", [scoped])
    parser.add_argument("--include-revoked", action="store_true", dest="include_revoked")

    objects = group("object", "objets bruts (magasin d'objets, jamais de chemin pour le worker)")
    parser = leaf(objects, "upload", "object_upload",
                  "depose un fichier LOCAL dans le magasin et renvoie son identifiant", [scoped])
    parser.add_argument("--store", required=True, type=_uuid, metavar="UUID")
    parser.add_argument("--kind", required=True, choices=SOURCE_KINDS, help="type de source")
    parser.add_argument("--file", required=True, metavar="CHEMIN",
                        help="fichier lu LOCALEMENT; il n'entre ni en base, ni dans un travail")

    job_group = group("job", "travaux (file PostgreSQL)")
    parser = leaf(job_group, "enqueue-import", "job_import", "met en file un import CSV", [scoped, idempotent])
    parser.add_argument("--store", required=True, type=_uuid, metavar="UUID")
    parser.add_argument("--connection", required=True, type=_uuid, metavar="UUID")
    for option, dest in (("--shopify-orders", "shopify_orders"), ("--shopify-products", "shopify_products"),
                         ("--stripe", "stripe"), ("--google-ads", "google_ads")):
        parser.add_argument(option, dest=dest, type=_uuid, metavar="UUID",
                            help="identifiant d'objet brut (mervio admin object upload)")
    parser.add_argument("--synthetic", action="store_true", help="donnees synthetiques (demonstration)")
    parser = leaf(job_group, "enqueue-analysis", "job_analysis", "met en file une analyse d'instantane",
                  [scoped, idempotent])
    parser.add_argument("--store", required=True, type=_uuid, metavar="UUID")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", type=_uuid, metavar="UUID")
    source.add_argument("--from-import", dest="from_import", type=_uuid, metavar="JOB",
                        help="instantane produit par ce travail d'import reussi")
    parser.add_argument("--grain", choices=("week", "month"))
    parser.add_argument("--as-of", dest="as_of", metavar="AAAA-MM-JJ", help="date de reference")
    parser.add_argument("--label")
    leaf(job_group, "enqueue-purge", "job_purge", "met en file une purge de retention (politique par defaut)",
         [scoped, idempotent])
    parser = leaf(job_group, "enqueue-redact", "job_redact",
                  "met en file l'effacement d'UN client, designe par une reference DEJA resolue",
                  [scoped, idempotent])
    parser.add_argument("--customer-ref", dest="customer_ref", required=True, metavar="REFERENCE",
                        help="reference client canonique (c1: ou g1: puis 32 hexadecimaux minuscules), "
                             "obtenue par `mervio worker resolve-customer-ref`; `admin` n'accepte "
                             "AUCUNE identite directe et ne detient pas la cle maitre (D-062)")
    parser = leaf(job_group, "list", "job_list", "travaux recents", [scoped])
    parser.add_argument("--status", action="append", choices=JOB_STATUSES)
    parser.add_argument("--type", dest="job_type", action="append", choices=JOB_TYPES)
    parser.add_argument("--store", type=_uuid, metavar="UUID")
    parser.add_argument("--limit", type=_bounded(1, 1000), default=50)
    for name, handler, text in (("show", "job_show", "detail d'un travail (chemins masques)"),
                                ("cancel", "job_cancel", "annule un travail encore en file")):
        parser = leaf(job_group, name, handler, text, [scoped])
        parser.add_argument("--job", required=True, type=_uuid, metavar="UUID")
    leaf(job_group, "stats", "job_stats", "compteurs de la file", [scoped])

    audit_group = group("audit", "journal d'audit (administrateur)")
    parser = leaf(audit_group, "list", "audit_list", "evenements, du plus ancien au plus recent", [scoped])
    parser.add_argument("--action", action="append", metavar="ACTION")
    parser.add_argument("--limit", type=_bounded(1, 1000), default=50)

    demo = group("demo", "demonstration sur donnees SYNTHETIQUES")
    parser = leaf(demo, "provision", "demo_provision",
                  "identite, organisation, boutique, connexion, donnees synthetiques, autorisation du worker "
                  "et import en file (rejouable)", [output])
    parser.add_argument("--owner", required=True, metavar="SUJET", help="proprietaire (cree s'il manque)")
    parser.add_argument("--data-dir", dest="data_dir", required=True, metavar="CHEMIN",
                        help="repertoire ABSOLU des CSV synthetiques, lisible par le worker")
    parser.add_argument("--service", metavar="ROLE", help="worker a autoriser sur l'organisation de demonstration")


# =============================================================================================
# Execution
# =============================================================================================

def _sources(args) -> Dict[str, Any]:
    """Un identifiant d'OBJET BRUT par source (004.4.4): plus jamais un chemin (D-054)."""
    return {kind: getattr(args, kind) for kind in ("shopify_orders", "shopify_products", "stripe", "google_ads")
            if getattr(args, kind) is not None}


HANDLERS: Dict[str, Callable[[Any, Any, Any], Dict[str, Any]]] = {
    "user_ensure": lambda ops, db, a: ops.ensure_user(db, a.subject),
    "org_create": lambda ops, db, a: ops.create_organization(db, actor=a.actor, name=a.name),
    "org_list": lambda ops, db, a: ops.list_organizations(db, actor=a.actor),
    "org_show": lambda ops, db, a: ops.show_organization(db, actor=a.actor, organization=a.org),
    "member_add": lambda ops, db, a: ops.add_member(db, actor=a.actor, organization=a.org, subject=a.subject,
                                                    role=a.role),
    "member_list": lambda ops, db, a: ops.list_members(db, actor=a.actor, organization=a.org),
    "store_create": lambda ops, db, a: ops.create_store(db, actor=a.actor, organization=a.org, name=a.name,
                                                        currency=a.currency),
    "store_list": lambda ops, db, a: ops.list_stores(db, actor=a.actor, organization=a.org),
    "connection_create": lambda ops, db, a: ops.create_connection(db, actor=a.actor, organization=a.org,
                                                                  store=a.store, label=a.label),
    "service_authorize": lambda ops, db, a: ops.authorize_service(db, actor=a.actor, organization=a.org,
                                                                  service_name=a.service),
    "service_revoke": lambda ops, db, a: ops.revoke_service(db, actor=a.actor, organization=a.org,
                                                            service_name=a.service),
    "service_list": lambda ops, db, a: ops.list_service_authorizations(db, actor=a.actor, organization=a.org,
                                                                       include_revoked=a.include_revoked),
    "object_upload": lambda ops, db, a: ops.upload_object(
        db, actor=a.actor, organization=a.org, store=a.store, kind=a.kind, file=a.file,
        object_store_settings=a.object_store_settings),
    "job_import": lambda ops, db, a: ops.enqueue_import(
        db, actor=a.actor, organization=a.org, store=a.store, connection=a.connection,
        raw_objects_by_kind=_sources(a), synthetic=a.synthetic, priority=a.priority,
        idempotency_key=a.idempotency_key),
    "job_analysis": lambda ops, db, a: ops.enqueue_analysis(
        db, actor=a.actor, organization=a.org, store=a.store, snapshot=a.snapshot, from_import=a.from_import,
        grain=a.grain, as_of=a.as_of, label=a.label, priority=a.priority, idempotency_key=a.idempotency_key),
    "job_purge": lambda ops, db, a: ops.enqueue_purge(db, actor=a.actor, organization=a.org, priority=a.priority,
                                                      idempotency_key=a.idempotency_key),
    "job_redact": lambda ops, db, a: ops.enqueue_redact(db, actor=a.actor, organization=a.org,
                                                        customer_ref=a.customer_ref, priority=a.priority,
                                                        idempotency_key=a.idempotency_key),
    "job_list": lambda ops, db, a: ops.list_jobs(db, actor=a.actor, organization=a.org, status=a.status,
                                                 job_type=a.job_type, store=a.store, limit=a.limit),
    "job_show": lambda ops, db, a: ops.show_job(db, actor=a.actor, organization=a.org, job=a.job),
    "job_cancel": lambda ops, db, a: ops.cancel_job(db, actor=a.actor, organization=a.org, job=a.job),
    "job_stats": lambda ops, db, a: ops.job_statistics(db, actor=a.actor, organization=a.org),
    "audit_list": lambda ops, db, a: ops.list_audit_events(db, actor=a.actor, organization=a.org,
                                                           action=a.action, limit=a.limit),
    "demo_provision": lambda ops, db, a: ops.demo_provision(db, owner=a.owner, data_dir=a.data_dir,
                                                        object_store_settings=a.object_store_settings,
                                                            service_name=a.service),
}


def cmd_admin(args, *, environ=None, stdout=None, database_factory=None) -> int:
    """Execute une commande `mervio admin`. `environ` et `database_factory` servent aux tests."""
    stdout = sys.stdout if stdout is None else stdout
    as_json = bool(getattr(args, "json", False))
    try:
        from ..admin import operations
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] not in _DATABASE_DRIVERS:
            raise
        return _fail(ConfigurationRefused("dependance absente: installer l'extra mervio[persistence]",
                                          code="dependency_missing"), as_json, stdout)
    try:
        settings = AdminSettings.from_env(environ)
    except SettingsError as error:
        # noms de variables et regles seulement, jamais une valeur
        problems = ", ".join(f"{name} ({rule})" for name, rule in error.problems)
        return _fail(ConfigurationRefused(f"configuration invalide: {problems}"), as_json, stdout)

    # le magasin d'objets accompagne les arguments: la couche CLI reste MINCE, aucune regle ici
    args.object_store_settings = settings.object_store

    factory = database_factory or operations.connect
    database = None
    try:
        database = factory(settings.database_url.reveal())
        result = operations.guarded(lambda: HANDLERS[args.admin_handler](operations, database, args))
    except AdminError as error:
        return _fail(error, as_json, stdout)
    except Exception as exc:  # noqa: BLE001 - dernier filet: jamais de trace ni de message brut
        return _fail(operations.translate(exc), as_json, stdout)
    finally:
        if database is not None:
            database.close()
    _print_result(result, as_json, stdout)
    return EXIT_OK


def _fail(error: AdminError, as_json: bool, stdout) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": error.to_dict()}, sort_keys=True), file=stdout)
    else:
        print(f"erreur [{error.code}]: {error.message}", file=sys.stderr)
    return error.exit_code if error.exit_code else EXIT_INTERNAL


# =============================================================================================
# Affichage
# =============================================================================================

def _print_result(result: Dict[str, Any], as_json: bool, stdout) -> None:
    if as_json:
        print(json.dumps({"ok": True, "result": result}, sort_keys=True, ensure_ascii=False), file=stdout)
        return
    for line in render_text(result):
        print(line, file=stdout)


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    text = str(value)
    if not text or any(char.isspace() for char in text) or "=" in text or '"' in text:
        return json.dumps(text, ensure_ascii=False)
    return text


def render_text(document: Dict[str, Any], prefix: str = "") -> list:
    """Lignes `cle=valeur` stables; une liste d'objets donne une ligne par element."""
    lines = []
    for key in sorted(document, key=lambda k: (k != "status", k)):
        value = document[key]
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value and all(not isinstance(v, (dict, list)) for v in value.values()):
            lines.append(f"{name}: " + " ".join(f"{k}={_scalar(v)}" for k, v in sorted(value.items())))
        elif isinstance(value, dict):
            lines.extend(render_text(value, prefix=f"{name}.") if value else [f"{name}: -"])
        elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
            lines.append(f"{name}: {len(value)}")
            for item in value:
                lines.append("  - " + " ".join(f"{k}={_scalar(v)}" for k, v in sorted(item.items())))
        else:
            lines.append(f"{name}: {_scalar(value)}")
    return lines


__all__ = ["HANDLERS", "add_admin_parser", "cmd_admin", "render_text"]
