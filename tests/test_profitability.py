"""Tests de profitabilite: Revenue != Profit, et jamais de cout invente."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import ad, make_dataset, make_order
from mervio.analytics.profitability import compute_profitability
from mervio.domain.models import Payment, Product, Refund


def full_cost_dataset():
    """Tous les couts presents SAUF shipping_cost, qu'on injecte a la main."""
    products = {"A": Product("A", "A", "A", unit_cogs=40.0)}
    orders = [make_order("#1", date(2026, 9, 8), "a@x.com", [("A", 1, 100.0)])]
    payments = [Payment("p1", datetime(2026, 9, 8, 12), 120.0, 2.0, 118.0, "paid", "#1")]
    return make_dataset(orders=orders, products=products, payments=payments,
                        ads=[ad(date(2026, 9, 8), 30.0, 50)])


def test_missing_cogs_blocks_complete_profit(simple_dataset):
    result = compute_profitability(simple_dataset, "2026-W37")
    assert result.data_available is False
    assert result.contribution_profit is None
    assert "cogs" in result.missing_components


def test_shipping_cost_always_missing_from_shopify_export():
    result = compute_profitability(full_cost_dataset(), "2026-W37")
    assert "shipping_cost" in result.missing_components
    assert result.data_available is False


def test_partial_profit_is_computed_and_labelled():
    result = compute_profitability(full_cost_dataset(), "2026-W37")
    # 100 CA - 40 cogs - 2 frais - 30 pub - 0 refunds = 28
    assert result.partial_contribution_profit == pytest.approx(28.0)
    assert result.partial_contribution_margin == pytest.approx(0.28)
    assert set(result.included_components) == {"cogs", "payment_fees", "advertising", "refunds"}


def test_limitations_explain_each_missing_component():
    result = compute_profitability(full_cost_dataset(), "2026-W37")
    assert result.limitations
    assert any("transport" in lim.lower() for lim in result.limitations)


def test_no_cost_is_ever_substituted_by_zero(simple_dataset):
    """Un cout absent doit rester None, jamais 0.0, sinon le profit est faux."""
    result = compute_profitability(simple_dataset, "2026-W37")
    assert result.components["cogs"].value is None
    assert result.components["shipping_cost"].value is None


def test_refunds_reduce_profit():
    ds = full_cost_dataset()
    ds.refunds.append(Refund("r1", datetime(2026, 9, 8, 13), 10.0, "#1", "shopify"))
    result = compute_profitability(ds, "2026-W37")
    assert result.partial_contribution_profit == pytest.approx(18.0)


def test_zero_revenue_yields_no_margin():
    result = compute_profitability(make_dataset(), "2026-W37")
    assert result.partial_contribution_margin is None
    assert result.data_available is False
