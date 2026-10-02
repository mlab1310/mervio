"""Configuration centralisee du moteur analytique.

Aucun seuil n'est code en dur ailleurs dans le moteur: tout ce qui influence
un score, une anomalie ou une severite passe par AnalyticsConfig, afin que la
logique reste auditable et que le SaaS puisse la parametrer par tenant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone, tzinfo
from typing import Dict, Tuple

ENGINE_VERSION = "0.1.0"


def resolve_timezone(name: str) -> tzinfo:
    """Identifiant IANA -> fuseau. Refuse, ne se rabat JAMAIS sur UTC.

    "UTC" est specialise vers `datetime.timezone.utc`, de la bibliotheque standard pure.
    Ce n'est pas une micro-optimisation: `zoneinfo.ZoneInfo("UTC")` resout contre `TZPATH`
    comme n'importe quelle autre cle, donc une image sans base de fuseaux ferait echouer
    jusqu'au fuseau PAR DEFAUT du moteur. Le defaut ne doit pas porter ce risque (C-3).

    Un repli silencieux sur UTC en cas de valeur invalide produirait des semaines fausses sans
    aucun signal: on leve.
    """
    if name == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError, OSError selon la cause
        raise ConfigError(f"fuseau horaire inconnu ou invalide: {name!r}") from exc

#: bareme de notation: suite de couples (valeur_seuil, score), seuils STRICTEMENT croissants.
#: `analytics.health.interpolate` suppose cet ordre; un bareme desordonne produirait des scores
#: silencieusement faux, d'ou la validation en `__post_init__`.
ThresholdScale = Tuple[Tuple[float, float], ...]


def _validate_scale(name: str, scale: ThresholdScale) -> None:
    if not scale:
        raise ConfigError(f"{name}: bareme vide")
    values = [point[0] for point in scale]
    if values != sorted(values) or len(set(values)) != len(values):
        raise ConfigError(f"{name}: seuils non strictement croissants: {values}")


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
class InsightsConfig:
    """Seuils des constats. AN-16: ils vivaient en dur dans `analytics/insights.py`."""

    #: marge brute en dessous de laquelle un produit est signale a faible marge
    low_product_margin: float = 0.20


@dataclass(frozen=True)
class ProfitabilityConfig:
    """AN-16: ce seuil vivait en dur, a DEUX endroits, dans `analytics/profitability.py`."""

    #: part du CA article dont le cout doit etre connu pour declarer le COGS COMPLET.
    #: En dessous, le COGS total n'est pas approxime: il est declare non calculable (D-047).
    complete_cogs_coverage: float = 0.999


@dataclass(frozen=True)
class HealthConfig:
    """Poids, baremes et seuils du Business Health Score.

    Les poids sont renormalises sur les seules dimensions calculables.

    AN-16: les cinq baremes etaient des constantes de module de `analytics/health.py`
    (`REVENUE_GROWTH_THRESHOLDS` et consorts), de meme que `MIN_PRODUCT_COVERAGE`.
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
    #: part de CA du top 5 clients au dela de laquelle on applique une penalite.
    #: Proprietaire UNIQUE de ce seuil: `analytics/insights.py` le dupliquait en dur.
    concentration_warning: float = 0.40
    concentration_penalty: float = 15.0
    #: couverture minimale du CA article pour que la marge produit represente le catalogue
    min_product_coverage: float = 0.80
    revenue_growth_thresholds: ThresholdScale = (
        (-0.25, 0.0), (-0.10, 40.0), (0.0, 70.0), (0.10, 100.0))
    profitability_thresholds: ThresholdScale = (
        (-0.05, 0.0), (0.0, 20.0), (0.05, 45.0), (0.15, 75.0), (0.25, 100.0))
    roas_thresholds: ThresholdScale = (
        (0.5, 0.0), (1.0, 25.0), (2.0, 55.0), (3.0, 80.0), (4.0, 100.0))
    repeat_thresholds: ThresholdScale = (
        (0.0, 10.0), (0.10, 45.0), (0.20, 75.0), (0.30, 100.0))
    product_margin_thresholds: ThresholdScale = (
        (0.10, 0.0), (0.25, 45.0), (0.40, 75.0), (0.55, 100.0))

    def __post_init__(self) -> None:
        for name in ("revenue_growth_thresholds", "profitability_thresholds", "roas_thresholds",
                     "repeat_thresholds", "product_margin_thresholds"):
            _validate_scale(name, getattr(self, name))


@dataclass(frozen=True)
class AnalyticsConfig:
    grain: str = "week"
    #: fuseau de DECOUPAGE des periodes (AN-06). Identifiant IANA.
    #: Defaut "UTC": seule valeur qui reproduit exactement le comportement anterieur, donc
    #: empreinte de configuration inchangee par defaut (A-16). Les instants restent stockes en
    #: UTC; seules les BORNES de periode sont calculees a partir de minuit LOCAL.
    timezone: str = "UTC"
    #: nombre de periodes d'historique conservees dans les series
    lookback_periods: int = 12
    #: fenetre de moyenne mobile
    rolling_window: int = 4
    #: ecart tolere entre refunds Shopify et refunds Stripe avant alerte
    refund_reconciliation_tolerance: float = 0.01
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    insights: InsightsConfig = field(default_factory=InsightsConfig)
    profitability: ProfitabilityConfig = field(default_factory=ProfitabilityConfig)

    def __post_init__(self) -> None:
        if self.grain not in ("week", "month"):
            raise ConfigError(f"grain non supporte: {self.grain}")
        if self.lookback_periods < 2:
            raise ConfigError("lookback_periods doit valoir au moins 2")
        # validation AU PLUS TOT, jamais au moment du decoupage
        resolve_timezone(self.timezone)


# import tardif volontaire pour eviter un cycle a la lecture du module
from .errors import ConfigurationError as ConfigError  # noqa: E402
