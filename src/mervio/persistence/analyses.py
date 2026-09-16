"""Executions d'analyse et rapports persistes.

Le rapport est stocke tel que produit par le moteur: `payload_json` contient les
octets exacts du livrable CLI. Aucune valeur n'est recalculee ni reformatee;
seules quelques metadonnees interrogeables (periode, versions, devise) sont
recopiees depuis le rapport en colonnes.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional
from uuid import UUID

from psycopg.types.json import Jsonb

from ..config import ENGINE_VERSION, AnalyticsConfig
from .codec import config_document, config_sha256, serialize_report, sha256_text
from .errors import NotFound, SnapshotIntegrityError
from .snapshots import fetch_snapshot
from .stores import _limit, _uuid, fetch_store
from .tenancy import Permission, TenantSession


@dataclass(frozen=True)
class AnalysisRunRecord:
    id: UUID
    organization_id: UUID
    store_id: UUID
    snapshot_id: UUID
    created_by: Optional[UUID]
    status: str
    engine_version: str
    grain: str
    as_of_date: Optional[date]
    config: dict
    config_sha256: str
    label: str
    started_at: datetime
    finished_at: Optional[datetime]
    failure_code: Optional[str]


@dataclass(frozen=True)
class ReportRecord:
    id: UUID
    organization_id: UUID
    store_id: UUID
    analysis_run_id: UUID
    snapshot_id: UUID
    engine_version: str
    generated_at: datetime
    grain: str
    period_label: str
    period_start: date
    period_end: date
    currency: str
    synthetic: bool
    payload_sha256: str
    payload_bytes: int
    created_at: datetime


@dataclass(frozen=True)
class StoredReport:
    record: ReportRecord
    payload_json: str

    def payload_bytes(self) -> bytes:
        return self.payload_json.encode("utf-8")

    def report(self) -> dict:
        return json.loads(self.payload_json)


_RUN_COLUMNS = ("id, organization_id, store_id, snapshot_id, created_by, status, engine_version, grain, as_of_date, "
                "config, config_sha256, label, started_at, finished_at, failure_code")
_REPORT_COLUMNS = ("id, organization_id, store_id, analysis_run_id, snapshot_id, engine_version, generated_at, grain, "
                   "period_label, period_start, period_end, currency, synthetic, payload_sha256, payload_bytes, "
                   "created_at")


def start_run(session: TenantSession, *, store_id: UUID, snapshot_id: UUID, config: AnalyticsConfig,
              as_of_date: Optional[date], label: str = "") -> AnalysisRunRecord:
    with session.transaction(Permission.RUN_ANALYSIS) as conn:
        snapshot = fetch_snapshot(conn, session, store_id, snapshot_id)
        if snapshot.status != "completed":
            raise SnapshotIntegrityError(f"instantane {snapshot.status}: seul un instantane complet est analysable")
        row = conn.execute(
            "INSERT INTO analysis_runs (id, organization_id, store_id, snapshot_id, created_by, status, engine_version, "
            "grain, as_of_date, config, config_sha256, label, started_at) "
            "VALUES (%s, %s, %s, %s, %s, 'running', %s, %s, %s, %s, %s, %s, clock_timestamp()) "
            f"RETURNING {_RUN_COLUMNS}",
            (uuid.uuid4(), session.organization_id, snapshot.store_id, snapshot.id, session.user_id, ENGINE_VERSION,
             config.grain, as_of_date, Jsonb(config_document(config)), config_sha256(config), label),
        ).fetchone()
    return AnalysisRunRecord(*row)


def find_completed_run(session: TenantSession, *, store_id: UUID, snapshot_id: UUID, config: AnalyticsConfig,
                       as_of_date: Optional[date], label: str = "") -> Optional[AnalysisRunRecord]:
    with session.transaction(Permission.READ) as conn:
        fetch_snapshot(conn, session, store_id, snapshot_id)
        row = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM analysis_runs WHERE organization_id = %s AND store_id = %s "
            "AND snapshot_id = %s AND config_sha256 = %s AND engine_version = %s AND label = %s "
            "AND as_of_date IS NOT DISTINCT FROM %s AND status = 'completed'",
            (session.organization_id, store_id, snapshot_id, config_sha256(config), ENGINE_VERSION, label, as_of_date),
        ).fetchone()
    return AnalysisRunRecord(*row) if row else None


def complete_run(session: TenantSession, run: AnalysisRunRecord, report: dict) -> ReportRecord:
    """Persiste le rapport et termine l'execution, dans une meme transaction."""
    payload_json = serialize_report(report)
    meta = report.get("_meta") or {}
    period = (report.get("period") or {}).get("current") or {}
    if meta.get("engine_version") != run.engine_version:
        raise SnapshotIntegrityError("version du moteur du rapport differente de celle de l'execution")
    if meta.get("grain") != run.grain or period.get("grain") != run.grain:
        raise SnapshotIntegrityError("grain du rapport different de celui de l'execution")
    try:
        generated_at = datetime.fromisoformat(meta["generated_at"])
        period_start = date.fromisoformat(period["start"])
        period_end = date.fromisoformat(period["end"])
        label, currency = period["label"], meta["currency"]
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotIntegrityError("rapport sans metadonnees de periode ou de generation lisibles") from exc

    with session.transaction(Permission.RUN_ANALYSIS) as conn:
        current = fetch_run(conn, session, run.store_id, run.id)
        if current.status != "running":
            raise SnapshotIntegrityError(f"execution {current.status}: rapport refuse")
        snapshot = fetch_snapshot(conn, session, run.store_id, run.snapshot_id)
        # le marqueur synthetique suit la donnee jusqu'au rapport (ADR-004-007), dans les deux sens
        if bool(meta.get("dataset_is_synthetic")) != snapshot.synthetic:
            raise SnapshotIntegrityError("marqueur synthetique du rapport incoherent avec l'instantane")
        row = conn.execute(
            "INSERT INTO reports (id, organization_id, store_id, analysis_run_id, snapshot_id, engine_version, "
            "generated_at, grain, period_label, period_start, period_end, currency, synthetic, payload_json, payload, "
            "payload_sha256, payload_bytes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS jsonb), %s, %s) "
            f"RETURNING {_REPORT_COLUMNS}",
            (uuid.uuid4(), session.organization_id, run.store_id, run.id, run.snapshot_id, run.engine_version,
             generated_at, run.grain, label, period_start, period_end, currency, snapshot.synthetic, payload_json,
             payload_json, sha256_text(payload_json), len(payload_json.encode("utf-8"))),
        ).fetchone()
        conn.execute(
            "UPDATE analysis_runs SET status = 'completed', finished_at = clock_timestamp() "
            "WHERE organization_id = %s AND id = %s",
            (session.organization_id, run.id),
        )
    return ReportRecord(*row)


def fail_run(session: TenantSession, run: AnalysisRunRecord, failure_code: str) -> AnalysisRunRecord:
    with session.transaction(Permission.RUN_ANALYSIS) as conn:
        row = conn.execute(
            "UPDATE analysis_runs SET status = 'failed', finished_at = clock_timestamp(), failure_code = %s "
            f"WHERE organization_id = %s AND store_id = %s AND id = %s AND status = 'running' RETURNING {_RUN_COLUMNS}",
            (failure_code, session.organization_id, run.store_id, run.id),
        ).fetchone()
        if row is None:
            fetch_run(conn, session, run.store_id, run.id)
            raise SnapshotIntegrityError("seule une execution en cours peut echouer")
    return AnalysisRunRecord(*row)


def fetch_run(conn, session: TenantSession, store_id: UUID, run_id: UUID) -> AnalysisRunRecord:
    row = conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM analysis_runs WHERE organization_id = %s AND store_id = %s AND id = %s",
        (session.organization_id, _uuid(store_id, "store"), _uuid(run_id, "analysis run")),
    ).fetchone()
    if row is None:
        raise NotFound("analysis run")
    return AnalysisRunRecord(*row)


def get_run(session: TenantSession, store_id: UUID, run_id: UUID) -> AnalysisRunRecord:
    with session.transaction(Permission.READ) as conn:
        return fetch_run(conn, session, store_id, run_id)


def list_runs(session: TenantSession, store_id: UUID, *, limit: int = 50) -> List[AnalysisRunRecord]:
    with session.transaction(Permission.READ) as conn:
        fetch_store(conn, session, store_id)
        rows = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM analysis_runs WHERE organization_id = %s AND store_id = %s "
            "ORDER BY started_at DESC, id LIMIT %s",
            (session.organization_id, store_id, _limit(limit)),
        ).fetchall()
    return [AnalysisRunRecord(*r) for r in rows]


def get_report(session: TenantSession, store_id: UUID, report_id: UUID) -> StoredReport:
    with session.transaction(Permission.READ) as conn:
        row = conn.execute(
            f"SELECT {_REPORT_COLUMNS}, payload_json FROM reports WHERE organization_id = %s AND store_id = %s AND id = %s",
            (session.organization_id, _uuid(store_id, "store"), _uuid(report_id, "report")),
        ).fetchone()
    if row is None:
        raise NotFound("report")
    return StoredReport(ReportRecord(*row[:-1]), row[-1])


def get_report_for_run(session: TenantSession, store_id: UUID, run_id: UUID) -> StoredReport:
    with session.transaction(Permission.READ) as conn:
        row = conn.execute(
            f"SELECT {_REPORT_COLUMNS}, payload_json FROM reports "
            "WHERE organization_id = %s AND store_id = %s AND analysis_run_id = %s",
            (session.organization_id, _uuid(store_id, "store"), _uuid(run_id, "analysis run")),
        ).fetchone()
    if row is None:
        raise NotFound("report")
    return StoredReport(ReportRecord(*row[:-1]), row[-1])


def latest_report(session: TenantSession, store_id: UUID, *, grain: Optional[str] = None) -> Optional[ReportRecord]:
    """Metadonnees du dernier rapport (periode la plus recente), sans charger la charge utile."""
    if grain not in (None, "week", "month"):
        raise ValueError("grain non supporte")
    with session.transaction(Permission.READ) as conn:
        fetch_store(conn, session, store_id)
        row = conn.execute(
            f"SELECT {_REPORT_COLUMNS} FROM reports WHERE organization_id = %s AND store_id = %s "
            "AND (%s::text IS NULL OR grain = %s::text) ORDER BY period_end DESC, created_at DESC LIMIT 1",
            (session.organization_id, store_id, grain, grain),
        ).fetchone()
    return ReportRecord(*row) if row else None
