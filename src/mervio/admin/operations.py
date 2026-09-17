"""Cas d'usage d'administration (Mission 004.3.7).

Chaque operation:
1. valide ses entrees AVANT toute requete (texte, identifiants, chemins);
2. resout l'acteur `--as` en identite HUMAINE existante (jamais creee au passage, jamais un service);
3. passe par une `TenantSession`: l'appartenance et le role sont relus en base, et la RLS forcee
   s'applique; un `organization_id` fourni n'accorde rien;
4. verifie la permission de l'operation AVANT de reveler quoi que ce soit (existence d'une
   boutique, d'un sujet, d'un service);
5. trace dans `audit_events`, dans la transaction qui cree ou change la ressource, avec
   l'acteur humain comme `actor_id` (il agit pour lui-meme: `on_behalf_of` reste vide).

Idempotence (provisionnement): une ressource deja presente avec les memes attributs est renvoyee
avec `status = "existing"` et rien n'est trace; des attributs differents sont un conflit. Les
lectures "existe deja?" et la creation sont serialisees par un verrou consultatif de session
(`tenancy.provisioning_lock`), donc deux executions concurrentes ne creent pas de doublon.

Les resultats sont des dictionnaires JSON: identifiants et dates en texte, cles stables.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, TypeVar
from uuid import UUID

from ..application.workspace import is_sample_path
from ..observability.logging import get_event_logger, scrub
from ..observability.redaction import redact_text
from ..persistence import audit, jobs, service, snapshots, stores, tenancy
from ..persistence.audit import Action, ActorType, Outcome, ResourceType
from ..persistence.database import Database
from ..persistence.errors import (
    JobStateError, NotFound, PayloadRejected, PermissionDenied, SchemaNotReady, ServiceIdentityError,
    UnsafeDatabaseConfiguration,
)
from ..persistence.jobs import JobRecord, JobStatus, JobType
from ..persistence.retry import database_error_code, database_failure_kind
from ..persistence.tenancy import Permission, Role, TenantContext, TenantSession
from ..synthetic import GeneratorConfig, generate_dataset
from ..synthetic.evaluation import analysis_day
from ..workers import worker as job_commands
from .errors import (
    AdminError, ConfigurationRefused, Conflict, DatabaseUnavailable, Forbidden, InvalidInput, NotFoundError,
    SchemaMissing,
)

#: Roles qu'un proprietaire peut attribuer par la CLI. `owner` est exclu: la co-propriete et le
#: transfert relevent d'un parcours dedie (membres, 004.x), pas d'une commande d'operateur.
ASSIGNABLE_ROLES = ("admin", "analyst", "viewer")
SOURCE_KINDS = ("shopify_orders", "shopify_products", "stripe", "google_ads")
SERVICE_SUBJECT_PREFIX = service.SERVICE_SUBJECT_PREFIX
MAX_PATH_LENGTH = 1024

DEMO_ORGANIZATION = "Mervio Demo (synthetic)"
DEMO_STORE = "Demo Store (synthetic)"
DEMO_CONNECTION = "Demo CSV imports (synthetic)"
#: Jeu synthetique de la demonstration: le meme que les tests PostgreSQL (analyse garantie).
DEMO_DATASET = {"orders": 1500, "days": 56, "seed": 7}
DEMO_FILES = {"shopify_orders": "shopify_orders.csv", "shopify_products": "shopify_products.csv",
              "stripe": "stripe_transactions.csv", "google_ads": "google_ads.csv"}

_SERVICE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,62}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}

T = TypeVar("T")


def connect(url: str) -> Database:
    """Connexion applicative de la CLI. `Database` refuse un superutilisateur ou un role BYPASSRLS."""
    return Database(url, application_name="mervio-admin", connect_timeout=10)


# =============================================================================================
# Traduction des erreurs
# =============================================================================================

def translate(exc: BaseException) -> AdminError:
    """Erreur publiable d'une exception quelconque. Jamais le message d'un serveur ni d'un pilote."""
    if isinstance(exc, AdminError):
        return exc
    if isinstance(exc, NotFound):  # y compris TenantAccessDenied: indiscernable d'une absence
        return NotFoundError(f"{exc.kind} introuvable")
    if isinstance(exc, PermissionDenied):
        return Forbidden(f"role {exc.role} insuffisant pour {exc.permission}")
    if isinstance(exc, JobStateError):
        return Conflict(redact_text(str(exc)), code="invalid_state")
    if isinstance(exc, PayloadRejected):
        return InvalidInput(redact_text(str(exc)), code="payload_rejected")
    if isinstance(exc, SchemaNotReady):
        return SchemaMissing("schema non migre: appliquer les migrations")
    if isinstance(exc, ServiceIdentityError):
        return ConfigurationRefused("identite de connexion refusee")
    if isinstance(exc, UnsafeDatabaseConfiguration):
        # message fixe du module database: ne cite ni URL ni role
        return ConfigurationRefused(redact_text(str(exc)))
    kind = database_failure_kind(exc)
    code = database_error_code(exc)
    if kind == "unavailable":
        return DatabaseUnavailable("base de donnees injoignable ou connexion refusee")
    if kind == "schema":
        return SchemaMissing("schema non migre: appliquer les migrations")
    if kind == "conflict":
        return Conflict("refuse par une contrainte de la base", code=code)
    if kind == "privilege":
        return Forbidden("refuse par la base", code=code)
    if kind == "other":
        return AdminError("erreur de base de donnees", code=code)
    if isinstance(exc, ValueError):
        return InvalidInput(redact_text(str(exc)))
    return AdminError("erreur interne", code=type(exc).__name__)


def guarded(call: Callable[[], T]) -> T:
    """Execute `call` et ne laisse sortir qu'une `AdminError`."""
    try:
        return call()
    except AdminError:
        raise
    except Exception as exc:  # noqa: BLE001 - toute erreur est traduite, jamais publiee brute
        raise translate(exc) from None


# =============================================================================================
# Validation des entrees
# =============================================================================================

def _clean(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field}: valeur non vide attendue")
    if len(value) > maximum:
        raise InvalidInput(f"{field}: {maximum} caracteres au plus")
    if value != value.strip():
        raise InvalidInput(f"{field}: pas d'espace en debut ni en fin")
    if any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in value):
        raise InvalidInput(f"{field}: caractere de controle ou invisible refuse")
    return value


def validate_subject(value: Any, *, field: str = "subject", allow_service: bool = False) -> str:
    subject = _clean(value, field, maximum=255)
    if not allow_service and subject.startswith(SERVICE_SUBJECT_PREFIX):
        raise InvalidInput(f"{field}: le prefixe '{SERVICE_SUBJECT_PREFIX}' est reserve aux services")
    return subject


def validate_name(value: Any, field: str = "name") -> str:
    return _clean(value, field, maximum=200)


def validate_service_name(value: Any) -> str:
    if not isinstance(value, str) or not _SERVICE_NAME.fullmatch(value):
        raise InvalidInput("service: nom de role PostgreSQL attendu (lettres, chiffres, _ . -)")
    return value


def validate_currency(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise InvalidInput("currency: code ISO 4217 en majuscules attendu")
    return value


def validate_uuid(value: Any, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise InvalidInput(f"{field}: identifiant UUID attendu") from None


def validate_source_path(value: Any, field: str) -> str:
    """Chemin lu par le WORKER: absolu, normalise, sans `..`. Son existence se verifie a l'import."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_LENGTH:
        raise InvalidInput(f"{field}: chemin absolu de {MAX_PATH_LENGTH} caracteres au plus attendu")
    if "\x00" in value or any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in value):
        raise InvalidInput(f"{field}: caractere de controle refuse")
    if not os.path.isabs(value) or os.path.normpath(value) != value or ".." in Path(value).parts:
        raise InvalidInput(f"{field}: chemin absolu et normalise attendu (sans '..')")
    return value


def validate_idempotency_key(value: Any) -> Optional[str]:
    if value is None:
        return None
    return _clean(value, "idempotency-key", maximum=200)


def validate_limit(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1000:
        raise InvalidInput("limit: entier entre 1 et 1000")
    return value


def validate_priority(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not -100 <= value <= 100:
        raise InvalidInput("priority: entier entre -100 et 100")
    return value


# =============================================================================================
# Acteur, session, audit
# =============================================================================================

def resolve_actor(database: Database, subject: Any) -> UUID:
    """Identifiant de l'humain `--as`. Il doit deja exister: une faute de frappe ne cree personne."""
    wanted = validate_subject(subject, field="as", allow_service=True)
    user = tenancy.find_user(database, wanted)
    if user is None:
        raise NotFoundError("acteur introuvable (creer l'identite: mervio admin user ensure)", code="actor_not_found")
    if not user.human:
        raise Forbidden("un principal de service n'administre aucune organisation")
    return user.id


def _session(database: Database, actor: Any, organization: Any) -> TenantSession:
    organization_id = validate_uuid(organization, "org")
    return TenantSession(database, TenantContext(organization_id, resolve_actor(database, actor)))


def _require(session: TenantSession, permission: Permission) -> None:
    """Appartenance et role verifies en base, sans rien lire d'autre."""
    with session.transaction(permission):
        pass


def _tracer(session: TenantSession, correlation_id: UUID, action: Action, resource_type: ResourceType, *,
            store_id: Optional[UUID] = None, metadata: Optional[Mapping[str, Any]] = None):
    def trace(conn, resource_id: UUID) -> None:
        audit.record(conn, organization_id=session.organization_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, correlation_id=correlation_id, outcome=Outcome.SUCCEEDED,
                     actor_type=ActorType.USER, actor_id=session.user_id, store_id=store_id,
                     metadata=metadata)
    return trace


@contextmanager
def _lock(database: Database, scope: str) -> Iterator[None]:
    with tenancy.provisioning_lock(database, "mervio.admin:" + scope):
        yield


# =============================================================================================
# Rendu
# =============================================================================================

def _text(value: Any) -> Any:
    if isinstance(value, (UUID, datetime, date)):
        return value.isoformat() if not isinstance(value, UUID) else str(value)
    return value


def _organization(organization_id: UUID, name: str) -> Dict[str, Any]:
    return {"id": str(organization_id), "name": name}


def _store(store: stores.Store) -> Dict[str, Any]:
    return {"id": str(store.id), "name": store.name, "currency": store.currency,
            "created_at": _text(store.created_at)}


def _connection(connection: stores.Connection) -> Dict[str, Any]:
    return {"id": str(connection.id), "store_id": str(connection.store_id), "kind": connection.kind,
            "label": connection.label, "status": connection.status, "created_at": _text(connection.created_at),
            "revoked_at": _text(connection.revoked_at)}


def _authorization(authorization: service.ServiceAuthorization, names: Mapping[UUID, str]) -> Dict[str, Any]:
    return {"id": str(authorization.id), "service_id": str(authorization.service_user_id),
            "service": names.get(authorization.service_user_id), "active": authorization.active,
            "granted_by": str(authorization.granted_by), "granted_at": _text(authorization.granted_at),
            "revoked_by": _text(authorization.revoked_by), "revoked_at": _text(authorization.revoked_at)}


def render_job(job: JobRecord, *, detailed: bool = False) -> Dict[str, Any]:
    document = {
        "id": str(job.id), "job_type": job.job_type, "status": job.status,
        "store_id": _text(job.store_id), "priority": job.priority, "attempts": job.attempts,
        "max_attempts": job.max_attempts, "created_at": _text(job.created_at),
        "finished_at": _text(job.finished_at), "last_error_code": job.last_error_code,
    }
    if detailed:
        document.update({
            "organization_id": str(job.organization_id), "idempotency_key": job.idempotency_key,
            "correlation_id": str(job.correlation_id), "enqueued_by": _text(job.enqueued_by),
            "available_at": _text(job.available_at), "started_at": _text(job.started_at),
            "locked_by": job.locked_by, "lease_expires_at": _text(job.lease_expires_at),
            "updated_at": _text(job.updated_at), "last_error": job.last_error,
            # un chemin de source ou une cle sensible n'est jamais reaffiche
            "payload": scrub(job.payload or {}),
            "result": scrub(job.result) if job.result is not None else None,
        })
    return document


def _event(event: audit.AuditEvent) -> Dict[str, Any]:
    return {"id": str(event.id), "created_at": _text(event.created_at), "action": event.action,
            "outcome": event.outcome, "actor_type": event.actor_type, "actor_id": _text(event.actor_id),
            "on_behalf_of": _text(event.on_behalf_of), "resource_type": event.resource_type,
            "resource_id": str(event.resource_id), "store_id": _text(event.store_id),
            "correlation_id": str(event.correlation_id), "metadata": event.metadata}


# =============================================================================================
# Identites
# =============================================================================================

def ensure_user(database: Database, subject: Any) -> Dict[str, Any]:
    """Identite humaine d'un sujet OIDC opaque. Idempotent. Pas d'organisation, donc pas d'audit."""
    wanted = validate_subject(subject)
    with _lock(database, "user:" + wanted):
        existing = tenancy.find_user(database, wanted)
        if existing is not None:
            return {"status": "existing", "user": {"id": str(existing.id), "subject": existing.subject}}
        user_id = tenancy.ensure_user(database, wanted)
    return {"status": "created", "user": {"id": str(user_id), "subject": wanted}}


# =============================================================================================
# Organisations
# =============================================================================================

def create_organization(database: Database, *, actor: Any, name: Any) -> Dict[str, Any]:
    """Organisation dont l'acteur est proprietaire. Idempotent par (proprietaire, nom exact)."""
    wanted = validate_name(name)
    owner_id = resolve_actor(database, actor)
    with _lock(database, f"organization:{owner_id}"):
        matches = [organization_id for organization_id, _ in _owned_named(database, owner_id, wanted)]
        if len(matches) > 1:
            raise Conflict("plusieurs organisations de cet acteur portent ce nom", code="ambiguous")
        if matches:
            return {"status": "existing", "organization": _organization(matches[0], wanted), "role": "owner"}
        correlation_id = uuid.uuid4()

        def trace(conn, organization_id: UUID) -> None:
            audit.record(conn, organization_id=organization_id, action=Action.ORGANIZATION_CREATED,
                         resource_type=ResourceType.ORGANIZATION, resource_id=organization_id,
                         correlation_id=correlation_id, actor_type=ActorType.USER, actor_id=owner_id,
                         metadata={"owner_id": str(owner_id)})

        organization_id = tenancy.create_organization(database, owner_user_id=owner_id, name=wanted, hook=trace)
    return {"status": "created", "organization": _organization(organization_id, wanted), "role": "owner"}


def _owned_named(database: Database, owner_id: UUID, name: str):
    for organization_id in tenancy.organizations_of(database, owner_id):
        session = TenantSession(database, TenantContext(organization_id, owner_id))
        if session.role() is Role.OWNER and tenancy.organization_name(session) == name:
            yield organization_id, session


def list_organizations(database: Database, *, actor: Any) -> Dict[str, Any]:
    user_id = resolve_actor(database, actor)
    rows = []
    for organization_id in tenancy.organizations_of(database, user_id):
        session = TenantSession(database, TenantContext(organization_id, user_id))
        rows.append({**_organization(organization_id, tenancy.organization_name(session)),
                     "role": session.role().value})
    return {"organizations": rows}


def show_organization(database: Database, *, actor: Any, organization: Any) -> Dict[str, Any]:
    """Etat operationnel d'UNE organisation, pour un membre. Aucune ligne canonique n'est lue."""
    session = _session(database, actor, organization)
    role = session.role()
    document: Dict[str, Any] = {
        "organization": _organization(session.organization_id, tenancy.organization_name(session)),
        "role": role.value,
        "stores": [],
        "jobs": jobs.queue_statistics(session),
    }
    for store in stores.list_stores(session, limit=1000):
        entry = _store(store)
        entry["connections"] = [_connection(c) for c in stores.list_connections(session, store.id, limit=1000)]
        document["stores"].append(entry)
    if tenancy.role_allows(role, Permission.MANAGE_SERVICES):
        document["services"] = _authorizations(database, session, include_revoked=False)
    return document


# =============================================================================================
# Membres
# =============================================================================================

def add_member(database: Database, *, actor: Any, organization: Any, subject: Any, role: Any) -> Dict[str, Any]:
    """Ajoute un humain existant. Idempotent si le role est identique; conflit sinon."""
    wanted_subject = validate_subject(subject)
    if role not in ASSIGNABLE_ROLES:
        raise InvalidInput("role: " + ", ".join(ASSIGNABLE_ROLES))
    wanted_role = Role(role)
    session = _session(database, actor, organization)
    with _lock(database, f"membership:{session.organization_id}"):
        # proprietaire verifie AVANT de dire si le sujet existe
        members = tenancy.list_members(session)
        target = tenancy.find_user(database, wanted_subject)
        if target is None:
            raise NotFoundError("user introuvable")
        current = next((m for m in members if m.user_id == target.id), None)
        if current is not None:
            if current.role != wanted_role.value:
                raise Conflict(f"deja membre avec le role {current.role}", code="role_mismatch")
            return {"status": "existing", "membership": _member(current)}
        correlation_id = uuid.uuid4()
        trace = _tracer(session, correlation_id, Action.MEMBER_ADDED, ResourceType.MEMBERSHIP,
                        metadata={"user_id": str(target.id), "role": wanted_role.value})
        membership_id = tenancy.add_member(session, user_id=target.id, role=wanted_role, hook=trace)
    created = next(m for m in tenancy.list_members(session) if m.membership_id == membership_id)
    return {"status": "created", "membership": _member(created)}


def _member(member: tenancy.Member) -> Dict[str, Any]:
    return {"id": str(member.membership_id), "user_id": str(member.user_id), "subject": member.subject,
            "role": member.role, "created_at": _text(member.created_at)}


def list_members(database: Database, *, actor: Any, organization: Any) -> Dict[str, Any]:
    session = _session(database, actor, organization)
    return {"members": [_member(m) for m in tenancy.list_members(session)]}


# =============================================================================================
# Boutiques et connexions
# =============================================================================================

def create_store(database: Database, *, actor: Any, organization: Any, name: Any,
                 currency: Any = None) -> Dict[str, Any]:
    """Boutique de l'organisation. Idempotent par nom exact; une devise differente est un conflit."""
    wanted = validate_name(name)
    wanted_currency = validate_currency(currency)
    session = _session(database, actor, organization)
    _require(session, Permission.MANAGE_STORES)
    with _lock(database, f"store:{session.organization_id}"):
        matches = stores.find_stores_by_name(session, wanted)
        if len(matches) > 1:
            raise Conflict("plusieurs boutiques portent ce nom", code="ambiguous")
        if matches:
            existing = matches[0]
            if wanted_currency is not None and existing.currency != wanted_currency:
                raise Conflict("boutique existante avec une autre devise", code="currency_mismatch")
            return {"status": "existing", "store": _store(existing)}
        correlation_id = uuid.uuid4()

        def trace(conn, store_id: UUID) -> None:
            _tracer(session, correlation_id, Action.STORE_CREATED, ResourceType.STORE, store_id=store_id,
                    metadata={"currency": wanted_currency})(conn, store_id)

        created = stores.create_store(session, name=wanted, currency=wanted_currency, hook=trace)
    return {"status": "created", "store": _store(created)}


def list_stores(database: Database, *, actor: Any, organization: Any) -> Dict[str, Any]:
    session = _session(database, actor, organization)
    return {"stores": [_store(s) for s in stores.list_stores(session, limit=1000)]}


def create_connection(database: Database, *, actor: Any, organization: Any, store: Any,
                      label: Any) -> Dict[str, Any]:
    """Connexion CSV d'une boutique. Idempotent par libelle parmi les connexions ACTIVES."""
    wanted = validate_name(label, "label")
    store_id = validate_uuid(store, "store")
    session = _session(database, actor, organization)
    _require(session, Permission.MANAGE_CONNECTIONS)
    with _lock(database, f"connection:{session.organization_id}"):
        matches = [c for c in stores.list_connections(session, store_id, limit=1000) if c.label == wanted]
        if len(matches) > 1:
            raise Conflict("plusieurs connexions actives portent ce libelle", code="ambiguous")
        if matches:
            return {"status": "existing", "connection": _connection(matches[0])}
        correlation_id = uuid.uuid4()
        trace = _tracer(session, correlation_id, Action.CONNECTION_CREATED, ResourceType.CONNECTION,
                        store_id=store_id, metadata={"kind": "csv_upload"})
        created = stores.create_csv_connection(session, store_id=store_id, label=wanted, hook=trace)
    return {"status": "created", "connection": _connection(created)}


# =============================================================================================
# Autorisations de service
# =============================================================================================

def _service_id(database: Database, session: TenantSession, name: str) -> UUID:
    # proprietaire verifie AVANT de dire si le service existe
    _require(session, Permission.MANAGE_SERVICES)
    try:
        return service.find_service(database, name)
    except NotFound:
        raise NotFoundError("service introuvable (le worker enregistre son principal a son premier demarrage)",
                            code="service_not_found") from None


def _authorizations(database: Database, session: TenantSession, *, include_revoked: bool) -> List[Dict[str, Any]]:
    rows = service.list_service_authorizations(session, include_revoked=include_revoked)
    users = tenancy.describe_users(database, [r.service_user_id for r in rows])
    names = {user_id: user.subject[len(SERVICE_SUBJECT_PREFIX):] for user_id, user in users.items()}
    return [_authorization(r, names) for r in rows]


def authorize_service(database: Database, *, actor: Any, organization: Any, service_name: Any) -> Dict[str, Any]:
    """Autorise un worker sur l'organisation (proprietaire). Idempotent: un rejeu ne trace rien."""
    service_name = validate_service_name(service_name)
    session = _session(database, actor, organization)
    service_id = _service_id(database, session, service_name)
    correlation_id = uuid.uuid4()
    created: List[bool] = []

    record = _tracer(session, correlation_id, Action.SERVICE_AUTHORIZED, ResourceType.SERVICE_AUTHORIZATION,
                     metadata={"service_id": str(service_id)})

    def trace(conn, authorization: service.ServiceAuthorization) -> None:
        created.append(True)
        record(conn, authorization.id)

    authorization = service.authorize_service(session, service_id, hook=trace)
    return {"status": "created" if created else "existing",
            "authorization": _authorization(authorization, {service_id: service_name})}


def revoke_service(database: Database, *, actor: Any, organization: Any, service_name: Any) -> Dict[str, Any]:
    """Revoque l'autorisation ACTIVE (effet immediat en base). Sans autorisation active: introuvable."""
    service_name = validate_service_name(service_name)
    session = _session(database, actor, organization)
    service_id = _service_id(database, session, service_name)
    record = _tracer(session, uuid.uuid4(), Action.SERVICE_REVOKED, ResourceType.SERVICE_AUTHORIZATION,
                     metadata={"service_id": str(service_id)})
    authorization = service.revoke_service(session, service_id,
                                           hook=lambda conn, revoked: record(conn, revoked.id))
    return {"status": "revoked", "authorization": _authorization(authorization, {service_id: service_name})}


def list_service_authorizations(database: Database, *, actor: Any, organization: Any,
                                include_revoked: bool = False) -> Dict[str, Any]:
    session = _session(database, actor, organization)
    return {"authorizations": _authorizations(database, session, include_revoked=include_revoked)}


# =============================================================================================
# Travaux
# =============================================================================================

def _enqueue(session: TenantSession, job_type: JobType, payload: Dict[str, Any], *, store_id: Optional[UUID],
             priority: int, idempotency_key: Optional[str]) -> Dict[str, Any]:
    """Mise en file auditee (`job.enqueued`, acteur humain). Entrees deja validees par l'appelant."""
    correlation_id = uuid.uuid4()
    job = job_commands.enqueue(session, job_type=job_type, payload=payload, store_id=store_id,
                               priority=priority, idempotency_key=idempotency_key,
                               correlation_id=correlation_id, log=get_event_logger("admin"))
    # un rejeu de cle d'idempotence renvoie le travail existant, avec SA correlation
    status = "created" if job.correlation_id == correlation_id else "existing"
    return {"status": status, "job": render_job(job, detailed=True)}


def enqueue_import(database: Database, *, actor: Any, organization: Any, store: Any, connection: Any,
                   sources: Mapping[str, Any], synthetic: bool = False, priority: Any = 0,
                   idempotency_key: Any = None) -> Dict[str, Any]:
    store_id = validate_uuid(store, "store")
    connection_id = validate_uuid(connection, "connection")
    priority, idempotency_key = validate_priority(priority), validate_idempotency_key(idempotency_key)
    files = {kind: validate_source_path(path, kind) for kind, path in (sources or {}).items() if path is not None}
    if not files:
        raise InvalidInput("au moins une source attendue")
    unknown = sorted(set(files) - set(SOURCE_KINDS))
    if unknown:
        raise InvalidInput("source inconnue: " + ", ".join(unknown))
    session = _session(database, actor, organization)
    _require(session, jobs.ENQUEUE_PERMISSION[JobType.IMPORT])
    target = stores.get_connection(session, store_id, connection_id)
    if target.status != "active":
        raise Conflict("connexion revoquee: aucun import possible", code="connection_revoked")
    payload = {"store_id": str(store_id), "connection_id": str(connection_id),
               "sources": {kind: files[kind] for kind in sorted(files)}, "synthetic": bool(synthetic)}
    return _enqueue(session, JobType.IMPORT, payload, store_id=store_id, priority=priority,
                    idempotency_key=idempotency_key)


def enqueue_analysis(database: Database, *, actor: Any, organization: Any, store: Any, snapshot: Any = None,
                     from_import: Any = None, grain: Optional[str] = None, as_of: Any = None,
                     label: Any = None, priority: Any = 0, idempotency_key: Any = None) -> Dict[str, Any]:
    store_id = validate_uuid(store, "store")
    priority, idempotency_key = validate_priority(priority), validate_idempotency_key(idempotency_key)
    if (snapshot is None) == (from_import is None):
        raise InvalidInput("preciser --snapshot OU --from-import")
    if grain is not None and grain not in ("week", "month"):
        raise InvalidInput("grain: week ou month")
    as_of_date = _as_of(as_of)
    wanted_label = validate_name(label, "label") if label is not None else None
    import_id = validate_uuid(from_import, "from-import") if from_import is not None else None
    snapshot_id = validate_uuid(snapshot, "snapshot") if snapshot is not None else None
    session = _session(database, actor, organization)
    _require(session, jobs.ENQUEUE_PERMISSION[JobType.ANALYSIS])
    if import_id is not None:
        snapshot_id = _snapshot_of_import(session, import_id, store_id)
    record = snapshots.get_snapshot(session, store_id, snapshot_id)
    if record.status != "completed":
        raise Conflict("instantane non scelle: aucune analyse possible", code="snapshot_not_completed")
    payload: Dict[str, Any] = {"store_id": str(store_id), "snapshot_id": str(snapshot_id)}
    if grain is not None:
        payload["grain"] = grain
    if as_of_date is not None:
        payload["as_of_date"] = as_of_date.isoformat()
    if wanted_label is not None:
        payload["label"] = wanted_label
    return _enqueue(session, JobType.ANALYSIS, payload, store_id=store_id, priority=priority,
                    idempotency_key=idempotency_key)


def _as_of(value: Any) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise InvalidInput("as-of: date AAAA-MM-JJ attendue") from None


def _snapshot_of_import(session: TenantSession, job_id: UUID, store_id: UUID) -> UUID:
    job = jobs.get_job(session, job_id)
    if job.job_type != JobType.IMPORT.value or job.store_id != store_id:
        raise NotFoundError("travail d'import introuvable pour cette boutique")
    snapshot_id = (job.result or {}).get("snapshot_id") if job.status == JobStatus.SUCCEEDED.value else None
    if not snapshot_id:
        raise Conflict(f"import {job.status}: aucun instantane disponible", code="import_not_succeeded")
    return validate_uuid(snapshot_id, "snapshot")


def enqueue_purge(database: Database, *, actor: Any, organization: Any, priority: Any = 0,
                  idempotency_key: Any = None) -> Dict[str, Any]:
    """Purge de retention avec la politique par defaut (proprietaire)."""
    priority, idempotency_key = validate_priority(priority), validate_idempotency_key(idempotency_key)
    session = _session(database, actor, organization)
    return _enqueue(session, JobType.PURGE, {"retention": {}}, store_id=None, priority=priority,
                    idempotency_key=idempotency_key)


def list_jobs(database: Database, *, actor: Any, organization: Any, status: Optional[List[str]] = None,
              job_type: Optional[List[str]] = None, store: Any = None, limit: Any = 50) -> Dict[str, Any]:
    statuses = _choices(status, [s.value for s in JobStatus], "status")
    kinds = _choices(job_type, [k.value for k in JobType], "type")
    store_id = validate_uuid(store, "store") if store is not None else None
    session = _session(database, actor, organization)
    found = jobs.list_jobs(session, store_id=store_id, status=statuses, job_type=kinds, limit=validate_limit(limit))
    return {"jobs": [render_job(j) for j in found]}


def _choices(values: Optional[List[str]], allowed: List[str], field: str) -> Optional[List[str]]:
    if not values:
        return None
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise InvalidInput(f"{field}: valeurs possibles " + ", ".join(allowed))
    return sorted(set(values))


def show_job(database: Database, *, actor: Any, organization: Any, job: Any) -> Dict[str, Any]:
    job_id = validate_uuid(job, "job")
    session = _session(database, actor, organization)
    return {"job": render_job(jobs.get_job(session, job_id), detailed=True)}


def cancel_job(database: Database, *, actor: Any, organization: Any, job: Any) -> Dict[str, Any]:
    job_id = validate_uuid(job, "job")
    session = _session(database, actor, organization)
    cancelled = job_commands.cancel(session, job_id, log=get_event_logger("admin"))
    return {"status": "cancelled", "job": render_job(cancelled, detailed=True)}


def job_statistics(database: Database, *, actor: Any, organization: Any) -> Dict[str, Any]:
    session = _session(database, actor, organization)
    return {"statistics": jobs.queue_statistics(session)}


# =============================================================================================
# Audit
# =============================================================================================

def list_audit_events(database: Database, *, actor: Any, organization: Any, action: Optional[List[str]] = None,
                      limit: Any = 50) -> Dict[str, Any]:
    actions = _choices(action, [a.value for a in Action], "action")
    session = _session(database, actor, organization)
    events = audit.list_events(session, action=actions, limit=validate_limit(limit))
    return {"events": [_event(e) for e in events]}


# =============================================================================================
# Demonstration
# =============================================================================================

def demo_provision(database: Database, *, owner: Any, data_dir: Any, service_name: Any = None) -> Dict[str, Any]:
    """Organisation de demonstration SYNTHETIQUE, prete pour le worker. Rejouable a l'identique.

    Identite, organisation, boutique, connexion CSV, jeu synthetique deterministe, autorisation du
    worker (si `service_name`), puis travail d'import (cle d'idempotence derivee des fichiers).
    Chaque etape passe par l'operation publique correspondante: memes controles, meme audit.
    """
    directory = _demo_directory(data_dir)
    if service_name is not None:
        validate_service_name(service_name)
    user = ensure_user(database, owner)
    organization = create_organization(database, actor=owner, name=DEMO_ORGANIZATION)
    organization_id = organization["organization"]["id"]
    manifest = _demo_dataset(directory)
    store = create_store(database, actor=owner, organization=organization_id, name=DEMO_STORE,
                         currency=manifest["currency"])
    store_id = store["store"]["id"]
    connection = create_connection(database, actor=owner, organization=organization_id, store=store_id,
                                   label=DEMO_CONNECTION)
    authorization = None
    if service_name is not None:
        authorization = authorize_service(database, actor=owner, organization=organization_id,
                                          service_name=service_name)
    fingerprint = hashlib.sha256(json.dumps(
        {kind: manifest["files"][name]["sha256"] for kind, name in DEMO_FILES.items()},
        sort_keys=True).encode("utf-8")).hexdigest()
    job = enqueue_import(database, actor=owner, organization=organization_id, store=store_id,
                         connection=connection["connection"]["id"],
                         sources={kind: str(directory / name) for kind, name in DEMO_FILES.items()},
                         synthetic=True, idempotency_key=f"demo-import-{fingerprint[:32]}")
    return {
        "synthetic": True,
        "user": user,
        "organization": organization,
        "store": store,
        "connection": connection,
        "service_authorization": authorization,
        "import_job": job,
        "dataset": {"inputs_fingerprint": fingerprint, "orders": manifest["counts"]["orders"],
                    "currency": manifest["currency"]},
        "next": {"analysis_as_of": analysis_day(manifest).isoformat()},
    }


def _demo_directory(value: Any) -> Path:
    path = Path(validate_source_path(value, "data-dir"))
    if is_sample_path(path):
        raise InvalidInput("data-dir: data/sample est reserve aux fixtures versionnees")
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise InvalidInput("data-dir: repertoire attendu")
        entries = {entry.name for entry in path.iterdir()}
        if entries and not _is_demo_directory(path):
            raise Conflict("data-dir: repertoire non vide qui n'est pas une demonstration Mervio",
                           code="data_dir_not_empty")
    return path


def _is_demo_directory(path: Path) -> bool:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(manifest, dict) and manifest.get("synthetic") is True
            and manifest.get("generator") == "mervio.synthetic")


def _demo_dataset(directory: Path) -> Dict[str, Any]:
    """Jeu synthetique de la demonstration, jamais reecrit sous un worker qui le lit.

    Deja present et intact (meme configuration, empreintes conformes): reutilise tel quel. Sinon,
    genere a cote puis mis en place fichier par fichier par renommage atomique: un lecteur en cours
    garde l'ancien contenu entier, jamais un fichier a moitie ecrit.
    """
    existing = _intact_demo_manifest(directory)
    if existing is not None:
        return existing
    try:
        directory.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".mervio-demo-", dir=directory))
        try:
            manifest = generate_dataset(GeneratorConfig(**DEMO_DATASET), staging).manifest
            for entry in sorted(staging.iterdir()):
                if entry.name != "manifest.json":
                    os.replace(entry, directory / entry.name)
            os.replace(staging / "manifest.json", directory / "manifest.json")  # en dernier: marque la fin
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    except OSError:
        raise InvalidInput("data-dir: ecriture impossible") from None
    return manifest


def _intact_demo_manifest(directory: Path) -> Optional[Dict[str, Any]]:
    if not _is_demo_directory(directory):
        return None
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    config = manifest.get("config") or {}
    if any(config.get(key) != value for key, value in DEMO_DATASET.items()):
        return None
    for name, description in (manifest.get("files") or {}).items():
        target = directory / name
        if not target.is_file() or target.is_symlink() or _sha256(target) != description.get("sha256"):
            return None
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ASSIGNABLE_ROLES", "SOURCE_KINDS", "add_member", "authorize_service", "cancel_job", "connect",
    "create_connection",
    "create_organization", "create_store", "demo_provision", "enqueue_analysis", "enqueue_import", "enqueue_purge",
    "ensure_user", "guarded", "job_statistics", "list_audit_events", "list_jobs", "list_members",
    "list_organizations", "list_service_authorizations", "list_stores", "render_job", "resolve_actor",
    "revoke_service", "show_job", "show_organization", "translate",
]
