"""Provenance au grain de l'execution (ADR-004-007), relue depuis les cles etrangeres.

Rapport -> execution d'analyse -> instantane -> fichiers source -> connexion,
et, pour un enregistrement canonique: source + source_record_id -> fichier
source -> instantane. Aucune table de provenance separee: la chaine est portee
par des cles etrangeres composites qui ne peuvent pas franchir un tenant.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional
from uuid import UUID

from .errors import NotFound
from .stores import _uuid
from .tenancy import Permission, TenantSession

#: entite -> table canonique (liste blanche: jamais un nom de table venant de l'appelant)
_ENTITY_TABLES = {
    "product": "products", "order": "orders", "payment": "payments", "refund": "refunds",
    "campaign": "campaigns", "ad_daily_performance": "ad_daily_performance",
}


@dataclass(frozen=True)
class SourceFileProvenance:
    source_kind: str
    file_sha256: str
    byte_size: int
    rows_read: Optional[int]
    rows_accepted: Optional[int]


@dataclass(frozen=True)
class ReportProvenance:
    organization_id: UUID
    store_id: UUID
    report_id: UUID
    payload_sha256: str
    generated_at: datetime
    analysis_run_id: UUID
    engine_version: str
    config_sha256: str
    as_of_date: Optional[date]
    snapshot_id: UUID
    inputs_sha256: str
    source: str
    connector: str
    connector_version: str
    schema_version: str
    normalization_version: str
    ingested_at: datetime
    synthetic: bool
    not_for_production: bool
    connection_id: UUID
    connection_kind: str
    sources: List[SourceFileProvenance]


@dataclass(frozen=True)
class RecordProvenance:
    organization_id: UUID
    store_id: UUID
    entity: str
    source: str
    source_record_id: str
    snapshot_id: UUID
    source_kind: str
    file_sha256: str
    connection_id: UUID
    connector: str
    normalization_version: str


def trace_report(session: TenantSession, store_id: UUID, report_id: UUID) -> ReportProvenance:
    with session.transaction(Permission.READ_PROVENANCE) as conn:
        row = conn.execute(
            """
            SELECT r.organization_id, r.store_id, r.id, r.payload_sha256, r.generated_at,
                   a.id, a.engine_version, a.config_sha256, a.as_of_date,
                   s.id, s.inputs_sha256, s.source, s.connector, s.connector_version, s.schema_version,
                   s.normalization_version, s.ingested_at, s.synthetic, s.not_for_production,
                   c.id, c.kind
            FROM reports r
            JOIN analysis_runs a ON a.organization_id = r.organization_id AND a.id = r.analysis_run_id
            JOIN data_snapshots s ON s.organization_id = r.organization_id AND s.id = r.snapshot_id
            JOIN connections c ON c.organization_id = s.organization_id AND c.id = s.connection_id
            WHERE r.organization_id = %s AND r.store_id = %s AND r.id = %s
            """,
            (session.organization_id, _uuid(store_id, "store"), _uuid(report_id, "report")),
        ).fetchone()
        if row is None:
            raise NotFound("report")
        sources = conn.execute(
            "SELECT source_kind, file_sha256, byte_size, rows_read, rows_accepted FROM snapshot_sources "
            "WHERE organization_id = %s AND snapshot_id = %s ORDER BY source_kind",
            (session.organization_id, row[9]),
        ).fetchall()
    return ReportProvenance(*row, sources=[SourceFileProvenance(*s) for s in sources])


def trace_record(session: TenantSession, store_id: UUID, snapshot_id: UUID, *, entity: str, source: str,
                 source_record_id: str) -> RecordProvenance:
    table = _ENTITY_TABLES.get(entity)
    if table is None:
        raise ValueError(f"entite sans provenance: {entity}")
    with session.transaction(Permission.READ_PROVENANCE) as conn:
        row = conn.execute(
            f"""
            SELECT x.organization_id, x.store_id, x.source, x.source_record_id, x.snapshot_id,
                   src.source_kind, src.file_sha256, s.connection_id, s.connector, s.normalization_version
            FROM {table} x
            JOIN snapshot_sources src ON src.organization_id = x.organization_id AND src.id = x.snapshot_source_id
            JOIN data_snapshots s ON s.organization_id = x.organization_id AND s.id = x.snapshot_id
            WHERE x.organization_id = %s AND x.store_id = %s AND x.snapshot_id = %s
              AND x.source = %s AND x.source_record_id = %s
            """,
            (session.organization_id, _uuid(store_id, "store"), _uuid(snapshot_id, "snapshot"), source,
             source_record_id),
        ).fetchone()
    if row is None:
        raise NotFound(entity)
    organization_id, store, src, record_id, snap, kind, sha, connection_id, connector, normalization = row
    return RecordProvenance(organization_id, store, entity, src, record_id, snap, kind, sha, connection_id, connector,
                            normalization)
