"""Journal d'audit en ajout seul.

Une ligne repond a sept questions: QUI (`actor_type`, `actor_id`, et pour le compte de
QUI `on_behalf_of`, 004.3), a fait QUOI
(`action`), sur QUELLE RESSOURCE (`resource_type`, `resource_id`), pour QUELLE
ORGANISATION (`organization_id`, `store_id`), QUAND (`created_at`), sous QUELLE
CORRELATION (`correlation_id`), avec QUEL RESULTAT (`outcome`).

Le module n'expose AUCUNE mise a jour et aucune suppression ordinaire. La seule
suppression possible est `purge_expired_events`, reservee au proprietaire de
l'organisation, bornee par un plancher impose par la base (30 jours) et elle-meme
tracee.

`record` ecrit dans une transaction DEJA ouverte: l'evenement et le changement
d'etat qu'il decrit sont commis ensemble, ou pas du tout. Un audit ne peut donc
ni manquer un changement commis, ni decrire un changement annule.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, List, Mapping, Optional
from uuid import UUID

from psycopg.types.json import Jsonb

from ..observability.logging import scrub
from .stores import _limit, _uuid, fetch_store
from .tenancy import Permission, TenantSession


class ActorType(str, Enum):
    USER = "user"
    #: la plateforme elle-meme (recuperation d'un bail, purge planifiee)
    SYSTEM = "system"
    #: un worker executant un travail pour le compte d'un utilisateur (`on_behalf_of`)
    WORKER = "worker"


class Action(str, Enum):
    JOB_ENQUEUED = "job.enqueued"
    JOB_CLAIMED = "job.claimed"
    JOB_SUCCEEDED = "job.succeeded"
    JOB_FAILED = "job.failed"
    JOB_REQUEUED = "job.requeued"
    JOB_RECOVERED = "job.recovered"
    JOB_CANCELLED = "job.cancelled"
    IMPORT_STARTED = "import.started"
    IMPORT_SUCCEEDED = "import.succeeded"
    IMPORT_FAILED = "import.failed"
    ANALYSIS_STARTED = "analysis.started"
    ANALYSIS_SUCCEEDED = "analysis.succeeded"
    ANALYSIS_FAILED = "analysis.failed"
    PURGE_STARTED = "purge.started"
    PURGE_COMPLETED = "purge.completed"
    # administration (004.3.7, revision 0008): toujours un humain qui agit pour lui-meme
    ORGANIZATION_CREATED = "organization.created"
    MEMBER_ADDED = "member.added"
    STORE_CREATED = "store.created"
    CONNECTION_CREATED = "connection.created"
    SERVICE_AUTHORIZED = "service.authorized"
    SERVICE_REVOKED = "service.revoked"
    # 004.4.4 (revision 0013): depot d'un objet brut par un operateur
    OBJECT_UPLOADED = "object.uploaded"
    # 004.4.5 (revision 0014): effacement client execute par le chemin privilegie.
    # La trace designe la ligne de preuve; elle ne porte NI l'identite, NI la valeur effacee.
    CUSTOMER_REDACTED = "customer.redacted"


class ResourceType(str, Enum):
    JOB = "job"
    SNAPSHOT = "snapshot"
    ANALYSIS_RUN = "analysis_run"
    REPORT = "report"
    ORGANIZATION = "organization"
    # 004.3.7 (revision 0008)
    MEMBERSHIP = "membership"
    STORE = "store"
    CONNECTION = "connection"
    SERVICE_AUTHORIZATION = "service_authorization"
    # 004.4.4 (revision 0013)
    RAW_OBJECT = "raw_object"
    # 004.4.5 (revision 0014): la PREUVE d'un effacement, jamais le client lui-meme
    CUSTOMER_REDACTION = "customer_redaction"


class Outcome(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class AuditEvent:
    id: UUID
    organization_id: UUID
    store_id: Optional[UUID]
    actor_type: str
    actor_id: Optional[UUID]
    action: str
    resource_type: str
    resource_id: UUID
    correlation_id: UUID
    outcome: str
    metadata: dict
    created_at: datetime
    #: acteur metier (humain) pour le compte duquel l'acteur technique agit (004.3)
    on_behalf_of: Optional[UUID] = None


_AUDIT_COLUMNS = ("id, organization_id, store_id, actor_type, actor_id, action, resource_type, resource_id, "
                  "correlation_id, outcome, metadata, created_at, on_behalf_of")


def record(conn, *, organization_id: UUID, action, resource_type, resource_id: UUID,
           correlation_id, outcome=Outcome.SUCCEEDED, actor_type=ActorType.SYSTEM,
           actor_id: Optional[UUID] = None, store_id: Optional[UUID] = None,
           metadata: Optional[Mapping[str, Any]] = None, on_behalf_of: Optional[UUID] = None) -> AuditEvent:
    """Ajoute un evenement dans la transaction ouverte `conn`.

    `actor_*` nomme l'acteur technique (un humain, le systeme, un principal de service);
    `on_behalf_of` l'humain a l'origine de la demande quand l'acteur agit pour lui. Sous
    le role worker, la base impose l'un et l'autre (revision 0007).

    `metadata` passe par le meme filtre que les logs: une cle qui evoque un secret
    ou une PII est remplacee par `[redacted]`, une valeur trop longue est tronquee,
    un chemin absolu est masque.
    """
    row = conn.execute(
        "INSERT INTO audit_events (id, organization_id, store_id, actor_type, actor_id, action, resource_type, "
        "resource_id, correlation_id, outcome, metadata, on_behalf_of) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_AUDIT_COLUMNS}",
        (uuid.uuid4(), organization_id, store_id, ActorType(actor_type).value, actor_id, Action(action).value,
         ResourceType(resource_type).value, _uuid(resource_id, "resource"), _uuid(correlation_id, "correlation"),
         Outcome(outcome).value, Jsonb(scrub(metadata or {})),
         _uuid(on_behalf_of, "on_behalf_of") if on_behalf_of is not None else None),
    ).fetchone()
    return AuditEvent(*row)


def record_event(session: TenantSession, *, action, resource_type, resource_id: UUID, correlation_id,
                 outcome=Outcome.SUCCEEDED, actor_type=ActorType.SYSTEM, actor_id: Optional[UUID] = None,
                 store_id: Optional[UUID] = None, metadata: Optional[Mapping[str, Any]] = None,
                 permission: Permission = Permission.RUN_JOBS, on_behalf_of: Optional[UUID] = None) -> AuditEvent:
    """Ajoute un evenement dans sa propre transaction (hors d'une unite de travail existante)."""
    with session.transaction(permission) as conn:
        return record(conn, organization_id=session.organization_id, action=action, resource_type=resource_type,
                      resource_id=resource_id, correlation_id=correlation_id, outcome=outcome,
                      actor_type=actor_type, actor_id=actor_id, store_id=store_id, metadata=metadata,
                      on_behalf_of=on_behalf_of)


def list_events(session: TenantSession, *, resource_type=None, resource_id: Optional[UUID] = None,
                correlation_id: Optional[UUID] = None, store_id: Optional[UUID] = None,
                action=None, limit: int = 50) -> List[AuditEvent]:
    """Lecture du journal. Reservee aux administrateurs (matrice de roles, section 8)."""
    kind = ResourceType(resource_type).value if resource_type is not None else None
    actions = [Action(a).value for a in action] if action is not None else None
    with session.transaction(Permission.READ_AUDIT) as conn:
        if store_id is not None:
            fetch_store(conn, session, store_id)
        rows = conn.execute(
            f"SELECT {_AUDIT_COLUMNS} FROM audit_events WHERE organization_id = %s "
            "  AND (%s::text IS NULL OR resource_type = %s::text) "
            "  AND (%s::uuid IS NULL OR resource_id = %s::uuid) "
            "  AND (%s::uuid IS NULL OR correlation_id = %s::uuid) "
            "  AND (%s::uuid IS NULL OR store_id = %s::uuid) "
            "  AND (%s::text[] IS NULL OR action = ANY(%s::text[])) "
            "ORDER BY created_at ASC, id ASC LIMIT %s",
            (session.organization_id, kind, kind,
             _uuid(resource_id, "resource") if resource_id is not None else None,
             _uuid(resource_id, "resource") if resource_id is not None else None,
             correlation_id, correlation_id, store_id, store_id, actions, actions, _limit(limit)),
        ).fetchall()
    return [AuditEvent(*r) for r in rows]


def count_events(session: TenantSession) -> int:
    with session.transaction(Permission.READ_AUDIT) as conn:
        return int(conn.execute(
            "SELECT count(*) FROM audit_events WHERE organization_id = %s", (session.organization_id,)).fetchone()[0])


def purge_expired_events(session: TenantSession, *, before: datetime, limit: int = 1000) -> int:
    """Supprime des evenements anterieurs a `before`, au-dela du plancher de la base.

    La politique RESTRICTIVE `audit_events_retention_floor` refuse toute suppression
    d'un evenement de moins de 30 jours, y compris en SQL brut par son propre tenant:
    une purge mal configuree ne peut pas effacer une trace recente.
    """
    if not isinstance(before, datetime):
        raise ValueError("before est un datetime")
    with session.transaction(Permission.PURGE_DATA) as conn:
        deleted = conn.execute(
            "DELETE FROM audit_events WHERE organization_id = %s AND id IN ("
            "    SELECT id FROM audit_events WHERE organization_id = %s AND created_at < %s "
            "    ORDER BY created_at ASC, id ASC LIMIT %s)",
            (session.organization_id, session.organization_id, before, _limit(limit)),
        ).rowcount
    return int(deleted)
