"""Ingestion des dates (D-042, Mission 003.2).

Constate sur un registre de ventes reel (Mission 003.1, OH5): un export
Shopify ouvert puis imprime depuis un tableur porte des dates "M/D/YYYY H:MM".
Mervio les rejetait toutes; et une date sans heure etait lue J/M avant M/J,
en silence. Regle: l'ordre jour/mois n'est jamais devine.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from mervio.application.inspector import inspect_file
from mervio.domain.quality import DataQualityReport
from mervio.errors import InvalidDataError
from mervio.ingestion import ingest_google_ads, ingest_shopify_orders, ingest_stripe
from mervio.ingestion.base import (
    DAY_FIRST, MONTH_FIRST, REJECT_SLASH_DATES, detect_date_order, parse_date, parse_datetime, slash_date_order,
)

K = dict(source="test", column="Created at", required=False)
ORDERS_HEADER = ("Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,Shipping,Taxes,Total,"
                 "Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku\n")


# -- formats explicitement supportes ---------------------------------------------------
@pytest.mark.parametrize("text,order,expected", [
    ("1/2/2020 03:04", MONTH_FIRST, datetime(2020, 1, 2, 3, 4)),
    ("01/02/2020 03:04", MONTH_FIRST, datetime(2020, 1, 2, 3, 4)),
    ("1/2/2020", MONTH_FIRST, datetime(2020, 1, 2)),
    ("01/02/2020", MONTH_FIRST, datetime(2020, 1, 2)),
    ("1/2/2020 03:04", DAY_FIRST, datetime(2020, 2, 1, 3, 4)),
    ("01/02/2020 03:04", DAY_FIRST, datetime(2020, 2, 1, 3, 4)),
    ("1/2/2020", DAY_FIRST, datetime(2020, 2, 1)),
    ("01/02/2020", DAY_FIRST, datetime(2020, 2, 1)),
    ("10/29/2020 9:07", MONTH_FIRST, datetime(2020, 10, 29, 9, 7)),
    ("10/29/2020 9:07:31", MONTH_FIRST, datetime(2020, 10, 29, 9, 7, 31)),
])
def test_slash_dates_follow_the_declared_order(text, order, expected):
    assert parse_datetime(text, date_order=order, **K) == expected


@pytest.mark.parametrize("text,expected", [
    ("2020-01-02", datetime(2020, 1, 2)),
    ("2020-01-02T03:04:00", datetime(2020, 1, 2, 3, 4)),
    ("2020-01-02 03:04", datetime(2020, 1, 2, 3, 4)),
    ("2020-10-29 09:07:31 -0400", datetime(2020, 10, 29, 13, 7, 31)),   # normalise en UTC
    ("2020-10-29T09:07:31+02:00", datetime(2020, 10, 29, 7, 7, 31)),
])
def test_iso_8601_needs_no_convention(text, expected):
    for order in (None, DAY_FIRST, MONTH_FIRST, REJECT_SLASH_DATES):
        assert parse_datetime(text, date_order=order, **K) == expected


@pytest.mark.parametrize("text,expected", [
    ("10/29/2020 9:07", datetime(2020, 10, 29, 9, 7)),   # 29 ne peut etre qu'un jour
    ("29/10/2020 09:07", datetime(2020, 10, 29, 9, 7)),
    ("13/1/2021", datetime(2021, 1, 13)),
])
def test_self_evident_values_parse_without_a_convention(text, expected):
    assert parse_datetime(text, **K) == expected


# -- ambiguite ---------------------------------------------------------------------
@pytest.mark.parametrize("text", ["04/05/2020", "1/2/2020 03:04", "01/02/2020", "12/12/2020 10:00"])
def test_ambiguous_dates_are_never_guessed(text):
    assert parse_datetime(text, **K) is None
    with pytest.raises(InvalidDataError, match="ambigue"):
        parse_datetime(text, source="test", column="Created at", required=True)


def test_value_contradicting_the_file_convention_is_rejected():
    assert parse_datetime("29/10/2020", date_order=MONTH_FIRST, **K) is None
    assert parse_datetime("10/29/2020", date_order=DAY_FIRST, **K) is None


def test_conflicting_file_rejects_every_slash_date():
    for text in ("10/29/2020", "29/10/2020", "01/02/2020"):
        assert parse_datetime(text, date_order=REJECT_SLASH_DATES, **K) is None


# -- formats refuses ------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "31/02/2026", "02/30/2020 10:00", "13/13/2020", "1/2/20", "2020/01/02", "01-02-2020", "2 Jan 2020",
    "10/29/2020 25:00", "10/29/2020 9:7", "not a date", "0/12/2020",
])
def test_invalid_or_unsupported_formats_are_refused(text):
    for order in (None, DAY_FIRST, MONTH_FIRST):
        assert parse_datetime(text, date_order=order, **K) is None


def test_parse_date_forwards_the_convention():
    assert parse_date("04/05/2020", date_order=DAY_FIRST, **K) == date(2020, 5, 4)
    assert parse_date("04/05/2020", date_order=MONTH_FIRST, **K) == date(2020, 4, 5)
    assert parse_date("04/05/2020", **K) is None


# -- detection par fichier ----------------------------------------------------------------
def test_slash_date_order_reads_one_value():
    assert slash_date_order("10/29/2020 9:07") == MONTH_FIRST
    assert slash_date_order("29/10/2020") == DAY_FIRST
    assert slash_date_order("04/05/2020") == "ambiguous"
    assert slash_date_order("13/13/2020") == "invalid"
    assert slash_date_order("2020-01-02") is None


@pytest.mark.parametrize("values,status,order", [
    (["10/29/2020 9:07", "4/5/2020 1:00", ""], "resolved", MONTH_FIRST),
    (["29/10/2020", "04/05/2020"], "resolved", DAY_FIRST),
    (["04/05/2020", "01/02/2020"], "ambiguous", None),
    (["29/10/2020", "10/29/2020"], "conflict", REJECT_SLASH_DATES),
    (["2020-01-02", None, ""], "not_needed", None),
])
def test_detect_date_order(values, status, order):
    convention = detect_date_order(values)
    assert (convention.status, convention.order) == (status, order)


# -- connecteurs ----------------------------------------------------------------------
def _orders(tmp_path, dates):
    body = "".join(f"#{1000 + i},c{i}@x.com,paid,{d},USD,10.00,0.00,0.00,0.00,10.00,,1,A,10.00,A\n" for i, d in enumerate(dates))
    path = tmp_path / "orders.csv"
    path.write_text(ORDERS_HEADER + body, encoding="utf-8")
    quality = DataQualityReport()
    orders, _, _ = ingest_shopify_orders(path, quality)
    return orders, {i.kind: i for i in quality.issues}


def test_shopify_spreadsheet_dates_are_ingested_with_the_file_convention(tmp_path):
    """Forme OH5: M/D/YYYY H:MM, dont des valeurs ambigues resolues par le fichier."""
    orders, issues = _orders(tmp_path, ["10/29/2020 9:07", "10/1/2020 11:28", "4/5/2020 15:48"])
    assert [o.created_at for o in orders] == [datetime(2020, 4, 5, 15, 48), datetime(2020, 10, 1, 11, 28),
                                             datetime(2020, 10, 29, 9, 7)]
    assert issues["date_order_inferred"].severity == "info" and "MM/JJ" in issues["date_order_inferred"].message


def test_shopify_day_first_file_is_not_read_month_first(tmp_path):
    orders, _ = _orders(tmp_path, ["29/10/2020 09:07", "04/05/2020 10:00"])
    assert sorted(o.created_at for o in orders) == [datetime(2020, 5, 4, 10, 0), datetime(2020, 10, 29, 9, 7)]


def test_shopify_file_with_only_ambiguous_dates_is_rejected_explicitly(tmp_path):
    orders, issues = _orders(tmp_path, ["04/05/2020 10:00", "01/02/2020 11:00"])
    assert orders == []
    assert issues["ambiguous_date_order"].severity == "error"
    assert "04/05" not in issues["ambiguous_date_order"].message   # jamais la valeur


def test_shopify_file_mixing_both_orders_is_rejected(tmp_path):
    orders, issues = _orders(tmp_path, ["29/10/2020 09:07", "10/29/2020 09:07", "2020-10-30 10:00:00"])
    assert [o.created_at for o in orders] == [datetime(2020, 10, 30, 10, 0)]   # seules les dates ISO passent
    assert issues["date_order_conflict"].severity == "error"


def test_iso_shopify_export_raises_no_date_issue(tmp_path):
    orders, issues = _orders(tmp_path, ["2020-10-29 09:07:31 -0400"])
    assert len(orders) == 1 and not {"date_order_inferred", "ambiguous_date_order", "date_order_conflict"} & set(issues)


def test_stripe_uses_its_own_file_convention(tmp_path):
    path = tmp_path / "stripe.csv"
    path.write_text("id,Created (UTC),Amount,Amount Refunded,Currency,Fee,Net,Status\n"
                    "ch_1,13/09/2026 10:00,10,0,eur,1,9,Paid\nch_2,04/09/2026 10:00,10,0,eur,1,9,Paid\n", encoding="utf-8")
    payments, _ = ingest_stripe(path, DataQualityReport())
    assert sorted(p.created_at.date() for p in payments) == [date(2026, 9, 4), date(2026, 9, 13)]


def test_google_ads_ambiguous_days_are_rejected(tmp_path):
    path = tmp_path / "ads.csv"
    path.write_text("Day,Campaign,Cost,Clicks\n04/09/2026,Search,10,5\n05/09/2026,Search,10,5\n", encoding="utf-8")
    quality = DataQualityReport()
    _, performance = ingest_google_ads(path, quality)
    assert performance == [] and any(i.kind == "ambiguous_date_order" for i in quality.issues)


def test_inspector_period_uses_the_column_convention(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text("order,when\nA,4/5/2020 10:00\nB,10/29/2020 9:07\n", encoding="utf-8")
    inspection = inspect_file(path)
    assert inspection.date_columns == ["when"]
    assert (inspection.period_start, inspection.period_end) == ("2020-04-05", "2020-10-29")
