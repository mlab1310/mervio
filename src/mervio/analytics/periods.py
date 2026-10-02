"""Decoupage temporel deterministe.

Une periode est toujours un intervalle semi-ouvert [start, end) et porte un
label explicite. Aucune comparaison n'est affichee sans sa periode de reference.

AN-06. Le decoupage se fait dans le fuseau de la BOUTIQUE, les instants restent stockes en UTC.
Une periode porte donc DEUX representations du meme intervalle:

  * `start` / `end`   instants UTC (naifs, comme les horodatages du dataset): ils servent au
                      fenetrage de la donnee;
  * `start_date` / `end_date`  dates CIVILES LOCALES: elles servent au label, a l'affichage et
                      a l'arithmetique des periodes.

Les confondre est precisement le defaut AN-06: en UTC strict, une commande passee le dimanche
a 23 h 30 UTC tombe dans la semaine du marchand parisien SUIVANTE, parce qu'il y est deja lundi.

Consequence a asserter et non a corriger: une semaine de changement d'heure fait 167 h ou 169 h.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional

from ..config import resolve_timezone

#: fuseau de decoupage par defaut: reproduit exactement le comportement anterieur a AN-06
DEFAULT_TIMEZONE = "UTC"


def _utc_instant(day: date, tz: str) -> datetime:
    """Instant UTC (naif) de MINUIT LOCAL le jour `day` dans le fuseau `tz`.

    Naif et non aware: les horodatages du dataset sont naifs et signifient UTC (contrat AN-06).
    Rendre les bornes aware forcerait a convertir tout le modele de donnees, la persistance et
    le codec des instantanes -- hors perimetre, et sans aucun gain de justesse.
    """
    local = datetime.combine(day, time.min, tzinfo=resolve_timezone(tz))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Period:
    label: str
    start: datetime  # instant UTC naif de la borne basse
    end: datetime  # instant UTC naif, exclusif
    grain: str
    #: dates CIVILES locales de l'intervalle; `end_date` est INCLUSIVE
    start_date: date
    end_date: date
    #: fuseau dans lequel les dates civiles ont ete calculees
    timezone: str = DEFAULT_TIMEZONE

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


def make_period(anchor: date, grain: str, tz: str = DEFAULT_TIMEZONE) -> Period:
    """`anchor` est une date CIVILE LOCALE; les bornes rendues sont des instants UTC."""
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
    return Period(label, _utc_instant(start, tz), _utc_instant(end, tz), grain,
                  start, end - timedelta(days=1), tz)


def previous_period(period: Period) -> Period:
    anchor = period.start_date - timedelta(days=1)
    return make_period(anchor, period.grain, period.timezone)


def shift_periods(period: Period, count: int) -> Period:
    current = period
    for _ in range(abs(count)):
        current = previous_period(current) if count > 0 else _next_period(current)
    return current


def _next_period(period: Period) -> Period:
    return make_period(period.end_date + timedelta(days=1), period.grain, period.timezone)


def last_complete_period(latest_day: date, today: Optional[date], grain: str,
                         tz: str = DEFAULT_TIMEZONE) -> Period:
    """Derniere periode entierement close presente dans la donnee.

    `latest_day` et `today` sont des dates CIVILES LOCALES dans `tz`.
    """
    candidate = make_period(latest_day, grain, tz)
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
        return make_period(period.start_date - timedelta(days=364), "week", period.timezone)
    return make_period(date(period.start_date.year - 1, period.start_date.month, 1), "month",
                       period.timezone)
