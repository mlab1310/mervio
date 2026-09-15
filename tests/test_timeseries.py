"""Tests des periodes, series et comparaisons."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import ad, make_dataset, make_order
from mervio.analytics.kpi import total_revenue
from mervio.analytics.periods import (
    last_complete_period, make_period, previous_period, year_over_year_period,
)
from mervio.analytics.timeseries import build_series, compare, pct_change, rolling_average


def test_week_period_is_monday_to_sunday():
    period = make_period(date(2026, 9, 10), "week")  # jeudi
    assert period.start_date == date(2026, 9, 7)
    assert period.end_date == date(2026, 9, 13)
    assert period.label == "2026-W37"


def test_month_period():
    period = make_period(date(2026, 9, 10), "month")
    assert (period.start_date, period.end_date) == (date(2026, 9, 1), date(2026, 9, 30))


def test_december_month_rolls_over_year():
    period = make_period(date(2026, 12, 5), "month")
    assert period.end_date == date(2026, 12, 31)


def test_previous_period():
    assert previous_period(make_period(date(2026, 9, 7), "week")).label == "2026-W36"


def test_last_complete_period_excludes_current_week():
    """Le 14/09 (lundi), la derniere semaine CLOSE est W37, pas W38 en cours."""
    assert last_complete_period(date(2026, 9, 13), date(2026, 9, 14), "week").label == "2026-W37"


def test_last_complete_period_backs_off_incomplete_week():
    assert last_complete_period(date(2026, 9, 9), date(2026, 9, 9), "week").label == "2026-W36"


def test_year_over_year_period_is_364_days_back():
    yoy = year_over_year_period(make_period(date(2026, 9, 7), "week"))
    assert yoy.start_date == date(2025, 9, 8)


@pytest.mark.parametrize("cur,prev,expected", [
    (110.0, 100.0, 0.10), (90.0, 100.0, -0.10), (100.0, 0.0, None),
    (None, 100.0, None), (100.0, None, None),
])
def test_pct_change(cur, prev, expected):
    result = pct_change(cur, prev)
    assert result is None if expected is None else result == pytest.approx(expected)


def test_rolling_average_handles_none():
    assert rolling_average([10.0, None, 20.0], 2) == [10.0, 10.0, 20.0]


def _three_week_dataset():
    orders = []
    for week, (day, amount) in enumerate([(date(2026, 8, 25), 100.0),
                                          (date(2026, 9, 1), 200.0),
                                          (date(2026, 9, 8), 50.0)]):
        orders.append(make_order(f"#{week}", day, f"c{week}@x.com", [("A", 1, amount)]))
    return make_dataset(orders=orders)


def test_build_series_returns_chronological_points():
    ds = _three_week_dataset()
    points = build_series(ds, make_period(date(2026, 9, 8), "week"), 3, total_revenue)
    assert [p.value for p in points] == [100.0, 200.0, 50.0]
    assert [p.period.label for p in points] == ["2026-W35", "2026-W36", "2026-W37"]


def test_wow_comparison_reports_both_periods():
    ds = _three_week_dataset()
    result = compare(ds, make_period(date(2026, 9, 8), "week"), "revenue", total_revenue, "wow")
    assert result.available
    assert result.current_period == "2026-W37" and result.previous_period == "2026-W36"
    assert result.pct_change == pytest.approx(-0.75)


def test_yoy_unavailable_states_the_reason():
    """Un YoY sans historique doit etre marque indisponible, pas rempli de zeros."""
    ds = _three_week_dataset()
    result = compare(ds, make_period(date(2026, 9, 8), "week"), "revenue", total_revenue, "yoy")
    assert result.available is False
    assert result.pct_change is None
    assert "historique insuffisant" in result.reason


def test_wow_rejected_on_month_grain():
    ds = _three_week_dataset()
    result = compare(ds, make_period(date(2026, 9, 8), "month"), "revenue", total_revenue, "wow")
    assert result.available is False
