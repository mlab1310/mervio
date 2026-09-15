"""Tests du KPI engine: exactitude des calculs et honnetete sur les manques."""
from __future__ import annotations

from datetime import date

import pytest

from conftest import ad, make_dataset, make_order
from mervio.analytics.kpi import campaign_performance, compute_kpis, product_performance
from mervio.analytics.periods import make_period
from mervio.domain.models import Product
from mervio.domain.quality import QualityStatus

PERIOD = make_period(date(2026, 9, 7), "week")


def test_revenue_is_net_of_discount(simple_dataset):
    assert compute_kpis(simple_dataset, PERIOD)["revenue"].value == pytest.approx(200.0)


def test_revenue_subtracts_discount():
    ds = make_dataset(orders=[make_order("#1", date(2026, 9, 8), "a@x.com", [("A", 1, 100.0)], discount=15.0)])
    assert compute_kpis(ds, PERIOD)["revenue"].value == pytest.approx(85.0)


def test_orders_units_and_aov(simple_dataset):
    kpis = compute_kpis(simple_dataset, PERIOD)
    assert kpis["orders"].value == 2
    assert kpis["units"].value == 3
    assert kpis["aov"].value == pytest.approx(100.0)


def test_aov_unavailable_when_no_orders():
    kpis = compute_kpis(make_dataset(), PERIOD)
    assert kpis["aov"].value is None
    assert kpis["aov"].data_quality == QualityStatus.UNAVAILABLE.value


def test_roas(simple_dataset):
    assert compute_kpis(simple_dataset, PERIOD)["roas"].value == pytest.approx(200.0 / 100.0)


def test_roas_unavailable_when_no_ad_spend():
    ds = make_dataset(orders=[make_order("#1", date(2026, 9, 8), "a@x.com", [("A", 1, 100.0)])])
    kpis = compute_kpis(ds, PERIOD)
    assert kpis["roas"].value is None
    assert kpis["cac"].value is None


def test_cac_uses_new_customers_only():
    """Un client dont la 1re commande est ANTERIEURE a la periode n'est pas nouveau."""
    orders = [
        make_order("#0", date(2026, 8, 1), "ancien@x.com", [("A", 1, 100.0)]),
        make_order("#1", date(2026, 9, 8), "ancien@x.com", [("A", 1, 100.0)]),
        make_order("#2", date(2026, 9, 9), "nouveau@x.com", [("A", 1, 100.0)]),
    ]
    full = make_dataset(orders=orders, ads=[ad(date(2026, 9, 8), 200.0, 100)])
    window = full.window(PERIOD.start, PERIOD.end)
    kpis = compute_kpis(window, PERIOD)
    assert kpis["new_customers"].value == 1
    assert kpis["cac"].value == pytest.approx(200.0)


def test_gross_margin_covers_only_known_cogs(simple_dataset):
    """A: 100 de CA, 40 de cout. B: cout inconnu -> exclu du calcul, pas suppose."""
    kpis = compute_kpis(simple_dataset, PERIOD)
    assert kpis["gross_margin"].value == pytest.approx(0.60)
    assert kpis["gross_margin"].data_quality == QualityStatus.INCOMPLETE.value
    assert "50%" in kpis["gross_margin"].notes[0]


def test_gross_margin_unavailable_without_any_cogs():
    ds = make_dataset(orders=[make_order("#1", date(2026, 9, 8), "a@x.com", [("A", 1, 100.0)])],
                      products={"A": Product("A", "A", "A", unit_cogs=None)})
    kpis = compute_kpis(ds, PERIOD)
    assert kpis["gross_margin"].value is None
    assert kpis["gross_margin"].data_quality == QualityStatus.UNAVAILABLE.value


def test_refund_rate(simple_dataset):
    from mervio.domain.models import Refund
    from datetime import datetime
    simple_dataset.refunds.append(Refund("r1", datetime(2026, 9, 8, 12), 20.0, "#1", "shopify"))
    kpis = compute_kpis(simple_dataset, PERIOD)
    assert kpis["refunds"].value == pytest.approx(20.0)
    assert kpis["refund_rate"].value == pytest.approx(0.10)


def test_repeat_purchase_rate():
    orders = [
        make_order("#1", date(2026, 9, 8), "a@x.com", [("A", 1, 50.0)]),
        make_order("#2", date(2026, 9, 9), "a@x.com", [("A", 1, 50.0)]),
        make_order("#3", date(2026, 9, 9), "b@x.com", [("A", 1, 50.0)]),
    ]
    kpis = compute_kpis(make_dataset(orders=orders), PERIOD)
    assert kpis["repeat_purchase_rate"].value == pytest.approx(0.5)


def test_customer_concentration():
    orders = [make_order("#1", date(2026, 9, 8), "gros@x.com", [("A", 1, 900.0)])] + [
        make_order(f"#{i}", date(2026, 9, 9), f"c{i}@x.com", [("A", 1, 10.0)]) for i in range(2, 12)
    ]
    kpis = compute_kpis(make_dataset(orders=orders), PERIOD)
    assert kpis["customer_concentration_top5"].value == pytest.approx((900 + 40) / 1000)


def test_paid_conversion_rate(simple_dataset):
    assert compute_kpis(simple_dataset, PERIOD)["paid_conversion_rate"].value == pytest.approx(2 / 200)


def test_every_metric_carries_definition_formula_and_period(simple_dataset):
    for metric in compute_kpis(simple_dataset, PERIOD).values():
        assert metric.definition and metric.formula and metric.sources
        assert metric.period == PERIOD.label
        assert metric.data_quality in {s.value for s in QualityStatus}


def test_ad_spend_is_flagged_incomplete_by_design(simple_dataset):
    """Google Ads seul ne couvre pas la depense publicitaire totale."""
    metric = compute_kpis(simple_dataset, PERIOD)["ad_spend"]
    assert metric.data_quality == QualityStatus.INCOMPLETE.value
    assert metric.notes


def test_product_performance(simple_dataset):
    rows = {r["sku"]: r for r in product_performance(simple_dataset)}
    assert rows["A"]["revenue"] == pytest.approx(100.0)
    assert rows["A"]["gross_margin"] == pytest.approx(0.60)
    assert rows["B"]["gross_margin"] is None  # cout inconnu -> pas de marge inventee


def test_campaign_performance(simple_dataset):
    row = campaign_performance(simple_dataset)[0]
    assert row["spend"] == pytest.approx(100.0)
    assert row["clicks"] == 200
    assert row["cpc"] == pytest.approx(0.5)
