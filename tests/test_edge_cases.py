"""Cas limites: le moteur doit degrader proprement, jamais mentir ni crasher."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import ad, make_dataset, make_order
from mervio.analytics.kpi import compute_kpis
from mervio.analytics.periods import make_period
from mervio.analytics.pipeline import SourcePaths, load_dataset, run_analysis
from mervio.analytics.profitability import compute_profitability
from mervio.errors import InsufficientDataError, InvalidDataError
from mervio.domain.models import Product, Refund

PERIOD = make_period(date(2026, 9, 7), "week")


def test_zero_orders_does_not_crash():
    """Depense connue + zero commande: ROAS = 0 est un FAIT, pas une absence."""
    kpis = compute_kpis(make_dataset(ads=[ad(date(2026, 9, 8), 100.0, 50)]), PERIOD)
    assert kpis["revenue"].value == 0.0
    assert kpis["aov"].value is None          # pas de commande -> pas de panier moyen
    assert kpis["roas"].value == 0.0          # 100 EUR depenses, 0 EUR de CA
    assert kpis["paid_conversion_rate"].value == 0.0


def test_no_ad_data_gives_unknown_roas_not_zero():
    """Difference essentielle: 'aucune donnee' n'est pas 'resultat nul'."""
    ds = make_dataset(orders=[make_order("#1", date(2026, 9, 8), "a@x.com", [("A", 1, 100.0)])])
    kpis = compute_kpis(ds, PERIOD)
    assert kpis["roas"].value is None
    assert kpis["roas"].data_quality == "unavailable"


def test_zero_ad_spend_gives_unknown_not_zero_cac():
    ds = make_dataset(orders=[make_order("#1", date(2026, 9, 8), "a@x.com", [("A", 1, 50.0)])])
    kpis = compute_kpis(ds, PERIOD)
    assert kpis["cac"].value is None
    assert any("inconnu" in note for note in kpis["cac"].notes)


def test_missing_cogs_never_treated_as_zero_cost():
    ds = make_dataset(orders=[make_order("#1", date(2026, 9, 8), "a@x.com", [("A", 1, 100.0)])],
                      products={"A": Product("A", "A", "A", unit_cogs=None)})
    profit = compute_profitability(ds, PERIOD.label)
    assert profit.components["cogs"].value is None
    assert profit.data_available is False


def test_negative_refund_is_rejected_at_ingestion(tmp_path):
    from mervio.ingestion.shopify import ingest_shopify_orders
    from mervio.domain.quality import DataQualityReport
    csv = ("Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,Shipping,"
           "Taxes,Total,Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku\n"
           "#1,a@x.com,paid,2026-09-08 10:00:00 +0000,EUR,100,0,0,20,120,-50,1,A,100.00,A\n")
    path = tmp_path / "o.csv"
    path.write_text(csv, encoding="utf-8")
    quality = DataQualityReport()
    orders, refunds, _ = ingest_shopify_orders(path, quality)
    assert refunds == []
    assert any(i.kind == "negative_refund" for i in quality.issues)


def test_refund_larger_than_revenue_is_reported_not_clamped():
    ds = make_dataset(orders=[make_order("#1", date(2026, 9, 8), "a@x.com", [("A", 1, 100.0)])],
                      refunds=[Refund("r1", datetime(2026, 9, 8, 13), 150.0, "#1", "shopify")])
    kpis = compute_kpis(ds, PERIOD)
    assert kpis["refund_rate"].value == pytest.approx(1.5)  # signale tel quel


def test_missing_file_raises_clear_error():
    with pytest.raises(InvalidDataError) as exc:
        load_dataset(SourcePaths(shopify_orders="/does/not/exist.csv"))
    assert "introuvable" in str(exc.value)


def test_no_source_at_all_raises_insufficient_data(tmp_path):
    empty = tmp_path / "ads.csv"
    empty.write_text("Day,Campaign,Cost\n", encoding="utf-8")
    with pytest.raises((InsufficientDataError, InvalidDataError)):
        run_analysis(SourcePaths(google_ads=str(empty)), today=date(2026, 9, 14))


def test_missing_optional_source_is_flagged_not_fatal(tmp_path):
    """Sans Stripe, l'analyse continue mais les frais sont declares inconnus."""
    orders_csv = ("Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,Shipping,"
                  "Taxes,Total,Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku\n")
    for week in range(6):
        day = date(2026, 8, 3) + __import__("datetime").timedelta(days=7 * week)
        orders_csv += (f"#{week},c{week}@x.com,paid,{day} 10:00:00 +0000,EUR,100,0,0,20,120,,"
                       f"1,Produit A,100.00,A\n")
    path = tmp_path / "o.csv"
    path.write_text(orders_csv, encoding="utf-8")
    report = run_analysis(SourcePaths(shopify_orders=str(path)), today=date(2026, 9, 14))
    assert report["kpis"]["payment_fees"]["value"] is None
    assert any("Stripe" in lim or "stripe" in lim for lim in report["limitations"])


def test_duplicate_orders_are_not_double_counted(tmp_path):
    from mervio.ingestion.shopify import ingest_shopify_orders
    from mervio.domain.quality import DataQualityReport
    row = "#1,a@x.com,paid,2026-09-08 10:00:00 +0000,EUR,100,0,0,20,120,,1,Produit A,100.00,A\n"
    csv = ("Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,Shipping,"
           "Taxes,Total,Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku\n"
           + row + row)
    path = tmp_path / "o.csv"
    path.write_text(csv, encoding="utf-8")
    orders, _, _ = ingest_shopify_orders(path, DataQualityReport())
    assert len(orders) == 1 and len(orders[0].items) == 1


def test_period_with_no_data_reports_zero_not_error():
    ds = make_dataset(orders=[make_order("#1", date(2026, 1, 5), "a@x.com", [("A", 1, 100.0)])])
    window = ds.window(PERIOD.start, PERIOD.end)
    assert compute_kpis(window, PERIOD)["revenue"].value == 0.0
