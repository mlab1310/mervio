"""Fixtures partagees: construction de datasets minimaux en memoire."""
from __future__ import annotations

import sys
from datetime import datetime, date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mervio.analytics.periods import make_period  # noqa: E402
from mervio.domain.models import (  # noqa: E402
    Campaign, DailyAdPerformance, Dataset, Order, OrderItem, Payment, Product, Refund,
)
from mervio.domain.quality import DataQualityReport  # noqa: E402

SAMPLE_DIR = ROOT / "data" / "sample"


def make_order(order_id, day, customer, lines, discount=0.0, shipping=0.0):
    """lines = [(sku, qty, price)]"""
    items = [OrderItem(order_id, sku, sku, qty, price) for sku, qty, price in lines]
    subtotal = sum(i.line_revenue for i in items)
    return Order(
        order_id=order_id, customer_id=customer,
        created_at=datetime.combine(day, datetime.min.time()).replace(hour=12),
        currency="EUR", subtotal=subtotal, discount=discount, shipping=shipping,
        tax=round((subtotal - discount) * 0.2, 2),
        total=subtotal - discount + shipping, financial_status="paid",
        items=items, customer_email=customer,
    )


def make_dataset(orders=None, products=None, payments=None, refunds=None, ads=None):
    ds = Dataset(
        orders=orders or [], products=products or {}, payments=payments or [],
        refunds=refunds or [], ad_performance=ads or [],
        campaigns={"c1": Campaign("c1", "Campagne 1")},
        quality=DataQualityReport(), currency="EUR",
    )
    ds.compute_customer_index()
    return ds


def ad(day, spend, clicks, conversions=0.0, value=0.0, campaign="c1"):
    return DailyAdPerformance(day=day, campaign_id=campaign, spend=spend,
                              impressions=clicks * 30, clicks=clicks,
                              conversions=conversions, conversion_value=value)


@pytest.fixture
def week_period():
    return make_period(date(2026, 9, 7), "week")


@pytest.fixture
def simple_dataset():
    """2 commandes, 1 produit avec cout connu, 1 sans, frais Stripe, pub."""
    products = {
        "A": Product("A", "A", "Produit A", unit_cogs=40.0),
        "B": Product("B", "B", "Produit B", unit_cogs=None),
    }
    orders = [
        make_order("#1", date(2026, 9, 8), "alice@example.com", [("A", 1, 100.0)]),
        make_order("#2", date(2026, 9, 9), "bob@example.com", [("B", 2, 50.0)]),
    ]
    payments = [
        Payment("p1", datetime(2026, 9, 8, 12), 120.0, 2.0, 118.0, "paid", "#1"),
        Payment("p2", datetime(2026, 9, 9, 12), 120.0, 2.5, 117.5, "paid", "#2"),
    ]
    ads = [ad(date(2026, 9, 8), 50.0, 100), ad(date(2026, 9, 9), 50.0, 100)]
    ds = make_dataset(orders=orders, products=products, payments=payments, ads=ads)
    ds.quality.set_field("payment_fees", covered=2, total=2)
    ds.quality.set_field("customer_identity", covered=2, total=2)
    return ds


@pytest.fixture
def sample_paths():
    from mervio.analytics.pipeline import SourcePaths
    if not (SAMPLE_DIR / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes: lancer scripts/generate_sample_data.py")
    return SourcePaths(
        shopify_orders=str(SAMPLE_DIR / "shopify_orders.csv"),
        shopify_products=str(SAMPLE_DIR / "shopify_products.csv"),
        stripe=str(SAMPLE_DIR / "stripe_transactions.csv"),
        google_ads=str(SAMPLE_DIR / "google_ads.csv"),
    )


@pytest.fixture(scope="session")
def sample_report():
    from mervio.analytics.pipeline import SourcePaths, run_analysis
    if not (SAMPLE_DIR / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    return run_analysis(
        SourcePaths(
            shopify_orders=str(SAMPLE_DIR / "shopify_orders.csv"),
            shopify_products=str(SAMPLE_DIR / "shopify_products.csv"),
            stripe=str(SAMPLE_DIR / "stripe_transactions.csv"),
            google_ads=str(SAMPLE_DIR / "google_ads.csv"),
        ),
        today=date(2026, 9, 14),
    )


@pytest.fixture(scope="module")
def sample_paths_module():
    """Meme chose que sample_paths, portee module (fixtures de reporting)."""
    from mervio.analytics.pipeline import SourcePaths
    if not (SAMPLE_DIR / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    return SourcePaths(
        shopify_orders=str(SAMPLE_DIR / "shopify_orders.csv"),
        shopify_products=str(SAMPLE_DIR / "shopify_products.csv"),
        stripe=str(SAMPLE_DIR / "stripe_transactions.csv"),
        google_ads=str(SAMPLE_DIR / "google_ads.csv"),
    )
