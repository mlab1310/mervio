"""Tests du Root Cause Engine."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from conftest import ad, make_dataset, make_order
from mervio.analytics.periods import make_period
from mervio.analytics.root_cause import analyse_revenue_change

CURRENT = make_period(date(2026, 9, 7), "week")
PREVIOUS = make_period(date(2026, 8, 31), "week")


def build(n_prev, n_cur, price_prev=100.0, price_cur=100.0, clicks_prev=1000, clicks_cur=1000):
    orders = []
    for i in range(n_prev):
        orders.append(make_order(f"p{i}", date(2026, 9, 1), f"p{i}@x.com", [("A", 1, price_prev)]))
    for i in range(n_cur):
        orders.append(make_order(f"c{i}", date(2026, 9, 8), f"c{i}@x.com", [("A", 1, price_cur)]))
    ads = [ad(date(2026, 9, 1), 500.0, clicks_prev), ad(date(2026, 9, 8), 500.0, clicks_cur)]
    return make_dataset(orders=orders, ads=ads)


def test_conversion_driven_decline_identifies_conversion():
    """Trafic stable, commandes en chute -> la cause doit etre la conversion."""
    analysis = analyse_revenue_change(build(100, 60), CURRENT, PREVIOUS)
    primary = analysis.primary_factor()
    assert primary.factor == "conversion_rate"
    assert analysis.observed_change_pct == pytest.approx(-0.40)


def test_traffic_driven_decline_identifies_traffic():
    """Taux de conversion stable, trafic divise par deux -> cause = trafic."""
    analysis = analyse_revenue_change(build(100, 50, clicks_prev=1000, clicks_cur=500), CURRENT, PREVIOUS)
    assert analysis.primary_factor().factor == "paid_traffic"


def test_aov_driven_decline_identifies_aov():
    analysis = analyse_revenue_change(build(100, 100, price_prev=100.0, price_cur=60.0), CURRENT, PREVIOUS)
    assert analysis.primary_factor().factor == "average_order_value"


def test_contributions_sum_to_one():
    """La decomposition log doit etre exhaustive: somme des contributions = 1."""
    analysis = analyse_revenue_change(build(100, 55, price_cur=90.0, clicks_cur=900), CURRENT, PREVIOUS)
    total = sum(f.contribution for f in analysis.factors if f.contribution is not None)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_zero_revenue_makes_analysis_unavailable():
    analysis = analyse_revenue_change(build(100, 0), CURRENT, PREVIOUS)
    assert analysis.available is False
    assert analysis.limitations


def test_hypothesis_is_never_stated_as_proven_cause():
    analysis = analyse_revenue_change(build(100, 60), CURRENT, PREVIOUS)
    for factor in analysis.factors:
        assert "associee" in factor.hypothesis or "insuffisante" in factor.hypothesis
    assert any("causalite" in lim for lim in analysis.limitations)


def test_confidence_is_bounded_and_never_certain():
    analysis = analyse_revenue_change(build(100, 60), CURRENT, PREVIOUS)
    for factor in analysis.factors:
        assert 0.1 <= factor.confidence <= 0.9


def test_each_factor_carries_its_supporting_metric():
    analysis = analyse_revenue_change(build(100, 60), CURRENT, PREVIOUS)
    assert {f.supporting_metric for f in analysis.factors} == {"clicks", "paid_conversion_rate", "aov"}


def test_product_contributors_are_ranked_by_impact():
    orders = [make_order("p1", date(2026, 9, 1), "a@x.com", [("A", 1, 500.0), ("B", 1, 100.0)]),
              make_order("c1", date(2026, 9, 8), "b@x.com", [("B", 1, 100.0)])]
    ds = make_dataset(orders=orders, ads=[ad(date(2026, 9, 1), 10.0, 50), ad(date(2026, 9, 8), 10.0, 50)])
    analysis = analyse_revenue_change(ds, CURRENT, PREVIOUS)
    assert analysis.product_contributors[0]["sku"] == "A"


def test_method_is_documented_in_output():
    analysis = analyse_revenue_change(build(100, 60), CURRENT, PREVIOUS)
    assert "multiplicative" in analysis.method
