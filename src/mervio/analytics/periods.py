"""Decoupage temporel deterministe.

Une periode est toujours un intervalle semi-ouvert [start, end) et porte un
label explicite. Aucune comparaison n'est affichee sans sa periode de reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional


@dataclass(frozen=True)
class Period:
    label: str
    start: datetime
    end: datetime  # exclusive
    grain: str

    @property
    def start_date(self) -> date:
        return self.start.date()

    @property
    def end_date(self) -> date:
        return (self.end - timedelta(seconds=1)).date()

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "grain": self.grain,
            "start": self.start_date.isoformat(),
            "end": self.end_date.isoformat(),
        }

    def __str__(self) -> str:
        return f"{self.label} ({self.start_date} -> {self.end_date})"


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def month_start(day: date) -> date:
    return day.replace(day=1)


def next_month(day: date) -> date:
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)


def make_period(anchor: date, grain: str) -> Period:
    if grain == "week":
        start = week_start(anchor)
        end = start + timedelta(days=7)
        label = f"{start.isocalendar()[0]}-W{start.isocalendar()[1]:02d}"
    elif grain == "month":
        start = month_start(anchor)
        end = next_month(start)
        label = f"{start.year}-{start.month:02d}"
    else:
        raise ValueError(f"grain non supporte: {grain}")
    return Period(label, datetime.combine(start, datetime.min.time()),
                  datetime.combine(end, datetime.min.time()), grain)


def previous_period(period: Period) -> Period:
    anchor = period.start_date - timedelta(days=1)
    return make_period(anchor, period.grain)


def shift_periods(period: Period, count: int) -> Period:
    current = period
    for _ in range(abs(count)):
        current = previous_period(current) if count > 0 else _next_period(current)
    return current


def _next_period(period: Period) -> Period:
    return make_period(period.end_date + timedelta(days=1), period.grain)


def last_complete_period(latest_day: date, today: Optional[date], grain: str) -> Period:
    """Derniere periode entierement close presente dans la donnee."""
    candidate = make_period(latest_day, grain)
    reference = today or latest_day
    if candidate.end_date > reference or candidate.end_date > latest_day:
        candidate = previous_period(candidate)
    return candidate


def build_period_series(latest: Period, count: int) -> List[Period]:
    """Retourne `count` periodes consecutives se terminant par `latest`."""
    periods = [latest]
    for _ in range(count - 1):
        periods.append(previous_period(periods[-1]))
    return list(reversed(periods))


def year_over_year_period(period: Period) -> Period:
    if period.grain == "week":
        return make_period(period.start_date - timedelta(days=364), "week")
    return make_period(date(period.start_date.year - 1, period.start_date.month, 1), "month")
