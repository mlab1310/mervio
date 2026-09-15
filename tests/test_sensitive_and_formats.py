"""Tests: separateurs CSV, donnees sensibles, non-fuite de valeurs."""
from __future__ import annotations

import json
import logging
from datetime import date

import pytest

from mervio.application.imports import validate_file
from mervio.application.sensitive import scan_rows
from mervio.ingestion.base import sniff_delimiter

ORDERS_HEADER = ("Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,"
                 "Shipping,Taxes,Total,Refunded Amount,Lineitem quantity,Lineitem name,"
                 "Lineitem price,Lineitem sku\n")
ORDERS_ROW = "#1,a@x.com,paid,2026-08-03 10:00:00 +0000,EUR,100,0,0,20,120,,1,Produit A,100.00,A\n"


def write(tmp_path, name, content, encoding="utf-8"):
    path = tmp_path / name
    path.write_text(content, encoding=encoding)
    return str(path)


# --- separateurs -----------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("a,b,c\n1,2,3", ","), ("a;b;c\n1;2;3", ";"), ("a\tb\tc", "\t"), ("a|b|c", "|"), ("", ","),
])
def test_sniff_delimiter(raw, expected):
    assert sniff_delimiter(raw) == expected


def test_semicolon_export_is_read_correctly(tmp_path):
    """Un export genere sur un poste francais sort en ';'."""
    csv = (ORDERS_HEADER + ORDERS_ROW).replace(",", ";")
    validation = validate_file(write(tmp_path, "o.csv", csv), "shopify_orders")
    assert validation.usable
    assert validation.delimiter == ";"
    assert validation.accepted_rows == 1


def test_delimiter_difference_is_reported(tmp_path):
    csv = (ORDERS_HEADER + ORDERS_ROW).replace(",", ";")
    validation = validate_file(write(tmp_path, "o.csv", csv), "shopify_orders")
    assert any(i["kind"] == "delimiter_detected" for i in validation.issues)


def test_comma_export_reports_no_delimiter_issue(tmp_path):
    validation = validate_file(write(tmp_path, "o.csv", ORDERS_HEADER + ORDERS_ROW))
    assert validation.delimiter == ","
    assert not any(i["kind"] == "delimiter_detected" for i in validation.issues)


# --- donnees sensibles ------------------------------------------------
def test_detects_valid_card_number():
    findings = scan_rows([{"Note": "4242424242424242"}])
    assert any(f.kind == "card_number" for f in findings)


def test_ignores_digits_that_fail_luhn():
    """Un identifiant long ne doit pas etre pris pour une carte."""
    findings = scan_rows([{"Note": "1234567890123456"}])
    assert not any(f.kind == "card_number" for f in findings)


def test_detects_api_key_and_iban():
    findings = scan_rows([{"Note": "sk_live_abcd1234efgh"}, {"Ref": "FR7630006000011234567890189"}])
    kinds = {f.kind for f in findings}
    assert {"api_key", "iban"} <= kinds


def test_detects_suspicious_column_name():
    findings = scan_rows([{"CVV": "123"}])
    assert any(f.kind == "suspicious_column" and f.column == "CVV" for f in findings)


def test_clean_file_triggers_nothing():
    assert scan_rows([{"Email": "a@x.com", "Total": "120.00", "SKU": "BRW-001"}]) == []


def test_finding_never_exposes_the_value():
    """Le point critique: signaler sans recopier le secret."""
    secret = "4242424242424242"
    findings = scan_rows([{"Card": secret}])
    assert findings
    for finding in findings:
        assert secret not in json.dumps(finding.to_dict())
        assert secret not in finding.message()


def test_validation_surfaces_sensitive_data_without_the_value(tmp_path):
    csv = ORDERS_HEADER.replace("Email", "Email,Card Number") \
        .replace("Name,", "Name,")  # ajoute une colonne
    csv = ("Name,Card Number,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,"
           "Shipping,Taxes,Total,Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,"
           "Lineitem sku\n"
           "#1,4242424242424242,a@x.com,paid,2026-08-03 10:00:00 +0000,EUR,100,0,0,20,120,,"
           "1,Produit A,100.00,A\n")
    validation = validate_file(write(tmp_path, "o.csv", csv), "shopify_orders")
    payload = json.dumps(validation.to_dict())
    assert validation.sensitive_findings
    assert "4242424242424242" not in payload
    assert any(i["kind"] == "sensitive_data" for i in validation.issues)


def test_sensitive_value_never_reaches_the_logs(tmp_path, caplog):
    csv = ("Name,Card Number,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,"
           "Shipping,Taxes,Total,Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,"
           "Lineitem sku\n"
           "#1,4242424242424242,a@x.com,paid,2026-08-03 10:00:00 +0000,EUR,100,0,0,20,120,,"
           "1,Produit A,100.00,A\n")
    with caplog.at_level(logging.DEBUG, logger="mervio"):
        validate_file(write(tmp_path, "o.csv", csv), "shopify_orders")
    assert "4242424242424242" not in caplog.text


# --- sections du rapport ----------------------------------------------
def test_report_has_revenue_analysis_and_traceability(sample_report):
    from mervio.reporting.executive import render_executive_report
    text = render_executive_report(sample_report)
    assert "## REVENUE ANALYSIS" in text
    assert "## TRACABILITE DES CHIFFRES" in text
    assert "formule :" in text and "sources :" in text


def test_revenue_analysis_states_unavailable_comparisons(sample_report):
    from mervio.reporting.executive import render_executive_report
    text = render_executive_report(sample_report)
    assert "Annee vs annee precedente" in text
    assert "historique" in text
