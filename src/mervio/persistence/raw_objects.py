"""Objets bruts par tenant (Mission 004.4.4, D-054, revision 0013).

La LIGNE est l'autorite, pas la cle ni le magasin. Un objet n'est lisible que si la RLS le
laisse voir dans le contexte de la transaction; un objet d'une AUTRE organisation et un objet
INEXISTANT levent donc la meme `NotFound`, indiscernables par construction (`persistence.errors`).

004.4.4 n'ecrit que des lignes `available` et ne fait AUCUNE transition d'etat: la base n'accorde
ni `UPDATE` ni `DELETE` (revision 0013). La destruction releve de D-056 (004.4.5) et le ramassage
des orphelins de 004.9.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional
from uuid import UUID

from .errors import NotFound
from .stores import _limit, _uuid
from .tenancy import Permission, TenantSession

#: Types de source acceptes, identiques a `snapshot_sources.source_kind` (revision 0002).
SOURCE_KINDS = ("shopify_orders", "shopify_products", "stripe", "google_ads")
#: Comment l'objet est arrive. Fixe COTE SERVEUR: un appelant ne nomme jamais son origine.
ORIGIN_CSV_UPLOAD = "csv_upload"
#: Seul etat produit par 004.4.4 (D-054: un orphelin est illisible car SANS ligne `available`).
STATE_AVAILABLE = "available"

_COLUMNS = ("id, organization_id, store_id, object_key, sha256, byte_size, source_kind, origin, "
            "state, retain_until, purged_at, created_at")

CreationHook = Callable[[object, UUID], None]


@dataclass(frozen=True)
class RawObject:
    """Metadonnees NON sensibles d'un objet brut: jamais de nom de fichier, jamais de chemin."""

    id: UUID
    organization_id: UUID
    store_id: UUID
    object_key: str
    sha256: str
    byte_size: int
    source_kind: str
    origin: str
    state: str
    retain_until: datetime
    purged_at: Optional[datetime]
    created_at: datetime

    @property
    def available(self) -> bool:
        return self.state == STATE_AVAILABLE

    def public(self) -> dict:
        """Vue publiable (CLI, futur API): aucune cle d'objet, aucun nom de fichier."""
        return {
            "id": str(self.id), "store_id": str(self.store_id), "source_kind": self.source_kind,
            "origin": self.origin, "sha256": self.sha256, "byte_size": self.byte_size,
            "state": self.state, "retain_until": self.retain_until.isoformat(),
            "created_at": self.created_at.isoformat(),
        }


def record_object(session: TenantSession, *, store_id: UUID, object_key: str, sha256: str,
                  byte_size: int, source_kind: str, retain_until: datetime,
                  hook: Optional[CreationHook] = None) -> RawObject:
    """Rend LISIBLES des octets deja ecrits: la ligne nait `available` (D-054, D-061).

    Appelee APRES `ObjectStore.put`, dans une transaction courte qui porte aussi l'audit.
    Un echec ici laisse des octets orphelins, sans ligne, donc illisibles: risque residuel
    explicitement accepte par D-054, ramassage reporte a 004.9.
    """
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"type de source inconnu: {source_kind}")
    with session.transaction(Permission.IMPORT_DATA) as conn:
        row = conn.execute(
            f"INSERT INTO raw_objects (id, organization_id, store_id, object_key, sha256, byte_size, "
            f"source_kind, origin, state, retain_until) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (uuid.uuid4(), session.organization_id, _uuid(store_id, "store"), object_key, sha256,
             byte_size, source_kind, ORIGIN_CSV_UPLOAD, STATE_AVAILABLE, retain_until),
        ).fetchone()
        if hook is not None:
            hook(conn, row[0])
    return RawObject(*row)


def fetch_object(conn, object_id: UUID) -> RawObject:
    """Resout un objet SOUS RLS, dans la transaction ouverte par l'appelant.

    La requete ne filtre QUE sur l'identifiant: l'appartenance a l'organisation est imposee par
    la politique `raw_objects_tenant_isolation`, pas par un controle applicatif. Ajouter ici un
    `organization_id = %s` donnerait l'illusion que l'isolation vient de Python; elle vient de
    PostgreSQL. Une ligne d'un autre tenant est INVISIBLE, donc introuvable: meme erreur qu'un
    identifiant qui n'a jamais existe, sans oracle d'existence.
    """
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM raw_objects WHERE id = %s",
        (_uuid(object_id, "raw_object"),),
    ).fetchone()
    if row is None:
        raise NotFound("raw_object")
    return RawObject(*row)


def get_object(session: TenantSession, object_id: UUID) -> RawObject:
    """`fetch_object` avec sa propre transaction (usage CLI et tests)."""
    with session.transaction(Permission.READ) as conn:
        return fetch_object(conn, object_id)


def list_objects(session: TenantSession, store_id: UUID, *, limit: int = 100) -> List[RawObject]:
    with session.transaction(Permission.READ) as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM raw_objects WHERE organization_id = %s AND store_id = %s "
            f"ORDER BY created_at DESC, id LIMIT %s",
            (session.organization_id, _uuid(store_id, "store"), _limit(limit)),
        ).fetchall()
    return [RawObject(*r) for r in rows]


__all__ = ["ORIGIN_CSV_UPLOAD", "SOURCE_KINDS", "STATE_AVAILABLE", "RawObject", "fetch_object",
           "get_object", "list_objects", "record_object"]
