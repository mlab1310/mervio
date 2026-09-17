"""Boutiques et connexions (metadonnees uniquement, jamais de secret)."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from .errors import NotFound
from .tenancy import CreationHook, Permission, TenantSession

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class Store:
    id: UUID
    organization_id: UUID
    name: str
    currency: Optional[str]
    created_at: datetime


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


_STORE_COLUMNS = "id, organization_id, name, currency, created_at"
_CONNECTION_COLUMNS = "id, organization_id, store_id, kind, label, status, created_at, revoked_at"


def create_store(session: TenantSession, *, name: str, currency: Optional[str] = None,
                 hook: Optional[CreationHook] = None) -> Store:
    if currency is not None and not _CURRENCY_RE.match(currency):
        raise ValueError("devise de boutique: code ISO 4217 en majuscules attendu")
    with session.transaction(Permission.MANAGE_STORES) as conn:
        row = conn.execute(
            f"INSERT INTO stores (id, organization_id, name, currency) VALUES (%s, %s, %s, %s) "
            f"RETURNING {_STORE_COLUMNS}",
            (uuid.uuid4(), session.organization_id, name, currency),
        ).fetchone()
        if hook is not None:
            hook(conn, row[0])
    return Store(*row)


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
