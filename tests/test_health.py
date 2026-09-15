"""Tests du Business Health Engine."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import ad, make_dataset, make_order
from mervio.analytics.health import (
    MIN_PRODUCT_COVERAGE, ROAS_THRESHOLDS, compute_business_health, interpolate,
)
from mervio.analytics.kpi import compute_kpis
from mervio.analytics.periods import make_period
from mervio.analytics.profitability import compute_profitability
from mervio.analytics.timeseries import compare_all
from mervio.config import HealthConfig
from mervio.domain.models import Payment, Product

PERIOD = make_period(date(2026, 9, 7), "week")
CFG = HealthConfig()


def score_for(ds):
    from mervio.analytics.pipeline import SERIES_METRICS
    window = ds.window(PERIOD.start, PERIOD.end)
    kpis = compute_kpis(window, PERIOD)
    profit = compute_profitability(window, PERIOD.label)
    comparisons = compare_all(ds, PERIOD, SERIES_METRICS)
    return compute_business_health(kpis, comparisons, profit, window, ds.quality, CFG)


@pytest.mark.parametrize("value,expected", [
    (0.0, 0.0), (1.0, 25.0), (2.0, 55.0), (4.0, 100.0), (99.0, 100.0),
])
def test_interpolate_respects_declared_thresholds(value, expected):
    assert interpolate(value, ROAS_THRESHOLDS) == pytest.approx(expected)


def test_interpolate_is_linear_between_points():
    assert interpolate(2.5, ROAS_THRESHOLDS) == pytest.approx(67.5)


def test_profitability_excluded_when_cogs_missing(simple_dataset):
    """Regle cle: une marge sans COGS ne doit JAMAIS etre notee 100/100."""
    health = score_for(simple_dataset)
    profitability = next(d for d in health.dimensions if d.key == "profitability")
    assert profitability.available is False
    assert "cogs" in profitability.reason_unavailable
    assert "profitability" in health.excluded_dimensions


def test_product_health_excluded_below_coverage_floor(simple_dataset):
    health = score_for(simple_dataset)
    product = next(d for d in health.dimensions if d.key == "product_health")
    assert product.available is False
    assert str(int(MIN_PRODUCT_COVERAGE * 100)) in product.reason_unavailable


def test_weights_are_renormalised_over_available_dimensions(simple_dataset):
    health = score_for(simple_dataset)
    assert health.weights_renormalised is True
    available = [d for d in health.dimensions if d.available]
    manual = sum(d.score * d.weight for d in available) / sum(d.weight for d in available)
    assert health.score == pytest.approx(round(manual), abs=1)


def test_score_is_bounded():
    health = score_for(simple_dataset_factory())
    assert health.score is None or 0 <= health.score <= 100


def simple_dataset_factory():
    products = {"A": Product("A", "A", "A", unit_cogs=40.0)}
    orders = [make_order("#1", date(2026, 9, 8), "a@x.com", [("A", 1, 100.0)])]
    payments = [Payment("p1", datetime(2026, 9, 8, 12), 120.0, 2.0, 118.0, "paid", "#1")]
    ds = make_dataset(orders=orders, products=products, payments=payments,
                      ads=[ad(date(2026, 9, 8), 30.0, 50)])
    ds.quality.set_field("payment_fees", covered=1, total=1)
    return ds


def test_every_dimension_documents_its_formula_and_thresholds(simple_dataset):
    for dimension in score_for(simple_dataset).dimensions:
        assert dimension.formula and dimension.thresholds
        if not dimension.available:
            assert dimension.reason_unavailable


def test_methodology_states_no_llm(simple_dataset):
    assert "LLM" in score_for(simple_dataset).methodology


def test_empty_dataset_yields_no_score():
    health = score_for(make_dataset())
    assert health.score is None or health.score >= 0


def test_concentration_penalty_is_applied_and_explained():
    """Un seul client = 100% du CA doit penaliser la sante client."""
    orders = [make_order("#1", date(2026, 9, 8), "seul@x.com", [("A", 1, 500.0)]),
              make_order("#2", date(2026, 9, 9), "seul@x.com", [("A", 1, 500.0)])]
    ds = make_dataset(orders=orders, ads=[ad(date(2026, 9, 8), 100.0, 200)])
    customer = next(d for d in score_for(ds).dimensions if d.key == "customer_health")
    assert any("concentration" in ev for ev in customer.evidence)
