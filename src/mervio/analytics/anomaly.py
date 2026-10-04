"""Detection d'anomalies v1: seuil relatif + z-score sur baseline glissante.

Une variation n'est jamais appelee anomalie sans que le critere utilise soit
ecrit dans l'objet retourne.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import List, Optional

from ..config import AnomalyConfig
from .evidence import (
    NO_COMPARISON, VS_BASELINE, Evidence, observed, to_dicts,
)
from .messages import Message
from .timeseries import SeriesPoint


#: sens "souhaitable" de chaque metrique. Sans cela, une BAISSE des
#: remboursements serait signalee comme une alerte, ce qui est faux.
DESIRABLE_DIRECTION = {
    "revenue": "up", "orders": "up", "aov": "up", "units": "up",
    "orders_per_paid_click": "up", "roas": "up", "gross_margin": "up",
    "refunds": "down", "refund_rate": "down", "cac": "down",
    "ad_spend": "neutral", "paid_clicks": "neutral",
}


#: unite de chaque grandeur suivie. Sans elle, un consommateur devine si 0,18 est un ratio ou un
#: montant -- exactement ce que les anciennes chaines le forcaient a faire.
_METRIC_UNITS = {
    "revenue": "currency", "refunds": "currency", "ad_spend": "currency", "aov": "currency",
    "orders": "count", "units": "count", "paid_clicks": "count", "new_customers": "count",
    "orders_per_paid_click": "ratio", "roas": "ratio", "gross_margin": "ratio",
    "refund_rate": "ratio", "cac": "currency",
}


def _unit_of(metric: str) -> str:
    return _METRIC_UNITS.get(metric, "ratio")


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
    #: identifiant TECHNIQUE de la methode, deja une cle: ni prose, ni nombre (AN-19 bucket 2)
    method: str
    #: AN-19 / D-085: le critere n'est plus une phrase, c'est une cle + les seuils appliques.
    #: D-007 exige que le critere utilise soit ECRIT; il l'est, sans etre reecrit a la main.
    criterion: Message
    severity: str  # low | medium | high
    assessment: str = "neutral"  # favorable | unfavorable | neutral
    evidence: List[Evidence] = field(default_factory=list)
    #: labels des periodes EFFECTIVEMENT retenues comme baseline (AN-01).
    #: Une baseline non declaree ne peut etre ni verifiee, ni reutilisee par la decomposition
    #: sans risque de divergence silencieuse.
    baseline_periods: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "metric": self.metric, "period": self.period,
            "observed_value": round(self.observed_value, 4),
            "expected_value": round(self.expected_value, 4),
            "delta": round(self.delta, 4),
            "delta_pct": round(self.delta_pct, 4) if self.delta_pct is not None else None,
            "z_score": round(self.z_score, 3) if self.z_score is not None else None,
            "direction": self.direction, "assessment": self.assessment, "method": self.method,
            "criterion": self.criterion.text,
            "criterion_key": self.criterion.key,
            "criterion_params": dict(self.criterion.params),
            "severity": self.severity,
            "evidence": to_dicts(self.evidence),
            "baseline_periods": list(self.baseline_periods),
        }


def _severity(delta_pct: Optional[float], z: Optional[float], cfg: AnomalyConfig) -> str:
    abs_pct = abs(delta_pct) if delta_pct is not None else 0.0
    abs_z = abs(z) if z is not None else 0.0
    if abs_pct >= cfg.high_pct or abs_z >= cfg.high_z:
        return "high"
    if abs_pct >= cfg.pct_threshold or abs_z >= cfg.zscore_threshold:
        return "medium"
    return "low"


def baseline_points(series: List[SeriesPoint], cfg: AnomalyConfig) -> List[SeriesPoint]:
    """Points EFFECTIVEMENT retenus comme baseline du dernier point de la serie.

    Source UNIQUE de la definition de la fenetre (AN-01): le detecteur et la decomposition
    appellent cette fonction, de sorte qu'elles ne peuvent pas divergier. Les points `None` sont
    exclus AVANT le fenetrage: une periode sans donnee ne consomme pas un cran de la fenetre.
    """
    observed = [p for p in series if p.value is not None]
    if len(observed) < 2:
        return []
    return observed[:-1][-cfg.baseline_window:]


def detect_anomalies(
    metric: str, series: List[SeriesPoint], cfg: AnomalyConfig, label: Optional[str] = None
) -> List[Anomaly]:
    """Evalue le dernier point de la serie contre sa baseline glissante."""
    values = [(p.period.label, p.value) for p in series if p.value is not None]
    if len(values) < 2:
        return []
    current_label, current = values[-1]
    points = baseline_points(series, cfg)
    baseline = [p.value for p in points]
    baseline_labels = [p.period.label for p in points]
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

    # AN-19: deux cles STATIQUES au lieu d'une phrase assemblee par concatenation. Les seuils
    # sont des parametres -- ils viennent de `AnomalyConfig` (AN-16) et restent auditables.
    criterion_params = {"min_material_pct": cfg.min_material_pct,
                        "pct_threshold": cfg.pct_threshold,
                        "baseline_periods": len(baseline)}
    # B-3: le seuil de z-score n'est publie QUE s'il a servi a la regle. Le publier sans
    # cela laissait croire qu'il s'etait applique.
    if z is None:
        criterion = Message("anomaly.criterion_without_zscore", criterion_params)
    else:
        criterion_params["zscore_threshold"] = cfg.zscore_threshold
        criterion = Message("anomaly.criterion", criterion_params)
    # AN-15: la preuve DUPLIQUAIT `observed_value`, `expected_value`, `delta`, `delta_pct` et
    # `z_score`, qui existent deja en champs typés sur le meme objet. Elle devient UN fait
    # structure portant la grandeur, sa reference de baseline et sa comparaison -- plus une
    # reformulation en prose de donnees dont on dispose.
    evidence = [observed(
        f"anomaly.{metric}", "evidence.anomaly.observed_vs_baseline",
        metric_key=metric, source="analytics", unit=_unit_of(metric), period=current_label,
        current=current, previous=expected, comparison=VS_BASELINE, delta_pct=pct,
    )]
    if z is not None:
        evidence.append(observed(
            f"anomaly.{metric}.zscore", "evidence.anomaly.zscore",
            metric_key=metric, source="analytics", unit="score", period=current_label,
            current=z, comparison=NO_COMPARISON,
        ))

    direction = "down" if delta < 0 else "up"
    return [Anomaly(
        metric=metric, period=current_label, observed_value=current, expected_value=expected,
        delta=delta, delta_pct=pct, z_score=z, direction=direction,
        method=method, criterion=criterion, severity=_severity(pct, z, cfg),
        assessment=assess_direction(metric, direction), evidence=evidence,
        baseline_periods=baseline_labels,
    )]
