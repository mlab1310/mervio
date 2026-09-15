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


# --- D-047: remboursements ramenes a la base du CA (Subtotal, hors port et taxes) -----------

def _billed_order(order_id, subtotal, shipping, tax, cogs_sku="A"):
    """Commande dont le Total porte port et taxes, comme l'export Shopify (Total = Subtotal + port + taxes)."""
    order = make_order(order_id, date(2026, 9, 8), f"{order_id}@x.com", [(cogs_sku, 1, subtotal)], shipping=shipping)
    order.tax, order.total = tax, subtotal + shipping + tax
    return order


def _with_refund(order, amount):
    ds = full_cost_dataset()
    ds.orders = [order]
    ds.refunds.append(Refund("r1", order.created_at, amount, order.order_id, "shopify"))
    return compute_profitability(ds, "2026-W37")


def test_full_refund_deducts_only_the_product_share():
    # Subtotal 100, port 10, taxes 20, Total 130 rembourse en entier: la part produit vaut 100, pas 130
    result = _with_refund(_billed_order("#1", 100.0, 10.0, 20.0), 130.0)
    assert result.components["refunds"].value == pytest.approx(100.0)
    assert result.partial_contribution_profit == pytest.approx(100.0 - 40.0 - 2.0 - 30.0 - 100.0)


def test_shipping_only_refund_on_zero_subtotal_deducts_nothing():
    # forme OH5: 4.94 de port rembourse sur une commande a Subtotal nul
    result = _with_refund(_billed_order("#1", 0.0, 4.94, 0.0), 4.94)
    assert result.components["refunds"].value == pytest.approx(0.0)


def test_partial_refund_without_shipping_or_tax_is_product_only():
    result = _with_refund(_billed_order("#1", 100.0, 0.0, 0.0), 25.0)
    assert result.components["refunds"].value == pytest.approx(25.0)


def test_partial_refund_with_shipping_and_tax_is_not_guessed():
    # 30 rembourses sur Subtotal 100 + port 10 + taxes 20: part produit entre 0 et 30, non determinable
    result = _with_refund(_billed_order("#1", 100.0, 10.0, 20.0), 30.0)
    refunds = result.components["refunds"]
    assert refunds.value is None and "refunds" in result.missing_components
    assert "entre 0.00 et 30.00" in refunds.note
    # le profit partiel n'integre jamais les 30 port et taxes compris, et le dit
    assert result.partial_contribution_profit == pytest.approx(100.0 - 40.0 - 2.0 - 30.0)
    assert any("Remboursements (part produit)" in lim for lim in result.limitations)


def test_refund_on_order_without_total_is_not_guessed():
    order = _billed_order("#1", 100.0, 0.0, 0.0)
    order.total = 0.0                                                         # Total absent a l'ingestion
    result = _with_refund(order, 20.0)
    assert result.components["refunds"].value is None


def test_undetermined_refunds_exclude_the_profitability_dimension():
    from mervio.analytics.health import compute_business_health
    from mervio.analytics.kpi import compute_kpis
    from mervio.analytics.periods import make_period
    from mervio.analytics.pipeline import SERIES_METRICS
    from mervio.analytics.timeseries import compare_all
    from mervio.config import HealthConfig
    ds = full_cost_dataset()
    ds.orders = [_billed_order("#1", 100.0, 10.0, 20.0)]
    ds.refunds.append(Refund("r1", ds.orders[0].created_at, 30.0, "#1", "shopify"))
    period = make_period(date(2026, 9, 7), "week")
    window = ds.window(period.start, period.end)
    health = compute_business_health(compute_kpis(window, period), compare_all(ds, period, SERIES_METRICS),
                                     compute_profitability(window, period.label), window, ds.quality, HealthConfig())
    profitability = next(d for d in health.dimensions if d.key == "profitability")
    assert not profitability.available and "refunds" in profitability.reason_unavailable


def test_no_partial_profit_without_any_cost_component():
    ds = make_dataset(orders=[_billed_order("#1", 100.0, 10.0, 20.0)],
                      refunds=[Refund("r1", datetime(2026, 9, 8, 13), 30.0, "#1", "shopify")])
    result = compute_profitability(ds, "2026-W37")
    assert result.included_components == []
    assert result.partial_contribution_profit is None and result.partial_contribution_margin is None
    assert any(lim.startswith("Profit de contribution complet non calculable") and "Aucun profit partiel" in lim
               for lim in result.limitations)


def test_zero_revenue_yields_no_margin():
    result = compute_profitability(make_dataset(), "2026-W37")
    assert result.partial_contribution_margin is None
    assert result.data_available is False
