"""Tests de restitution: rapport lisible, fichiers generes, securite."""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pytest

from mervio.application.service import AnalysisRequest, analyze_dataset
from mervio.application.workspace import WorkspaceError
from mervio.reporting import DATA_QUALITY_JSON, REPORT_JSON, REPORT_TXT
from mervio.reporting.executive import render_executive_report
from mervio.reporting.writers import write_outputs

MANDATORY_SECTIONS = (
    "MERVIO BUSINESS HEALTH REPORT", "## EXECUTIVE SUMMARY", "## CRITICAL ISSUES",
    "## WARNINGS", "## OPPORTUNITIES", "## KEY KPIs", "## PROFITABILITY",
    "## CUSTOMER HEALTH", "## PRODUCT HEALTH", "## MARKETING", "## ROOT CAUSES",
    "## RECOMMENDATIONS", "## DATA QUALITY", "## LIMITATIONS",
)


@pytest.fixture(scope="module")
def result(sample_paths_module):
    return analyze_dataset(AnalysisRequest(
        shopify_orders=sample_paths_module.shopify_orders,
        shopify_products=sample_paths_module.shopify_products,
        stripe=sample_paths_module.stripe,
        google_ads=sample_paths_module.google_ads,
        today=date(2026, 9, 14), label="test",
    ))


def test_report_contains_every_mandatory_section(result):
    text = render_executive_report(result.report)
    for section in MANDATORY_SECTIONS:
        assert section in text, section


def test_header_carries_score_period_and_currency(result):
    text = render_executive_report(result.report)
    assert "Business Health Score :" in text
    assert "Periode               :" in text
    assert "Devise                :" in text


def test_fact_evidence_hypothesis_recommendation_are_kept_apart(result):
    text = render_executive_report(result.report)
    assert "FACT   :" in text and "PREUVE :" in text
    assert "HYPOTH.:" in text and "ACTION :" in text


def test_hypothesis_is_never_presented_as_proven(result):
    text = render_executive_report(result.report)
    assert "n'etablissent pas un lien de causalite prouve" in text


def test_unavailable_values_say_so_instead_of_showing_zero(result):
    text = render_executive_report(result.report)
    assert "NON CALCULABLE" in text or "indisponible" in text
    assert "Couts MANQUANTS" in text


def test_unknown_margin_is_explained_not_shown_as_zero(result):
    text = render_executive_report(result.report)
    assert "pas que la marge est nulle" in text


def test_report_states_no_llm_is_involved(result):
    assert "Aucun modele de langage" in render_executive_report(result.report)


def test_synthetic_banner_appears_only_when_flagged(result, sample_paths_module):
    assert "DEMO DATA - SYNTHETIC" not in render_executive_report(result.report)
    demo = analyze_dataset(AnalysisRequest(
        shopify_orders=sample_paths_module.shopify_orders,
        today=date(2026, 9, 14), synthetic=True))
    assert "DEMO DATA - SYNTHETIC" in render_executive_report(demo.report)


# --- ecriture des fichiers -------------------------------------------
def test_write_outputs_creates_three_files(tmp_path, result):
    written = write_outputs(result, tmp_path / "out")
    names = {p.name for p in written}
    assert names == {REPORT_JSON, REPORT_TXT, DATA_QUALITY_JSON}
    for path in written:
        assert path.exists() and path.stat().st_size > 0


def test_written_json_is_the_engine_report(tmp_path, result):
    write_outputs(result, tmp_path / "out")
    payload = json.loads((tmp_path / "out" / REPORT_JSON).read_text(encoding="utf-8"))
    assert payload["business_health_score"] == result.report["business_health_score"]


def test_data_quality_document_carries_validations(tmp_path, result):
    write_outputs(result, tmp_path / "out")
    payload = json.loads((tmp_path / "out" / DATA_QUALITY_JSON).read_text(encoding="utf-8"))
    assert payload["analysis_id"] == result.analysis_id
    assert payload["validations"] and payload["data_quality"]
    assert "limitations" in payload


def test_writing_into_sample_directory_is_refused(tmp_path, result):
    with pytest.raises(WorkspaceError):
        write_outputs(result, tmp_path / "data" / "sample" / "leak")


def test_rejected_analysis_produces_no_file(tmp_path):
    from mervio.application.service import AnalysisRequest as Req
    rejected = analyze_dataset(Req())
    with pytest.raises(ValueError):
        write_outputs(rejected, tmp_path / "out")
    assert not (tmp_path / "out").exists()


# --- securite ---------------------------------------------------------
def test_no_customer_email_leaks_into_logs(caplog, sample_paths_module):
    with caplog.at_level(logging.DEBUG, logger="mervio"):
        analyze_dataset(AnalysisRequest(
            shopify_orders=sample_paths_module.shopify_orders,
            stripe=sample_paths_module.stripe, today=date(2026, 9, 14)))
    assert "@example.com" not in caplog.text


def test_no_customer_email_leaks_into_data_quality(result):
    payload = json.dumps(result.data_quality_document())
    assert "@example.com" not in payload


def test_no_customer_email_leaks_into_the_readable_report(result):
    assert "@example.com" not in render_executive_report(result.report)


def test_error_messages_redact_emails():
    from mervio.ingestion.base import redact
    assert "@" not in redact("valeur cassee jean.dupont@client.fr")


# --- pseudonymisation des clients dans les livrables -----------------
def test_customer_ids_are_pseudonymised_in_the_report(result):
    """Un livrable circule: il ne doit contenir aucun email client."""
    payload = json.dumps(result.report)
    assert "@example.com" not in payload
    for customer in result.report["customers"]["top_customers"]:
        assert customer["customer_id"].startswith("cust_")


def test_pseudonym_is_stable_and_not_reversible():
    from mervio.analytics.report import pseudonymise
    assert pseudonymise("a@x.com") == pseudonymise("a@x.com")
    assert pseudonymise("a@x.com") != pseudonymise("b@x.com")
    assert "a@x.com" not in pseudonymise("a@x.com")
