"""Critere d'acceptation 004.1: rapport CLI -> PostgreSQL -> relu, identique OCTET POUR OCTET.

Deux preuves:
1. le rapport produit par la CLI est persiste puis relu: memes octets que le
   fichier `report.json` ecrit par `reporting.writers.write_outputs`;
2. chaine SaaS complete: memes fichiers -> import -> instantane PostgreSQL ->
   Dataset relu -> meme moteur -> rapport persiste -> relu: memes octets que la CLI.
   L'horloge du rapport (`_meta.generated_at`) est figee des deux cotes: c'est la
   seule valeur non deterministe du rapport (tests/test_pipeline_report.py la retire aussi).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone

import psycopg
import pytest

import mervio.analytics.report as report_module
from mervio.application.persisted_analysis import SnapshotImportRequest, analyze_snapshot, import_csv_snapshot
from mervio.application.service import AnalysisRequest, analyze_dataset
from mervio.config import AnalyticsConfig
from mervio.persistence import analyses
from mervio.persistence.codec import serialize_report
from mervio.persistence.errors import NotFound, SnapshotIntegrityError
from mervio.reporting.writers import REPORT_JSON, write_outputs

from .persistence_support import SAMPLE_FILES, analysis_day, engine_files

FROZEN = datetime(2026, 9, 14, 6, 30, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN if tz is not None else FROZEN.replace(tzinfo=None)


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(report_module, "datetime", _FrozenDatetime)


def _cli_report_bytes(files, tmp_path, *, today, synthetic, label=""):
    result = analyze_dataset(AnalysisRequest(**files, today=today, synthetic=synthetic, label=label))
    assert result.succeeded, result.error
    out = tmp_path / "cli_output"
    write_outputs(result, out)
    return result.report, (out / REPORT_JSON).read_bytes()


def _import(tenant, files, **extra):
    result = import_csv_snapshot(tenant.session, store_id=tenant.store_id, connection_id=tenant.connection_id,
                                 request=SnapshotImportRequest(**files, **extra))
    assert result.status == "completed", result.error
    return result.snapshot


def test_cli_report_persisted_and_retrieved_is_byte_for_byte_identical(db, tenant_a, synthetic_set, tmp_path):
    files = engine_files(synthetic_set.directory)
    today = analysis_day(synthetic_set.manifest)
    report, cli_bytes = _cli_report_bytes(files, tmp_path, today=today, synthetic=True)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)

    run = analyses.start_run(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id,
                             config=AnalyticsConfig(), as_of_date=today)
    record = analyses.complete_run(tenant_a.session, run, report)
    stored = analyses.get_report(tenant_a.session, tenant_a.store_id, record.id)

    assert stored.payload_bytes() == cli_bytes
    assert serialize_report(report).encode("utf-8") == cli_bytes  # le codec est bien celui du livrable CLI
    assert stored.report() == json.loads(cli_bytes)
    assert stored.record.payload_sha256 == hashlib.sha256(cli_bytes).hexdigest()
    assert stored.record.payload_bytes == len(cli_bytes)


def test_full_saas_chain_reproduces_the_cli_report_byte_for_byte(db, tenant_a, synthetic_set, tmp_path, frozen_clock):
    files = engine_files(synthetic_set.directory)
    today = analysis_day(synthetic_set.manifest)
    _, cli_bytes = _cli_report_bytes(files, tmp_path, today=today, synthetic=True, label="hebdo")

    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id, today=today,
                               label="hebdo")
    assert outcome.status == "completed", outcome.error
    stored = analyses.get_report_for_run(tenant_a.session, tenant_a.store_id, outcome.run.id)
    assert stored.payload_bytes() == cli_bytes


def test_full_chain_is_byte_identical_on_the_versioned_sample(db, tenant_a, tmp_path,
                                                                                    frozen_clock):
    """Jeu versionne data/sample: dates invalides, doublons, remboursements Stripe, lignes rejetees."""
    today = date(2026, 9, 14)
    _, cli_bytes = _cli_report_bytes(SAMPLE_FILES, tmp_path, today=today, synthetic=False)
    snapshot = _import(tenant_a, SAMPLE_FILES)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id, today=today)
    assert analyses.get_report(tenant_a.session, tenant_a.store_id, outcome.report.id).payload_bytes() == cli_bytes


@pytest.mark.parametrize("sources", [("shopify_orders",), ("shopify_orders", "stripe"),
                                     ("shopify_orders", "shopify_products", "google_ads")])
def test_partial_sources_keep_byte_identity(db, tenant_a, tmp_path, frozen_clock, sources):
    files = {k: v for k, v in SAMPLE_FILES.items() if k in sources}
    today = date(2026, 9, 14)
    _, cli_bytes = _cli_report_bytes(files, tmp_path, today=today, synthetic=False)
    snapshot = _import(tenant_a, files)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id, today=today)
    assert analyses.get_report(tenant_a.session, tenant_a.store_id, outcome.report.id).payload_bytes() == cli_bytes


def test_monthly_grain_keeps_byte_identity(db, tenant_a, synthetic_set, tmp_path, frozen_clock):
    files = engine_files(synthetic_set.directory)
    config = AnalyticsConfig(grain="month")
    result = analyze_dataset(AnalysisRequest(**files, config=config, synthetic=True))
    write_outputs(result, tmp_path / "month")
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id, config=config)
    stored = analyses.get_report(tenant_a.session, tenant_a.store_id, outcome.report.id)
    assert stored.payload_bytes() == (tmp_path / "month" / REPORT_JSON).read_bytes()
    assert stored.record.grain == "month" and outcome.run.as_of_date is None


def test_report_metadata_is_copied_from_the_report_not_recomputed(db, tenant_a, synthetic_set):
    files = engine_files(synthetic_set.directory)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id,
                               today=analysis_day(synthetic_set.manifest))
    stored = analyses.get_report(tenant_a.session, tenant_a.store_id, outcome.report.id)
    payload = stored.report()
    record = stored.record
    assert record.period_label == payload["period"]["current"]["label"]
    assert record.period_start.isoformat() == payload["period"]["current"]["start"]
    assert record.period_end.isoformat() == payload["period"]["current"]["end"]
    assert record.engine_version == payload["_meta"]["engine_version"]
    assert record.currency == payload["_meta"]["currency"]
    assert record.generated_at == datetime.fromisoformat(payload["_meta"]["generated_at"])
    assert record.synthetic is True and payload["_meta"]["dataset_is_synthetic"] is True
    assert (record.snapshot_id, record.analysis_run_id) == (snapshot.id, outcome.run.id)
    assert outcome.run.status == "completed" and outcome.run.finished_at is not None
    assert analyses.latest_report(tenant_a.session, tenant_a.store_id).id == record.id
    assert analyses.latest_report(tenant_a.session, tenant_a.store_id, grain="month") is None


def test_jsonb_projection_matches_the_authoritative_payload(owner, db, tenant_a, synthetic_set):
    files = engine_files(synthetic_set.directory)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id)
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant_a.organization_id),))
        payload, text, score = owner.execute(
            "SELECT payload, payload_json, payload #>> '{business_health_score,score}' FROM reports WHERE id = %s",
            (outcome.report.id,)).fetchone()
    assert payload == json.loads(text)
    assert score == str(json.loads(text)["business_health_score"]["score"])


def test_database_rejects_a_payload_whose_hash_does_not_match(owner, db, tenant_a, synthetic_set):
    files = engine_files(synthetic_set.directory)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    run = analyses.start_run(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id,
                             config=AnalyticsConfig(), as_of_date=None)
    with pytest.raises(psycopg.errors.CheckViolation):
        with owner.transaction():
            owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant_a.organization_id),))
            owner.execute(
                "INSERT INTO reports (id, organization_id, store_id, analysis_run_id, snapshot_id, engine_version, "
                "generated_at, grain, period_label, period_start, period_end, currency, synthetic, payload_json, "
                "payload, payload_sha256, payload_bytes) VALUES (%s, %s, %s, %s, %s, '0.1.0', now(), 'week', 'x', "
                "'2026-01-01', '2026-01-07', 'EUR', true, '{\"a\": 1}', '{\"a\": 1}', %s, 8)",
                (uuid.uuid4(), tenant_a.organization_id, tenant_a.store_id, run.id, snapshot.id, "0" * 64))


def test_persisted_report_is_immutable(owner, db, tenant_a, synthetic_set):
    files = engine_files(synthetic_set.directory)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id)
    for statement in ("UPDATE reports SET currency = 'USD' WHERE id = %s",
                      "UPDATE analysis_runs SET status = 'failed', failure_code = 'x' WHERE id = %s"):
        with pytest.raises(psycopg.errors.RestrictViolation):
            with owner.transaction():
                owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant_a.organization_id),))
                target = outcome.report.id if statement.startswith("UPDATE reports") else outcome.run.id
                owner.execute(statement, (target,))


def test_identical_analysis_is_reused_not_duplicated(db, tenant_a, synthetic_set):
    files = engine_files(synthetic_set.directory)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    first = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id)
    second = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id)
    assert second.status == "reused" and second.run.id == first.run.id and second.report.id == first.report.id
    other = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id, label="autre")
    assert other.status == "completed" and other.run.id != first.run.id
    assert len(analyses.list_runs(tenant_a.session, tenant_a.store_id)) == 2


def test_database_refuses_a_second_completed_run_for_the_same_inputs(db, tenant_a, synthetic_set):
    files = engine_files(synthetic_set.directory)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    first = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id)
    duplicate = analyses.start_run(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id,
                                   config=AnalyticsConfig(), as_of_date=None)
    report = analyses.get_report(tenant_a.session, tenant_a.store_id, first.report.id).report()
    with pytest.raises(psycopg.errors.UniqueViolation):
        analyses.complete_run(tenant_a.session, duplicate, report)


def test_a_report_can_be_attached_only_once_to_a_run(db, tenant_a, synthetic_set):
    files = engine_files(synthetic_set.directory)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id)
    report = analyses.get_report(tenant_a.session, tenant_a.store_id, outcome.report.id).report()
    with pytest.raises(SnapshotIntegrityError):
        analyses.complete_run(tenant_a.session, outcome.run, report)


def test_synthetic_marker_must_reach_the_report(db, tenant_a, synthetic_set, tmp_path):
    files = engine_files(synthetic_set.directory)
    unmarked, _ = _cli_report_bytes(files, tmp_path, today=None, synthetic=False)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    run = analyses.start_run(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id,
                             config=AnalyticsConfig(), as_of_date=None)
    with pytest.raises(SnapshotIntegrityError):
        analyses.complete_run(tenant_a.session, run, unmarked)


def test_report_from_another_engine_version_or_grain_is_refused(db, tenant_a, synthetic_set, tmp_path):
    files = engine_files(synthetic_set.directory)
    report, _ = _cli_report_bytes(files, tmp_path, today=None, synthetic=True)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    run = analyses.start_run(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id,
                             config=AnalyticsConfig(), as_of_date=None)
    for mutate in (lambda r: r["_meta"].__setitem__("engine_version", "9.9.9"),
                   lambda r: r["_meta"].__setitem__("grain", "month")):
        altered = json.loads(json.dumps(report))
        mutate(altered)
        with pytest.raises(SnapshotIntegrityError):
            analyses.complete_run(tenant_a.session, run, altered)


def test_analysis_failure_is_recorded_on_the_run(db, tenant_a):
    """Instantane scelle mais vide: le moteur refuse (InsufficientDataError), l'echec est trace, aucun rapport."""
    from mervio.domain.models import Dataset
    from mervio.domain.quality import DataQualityReport
    from mervio.persistence.snapshots import SourceFile, write_snapshot
    empty = write_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                           dataset=Dataset(quality=DataQualityReport(), currency="unknown"),
                           sources=[SourceFile("shopify_orders", "3" * 64, 0)], inputs_sha256="4" * 64,
                           synthetic=False)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=empty.id)
    assert outcome.status == "failed" and outcome.report is None and outcome.error
    assert outcome.run.status == "failed" and outcome.run.failure_code == "insufficient_data"
    assert outcome.run.finished_at is not None
    with pytest.raises(NotFound):
        analyses.get_report_for_run(tenant_a.session, tenant_a.store_id, outcome.run.id)


def test_failed_snapshot_cannot_be_analysed(db, tenant_a, tmp_path):
    broken = tmp_path / "orders.csv"
    broken.write_text("foo\n1\n", encoding="utf-8")
    failed = import_csv_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                                 request=SnapshotImportRequest(shopify_orders=str(broken))).snapshot
    with pytest.raises(SnapshotIntegrityError):
        analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=failed.id)
