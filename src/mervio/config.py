"""Configuration centralisee du moteur analytique.

Aucun seuil n'est code en dur ailleurs dans le moteur: tout ce qui influence
un score, une anomalie ou une severite passe par AnalyticsConfig, afin que la
logique reste auditable et que le SaaS puisse la parametrer par tenant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

ENGINE_VERSION = "0.1.0"


@dataclass(frozen=True)
class AnomalyConfig:
    #: ecart relatif minimal vs baseline pour declencher une anomalie
    pct_threshold: float = 0.15
    #: |z| minimal pour declencher une anomalie
    zscore_threshold: float = 2.0
    #: plancher de materialite: en dessous, aucune anomalie n'est emise, meme si
    #: le z-score est eleve. Sur une baseline tres stable, un ecart de 4% peut
    #: donner z=3 tout en n'ayant aucune portee business. Le z-score peut
    #: AGGRAVER une anomalie, jamais en CREER une sous ce plancher.
    min_material_pct: float = 0.08
    #: nombre de periodes de baseline requises avant de calculer un z-score
    min_baseline_periods: int = 4
    #: taille de la fenetre glissante de baseline
    baseline_window: int = 8
    #: seuils de severite (sur |pct| et |z|)
    high_pct: float = 0.30
    high_z: float = 3.0


@dataclass(frozen=True)
class QualityConfig:
    #: couverture minimale pour qu'un champ soit considere fiable
    reliable_coverage: float = 0.95
    #: couverture en dessous de laquelle le champ est considere indisponible
    unavailable_coverage: float = 0.0


@dataclass(frozen=True)
class HealthConfig:
    """Poids des dimensions du Business Health Score.

    Les poids sont renormalises sur les seules dimensions calculables.
    """

    weights: Dict[str, float] = field(
        default_factory=lambda: {
            "revenue_growth": 0.20,
            "profitability": 0.25,
            "marketing_efficiency": 0.20,
            "customer_health": 0.15,
            "product_health": 0.10,
            "data_quality": 0.10,
        }
    )
    #: part de CA du top 5 clients au dela de laquelle on applique une penalite
    concentration_warning: float = 0.40
    concentration_penalty: float = 15.0


@dataclass(frozen=True)
class AnalyticsConfig:
    grain: str = "week"
    #: nombre de periodes d'historique conservees dans les series
    lookback_periods: int = 12
    #: fenetre de moyenne mobile
    rolling_window: int = 4
    #: ecart tolere entre refunds Shopify et refunds Stripe avant alerte
    refund_reconciliation_tolerance: float = 0.01
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    health: HealthConfig = field(default_factory=HealthConfig)

    def __post_init__(self) -> None:
        if self.grain not in ("week", "month"):
            raise ConfigError(f"grain non supporte: {self.grain}")
        if self.lookback_periods < 2:
            raise ConfigError("lookback_periods doit valoir au moins 2")


# import tardif volontaire pour eviter un cycle a la lecture du module
from .errors import ConfigurationError as ConfigError  # noqa: E402
