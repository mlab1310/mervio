"""Devise absente (Mission 003).

Decouvert en validation: sans colonne Currency renseignee, le connecteur
Shopify supposait "EUR", l'enregistrait comme devise OBSERVEE et la qualite
de donnee annoncait "devise unique: EUR, fiable". Une devise absente doit
rester inconnue.
"""
from __future__ import annotations

from datetime import date

from mervio.analytics.pipeline import UNKNOWN_CURRENCY, SourcePaths, run_analysis
from mervio.domain.quality import DataQualityReport
from mervio.ingestion import ingest_shopify_orders
from mervio.llm import build_llm_context

HEADER = ("Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,Shipping,Taxes,Total,"
          "Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku\n")
STRIPE = "id,Created (UTC),Amount,Amount Refunded,Currency,Fee,Net,Status,Customer Email,order_id\n"


def _orders(tmp_path, currency_for):
    body = "".join(
        f"#{1000 + i},c{i}@example.com,paid,2026-{7 + i // 31:02d}-{1 + i % 28:02d} 10:00:00 +0000,"
        f"{currency_for(i)},100.00,0.00,0.00,20.00,120.00,,1,Produit,100.00,A\n"
        for i in range(70))
    path = tmp_path / "orders.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def _field(report, name):
    return next((f for f in report["data_quality"]["fields"] if f["field"] == name), None)


def test_missing_currency_is_never_assumed(tmp_path):
    path = _orders(tmp_path, lambda i: "")
    orders, _, currency = ingest_shopify_orders(path, DataQualityReport())
    assert currency == "" and all(order.currency == "" for order in orders)

    report = run_analysis(SourcePaths(shopify_orders=str(path)), today=date(2026, 9, 14))
    assert report["_meta"]["currency"] == UNKNOWN_CURRENCY == "unknown"
    assert report["data_quality"]["observed_currencies"] == {}
    assert _field(report, "currency")["status"] == "unavailable"
    kinds = {i["kind"] for i in report["data_quality"]["issues"]}
    assert {"missing_currency", "currency_absent"} <= kinds
    assert "EUR" not in report["executive_summary"]


def test_unknown_currency_reaches_the_llm_context_as_unknown(tmp_path):
    report = run_analysis(SourcePaths(shopify_orders=str(_orders(tmp_path, lambda i: ""))), today=date(2026, 9, 14))
    assert build_llm_context(report)["currency"] == "unknown"


def test_orders_without_currency_do_not_inherit_the_previous_one(tmp_path):
    path = _orders(tmp_path, lambda i: "EUR" if i % 2 == 0 else "")
    orders, _, currency = ingest_shopify_orders(path, DataQualityReport())
    assert currency == "EUR"
    assert {order.currency for order in orders} == {"EUR", ""}
    report = run_analysis(SourcePaths(shopify_orders=str(path)), today=date(2026, 9, 14))
    assert report["_meta"]["currency"] == "EUR"
    assert _field(report, "currency")["status"] == "incomplete"


def test_stripe_currency_does_not_stand_in_for_missing_order_currency(tmp_path):
    stripe = tmp_path / "stripe.csv"
    stripe.write_text(STRIPE + "ch_1,2026-09-08 10:00:00,120.00,0,eur,2.00,118.00,Paid,c1@example.com,#1001\n",
                      encoding="utf-8")
    report = run_analysis(SourcePaths(shopify_orders=str(_orders(tmp_path, lambda i: "")), stripe=str(stripe)),
                          today=date(2026, 9, 14))
    assert report["_meta"]["currency"] == "unknown"
    assert _field(report, "currency")["status"] == "unavailable"


def test_declared_currency_behaviour_is_unchanged(tmp_path):
    report = run_analysis(SourcePaths(shopify_orders=str(_orders(tmp_path, lambda i: "EUR"))), today=date(2026, 9, 14))
    assert report["_meta"]["currency"] == "EUR"
    assert _field(report, "currency") == {"field": "currency", "status": "reliable", "coverage": 1.0,
                                          "note": "devise unique: EUR"}
    assert not {"missing_currency", "currency_absent"} & {i["kind"] for i in report["data_quality"]["issues"]}
