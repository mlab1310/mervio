"""Detection d'anomalies v1: seuil relatif + z-score sur baseline glissante.

Une variation n'est jamais appelee anomalie sans que le critere utilise soit
ecrit dans l'objet retourne.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import List, Optional

from ..config import AnomalyConfig
from .timeseries import SeriesPoint


#: sens "souhaitable" de chaque metrique. Sans cela, une BAISSE des
#: remboursements serait signalee comme une alerte, ce qui est faux.
DESIRABLE_DIRECTION = {
    "revenue": "up", "orders": "up", "aov": "up", "units": "up",
    "paid_conversion_rate": "up", "roas": "up", "gross_margin": "up",
    "refunds": "down", "refund_rate": "down", "cac": "down",
    "ad_spend": "neutral", "paid_clicks": "neutral",
}


def assess_direction(metric: str, direction: str) -> str:
    desired = DESIRABLE_DIRECTION.get(metric, "neutral")
    if desired == "neutral":
        return "neutral"
    return "favorable" if direction == desired else "unfavorable"


@dataclass
class Anomaly:
    metric: str
    period: str
    observed_value: float
    expected_value: float
    delta: float
    delta_pct: Optional[float]
    z_score: Optional[float]
    direction: str  # up | down
    method: str
    criterion: str
    severity: str  # low | medium | high
    assessment: str = "neutral"  # favorable | unfavorable | neutral
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "metric": self.metric, "period": self.period,
            "observed_value": round(self.observed_value, 4),
            "expected_value": round(self.expected_value, 4),
            "delta": round(self.delta, 4),
            "delta_pct": round(self.delta_pct, 4) if self.delta_pct is not None else None,
            "z_score": round(self.z_score, 3) if self.z_score is not None else None,
            "direction": self.direction, "assessment": self.assessment, "method": self.method,
            "criterion": self.criterion, "severity": self.severity, "evidence": self.evidence,
        }


def _severity(delta_pct: Optional[float], z: Optional[float], cfg: AnomalyConfig) -> str:
    abs_pct = abs(delta_pct) if delta_pct is not None else 0.0
    abs_z = abs(z) if z is not None else 0.0
    if abs_pct >= cfg.high_pct or abs_z >= cfg.high_z:
        return "high"
    if abs_pct >= cfg.pct_threshold or abs_z >= cfg.zscore_threshold:
        return "medium"
    return "low"


def detect_anomalies(
    metric: str, series: List[SeriesPoint], cfg: AnomalyConfig, label: Optional[str] = None
) -> List[Anomaly]:
    """Evalue le dernier point de la serie contre sa baseline glissante."""
    values = [(p.period.label, p.value) for p in series if p.value is not None]
    if len(values) < 2:
        return []
    current_label, current = values[-1]
    baseline = [v for _, v in values[:-1]][-cfg.baseline_window:]
    if not baseline:
        return []

    expected = mean(baseline)
    delta = current - expected
    pct = (delta / abs(expected)) if expected else None
    z: Optional[float] = None
    method = "percentage_threshold_vs_rolling_mean"
    if len(baseline) >= cfg.min_baseline_periods:
        spread = pstdev(baseline)
        if spread > 0:
            z = delta / spread
            method = "zscore_and_percentage_threshold_vs_rolling_baseline"

    material = pct is not None and abs(pct) >= cfg.min_material_pct
    statistically_unusual = z is not None and abs(z) >= cfg.zscore_threshold
    large = pct is not None and abs(pct) >= cfg.pct_threshold
    # le plancher de materialite est une condition NECESSAIRE: le z-score seul
    # ne suffit jamais a declarer une anomalie business
    if not (material and (large or statistically_unusual)):
        return []

    criterion = (
        f"|variation| >= {cfg.min_material_pct:.0%} (materialite) ET "
        f"(|variation| >= {cfg.pct_threshold:.0%} vs moyenne des {len(baseline)} periodes precedentes"
        + (f" OU |z| >= {cfg.zscore_threshold}" if z is not None else "") + ")"
    )
    evidence = [
        f"{label or metric} observe sur {current_label}: {current:,.2f}",
        f"attendu (moyenne {len(baseline)} periodes): {expected:,.2f}",
    ]
    if pct is not None:
        evidence.append(f"ecart relatif: {pct:+.1%}")
    if z is not None:
        evidence.append(f"z-score: {z:+.2f}")

    direction = "down" if delta < 0 else "up"
    return [Anomaly(
        metric=metric, period=current_label, observed_value=current, expected_value=expected,
        delta=delta, delta_pct=pct, z_score=z, direction=direction,
        method=method, criterion=criterion, severity=_severity(pct, z, cfg),
        assessment=assess_direction(metric, direction), evidence=evidence,
    )]
