"""Contexte tenant, roles et provisionnement.

`TenantContext` porte l'identite AUTHENTIFIEE (utilisateur) et l'organisation
demandee. Il n'accorde rien par lui-meme: chaque transaction d'une
`TenantSession` reverifie en base l'appartenance de l'utilisateur a
l'organisation et en lit le role. Un contexte fabrique pour une organisation
dont l'utilisateur n'est pas membre echoue en `TenantAccessDenied` (vu comme
introuvable), quel que soit le role qu'imagine l'appelant.

La racine de confiance est l'identifiant utilisateur, qui viendra de la
validation du jeton OIDC en 004.3; jamais d'un parametre client.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, List, Optional
from uuid import UUID

import psycopg

from .database import Database
from .errors import NotFound, PermissionDenied, TenantAccessDenied


class Role(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


_RANK = {Role.VIEWER: 0, Role.ANALYST: 1, Role.ADMIN: 2, Role.OWNER: 3}


class Permission(str, Enum):
    """Matrice de roles de MISSION_004_0_ARCHITECTURE.md section 8."""

    READ = "read"
    IMPORT_DATA = "import_data"
    RUN_ANALYSIS = "run_analysis"
    READ_PROVENANCE = "read_provenance"
    #: executer un travail deja mis en file (prise, resultat, echec, reprise)
    RUN_JOBS = "run_jobs"
    MANAGE_CONNECTIONS = "manage_connections"
    MANAGE_STORES = "manage_stores"
    #: lire le journal d'audit
    READ_AUDIT = "read_audit"
    MANAGE_MEMBERS = "manage_members"
    #: supprimer des donnees (purge de retention)
    PURGE_DATA = "purge_data"


MINIMUM_ROLE = {
    Permission.READ: Role.VIEWER,
    Permission.IMPORT_DATA: Role.ANALYST,
    Permission.RUN_ANALYSIS: Role.ANALYST,
    Permission.READ_PROVENANCE: Role.ANALYST,
    Permission.RUN_JOBS: Role.ANALYST,
    Permission.MANAGE_CONNECTIONS: Role.ADMIN,
    Permission.MANAGE_STORES: Role.ADMIN,
    Permission.READ_AUDIT: Role.ADMIN,
    Permission.MANAGE_MEMBERS: Role.OWNER,
    Permission.PURGE_DATA: Role.OWNER,
}


def role_allows(role: Role, permission: Permission) -> bool:
    return _RANK[role] >= _RANK[MINIMUM_ROLE[permission]]


@dataclass(frozen=True)
class TenantContext:
    organization_id: UUID
    user_id: UUID

    def __post_init__(self) -> None:
        # refuse des chaines: un identifiant non UUID n'atteint jamais le SQL
        if not isinstance(self.organization_id, UUID) or not isinstance(self.user_id, UUID):
            raise TypeError("TenantContext attend des UUID")


class TenantSession:
    """Unite d'acces aux donnees d'UNE organisation pour UN utilisateur."""

    def __init__(self, database: Database, context: TenantContext) -> None:
        self.database = database
        self.context = context

    @property
    def organization_id(self) -> UUID:
        return self.context.organization_id

    @property
    def user_id(self) -> UUID:
        return self.context.user_id

    @contextmanager
    def transaction(self, permission: Permission) -> Iterator[psycopg.Connection]:
        """Transaction avec contexte RLS, appartenance et role verifies en base."""
        with self.database.transaction(organization_id=self.organization_id, user_id=self.user_id) as conn:
            row = conn.execute(
                "SELECT role FROM memberships WHERE organization_id = %s AND user_id = %s",
                (self.organization_id, self.user_id),
            ).fetchone()
            if row is None:
                raise TenantAccessDenied()
            role = Role(row[0])
            if not role_allows(role, permission):
                raise PermissionDenied(permission.value, role.value)
            yield conn

    def role(self) -> Role:
        with self.database.transaction(organization_id=self.organization_id, user_id=self.user_id) as conn:
            row = conn.execute(
                "SELECT role FROM memberships WHERE organization_id = %s AND user_id = %s",
                (self.organization_id, self.user_id),
            ).fetchone()
        if row is None:
            raise TenantAccessDenied()
        return Role(row[0])


# -- provisionnement ------------------------------------------------------------------

def ensure_user(database: Database, idp_subject: str) -> UUID:
    """Identifiant Mervio d'un sujet d'identite, cree au premier appel."""
    if not idp_subject or len(idp_subject) > 255:
        raise ValueError("sujet d'identite invalide")
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO users (id, idp_subject) VALUES (%s, %s) ON CONFLICT (idp_subject) DO NOTHING",
            (uuid.uuid4(), idp_subject),
        )
        return conn.execute("SELECT id FROM users WHERE idp_subject = %s", (idp_subject,)).fetchone()[0]


def create_organization(database: Database, *, owner_user_id: UUID, name: str) -> UUID:
    """Cree une organisation et son proprietaire dans une seule transaction."""
    organization_id = uuid.uuid4()
    with database.transaction(organization_id=organization_id, user_id=owner_user_id) as conn:
        conn.execute("INSERT INTO organizations (id, name) VALUES (%s, %s)", (organization_id, name))
        conn.execute(
            "INSERT INTO memberships (id, organization_id, user_id, role) VALUES (%s, %s, %s, 'owner')",
            (uuid.uuid4(), organization_id, owner_user_id),
        )
    return organization_id


def add_member(session: TenantSession, *, user_id: UUID, role: Role) -> None:
    with session.transaction(Permission.MANAGE_MEMBERS) as conn:
        if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
            raise NotFound("user")
        conn.execute(
            "INSERT INTO memberships (id, organization_id, user_id, role) VALUES (%s, %s, %s, %s)",
            (uuid.uuid4(), session.organization_id, user_id, Role(role).value),
        )


def remove_member(session: TenantSession, *, user_id: UUID) -> None:
    with session.transaction(Permission.MANAGE_MEMBERS) as conn:
        deleted = conn.execute(
            "DELETE FROM memberships WHERE organization_id = %s AND user_id = %s AND role <> 'owner'",
            (session.organization_id, user_id),
        ).rowcount
        if not deleted:
            raise NotFound("membership")


def organizations_of(database: Database, user_id: UUID) -> List[UUID]:
    """Organisations dont l'utilisateur est membre (politique memberships_own_rows_readable)."""
    with database.transaction(user_id=user_id) as conn:
        rows = conn.execute(
            "SELECT organization_id FROM memberships WHERE user_id = %s ORDER BY created_at, organization_id",
            (user_id,),
        ).fetchall()
    return [r[0] for r in rows]


def organization_name(session: TenantSession) -> Optional[str]:
    with session.transaction(Permission.READ) as conn:
        row = conn.execute("SELECT name FROM organizations WHERE id = %s", (session.organization_id,)).fetchone()
    if row is None:  # pragma: no cover - l'appartenance verifiee implique l'organisation
        raise NotFound("organization")
    return row[0]
