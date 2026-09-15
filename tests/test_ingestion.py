"""Tests d'ingestion: parsing, validation, doublons, donnees invalides."""
from __future__ import annotations

from datetime import datetime

import pytest

from mervio.errors import InvalidDataError, MissingColumnsError
from mervio.ingestion.base import parse_date, parse_datetime, parse_float, parse_int
from mervio.ingestion.google_ads import ingest_google_ads
from mervio.ingestion.shopify import ingest_shopify_orders, ingest_shopify_products
from mervio.ingestion.stripe import ingest_stripe
from mervio.domain.quality import DataQualityReport, QualityStatus

SRC = "test"


def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# --- parsers ---------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("1234.56", 1234.56), ("1 234,56", 1234.56), ("1,234.56", 1234.56),
    ("\u20ac89.90", 89.90), ("(45.20)", -45.20), ("-12", -12.0), ("0", 0.0),
])
def test_parse_float_formats(raw, expected):
    assert parse_float(raw, source=SRC, column="c") == pytest.approx(expected)


def test_parse_float_empty_is_none_not_zero():
    """Un champ vide ne doit JAMAIS devenir 0: 0 est une affirmation, None une absence."""
    assert parse_float("", source=SRC, column="c") is None
    assert parse_float("n/a", source=SRC, column="c") is None


def test_parse_float_invalid_raises():
    with pytest.raises(InvalidDataError):
        parse_float("abc", source=SRC, column="c")


def test_parse_float_required_empty_raises():
    with pytest.raises(InvalidDataError):
        parse_float("", source=SRC, column="c", required=True)


def test_parse_int_rounds():
    assert parse_int("3.6", source=SRC, column="c") == 4


def test_parse_datetime_normalises_to_utc():
    assert parse_datetime("2026-06-01 10:23:45 +0200", source=SRC, column="c") == datetime(2026, 6, 1, 8, 23, 45)


def test_parse_datetime_iso_and_plain():
    assert parse_datetime("2026-06-01", source=SRC, column="c") == datetime(2026, 6, 1)
    assert parse_datetime("2026-06-01T09:00:00", source=SRC, column="c") == datetime(2026, 6, 1, 9)


def test_parse_datetime_invalid_optional_returns_none():
    assert parse_date("31/02/2026", source=SRC, column="Day", required=False) is None


def test_parse_datetime_invalid_required_raises():
    with pytest.raises(InvalidDataError):
        parse_datetime("31/02/2026", source=SRC, column="Day", required=True)


# --- shopify ---------------------------------------------------------
ORDERS_CSV = """Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,Shipping,Taxes,Total,Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku
#1,a@x.com,paid,2026-09-08 10:00:00 +0000,EUR,100.00,0.00,0.00,20.00,120.00,,1,Produit A,100.00,A
#1,,,,,,,,,,,2,Produit B,25.00,B
#2,b@x.com,paid,2026-09-09 10:00:00 +0000,EUR,45.00,5.00,5.90,9.00,59.90,10.00,1,Produit B,50.00,B
"""


def test_shopify_groups_line_items(tmp_path):
    q = DataQualityReport()
    orders, refunds, currency = ingest_shopify_orders(write(tmp_path, "o.csv", ORDERS_CSV), q)
    assert len(orders) == 2
    assert len(orders[0].items) == 2  # les 2 lignes de #1 sont regroupees
    assert currency == "EUR"
    assert len(refunds) == 1 and refunds[0].amount == 10.0


def test_shopify_net_revenue_excludes_shipping_and_tax(tmp_path):
    q = DataQualityReport()
    orders, _, _ = ingest_shopify_orders(write(tmp_path, "o.csv", ORDERS_CSV), q)
    order2 = [o for o in orders if o.order_id == "#2"][0]
    assert order2.net_revenue == pytest.approx(45.0)  # Subtotal Shopify deja net de la remise de 5, pas 59.90 (D-041)


def test_shopify_deduplicates_identical_line(tmp_path):
    csv = ORDERS_CSV + "#2,,,,,,,,,,,1,Produit B,50.00,B\n"
    q = DataQualityReport()
    orders, _, _ = ingest_shopify_orders(write(tmp_path, "o.csv", csv), q)
    order2 = [o for o in orders if o.order_id == "#2"][0]
    assert len(order2.items) == 1
    assert any(i.kind == "duplicate_line_item" for i in q.issues)


def test_shopify_missing_email_becomes_guest(tmp_path):
    csv = ORDERS_CSV.replace("b@x.com", "")
    q = DataQualityReport()
    orders, _, _ = ingest_shopify_orders(write(tmp_path, "o.csv", csv), q)
    order2 = [o for o in orders if o.order_id == "#2"][0]
    assert order2.customer_id.startswith("guest:")
    assert q.status_of("customer_identity") is not QualityStatus.RELIABLE


def test_shopify_invalid_date_row_skipped_not_crash(tmp_path):
    csv = ORDERS_CSV.replace("2026-09-09 10:00:00 +0000", "pas-une-date")
    q = DataQualityReport()
    orders, _, _ = ingest_shopify_orders(write(tmp_path, "o.csv", csv), q)
    assert [o.order_id for o in orders] == ["#1"]
    assert any(i.kind == "invalid_date" for i in q.issues)


def test_shopify_missing_columns_raises(tmp_path):
    with pytest.raises(MissingColumnsError):
        ingest_shopify_orders(write(tmp_path, "bad.csv", "Name,Email\n#1,a@x.com\n"), DataQualityReport())


PRODUCTS_CSV = """SKU,Product ID,Title,Variant Price,Cost per item
A,gid-A,Produit A,100.00,40.00
B,gid-B,Produit B,50.00,
C,gid-C,Produit C,30.00,-5.00
"""


def test_shopify_products_missing_cogs_is_none(tmp_path):
    q = DataQualityReport()
    products = ingest_shopify_products(write(tmp_path, "p.csv", PRODUCTS_CSV), q)
    assert products["A"].unit_cogs == 40.0
    assert products["B"].unit_cogs is None
    assert products["C"].unit_cogs is None  # cout negatif rejete, pas corrige
    assert q.coverage_of("product_cogs") == pytest.approx(1 / 3)


# --- stripe ----------------------------------------------------------
STRIPE_CSV = """id,Created (UTC),Amount,Amount Refunded,Currency,Fee,Net,Status,Customer Email,order_id
ch_1,2026-09-08 10:00:00,120.00,0.00,eur,2.00,118.00,Paid,a@x.com,#1
ch_2,2026-09-09 10:00:00,59.90,10.00,eur,1.09,58.81,Paid,b@x.com,#2
ch_2,2026-09-09 10:00:00,59.90,10.00,eur,1.09,58.81,Paid,b@x.com,#2
ch_3,2026-09-10 10:00:00,80.00,-5.00,eur,,,Paid,,
"""


def test_stripe_dedup_and_negative_refund_rejected(tmp_path):
    q = DataQualityReport()
    payments, refunds = ingest_stripe(write(tmp_path, "s.csv", STRIPE_CSV), q)
    assert len(payments) == 3  # ch_2 dedupliquee
    assert any(i.kind == "duplicate_payment" for i in q.issues)
    assert len(refunds) == 1 and refunds[0].amount == 10.0
    assert any(i.kind == "negative_refund" for i in q.issues)


def test_stripe_missing_fee_tracked_as_incomplete(tmp_path):
    q = DataQualityReport()
    ingest_stripe(write(tmp_path, "s.csv", STRIPE_CSV), q)
    assert q.status_of("payment_fees") is QualityStatus.INCOMPLETE


# --- google ads ------------------------------------------------------
ADS_CSV = """Day,Campaign,Campaign ID,Cost,Impressions,Clicks,Conversions,Conv. value
2026-09-08,Search,c1,100.00,3000,120,4.00,500.00
2026-09-08,Search,c1,100.00,3000,120,4.00,500.00
31/02/2026,Search,c1,50.00,1000,40,1.00,100.00
2026-09-09,Shopping,c2,-20.00,500,10,0.00,0.00
"""


def test_google_ads_rejects_bad_rows(tmp_path):
    q = DataQualityReport()
    campaigns, perf = ingest_google_ads(write(tmp_path, "g.csv", ADS_CSV), q)
    assert len(perf) == 1  # doublon, date invalide et depense negative rejetes
    kinds = {i.kind for i in q.issues}
    assert {"duplicate_ad_row", "invalid_date", "negative_spend"} <= kinds
    assert "c1" in campaigns
