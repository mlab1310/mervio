"""Report Contract 2.0 sur la chaine persistee (004.5 -- phase 3, section 6).

D-058 exige que le rapport CLI et le rapport persiste soient identiques OCTET POUR OCTET,
`_meta.generated_at` excepte. `tests/persistence/test_persistence_reports.py` le mesure deja.
Ce module ajoute la question que le contrat 2.0 pose: les DEUX cotes valident-ils le schema ?

Un seul cote valide ne suffirait pas. L'API 004.6 servira le rapport PERSISTE: si seule la
sortie CLI respectait le contrat, la ressource publiee serait hors contrat sans qu'aucun test
ne le dise.

Aucune migration, aucune ecriture nouvelle: les memes chemins que ceux deja couverts.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

import mervio.analytics.report as report_module
import test_report_schema_2_0 as schema_contract
from mervio.application.persisted_analysis import SnapshotImportRequest, analyze_snapshot, import_csv_snapshot
from mervio.application.service import AnalysisRequest, analyze_dataset
from mervio.config import REPORT_CONTRACT
from mervio.persistence import analyses
from mervio.persistence.codec import serialize_report
from mervio.reporting.writers import REPORT_JSON, write_outputs

from .persistence_support import SAMPLE_FILES, TEST_MASTER_KEY, analysis_day, engine_files, org_identity

FROZEN = datetime(2026, 9, 14, 6, 30, 0, tzinfo=timezone.utc)
#: seul champ non deterministe autorise (D-058, reserve 1)
NON_DETERMINISTIC = "generated_at"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN if tz is not None else FROZEN.replace(tzinfo=None)


@pytest.fixture
def frozen_clock(monkeypatch):
    """Horloge figee des DEUX cotes: `generated_at` est le seul champ exclu de la comparaison,
    et le figer rend l'egalite d'octets totale au lieu de partielle."""
    monkeypatch.setattr(report_module, "datetime", _FrozenDatetime)


def _cli(files, tmp_path, *, today, synthetic, label="", identity=None):
    result = analyze_dataset(AnalysisRequest(**files, today=today, synthetic=synthetic,
                                             label=label, identity=identity))
    assert result.succeeded, result.error
    out = tmp_path / "cli_output"
    write_outputs(result, out)
    return result.report, (out / REPORT_JSON).read_bytes()


def _import(tenant, files, **extra):
    result = import_csv_snapshot(tenant.session, store_id=tenant.store_id,
                                 connection_id=tenant.connection_id,
                                 request=SnapshotImportRequest(**files, **extra),
                                 master_key=TEST_MASTER_KEY)
    assert result.status == "completed", result.error
    return result.snapshot


def test_6_the_cli_and_the_persisted_report_both_satisfy_the_2_0_contract(
        db, tenant_a, synthetic_set, tmp_path, frozen_clock):
    """Les deux cotes, le meme schema, les memes octets.

    L'ordre des assertions est celui des garanties: le contrat d'abord (chaque cote est un
    rapport 2.0), l'identite ensuite (c'est le MEME rapport 2.0).
    """
    files = engine_files(synthetic_set.directory)
    today = analysis_day(synthetic_set.manifest)
    cli_report, cli_bytes = _cli(files, tmp_path, today=today, synthetic=True, label="hebdo",
                                 identity=org_identity(tenant_a))

    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id,
                              snapshot_id=snapshot.id, today=today, label="hebdo")
    assert outcome.status == "completed", outcome.error
    stored = analyses.get_report_for_run(tenant_a.session, tenant_a.store_id, outcome.run.id)
    persisted = stored.report()

    schema_contract.assert_valid(cli_report, "rapport CLI")
    schema_contract.assert_valid(persisted, "rapport persiste")
    assert cli_report["_meta"]["report_contract"] == REPORT_CONTRACT
    assert persisted["_meta"]["report_contract"] == REPORT_CONTRACT
    assert stored.payload_bytes() == cli_bytes
    assert serialize_report(persisted).encode("utf-8") == cli_bytes


def test_6_the_persisted_report_read_back_is_a_2_0_report_on_the_versioned_sample(
        db, tenant_a, tmp_path, frozen_clock):
    """Jeu versionne `data/sample`: dates invalides, doublons, remboursements Stripe, lignes
    rejetees. Le contrat doit tenir sur la donnee ABIMEE, pas seulement sur la donnee propre."""
    today = date(2026, 9, 14)
    cli_report, cli_bytes = _cli(SAMPLE_FILES, tmp_path, today=today, synthetic=False,
                                 identity=org_identity(tenant_a))
    snapshot = _import(tenant_a, SAMPLE_FILES)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id,
                              snapshot_id=snapshot.id, today=today)
    stored = analyses.get_report(tenant_a.session, tenant_a.store_id, outcome.report.id)
    schema_contract.assert_valid(cli_report, "rapport CLI (data/sample)")
    schema_contract.assert_valid(stored.report(), "rapport persiste (data/sample)")
    assert stored.payload_bytes() == cli_bytes
    assert stored.record.payload_bytes == len(cli_bytes)


@pytest.mark.parametrize("sources", [("shopify_orders",),
                                     ("shopify_orders", "stripe"),
                                     ("shopify_orders", "shopify_products", "google_ads")],
                         ids=lambda sources: "+".join(s.replace("shopify_", "") for s in sources))
def test_6_partial_sources_stay_in_contract_on_both_sides(db, tenant_a, tmp_path, frozen_clock,
                                                          sources):
    files = {name: path for name, path in SAMPLE_FILES.items() if name in sources}
    today = date(2026, 9, 14)
    cli_report, cli_bytes = _cli(files, tmp_path, today=today, synthetic=False,
                                 identity=org_identity(tenant_a))
    snapshot = _import(tenant_a, files)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id,
                              snapshot_id=snapshot.id, today=today)
    stored = analyses.get_report(tenant_a.session, tenant_a.store_id, outcome.report.id)
    schema_contract.assert_valid(cli_report, f"CLI {sources}")
    schema_contract.assert_valid(stored.report(), f"persiste {sources}")
    assert stored.payload_bytes() == cli_bytes


def test_6_the_persisted_projection_agrees_with_the_contract(db, tenant_a, synthetic_set,
                                                             tmp_path, frozen_clock):
    """Les colonnes projetees de `reports` sont COPIEES du rapport, jamais recalculees: elles
    doivent donc s'accorder avec les valeurs que le contrat 2.0 publie."""
    files = engine_files(synthetic_set.directory)
    today = analysis_day(synthetic_set.manifest)
    snapshot = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id,
                              snapshot_id=snapshot.id, today=today, label="projection")
    stored = analyses.get_report_for_run(tenant_a.session, tenant_a.store_id, outcome.run.id)
    payload = stored.report()
    schema_contract.assert_valid(payload, "rapport persiste")
    record = stored.record
    assert record.grain == payload["_meta"]["grain"]
    assert record.currency == payload["_meta"]["currency"]
    assert record.period_label == payload["period"]["current"]["label"]
    assert record.period_start.isoformat() == payload["period"]["current"]["start"]
    assert record.period_end.isoformat() == payload["period"]["current"]["end"]
    assert record.generated_at == datetime.fromisoformat(payload["_meta"][NON_DETERMINISTIC])
    assert record.synthetic is payload["_meta"]["dataset_is_synthetic"]


def test_6_a_report_of_another_contract_version_is_not_silently_accepted(db, tenant_a,
                                                                        synthetic_set, tmp_path):
    """Garde de frontiere: le schema refuse un rapport 1.x, et la chaine persistee ne fabrique
    jamais un tel rapport. Les deux proprietes ensemble sont ce qui rend l'API 004.6 sure."""
    files = engine_files(synthetic_set.directory)
    report, _ = _cli(files, tmp_path, today=analysis_day(synthetic_set.manifest), synthetic=True)
    legacy = json.loads(json.dumps(report))
    legacy["_meta"]["report_contract"] = "1.0"
    schema_contract.assert_rejected(legacy, "rapport annoncant 1.0")
