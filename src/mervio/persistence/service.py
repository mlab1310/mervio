"""Identite de service, autorisations explicites et execution deleguee (Mission 004.3).

Trois acteurs distincts, jamais confondus:

    principal de service   QUI execute: `service:<role PostgreSQL>`, jamais un humain
    enqueued_by            QUI a demande le travail (un humain membre)
    on_behalf_of           pour le compte de QUI l'acteur technique agit (audit)

Le principal n'est pas choisi par l'application: la base le derive du role de
connexion (`app_current_service_id()`, revision 0007). Un service ne traite QUE les
organisations qui l'ont explicitement autorise (`service_authorizations`), et la base
le garantit par des politiques RESTRICTIVES, meme avec un contexte d'organisation forge.

Deux sessions pour un travail:

    ServiceSession      etat du travail (prise, bail, resultat, reprise): RUN_JOBS et READ
                        seulement, aucun delegue, donc aucune donnee metier lisible
    delegated_session   travail metier au nom du demandeur: appartenance et role relus
                        en base au moment de l'execution, et a chaque transaction
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, List, Optional
from uuid import UUID

import psycopg

from .database import Database
from .errors import NotFound, PermissionDenied, SchemaNotReady, ServiceIdentityError, TenantAccessDenied
from .jobs import ENQUEUE_PERMISSION, JobRecord
from .stores import _uuid
from .tenancy import Permission, Role, TenantContext, TenantSession

SERVICE_SUBJECT_PREFIX = "service:"
#: Ce qu'un service fait seul, sans delegue humain.
SERVICE_PERMISSIONS = frozenset({Permission.READ, Permission.RUN_JOBS})


@dataclass(frozen=True)
class ServicePrincipal:
    id: UUID
    subject: str

    @property
    def name(self) -> str:
        return self.subject[len(SERVICE_SUBJECT_PREFIX):]


@dataclass(frozen=True)
class ServiceAuthorization:
    id: UUID
    organization_id: UUID
    service_user_id: UUID
    granted_by: UUID
    granted_at: datetime
    revoked_by: Optional[UUID]
    revoked_at: Optional[datetime]

    @property
    def active(self) -> bool:
        return self.revoked_at is None


_AUTHORIZATION_COLUMNS = "id, organization_id, service_user_id, granted_by, granted_at, revoked_by, revoked_at"


# -- identite ---------------------------------------------------------------------------------

def service_principal(database: Database) -> ServicePrincipal:
    """Principal du role connecte, cree au premier appel.

    Refus (`ServiceIdentityError`) si le role n'herite pas de `mervio_worker`: la base
    refuse alors l'execution de la fonction, et aucun principal n'est cree.
    `SchemaNotReady` si la base n'est pas migree jusqu'a la revision 0007.
    """
    try:
        with database.transaction() as conn:
            # deux instructions: la ligne creee par la fonction n'est visible qu'a la suivante
            principal_id = conn.execute("SELECT app_ensure_service_principal()").fetchone()[0]
            row = conn.execute("SELECT id, idp_subject FROM users WHERE id = %s", (principal_id,)).fetchone()
    except psycopg.errors.InsufficientPrivilege:
        raise ServiceIdentityError("le role connecte n'est pas un role de service") from None
    except (psycopg.errors.UndefinedFunction, psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        raise SchemaNotReady("schema sans identite de service: appliquer les migrations") from None
    return ServicePrincipal(row[0], row[1])


def find_service(database: Database, name: str) -> UUID:
    """Identifiant du principal `service:<name>`, pour qu'un proprietaire l'autorise."""
    with database.transaction() as conn:
        row = conn.execute("SELECT id FROM users WHERE kind = 'service' AND idp_subject = %s",
                           (SERVICE_SUBJECT_PREFIX + str(name),)).fetchone()
    if row is None:
        raise NotFound("service")
    return row[0]


class ServiceSession(TenantSession):
    """Session d'un service sur UNE organisation qui l'a autorise.

    Elle porte l'etat des travaux, jamais les donnees metier: aucun delegue n'est pose,
    donc la base refuse toute lecture d'instantane, de ligne canonique ou de rapport.
    """

    def __init__(self, database: Database, organization_id: UUID, principal: ServicePrincipal) -> None:
        super().__init__(database, TenantContext(organization_id, principal.id))
        self.principal = principal

    @contextmanager
    def transaction(self, permission: Permission) -> Iterator[psycopg.Connection]:
        permission = Permission(permission)
        if permission not in SERVICE_PERMISSIONS:
            raise PermissionDenied(permission.value, "service")
        with self.database.transaction(organization_id=self.organization_id) as conn:
            service_id, authorized = conn.execute(
                "SELECT app_current_service_id(), app_service_authorized(%s)", (self.organization_id,)).fetchone()
            if service_id != self.principal.id:
                raise ServiceIdentityError("la connexion n'appartient pas a ce principal de service")
            if not authorized:
                # indiscernable d'une organisation inexistante
                raise TenantAccessDenied()
            yield conn

    def role(self) -> Role:
        raise PermissionDenied("role", "service")


def delegated_session(service_session: ServiceSession, job: JobRecord) -> TenantSession:
    """Session metier d'execution: le DEMANDEUR du travail, reverifie maintenant.

    - demandeur absent: `PermissionDenied` (aucun travail systeme avant 004.12);
    - demandeur retire de l'organisation, ou service revoque: `TenantAccessDenied`;
    - role devenu insuffisant pour ce type de travail: `PermissionDenied`.

    La meme verification est refaite a chaque transaction de la session renvoyee, et la
    base l'impose aussi pour les donnees metier (politiques de delegation, 0007).
    """
    if job.organization_id != service_session.organization_id:
        raise NotFound("job")
    required = ENQUEUE_PERMISSION[job.kind]
    if job.enqueued_by is None:
        raise PermissionDenied(required.value, "none")
    session = TenantSession(service_session.database, TenantContext(job.organization_id, job.enqueued_by))
    with session.transaction(required):
        pass
    return session


# -- autorisations ----------------------------------------------------------------------------

def _require_service(conn, service_user_id) -> UUID:
    service_id = _uuid(service_user_id, "service")
    if conn.execute("SELECT 1 FROM users WHERE id = %s AND kind = 'service'", (service_id,)).fetchone() is None:
        raise NotFound("service")
    return service_id


def authorize_service(session: TenantSession, service_user_id: UUID) -> ServiceAuthorization:
    """Autorise un service sur l'organisation. Idempotent: renvoie l'autorisation active."""
    with session.transaction(Permission.MANAGE_SERVICES) as conn:
        service_id = _require_service(conn, service_user_id)
        row = conn.execute(
            "INSERT INTO service_authorizations (id, organization_id, service_user_id, granted_by) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (organization_id, service_user_id) WHERE revoked_at IS NULL DO NOTHING "
            f"RETURNING {_AUTHORIZATION_COLUMNS}",
            (uuid.uuid4(), session.organization_id, service_id, session.user_id),
        ).fetchone()
        if row is None:
            row = conn.execute(
                f"SELECT {_AUTHORIZATION_COLUMNS} FROM service_authorizations "
                "WHERE organization_id = %s AND service_user_id = %s AND revoked_at IS NULL",
                (session.organization_id, service_id),
            ).fetchone()
    return ServiceAuthorization(*row)


def revoke_service(session: TenantSession, service_user_id: UUID) -> ServiceAuthorization:
    """Revoque l'autorisation active. Effet immediat: la base refuse la suite au service."""
    with session.transaction(Permission.MANAGE_SERVICES) as conn:
        service_id = _require_service(conn, service_user_id)
        row = conn.execute(
            "UPDATE service_authorizations SET revoked_at = clock_timestamp(), revoked_by = %s "
            "WHERE organization_id = %s AND service_user_id = %s AND revoked_at IS NULL "
            f"RETURNING {_AUTHORIZATION_COLUMNS}",
            (session.user_id, session.organization_id, service_id),
        ).fetchone()
    if row is None:
        raise NotFound("service_authorization")
    return ServiceAuthorization(*row)


def list_service_authorizations(session: TenantSession, *, include_revoked: bool = False) -> List[ServiceAuthorization]:
    with session.transaction(Permission.MANAGE_SERVICES) as conn:
        rows = conn.execute(
            f"SELECT {_AUTHORIZATION_COLUMNS} FROM service_authorizations "
            "WHERE organization_id = %s AND (%s OR revoked_at IS NULL) ORDER BY granted_at, id",
            (session.organization_id, include_revoked),
        ).fetchall()
    return [ServiceAuthorization(*r) for r in rows]


def authorized_organizations(database: Database, principal: ServicePrincipal, *, limit: int = 1000) -> List[UUID]:
    """Liste explicite des organisations que le service connecte peut traiter."""
    if not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError("limit entre 1 et 10000")
    with database.transaction() as conn:
        _require_connected_principal(conn, principal)
        rows = conn.execute(
            "SELECT organization_id FROM service_authorizations "
            "WHERE service_user_id = %s AND revoked_at IS NULL ORDER BY organization_id LIMIT %s",
            (principal.id, limit),
        ).fetchall()
    return [r[0] for r in rows]


def _require_connected_principal(conn, principal: ServicePrincipal) -> None:
    if conn.execute("SELECT app_current_service_id()").fetchone()[0] != principal.id:
        raise ServiceIdentityError("la connexion n'appartient pas a ce principal de service")


__all__ = [
    "SERVICE_PERMISSIONS", "SERVICE_SUBJECT_PREFIX", "ServiceAuthorization", "ServicePrincipal", "ServiceSession",
    "authorize_service", "authorized_organizations", "delegated_session", "find_service",
    "list_service_authorizations", "revoke_service", "service_principal",
]
