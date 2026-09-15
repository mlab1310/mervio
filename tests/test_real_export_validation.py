"""Harnais de validation d'export reel (scripts/validate_real_export.py).

Aucune donnee marchande reelle n'est versionnee: chaque variante est derivee
des fixtures synthetiques dans tmp_path, pour reproduire une particularite
connue des exports reels et verifier que le harnais la detecte.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
TODAY = __import__("datetime").date(2026, 9, 14)


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location("validate_real_export", ROOT / "scripts" / "validate_real_export.py")
    module = importlib.util.module_from_spec(spec)
    # les dataclasses resolvent leurs annotations via sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture(scope="module")
def full_result(harness):
    return harness.run(_sources(), today=TODAY)


def _sources(**overrides):
    sources = {"shopify_orders": str(SAMPLE / "shopify_orders.csv"),
               "shopify_products": str(SAMPLE / "shopify_products.csv"),
               "stripe": str(SAMPLE / "stripe_transactions.csv"),
               "google_ads": str(SAMPLE / "google_ads.csv")}
    sources.update(overrides)
    return sources


def _rewrite(tmp_path, name, transform, fieldnames=None):
    source = SAMPLE / name
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows, header = list(reader), list(reader.fieldnames)
    header = fieldnames(header) if fieldnames else header
    target = tmp_path / name
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for index, row in enumerate(rows):
            writer.writerow(transform(index, row) or row)
    return str(target)


# -- fixtures: tout concorde -----------------------------------------------------------
def test_every_kpi_reconciles_with_the_independent_reference(full_result):
    checks = {c["metric"]: c["status"] for c in full_result["reconciliation"]}
    assert set(checks) >= {"revenue", "orders", "units", "aov", "refunds", "active_customers", "new_customers",
                           "payment_fees", "ad_spend", "roas", "cac", "cogs_coverage_pct_rounded",
                           "revenue_change_vs_previous_period"}
    assert set(checks.values()) == {"PASS"}
    assert set(full_result["report_checks"].values()) == {"PASS"}


def test_fixtures_are_classified_partial_because_profit_is_unavailable(full_result):
    assert full_result["classification"] == "PARTIALLY_SUPPORTED"
    assert full_result["blockers"] == []
    assert any("profitability_unavailable" in r for r in full_result["classification_reasons"])


def test_subtotal_convention_of_fixtures_is_pre_discount(full_result):
    convention = full_result["semantics"]["subtotal_convention"]
    assert convention["conclusion"] == "pre_discount"
    assert convention["discounted_matching_post_discount"] == 0


def test_llm_stage_is_local_bounded_and_free_of_identifiers(full_result):
    llm = full_result["llm"]
    assert llm["provider"].startswith("mock")
    assert llm["context_bytes"] <= llm["context_limit_bytes"]
    assert llm["raw_identifiers_in_context"] == 0 and llm["emails_in_context"] == 0
    assert llm["sensitive_kinds_in_context"] == [] and llm["context_deterministic"] == "PASS"
    assert llm["mock_explanation_status"] == "completed"


def test_output_contains_no_raw_value(full_result):
    serialized = json.dumps(full_result, ensure_ascii=False, default=str)
    assert "@" not in serialized and "#10" not in serialized
    assert str(SAMPLE) not in serialized  # noms de fichiers seulement, jamais de chemin


# -- particularites des exports reels -------------------------------------------------
def test_subtotal_already_net_of_discount_is_detected_as_a_blocker(harness, tmp_path):
    def post_discount(_index, row):
        if row["Subtotal"]:
            row["Subtotal"] = f"{float(row['Subtotal']) - float(row['Discount Amount']):.2f}"
        return row
    result = harness.run(_sources(shopify_orders=_rewrite(tmp_path, "shopify_orders.csv", post_discount)), today=TODAY)
    assert result["semantics"]["subtotal_convention"]["conclusion"] == "post_discount"
    assert any(b.startswith("subtotal_already_net_of_discount") for b in result["blockers"])


def test_undiscounted_export_leaves_the_convention_unverified(harness, tmp_path):
    def no_discount(_index, row):
        if row["Subtotal"]:
            row["Discount Amount"] = "0.00"
            row["Total"] = f"{float(row['Subtotal']) + float(row['Shipping']) + float(row['Taxes']):.2f}"
        return row
    result = harness.run(_sources(shopify_orders=_rewrite(tmp_path, "shopify_orders.csv", no_discount)), today=TODAY)
    assert result["semantics"]["subtotal_convention"]["conclusion"].startswith("undetermined")
    assert any(b.startswith("subtotal_convention_unverified") for b in result["blockers"])


def test_cancelled_and_unpaid_orders_are_reported_as_counted_revenue(harness, tmp_path):
    def statuses(index, row):
        row["Cancelled at"] = "2026-09-01 10:00:00 +0200" if row["Subtotal"] and index % 50 == 0 else ""
        if row["Subtotal"] and index % 60 == 1:
            row["Financial Status"] = "voided"
        return row
    path = _rewrite(tmp_path, "shopify_orders.csv", statuses, fieldnames=lambda h: h + ["Cancelled at"])
    result = harness.run(_sources(shopify_orders=path), today=TODAY)
    semantics = result["semantics"]
    assert semantics["cancelled_column_present"] and semantics["cancelled_orders"] > 0
    assert semantics["financial_status_counts"].get("voided", 0) > 0
    assert semantics["non_revenue_or_cancelled_revenue_share"] > 0
    assert "non_revenue_or_cancelled_orders_counted_as_revenue" in result["blockers"]


def test_multiple_currencies_make_the_dataset_invalid(harness, tmp_path):
    def currency(index, row):
        if row["Currency"] and index % 3 == 0:
            row["Currency"] = "USD"
        return row
    result = harness.run(_sources(shopify_orders=_rewrite(tmp_path, "shopify_orders.csv", currency)), today=TODAY)
    assert result["classification"] == "INVALID"
    assert "multiple currencies" in result["classification_reasons"][0]


def test_real_stripe_date_column_name_is_flagged(harness, tmp_path):
    rename = lambda h: ["Created date (UTC)" if c == "Created (UTC)" else c for c in h]  # noqa: E731

    def move(_index, row):
        row["Created date (UTC)"] = row.pop("Created (UTC)")
        return row
    stripe = _rewrite(tmp_path, "stripe_transactions.csv", move, fieldnames=rename)
    result = harness.run(_sources(stripe=stripe), today=TODAY)
    assert result["files"]["stripe"]["validation"]["forced_status"] == "invalid"
    assert any(b.startswith("stripe_date_column_not_recognised") for b in result["blockers"])


def test_google_ads_title_rows_before_the_header_are_flagged(harness, tmp_path):
    target = tmp_path / "google_ads.csv"
    target.write_text("Campaign performance report\n\"August 1, 2026 - September 13, 2026\"\n"
                      + (SAMPLE / "google_ads.csv").read_text(encoding="utf-8-sig"), encoding="utf-8")
    result = harness.run(_sources(google_ads=str(target)), today=TODAY)
    assert result["files"]["google_ads"]["validation"]["forced_status"] == "invalid"
    assert "google_ads_header_not_on_first_line" in result["blockers"]
    # constat moteur: un fichier optionnel invalide fait rejeter toute l'analyse
    assert result["analysis"]["status"] == "rejected" and result["classification"] == "INVALID"


def test_external_order_table_is_unsupported_and_never_analysed(harness, tmp_path):
    kaggle_like = tmp_path / "shopify_sales_dataset.csv"
    kaggle_like.write_text(
        "order_id,order_date,customer_id,product_id,product_category,product_price,discount_percent,quantity,"
        "customer_country,traffic_source,payment_method,shipping_cost,rating,is_returned,discounted_price,"
        "revenue,profit\n1,2025-01-01,7,3,Home,10.0,0,1,FR,ads,card,1.0,4.5,False,10.0,10.0,9.0\n",
        encoding="utf-8")
    result = harness.run({"shopify_orders": str(kaggle_like)}, today=TODAY)
    assert result["classification"] == "UNSUPPORTED"
    assert result["analysis"]["status"] == "not_run" and "reconciliation" not in result


def test_excel_workbook_is_invalid(harness, tmp_path):
    workbook = tmp_path / "orders_export.xlsx"
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    result = harness.run({"shopify_orders": str(workbook)}, today=TODAY)
    assert result["classification"] == "INVALID"
    assert "CSV UTF-8" in result["classification_reasons"][0]


# -- garde-fous de confidentialite ----------------------------------------------------
def test_output_is_refused_if_an_identifier_would_leak(harness):
    with pytest.raises(harness.PrivacyError):
        harness.assert_private({"note": "client jane.doe@example.com"}, set())
    with pytest.raises(harness.PrivacyError):
        harness.assert_private({"note": "commande #A-100234"}, {"#A-100234"})
    harness.assert_private({"orders": 12}, {"12"})  # un identifiant trop court ne bloque pas un comptage


def test_output_file_is_refused_inside_the_repository_outside_analysis(harness, full_result, tmp_path):
    with pytest.raises(harness.PrivacyError):
        harness.write_output(full_result, ROOT / "docs" / "validation.json")
    target = tmp_path / "validation.json"
    harness.write_output(full_result, target)
    assert json.loads(target.read_text(encoding="utf-8"))["classification"] == "PARTIALLY_SUPPORTED"


def test_independent_amount_parser_handles_export_formats(harness):
    assert harness.amount("1,234.50") == 1234.5
    assert harness.amount("1 234,50 €") == 1234.5
    assert harness.amount("(12.00)") == -12.0
    assert harness.amount("--") is None
    with pytest.raises(harness.ReferenceError_):
        harness.amount("abc")


def test_cli_exit_code_follows_classification(harness, capsys, tmp_path):
    code = harness.main(["--shopify-orders", str(SAMPLE / "shopify_orders.csv"), "--today", "2026-09-14"])
    assert code == 0 and "CLASSIFICATION : PARTIALLY_SUPPORTED" in capsys.readouterr().out
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    assert harness.main(["--shopify-orders", str(empty)]) == 1
