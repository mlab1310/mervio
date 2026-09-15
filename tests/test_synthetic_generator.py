"""Generateur synthetique deterministe (Mission 004.0, ADR-004-011).

Ces tests verifient le generateur, pas le moteur: determinisme, coherence
relationnelle relue depuis le disque, compatibilite avec les connecteurs
existants et isolation du moteur.
"""
from __future__ import annotations

import ast
import csv
import json
from datetime import date
from pathlib import Path

import pytest

from mervio.analytics.pipeline import load_dataset
from mervio.synthetic import SCENARIOS, GeneratorConfig, generate_dataset, validate_dataset
from mervio.synthetic.evaluation import engine_paths

SMALL = dict(orders=1500, days=56, seed=7)
SRC = Path(__file__).resolve().parent.parent / "src" / "mervio"


def _generate(tmp_path, name="set", **overrides):
    config = GeneratorConfig(**{**SMALL, **overrides})
    return generate_dataset(config, tmp_path / name)


def test_same_configuration_produces_identical_bytes(tmp_path):
    first, second = _generate(tmp_path, "a"), _generate(tmp_path, "b")
    assert first.manifest == second.manifest
    for name in first.manifest["files"]:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    assert (tmp_path / "a" / "manifest.json").read_bytes() == (tmp_path / "b" / "manifest.json").read_bytes()


def test_a_different_seed_changes_the_data(tmp_path):
    first, second = _generate(tmp_path, "a"), _generate(tmp_path, "b", seed=8)
    assert first.manifest["files"]["shopify_orders.csv"]["sha256"] != second.manifest["files"]["shopify_orders.csv"]["sha256"]


def test_order_volume_follows_the_target(tmp_path):
    result = _generate(tmp_path, orders=3000)
    assert 2700 <= result.manifest["counts"]["orders"] <= 3300


def test_generated_dataset_is_relationally_consistent(tmp_path):
    for profile in ("fashion_eu", "electronics_us"):
        _generate(tmp_path, profile, profile=profile, scenario="stockout")
        report = validate_dataset(tmp_path / profile)
        assert report["status"] == "PASS", {k: v for k, v in report["checks"].items() if v["failures"]}


def test_validation_detects_a_tampered_file(tmp_path):
    _generate(tmp_path)
    path = tmp_path / "set" / "shopify_orders.csv"
    rows = list(csv.reader(path.open(encoding="utf-8")))
    header = rows[0]
    first = next(r for r in rows[1:] if r[header.index("Created at")])
    first[header.index("Total")] = "99999.99"
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)
    report = validate_dataset(tmp_path / "set")
    assert report["status"] == "FAIL"
    assert report["checks"]["files_hash_match"]["failures"] == 1
    assert report["checks"]["total_equals_components"]["failures"] == 1


def test_manifest_declares_synthetic_provenance(tmp_path):
    _generate(tmp_path)
    manifest = json.loads((tmp_path / "set" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True and manifest["not_for_production"] is True
    assert manifest["generator"] == "mervio.synthetic" and manifest["schema_version"] == "1"
    assert manifest["scenario"]["ground_truth"]["primary_root_cause"] == "none"
    assert "generated_at" not in json.dumps(manifest)                  # aucun horodatage: octets reproductibles


def test_existing_connectors_read_the_files_without_rejection(tmp_path):
    _generate(tmp_path, scenario="discount_explosion")
    dataset = load_dataset(engine_paths(tmp_path / "set"))
    kinds = {issue.kind for issue in dataset.quality.issues}
    assert not {"subtotal_convention_contradiction", "subtotal_contract_unverified", "missing_order_total",
                "refund_exceeds_order_total", "invalid_date"} & kinds
    assert all(count.rejected == 0 for count in dataset.quality.row_counts.values())
    assert dataset.currency == "EUR" and len(dataset.orders) > 0 and dataset.payments and dataset.ad_performance


def test_customer_emails_use_a_reserved_domain(tmp_path):
    _generate(tmp_path)
    with (tmp_path / "set" / "shopify_orders.csv").open(encoding="utf-8") as handle:
        emails = {row["Email"] for row in csv.DictReader(handle) if row["Email"]}
    assert emails and all(email.endswith("@customers.synthetic.invalid") for email in emails)


@pytest.mark.parametrize("overrides, message", [
    (dict(end_date=date(2026, 9, 12)), "dimanche"),
    (dict(scenario="unknown"), "scenario inconnu"),
    (dict(profile="unknown"), "profil inconnu"),
    (dict(days=7), "days"),
    (dict(orders=0), "orders"),
])
def test_invalid_configuration_is_refused(overrides, message):
    with pytest.raises(ValueError, match=message):
        GeneratorConfig(**{**SMALL, **overrides})


def test_the_analytics_engine_never_imports_the_generator():
    """Le moteur ne doit dependre d'aucune donnee fabriquee (ADR-004-011)."""
    offenders = []
    for package in ("analytics", "ingestion", "domain", "application", "reporting", "llm", "cli"):
        for path in (SRC / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                if any("synthetic" in name for name in names):
                    offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_all_mission_scenarios_are_defined_with_ground_truth():
    required = {"healthy_store", "revenue_drop", "order_volume_drop", "AOV_decline", "discount_explosion",
                "margin_collapse", "product_failure", "customer_churn", "retention_decline",
                "marketing_campaign_failure", "traffic_drop", "conversion_drop", "refund_spike",
                "seasonal_business", "stockout", "shipping_problem", "mixed_root_causes"}
    assert required <= set(SCENARIOS)
    for spec in SCENARIOS.values():
        truth = spec.ground_truth
        assert spec.baseline and spec.injected
        assert {"primary_root_cause", "revenue_direction", "orders_direction", "possible_secondary_causes",
                "recommendation_category"} <= set(truth)
        assert spec.expectations, spec.name
        # un scenario sans attente MATCH n'existe que pour exposer un angle mort, et doit le declarer
        assert any(e.status == "MATCH" for e in spec.expectations) or truth.get("engine_coverage") == "gap_exposure"
        assert all(e.status in ("MATCH", "KNOWN_GAP") for e in spec.expectations)
        assert all(e.note for e in spec.expectations if e.status == "KNOWN_GAP"), spec.name


def test_versioned_scenario_fixtures_match_the_code():
    """data/scenarios/*.json doit refleter exactement SCENARIOS (regenerer via scripts/generate_data.py)."""
    directory = SRC.parent.parent / "data" / "scenarios"
    files = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")}
    assert set(files) == set(SCENARIOS)
    for name, spec in SCENARIOS.items():
        assert files[name] == json.loads(json.dumps(spec.to_dict())), name
