"""Tests bout en bout: pipeline, structure du rapport, contexte LLM."""
from __future__ import annotations

from datetime import date

import pytest

from mervio.analytics.pipeline import run_analysis
from mervio.identity import CustomerIdentity
from mervio.llm import build_llm_context

REQUIRED_KEYS = {
    "business_health_score", "period", "executive_summary", "critical_issues", "warnings",
    "opportunities", "kpis", "profitability", "customers", "products", "marketing",
    "anomalies", "root_causes", "recommendations", "data_quality", "limitations",
}


def test_report_contains_every_contract_key(sample_report):
    assert REQUIRED_KEYS <= set(sample_report)


def test_report_declares_no_llm_used(sample_report):
    assert sample_report["_meta"]["llm_used"] is False


def test_report_warns_that_sample_data_is_synthetic(sample_report):
    assert "SYNTHETIQUE" in sample_report["_meta"]["synthetic_data_warning"]


def test_engine_detects_the_injected_conversion_incident(sample_report):
    """La fixture contient une chute de conversion: le moteur doit la retrouver."""
    analysis = sample_report["root_causes"][0]
    assert analysis["available"] is True
    assert analysis["primary_factor"] == "conversion_rate"
    assert analysis["observed_change_pct"] < 0


def test_engine_blames_the_right_campaign(sample_report):
    """L'incident a ete injecte sur Shopping - Core."""
    worst = sample_report["root_causes"][0]["campaign_contributors"][0]
    assert worst["name"] == "Shopping - Core"
    assert worst["conversions_delta"] < 0


def test_revenue_anomaly_is_surfaced_as_critical(sample_report):
    assert any(i["type"] == "revenue_decline" for i in sample_report["critical_issues"])


def test_profitability_is_declared_incomplete_on_sample(sample_report):
    """Les fixtures n'ont ni COGS complet ni cout transport."""
    profit = sample_report["profitability"]
    assert profit["data_available"] is False
    assert {"cogs", "shipping_cost"} <= set(profit["missing_components"])


def test_no_insight_invents_a_financial_impact(sample_report):
    for bucket in ("critical_issues", "warnings", "opportunities"):
        for insight in sample_report[bucket]:
            if insight["estimated_impact"] is not None:
                assert insight["estimated_impact_basis"], insight["type"]


def test_every_kpi_exposes_its_quality(sample_report):
    for key, metric in sample_report["kpis"].items():
        assert metric["data_quality"] in {"reliable", "incomplete", "unavailable"}
        if metric["value"] is None:
            assert metric["data_quality"] == "unavailable" or metric["notes"]


def test_limitations_are_not_empty(sample_report):
    assert len(sample_report["limitations"]) >= 3


def test_data_quality_records_injected_defects(sample_report):
    kinds = {i["kind"] for i in sample_report["data_quality"]["issues"]}
    assert "invalid_date" in kinds
    assert "duplicate_line_item" in kinds or "duplicate_payment" in kinds


def test_analysis_is_deterministic(sample_paths):
    # 004.4.2 (D-058): determinisme a cle d'identite IDENTIQUE; sans cle, une cle ephemere est tiree
    identity = CustomerIdentity.ephemeral()
    a = run_analysis(sample_paths, today=date(2026, 9, 14), identity=identity)
    b = run_analysis(sample_paths, today=date(2026, 9, 14), identity=identity)
    a["_meta"].pop("generated_at"), b["_meta"].pop("generated_at")
    assert a == b


def test_without_an_explicit_key_only_the_customer_pseudonyms_differ(sample_paths):
    a = run_analysis(sample_paths, today=date(2026, 9, 14))
    b = run_analysis(sample_paths, today=date(2026, 9, 14))
    for report in (a, b):
        report["_meta"].pop("generated_at")
    ids = [[c.pop("customer_id") for c in r["customers"]["top_customers"]] for r in (a, b)]
    assert a == b  # KPI, series, constats, clients (hors pseudonymes): identiques
    assert ids[0] != ids[1] and all(i.startswith("cust_") for i in ids[0] + ids[1])


def test_time_series_length_matches_lookback(sample_report):
    assert len(sample_report["time_series"]["revenue"]["points"]) == 12


def test_llm_context_carries_facts_and_contract(sample_report):
    context = build_llm_context(sample_report)
    assert context["facts"]
    assert "ne calcule aucun chiffre" in context["system_contract"]
    assert context["limitations"]


def test_llm_context_lists_unavailable_metrics_explicitly(sample_paths):
    """Sans Stripe ni Google Ads, le contexte LLM doit DIRE ce qu'il ignore,
    afin que le modele ne puisse pas combler les trous par invention."""
    from mervio.analytics.pipeline import SourcePaths
    partial = run_analysis(
        SourcePaths(shopify_orders=sample_paths.shopify_orders,
                    shopify_products=sample_paths.shopify_products),
        today=date(2026, 9, 14),
    )
    context = build_llm_context(partial)
    keys = {m["metric"] for m in context["unavailable_metrics"]}
    assert {"roas", "cac", "payment_fees"} <= keys
    for entry in context["unavailable_metrics"]:
        assert entry["reason"]


def test_cli_runs_end_to_end(tmp_path, sample_paths):
    """--out designe desormais un REPERTOIRE et produit les 3 livrables."""
    from mervio.cli import main
    out = tmp_path / "run"
    code = main(["analyze", "--shopify", sample_paths.shopify_orders,
                 "--products", sample_paths.shopify_products,
                 "--stripe", sample_paths.stripe, "--google-ads", sample_paths.google_ads,
                 "--out", str(out), "--today", "2026-09-14"])
    assert code == 0
    assert {p.name for p in out.iterdir()} == {"report.json", "report.txt", "data_quality.json"}


def test_cli_rejects_call_without_source():
    from mervio.cli import main
    assert main(["analyze", "--out", "/tmp/x.json"]) == 2
