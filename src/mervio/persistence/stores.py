"""Boutiques et connexions (metadonnees uniquement, jamais de secret)."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from ..config import resolve_timezone
from .errors import NotFound
from .tenancy import CreationHook, Permission, TenantSession

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
#: valeurs par defaut des colonnes `stores.timezone` / `stores.timezone_source` (migration 0011)
DEFAULT_TIMEZONE = "UTC"
DEFAULT_TIMEZONE_SOURCE = "default"
_EXPLICIT_TIMEZONE_SOURCE = "explicit"


@dataclass(frozen=True)
class Store:
    id: UUID
    organization_id: UUID
    name: str
    currency: Optional[str]
    created_at: datetime
    #: fuseau de decoupage des periodes analytiques (AN-06). Identifiant IANA.
    #: Les colonnes existent depuis `0011_identity_pii_schema` mais n'etaient ni ecrites ni
    #: lues: la roadmap les declarait livrees a tort (rectifie en 004.5-A).
    timezone: str = DEFAULT_TIMEZONE
    #: `default` tant que le marchand n'a rien choisi, `explicit` des qu'il choisit.
    #: Distinguer les deux importe: un `UTC` par defaut n'est pas une decision du marchand, et
    #: on ne doit pas confondre << pas encore configure >> et << configure a UTC >>.
    timezone_source: str = DEFAULT_TIMEZONE_SOURCE


@dataclass(frozen=True)
class Connection:
    id: UUID
    organization_id: UUID
    store_id: UUID
    kind: str
    label: str
    status: str
    created_at: datetime
    revoked_at: Optional[datetime]


_STORE_COLUMNS = "id, organization_id, name, currency, created_at, timezone, timezone_source"
_CONNECTION_COLUMNS = "id, organization_id, store_id, kind, label, status, created_at, revoked_at"


def create_store(session: TenantSession, *, name: str, currency: Optional[str] = None,
                 timezone: Optional[str] = None,
                 hook: Optional[CreationHook] = None) -> Store:
    """Cree une boutique. `timezone`: identifiant IANA; absent => `UTC` / source `default`.

    La contrainte `CHECK` de la migration 0011 est purement SYNTAXIQUE (elle accepte `aaa/bbb`):
    la validation semantique du fuseau se fait donc ici, avant toute ecriture.
    """
    if currency is not None and not _CURRENCY_RE.match(currency):
        raise ValueError("devise de boutique: code ISO 4217 en majuscules attendu")
    if timezone is None:
        zone, source = DEFAULT_TIMEZONE, DEFAULT_TIMEZONE_SOURCE
    else:
        resolve_timezone(timezone)  # leve ConfigurationError si le fuseau n'existe pas
        zone, source = timezone, _EXPLICIT_TIMEZONE_SOURCE
    with session.transaction(Permission.MANAGE_STORES) as conn:
        row = conn.execute(
            f"INSERT INTO stores (id, organization_id, name, currency, timezone, timezone_source) "
            f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_STORE_COLUMNS}",
            (uuid.uuid4(), session.organization_id, name, currency, zone, source),
        ).fetchone()
        if hook is not None:
            hook(conn, row[0])
    return Store(*row)


# PAS de `set_store_timezone` en 004.5, et ce n'est pas un oubli.
#
# `0001_tenancy.py:144` accorde `UPDATE (name, currency) ON stores` -- un grant au niveau
# COLONNE. `timezone` et `timezone_source` n'y figurent pas: tout UPDATE de ces colonnes est
# refuse par PostgreSQL (`InsufficientPrivilege`), et l'autoriser demanderait un
# `GRANT UPDATE (timezone, timezone_source)`, donc une MIGRATION.
#
# ND-01 = B interdit une migration nouvelle sauf necessite technique, et la verification demandee
# porte sur << create store, retrieve store >>: le fuseau se choisit a la CREATION, ce qui suffit
# a la plomberie ratifiee. Le changement de fuseau d'une boutique existante releve de 004.6, qui
# touchera de toute facon le modele `stores` et pourra porter le grant.
#
# Ce refus est une PREUVE que le modele de privileges par colonne fonctionne: il a bloque une
# ecriture non prevue. Voir tests/persistence/test_store_timezone.py.


def find_stores_by_name(session: TenantSession, name: str) -> List[Store]:
    """Boutiques de l'organisation portant exactement ce nom (le nom n'est pas unique en base)."""
    with session.transaction(Permission.READ) as conn:
        rows = conn.execute(
            f"SELECT {_STORE_COLUMNS} FROM stores WHERE organization_id = %s AND name = %s ORDER BY created_at, id",
            (session.organization_id, name),
        ).fetchall()
    return [Store(*r) for r in rows]


def get_store(session: TenantSession, store_id: UUID) -> Store:
    with session.transaction(Permission.READ) as conn:
        return fetch_store(conn, session, store_id)


def fetch_store(conn, session: TenantSession, store_id: UUID) -> Store:
    row = conn.execute(
        f"SELECT {_STORE_COLUMNS} FROM stores WHERE organization_id = %s AND id = %s",
        (session.organization_id, _uuid(store_id, "store")),
    ).fetchone()
    if row is None:
        raise NotFound("store")
    return Store(*row)


def list_stores(session: TenantSession, *, limit: int = 100) -> List[Store]:
    with session.transaction(Permission.READ) as conn:
        rows = conn.execute(
            f"SELECT {_STORE_COLUMNS} FROM stores WHERE organization_id = %s ORDER BY created_at, id LIMIT %s",
            (session.organization_id, _limit(limit)),
        ).fetchall()
    return [Store(*r) for r in rows]


def create_csv_connection(session: TenantSession, *, store_id: UUID, label: str,
                          hook: Optional[CreationHook] = None) -> Connection:
    with session.transaction(Permission.MANAGE_CONNECTIONS) as conn:
        fetch_store(conn, session, store_id)
        row = conn.execute(
            f"INSERT INTO connections (id, organization_id, store_id, kind, label) "
            f"VALUES (%s, %s, %s, 'csv_upload', %s) RETURNING {_CONNECTION_COLUMNS}",
            (uuid.uuid4(), session.organization_id, store_id, label),
        ).fetchone()
        if hook is not None:
            hook(conn, row[0])
    return Connection(*row)


def list_connections(session: TenantSession, store_id: UUID, *, include_revoked: bool = False,
                     limit: int = 100) -> List[Connection]:
    with session.transaction(Permission.READ) as conn:
        fetch_store(conn, session, store_id)
        rows = conn.execute(
            f"SELECT {_CONNECTION_COLUMNS} FROM connections WHERE organization_id = %s AND store_id = %s "
            "  AND (%s OR status = 'active') ORDER BY created_at, id LIMIT %s",
            (session.organization_id, _uuid(store_id, "store"), include_revoked, _limit(limit)),
        ).fetchall()
    return [Connection(*r) for r in rows]


def fetch_connection(conn, session: TenantSession, store_id: UUID, connection_id: UUID) -> Connection:
    row = conn.execute(
        f"SELECT {_CONNECTION_COLUMNS} FROM connections WHERE organization_id = %s AND store_id = %s AND id = %s",
        (session.organization_id, _uuid(store_id, "store"), _uuid(connection_id, "connection")),
    ).fetchone()
    if row is None:
        raise NotFound("connection")
    return Connection(*row)


def get_connection(session: TenantSession, store_id: UUID, connection_id: UUID) -> Connection:
    with session.transaction(Permission.READ) as conn:
        return fetch_connection(conn, session, store_id, connection_id)


def revoke_connection(session: TenantSession, store_id: UUID, connection_id: UUID) -> Connection:
    """Revocation: l'historique (instantanes) est conserve, la connexion ne sert plus a importer."""
    with session.transaction(Permission.MANAGE_CONNECTIONS) as conn:
        row = conn.execute(
            f"UPDATE connections SET status = 'revoked', revoked_at = now() "
            f"WHERE organization_id = %s AND store_id = %s AND id = %s AND status = 'active' "
            f"RETURNING {_CONNECTION_COLUMNS}",
            (session.organization_id, _uuid(store_id, "store"), _uuid(connection_id, "connection")),
        ).fetchone()
        if row is None:
            fetch_connection(conn, session, store_id, connection_id)  # NotFound si hors tenant
            raise NotFound("active connection")
    return Connection(*row)


def _uuid(value, kind: str) -> UUID:
    """Un identifiant mal forme est traite comme introuvable, jamais transmis tel quel."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise NotFound(kind) from None


def _limit(limit: int) -> int:
    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit doit etre compris entre 1 et 1000")
    return limit
