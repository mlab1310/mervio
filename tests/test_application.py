"""Tests de la couche applicative: import, validation, service d'analyse."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from mervio.application.imports import UnknownSourceError, detect_source, validate_file
from mervio.application.service import AnalysisRequest, analyze_dataset
from mervio.application.workspace import (
    Workspace, WorkspaceError, assert_not_sample, is_sample_path, uploads_root,
)

ORDERS_HEADER = ("Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,"
                 "Shipping,Taxes,Total,Refunded Amount,Lineitem quantity,Lineitem name,"
                 "Lineitem price,Lineitem sku\n")


def orders_csv(rows=3, start=date(2026, 8, 3)):
    from datetime import timedelta
    body = ""
    for index in range(rows):
        day = start + timedelta(days=7 * index)
        body += (f"#{index},c{index}@x.com,paid,{day} 10:00:00 +0000,EUR,100,0,0,20,120,,"
                 f"1,Produit A,100.00,A\n")
    return ORDERS_HEADER + body


def write(tmp_path, name, content, encoding="utf-8"):
    path = tmp_path / name
    path.write_text(content, encoding=encoding)
    return str(path)


# --- detection de source ---------------------------------------------
def test_detects_each_supported_source(sample_paths):
    assert detect_source(sample_paths.shopify_orders) == "shopify_orders"
    assert detect_source(sample_paths.shopify_products) == "shopify_products"
    assert detect_source(sample_paths.stripe) == "stripe"
    assert detect_source(sample_paths.google_ads) == "google_ads"


def test_unknown_headers_raise_clear_error(tmp_path):
    path = write(tmp_path, "x.csv", "colonne_a,colonne_b\n1,2\n")
    with pytest.raises(UnknownSourceError) as exc:
        detect_source(path)
    assert "signature" in str(exc.value)


# --- validation ------------------------------------------------------
def test_valid_file_reports_expected_shape(tmp_path):
    validation = validate_file(write(tmp_path, "o.csv", orders_csv()))
    payload = validation.to_dict()
    assert set(payload) >= {"source", "status", "rows", "accepted_rows", "rejected_rows", "issues"}
    assert payload["source"] == "shopify_orders"
    assert payload["status"] == "valid"
    assert payload["rows"] == 3 and payload["accepted_rows"] == 3 and payload["rejected_rows"] == 0
    assert payload["currency"] == "EUR"
    assert payload["period"]["start"] == "2026-08-03"


def test_missing_file_is_invalid_not_crash():
    validation = validate_file("/does/not/exist.csv")
    assert validation.status == "invalid"
    assert validation.usable is False
    assert validation.error


def test_missing_columns_makes_file_invalid(tmp_path):
    path = write(tmp_path, "bad.csv", "Name,Email\n#1,a@x.com\n")
    validation = validate_file(path, "shopify_orders")
    assert validation.status == "invalid"
    assert "manquantes" in validation.error


def test_empty_csv_is_invalid(tmp_path):
    validation = validate_file(write(tmp_path, "empty.csv", ""), "shopify_orders")
    assert validation.status == "invalid"


def test_header_only_csv_is_invalid(tmp_path):
    validation = validate_file(write(tmp_path, "h.csv", ORDERS_HEADER), "shopify_orders")
    assert validation.status == "invalid"


def test_invalid_rows_are_counted_as_rejected(tmp_path):
    csv = orders_csv(2) + "#9,z@x.com,paid,pas-une-date,EUR,50,0,0,10,60,,1,Produit A,50.00,A\n"
    validation = validate_file(write(tmp_path, "o.csv", csv))
    assert validation.status == "valid_with_issues"
    assert validation.rejected_rows == 1
    assert any(i["kind"] == "invalid_date" for i in validation.issues)


def test_duplicate_rows_are_reported(tmp_path):
    row = "#1,a@x.com,paid,2026-08-03 10:00:00 +0000,EUR,100,0,0,20,120,,1,Produit A,100.00,A\n"
    validation = validate_file(write(tmp_path, "o.csv", ORDERS_HEADER + row + row))
    assert any(i["kind"] == "duplicate_line_item" for i in validation.issues)
    assert validation.rejected_rows == 1


def test_cp1252_encoding_is_read_with_a_warning(tmp_path):
    """Un export passe par Excel sous Windows ne doit pas faire echouer l'import."""
    csv = orders_csv(2).replace("Produit A", "Caf\u00e9 cr\u00e8me")
    path = write(tmp_path, "o.csv", csv, encoding="cp1252")
    validation = validate_file(path)
    assert validation.usable
    assert any(i["kind"] == "encoding_fallback" for i in validation.issues)


def test_undecodable_file_is_invalid(tmp_path):
    path = tmp_path / "bin.csv"
    path.write_bytes(b"\xff\xfe\x00\x00" * 100)
    validation = validate_file(str(path), "shopify_orders")
    assert validation.status == "invalid"


# --- securite de l'espace de travail ---------------------------------
def test_sample_directory_is_detected():
    assert is_sample_path("/x/data/sample/shopify_orders.csv") is True
    assert is_sample_path("/x/data/uploads/ws1/orders.csv") is False


def test_writing_client_data_into_sample_is_forbidden():
    with pytest.raises(WorkspaceError):
        assert_not_sample("data/sample/client")


def test_workspace_is_isolated_and_gitignored(tmp_path):
    workspace = Workspace.create(base=tmp_path)
    assert "uploads" in workspace.root.parts
    assert (workspace.root / ".gitignore").read_text().strip() == "*"


def test_workspace_copies_and_cleans_up(tmp_path):
    source = tmp_path / "orders.csv"
    source.write_text(orders_csv(), encoding="utf-8")
    workspace = Workspace.create(base=tmp_path)
    copied = workspace.add_file(source)
    assert copied.exists() and copied.parent == workspace.root
    workspace.cleanup()
    assert not workspace.root.exists()
    assert source.exists()  # l'original n'est jamais touche


def test_workspace_rejects_missing_file(tmp_path):
    with pytest.raises(WorkspaceError):
        Workspace.create(base=tmp_path).add_file(tmp_path / "nope.csv")


# --- service d'analyse -----------------------------------------------
def test_service_completes_on_valid_input(tmp_path):
    result = analyze_dataset(AnalysisRequest(
        shopify_orders=write(tmp_path, "o.csv", orders_csv(8)), today=date(2026, 10, 5)))
    assert result.succeeded
    assert result.analysis_id
    assert result.report["_meta"]["llm_used"] is False


def test_service_rejects_without_mandatory_source(tmp_path, sample_paths):
    """Sans commandes, il n'y a pas de CA: l'analyse doit etre refusee."""
    result = analyze_dataset(AnalysisRequest(google_ads=sample_paths.google_ads))
    assert result.status == "rejected"
    assert "shopify_orders" in result.error


def test_service_rejects_empty_request():
    result = analyze_dataset(AnalysisRequest())
    assert result.status == "rejected" and "aucun fichier" in result.error


def test_service_never_raises_on_broken_file(tmp_path):
    result = analyze_dataset(AnalysisRequest(shopify_orders=write(tmp_path, "b.csv", "a,b\n1,2\n")))
    assert result.status == "rejected"


def test_summary_is_api_ready(tmp_path):
    result = analyze_dataset(AnalysisRequest(
        shopify_orders=write(tmp_path, "o.csv", orders_csv(8)), today=date(2026, 10, 5)))
    summary = result.summary()
    assert set(summary) >= {"analysis_id", "status", "business_health_score", "period", "sources"}
    json.dumps(summary)  # doit etre serialisable tel quel


def test_synthetic_flag_is_written_into_the_report(sample_paths):
    result = analyze_dataset(AnalysisRequest(
        shopify_orders=sample_paths.shopify_orders, synthetic=True, today=date(2026, 9, 14)))
    assert result.report["_meta"]["dataset_is_synthetic"] is True


def test_currency_mismatch_is_flagged_as_error(tmp_path, sample_paths):
    """EUR cote Shopify et USD cote Stripe: additionner serait faux."""
    stripe = ("id,Created (UTC),Amount,Amount Refunded,Currency,Fee,Net,Status,Customer Email,order_id\n"
              "ch_1,2026-08-03 10:00:00,120.00,0.00,usd,2.00,118.00,Paid,a@x.com,#0\n")
    result = analyze_dataset(AnalysisRequest(
        shopify_orders=write(tmp_path, "o.csv", orders_csv(8)),
        stripe=write(tmp_path, "s.csv", stripe), today=date(2026, 10, 5)))
    kinds = {i["kind"] for i in result.report["data_quality"]["issues"]}
    assert "currency_mismatch" in kinds
