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
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional
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
    #: effacer UN client (004.4.5). La base relit ce rang a l'EXECUTION, pas seulement a la
    #: mise en file: retrograder le demandeur arrete un effacement deja en file (D-052).
    REDACT_CUSTOMER = "redact_customer"
    #: autoriser ou revoquer un service (worker) sur l'organisation (004.3)
    MANAGE_SERVICES = "manage_services"


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
    # D-052: `admin` ou plus pour l'effacement; `owner` reste exige pour les purges.
    Permission.REDACT_CUSTOMER: Role.ADMIN,
    # un service autorise traite TOUTES les donnees de l'organisation: decision du proprietaire
    Permission.MANAGE_SERVICES: Role.OWNER,
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


#: Crochet appele DANS la transaction qui cree la ressource, avec (connexion, identifiant).
#: Il sert a ecrire l'audit (004.3.7): la trace et la creation sont commises ensemble, ou pas
#: du tout. Meme contrat que `jobs.TransitionHook`.
CreationHook = Callable[[Any, UUID], None]


def create_organization(database: Database, *, owner_user_id: UUID, name: str,
                        hook: Optional[CreationHook] = None) -> UUID:
    """Cree une organisation et son proprietaire dans une seule transaction."""
    organization_id = uuid.uuid4()
    with database.transaction(organization_id=organization_id, user_id=owner_user_id) as conn:
        conn.execute("INSERT INTO organizations (id, name) VALUES (%s, %s)", (organization_id, name))
        conn.execute(
            "INSERT INTO memberships (id, organization_id, user_id, role) VALUES (%s, %s, %s, 'owner')",
            (uuid.uuid4(), organization_id, owner_user_id),
        )
        if hook is not None:
            hook(conn, organization_id)
    return organization_id


def add_member(session: TenantSession, *, user_id: UUID, role: Role,
               hook: Optional[CreationHook] = None) -> UUID:
    """Ajoute un membre; renvoie l'identifiant de l'appartenance creee."""
    membership_id = uuid.uuid4()
    with session.transaction(Permission.MANAGE_MEMBERS) as conn:
        if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
            raise NotFound("user")
        conn.execute(
            "INSERT INTO memberships (id, organization_id, user_id, role) VALUES (%s, %s, %s, %s)",
            (membership_id, session.organization_id, user_id, Role(role).value),
        )
        if hook is not None:
            hook(conn, membership_id)
    return membership_id


@dataclass(frozen=True)
class Member:
    membership_id: UUID
    user_id: UUID
    subject: str
    role: str
    created_at: datetime


def list_members(session: TenantSession) -> List[Member]:
    """Membres de l'organisation. Reserve au proprietaire: la liste nomme des identites."""
    with session.transaction(Permission.MANAGE_MEMBERS) as conn:
        rows = conn.execute(
            "SELECT m.id, m.user_id, u.idp_subject, m.role, m.created_at "
            "FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.organization_id = %s ORDER BY m.created_at, m.id",
            (session.organization_id,),
        ).fetchall()
    return [Member(*r) for r in rows]


@dataclass(frozen=True)
class UserRecord:
    id: UUID
    subject: str
    kind: str

    @property
    def human(self) -> bool:
        return self.kind == "human"


def find_user(database: Database, idp_subject: str) -> Optional[UserRecord]:
    """Identite plateforme d'un sujet, sans la creer. None si inconnue."""
    if not isinstance(idp_subject, str) or not idp_subject or len(idp_subject) > 255:
        return None
    with database.transaction() as conn:
        row = conn.execute("SELECT id, idp_subject, kind FROM users WHERE idp_subject = %s",
                           (idp_subject,)).fetchone()
    return UserRecord(*row) if row is not None else None


def describe_users(database: Database, user_ids: Iterable[UUID]) -> Dict[UUID, UserRecord]:
    """Sujets d'identifiants deja lus dans une organisation (table plateforme, sans donnee de tenant)."""
    wanted = sorted({user_id for user_id in user_ids if isinstance(user_id, UUID)})
    if not wanted:
        return {}
    with database.transaction() as conn:
        rows = conn.execute("SELECT id, idp_subject, kind FROM users WHERE id = ANY(%s)", (wanted,)).fetchall()
    return {r[0]: UserRecord(*r) for r in rows}


@contextmanager
def provisioning_lock(database: Database, scope: str) -> Iterator[None]:
    """Serialise les provisionnements concurrents d'une meme portee (004.3.7).

    Verrou consultatif de SESSION sur la connexion de `database`: il couvre la lecture
    "existe deja?" et la creation, qui sont deux transactions distinctes. Il est libere a la
    sortie, ou par la base si la connexion tombe. Une collision de hachage ne fait que
    serialiser deux portees: aucune information ne passe d'un tenant a l'autre.
    """
    if not scope or len(scope) > 1000:
        raise ValueError("portee de verrou invalide")
    connection = database.connection()
    connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (scope,))
    try:
        yield
    finally:
        try:
            if not connection.closed:
                connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (scope,))
        except Exception:  # noqa: BLE001 - connexion perdue: la base a deja libere le verrou
            pass


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
