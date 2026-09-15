"""Series temporelles et comparaisons (WoW / MoM / YoY)."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Callable, Dict, List, Optional

from ..domain.models import Dataset
from .periods import Period, build_period_series, previous_period, year_over_year_period


@dataclass
class SeriesPoint:
    period: Period
    value: Optional[float]

    def to_dict(self) -> dict:
        return {"period": self.period.label, "value": round(self.value, 4) if self.value is not None else None}


@dataclass
class Comparison:
    metric: str
    kind: str  # wow | mom | yoy
    current_period: str
    previous_period: str
    current_value: Optional[float]
    previous_value: Optional[float]
    delta: Optional[float]
    pct_change: Optional[float]
    available: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "metric": self.metric, "kind": self.kind,
            "current_period": self.current_period, "previous_period": self.previous_period,
            "current_value": round(self.current_value, 4) if self.current_value is not None else None,
            "previous_value": round(self.previous_value, 4) if self.previous_value is not None else None,
            "delta": round(self.delta, 4) if self.delta is not None else None,
            "pct_change": round(self.pct_change, 4) if self.pct_change is not None else None,
            "available": self.available, "reason": self.reason,
        }


def pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous)


def rolling_average(values: List[Optional[float]], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for i in range(len(values)):
        chunk = [v for v in values[max(0, i - window + 1): i + 1] if v is not None]
        out.append(mean(chunk) if chunk else None)
    return out


MetricFn = Callable[[Dataset], Optional[float]]


def build_series(full: Dataset, latest: Period, count: int, metric_fn: MetricFn) -> List[SeriesPoint]:
    points: List[SeriesPoint] = []
    for period in build_period_series(latest, count):
        window = full.window(period.start, period.end)
        points.append(SeriesPoint(period, metric_fn(window)))
    return points


def _has_data(full: Dataset, period: Period) -> bool:
    window = full.window(period.start, period.end)
    return bool(window.orders or window.ad_performance)


def compare(full: Dataset, latest: Period, metric_name: str, metric_fn: MetricFn, kind: str) -> Comparison:
    if kind == "wow" and latest.grain != "week":
        return Comparison(metric_name, kind, latest.label, "-", None, None, None, None, False,
                          "comparaison hebdomadaire indisponible sur un decoupage mensuel")
    if kind == "mom" and latest.grain != "month":
        return Comparison(metric_name, kind, latest.label, "-", None, None, None, None, False,
                          "comparaison mensuelle indisponible sur un decoupage hebdomadaire")
    reference = year_over_year_period(latest) if kind == "yoy" else previous_period(latest)
    if not _has_data(full, reference):
        return Comparison(metric_name, kind, latest.label, reference.label, None, None, None, None, False,
                          f"aucune donnee sur la periode de reference {reference.label}: historique insuffisant")
    current = metric_fn(full.window(latest.start, latest.end))
    prior = metric_fn(full.window(reference.start, reference.end))
    delta = (current - prior) if (current is not None and prior is not None) else None
    return Comparison(metric_name, kind, latest.label, reference.label, current, prior,
                      delta, pct_change(current, prior), True)


def compare_all(full: Dataset, latest: Period, metrics: Dict[str, MetricFn]) -> Dict[str, Dict[str, Comparison]]:
    kinds = ["wow", "yoy"] if latest.grain == "week" else ["mom", "yoy"]
    return {
        name: {kind: compare(full, latest, name, fn, kind) for kind in kinds}
        for name, fn in metrics.items()
    }
