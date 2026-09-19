"""Cas d'usage persistes: import CSV -> instantane, instantane -> rapport persiste.

Chemin CLI (inchange):   CSV -> load_dataset -> moteur -> rapport -> fichiers
Chemin SaaS (ce module): CSV -> validation -> load_dataset -> instantane PostgreSQL
                         instantane -> Dataset relu -> MEME moteur -> rapport -> PostgreSQL

Ce module ne calcule rien: il orchestre les connecteurs, la persistance et le
moteur. Il n'est pas importe par `mervio.application` (paquet sans dependance):
l'importer exige l'extra `persistence`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional
from uuid import UUID

import psycopg

from ..analytics.pipeline import SourcePaths, analyze_loaded_dataset, load_dataset
from ..config import AnalyticsConfig
from ..errors import InsufficientDataError, MervioError
from ..identity import MasterKey
from ..logging_config import get_logger
from ..persistence import analyses, snapshots
from ..persistence.analyses import AnalysisRunRecord, ReportRecord
from ..persistence.codec import file_sha256, inputs_sha256
from ..persistence.errors import ImportRejected
from ..persistence.identity_keys import ensure_identity_key
from ..persistence.snapshots import SnapshotRecord, SourceFile
from ..persistence.stores import fetch_connection, fetch_store
from ..persistence.tenancy import Permission, TenantSession
from .imports import REQUIRED_SOURCES, FileValidation, validate_many
from .service import annotate_report

log = get_logger("application.persisted_analysis")


@dataclass(frozen=True)
class SnapshotImportRequest:
    shopify_orders: Optional[str] = None
    shopify_products: Optional[str] = None
    stripe: Optional[str] = None
    google_ads: Optional[str] = None
    synthetic: bool = False
    #: manifeste mervio.synthetic; s'il declare `synthetic`, l'instantane l'est aussi
    synthetic_manifest: Optional[dict] = None

    def provided(self) -> Dict[str, str]:
        files = {"shopify_orders": self.shopify_orders, "shopify_products": self.shopify_products,
                 "stripe": self.stripe, "google_ads": self.google_ads}
        return {kind: path for kind, path in files.items() if path}

    @property
    def is_synthetic(self) -> bool:
        return bool(self.synthetic or (self.synthetic_manifest or {}).get("synthetic"))


@dataclass
class SnapshotImportResult:
    status: str  # completed | reused | rejected
    snapshot: SnapshotRecord
    validations: List[FileValidation] = field(default_factory=list)
    error_code: Optional[str] = None
    error: Optional[str] = None


def import_csv_snapshot(session: TenantSession, *, store_id: UUID, connection_id: UUID,
                        request: SnapshotImportRequest, master_key: MasterKey,
                        on_started: Optional[Callable[[], None]] = None) -> SnapshotImportResult:
    """Valide les fichiers avec les connecteurs reels puis les persiste en instantane scelle.

    `master_key` (obligatoire): les references client sont calculees avec la cle d'identite de
    l'organisation (D-053); aucun e-mail n'est persiste. `IdentityKeyUnavailable` si la cle ne
    peut pas etre utilisee: aucun fichier n'est alors lu.

    Ordre garanti (004.4.2, F-02): autorisation -> cle d'identite disponible et coherente ->
    `on_started()` (le worker y trace `import.started`) -> premier acces a un fichier. Un refus
    d'identite survient donc AVANT que l'import soit considere comme commence.
    """
    # autorisation AVANT toute lecture de fichier
    with session.transaction(Permission.IMPORT_DATA) as conn:
        store = fetch_store(conn, session, store_id)
        fetch_connection(conn, session, store.id, connection_id)
    identity = ensure_identity_key(session, master_key)
    if on_started is not None:
        on_started()

    provided = request.provided()
    synthetic = request.is_synthetic
    sources = [SourceFile(kind, *file_sha256(path)) for kind, path in provided.items()]

    def rejected(code: str, message: str, validations=()) -> SnapshotImportResult:
        record = snapshots.record_failed_snapshot(session, store_id=store.id, connection_id=connection_id,
                                                  failure_code=code, synthetic=synthetic, sources=sources)
        log.warning("import refuse (%s) pour l'instantane %s", code, record.id)
        return SnapshotImportResult("rejected", record, list(validations), code, message)

    validations = validate_many(provided)
    usable = {v.source for v in validations if v.usable}
    missing = [s for s in REQUIRED_SOURCES if s not in usable]
    if missing:
        return rejected("validation_failed", "source obligatoire indisponible: " + ", ".join(missing), validations)

    fingerprint = inputs_sha256(
        file_hashes={s.kind: s.sha256 for s in sources}, connector=snapshots.CSV_CONNECTOR,
        schema_version=snapshots.CSV_SCHEMA_VERSION, normalization_version=snapshots.NORMALIZATION_VERSION,
        synthetic=synthetic, identity_key_id=identity.key_id,
    )
    existing = snapshots.find_completed_snapshot(session, store.id, fingerprint)
    if existing is not None:
        return SnapshotImportResult("reused", existing, validations)

    try:
        dataset = load_dataset(SourcePaths(**provided), identity)
    except MervioError as exc:
        return rejected("ingestion_failed", f"lecture impossible: {exc}", validations)
    if [SourceFile(kind, *file_sha256(path)) for kind, path in provided.items()] != sources:
        return rejected("source_changed_during_import", "fichier modifie pendant l'import", validations)

    try:
        record = snapshots.write_snapshot(
            session, store_id=store.id, connection_id=connection_id, dataset=dataset, sources=sources,
            inputs_sha256=fingerprint, synthetic=synthetic, identity=identity,
            synthetic_manifest=request.synthetic_manifest,
        )
    except ImportRejected as exc:
        return rejected(exc.code, str(exc), validations)
    except psycopg.errors.UniqueViolation:
        # import concurrent des memes octets: l'autre transaction a scelle l'instantane
        concurrent = snapshots.find_completed_snapshot(session, store.id, fingerprint)
        if concurrent is None:
            raise
        return SnapshotImportResult("reused", concurrent, validations)
    log.info("instantane %s scelle (%s enregistrements)", record.id, record.row_count)
    return SnapshotImportResult("completed", record, validations)


@dataclass
class PersistedAnalysis:
    status: str  # completed | reused | failed
    run: AnalysisRunRecord
    report: Optional[ReportRecord] = None
    error: Optional[str] = None


def analyze_snapshot(session: TenantSession, *, store_id: UUID, snapshot_id: UUID,
                     config: Optional[AnalyticsConfig] = None, today: Optional[date] = None,
                     label: str = "") -> PersistedAnalysis:
    """Analyse un instantane persiste avec le moteur inchange et persiste le rapport."""
    cfg = config or AnalyticsConfig()
    existing = analyses.find_completed_run(session, store_id=store_id, snapshot_id=snapshot_id, config=cfg,
                                           as_of_date=today, label=label)
    if existing is not None:
        stored = analyses.get_report_for_run(session, store_id, existing.id)
        return PersistedAnalysis("reused", existing, stored.record)

    run = analyses.start_run(session, store_id=store_id, snapshot_id=snapshot_id, config=cfg, as_of_date=today,
                             label=label)
    try:
        snapshot = snapshots.get_snapshot(session, store_id, snapshot_id)
        dataset = snapshots.load_dataset(session, store_id, snapshot_id, permission=Permission.RUN_ANALYSIS)
        report = analyze_loaded_dataset(dataset, cfg, today)
        annotate_report(report, synthetic=snapshot.synthetic, label=label)
    except InsufficientDataError as exc:
        return PersistedAnalysis("failed", analyses.fail_run(session, run, "insufficient_data"), error=str(exc))
    except MervioError as exc:
        return PersistedAnalysis("failed", analyses.fail_run(session, run, "analysis_failed"), error=str(exc))
    except BaseException:
        _fail_quietly(session, run, "internal_error")
        raise
    try:
        record = analyses.complete_run(session, run, report)
    except BaseException:
        _fail_quietly(session, run, "report_persistence_failed")
        raise
    log.info("analyse %s terminee, rapport %s", run.id, record.id)
    return PersistedAnalysis("completed", analyses.get_run(session, store_id, run.id), record)


def _fail_quietly(session: TenantSession, run: AnalysisRunRecord, code: str) -> None:
    """Marque l'execution en echec sans masquer l'exception d'origine."""
    try:
        analyses.fail_run(session, run, code)
    except Exception:  # noqa: BLE001 - l'exception d'origine est relevee par l'appelant
        log.exception("impossible de marquer l'execution %s en echec", run.id)
