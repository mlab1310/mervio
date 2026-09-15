"""Donnees synthetiques: generer -> valider -> persister -> relire -> analyser, sans changer un chiffre."""
from __future__ import annotations

import psycopg
import pytest

from mervio.analytics.pipeline import SourcePaths, run_analysis
from mervio.application.persisted_analysis import SnapshotImportRequest, analyze_snapshot, import_csv_snapshot
from mervio.persistence import analyses, provenance, snapshots
from mervio.synthetic import validate_dataset
from mervio.synthetic.evaluation import evaluate
from mervio.synthetic.scenarios import get_scenario

from .persistence_support import analysis_day, engine_files


def test_synthetic_dataset_survives_persistence_with_identical_authoritative_results(db, tenant_a, synthetic_set):
    directory = synthetic_set.directory
    assert validate_dataset(directory)["status"] == "PASS"
    files = engine_files(directory)
    today = analysis_day(synthetic_set.manifest)

    imported = import_csv_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                                   request=SnapshotImportRequest(**files, synthetic_manifest=synthetic_set.manifest))
    assert imported.status == "completed"
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=imported.snapshot.id,
                               today=today)
    persisted = analyses.get_report(tenant_a.session, tenant_a.store_id, outcome.report.id).report()

    direct = run_analysis(SourcePaths(**files), today=today)
    for key in ("generated_at", "dataset_is_synthetic", "analysis_label"):
        persisted["_meta"].pop(key, None)
        direct["_meta"].pop(key, None)
    assert persisted == direct
    # chiffres autoritaires explicitement
    assert persisted["kpis"]["revenue"] == direct["kpis"]["revenue"]
    assert persisted["business_health_score"] == direct["business_health_score"]
    assert persisted["root_causes"] == direct["root_causes"]


def test_synthetic_markers_are_kept_from_manifest_to_report(db, tenant_a, synthetic_set):
    files = engine_files(synthetic_set.directory)
    # meme sans synthetic=True explicite, le manifeste suffit a marquer l'instantane
    imported = import_csv_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                                   request=SnapshotImportRequest(**files, synthetic_manifest=synthetic_set.manifest))
    snapshot = imported.snapshot
    assert snapshot.synthetic is True and snapshot.not_for_production is True
    assert snapshot.synthetic_manifest["synthetic"] is True and snapshot.synthetic_manifest["not_for_production"] is True
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=snapshot.id)
    stored = analyses.get_report(tenant_a.session, tenant_a.store_id, outcome.report.id)
    assert stored.record.synthetic is True
    assert stored.report()["_meta"]["dataset_is_synthetic"] is True
    chain = provenance.trace_report(tenant_a.session, tenant_a.store_id, outcome.report.id)
    assert chain.synthetic is True and chain.not_for_production is True


def test_database_refuses_synthetic_data_marked_for_production(owner, db, tenant_a):
    with pytest.raises(psycopg.errors.CheckViolation):
        with owner.transaction():
            owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant_a.organization_id),))
            owner.execute(
                "INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, source, connector, "
                "connector_version, schema_version, normalization_version, status, ingestion_started_at, synthetic, "
                "not_for_production) VALUES (gen_random_uuid(), %s, %s, %s, 'csv', 'x', 'x', 'x', 'x', 'ingesting', "
                "now(), true, false)", (tenant_a.organization_id, tenant_a.store_id, tenant_a.connection_id))


def test_scenario_ground_truth_evaluation_is_unchanged_after_persistence(db, tenant_a, tmp_path):
    """Un scenario a verite terrain donne la meme evaluation depuis le rapport persiste que depuis la CLI."""
    from mervio.synthetic import GeneratorConfig, generate_dataset
    # meme configuration que tests/test_synthetic_scenarios.py
    result = generate_dataset(GeneratorConfig(scenario="revenue_drop", orders=20_000, days=126, seed=3),
                              tmp_path / "scenario")
    files = engine_files(result.directory)
    today = analysis_day(result.manifest)
    imported = import_csv_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                                   request=SnapshotImportRequest(**files, synthetic_manifest=result.manifest))
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=imported.snapshot.id,
                               today=today)
    persisted = analyses.get_report(tenant_a.session, tenant_a.store_id, outcome.report.id).report()
    direct = run_analysis(SourcePaths(**files), today=today)
    scenario = get_scenario("revenue_drop")
    from_database = evaluate(persisted, scenario, result.manifest)
    assert from_database == evaluate(direct, scenario, result.manifest)
    assert from_database["satisfied"] is True
