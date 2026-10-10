"""Business Health Engine.

Le score est calcule par une logique explicite et documentee. AUCUN LLM ne
produit ni n'ajuste ce score.

Chaque dimension est notee 0-100 par interpolation lineaire entre des seuils
declares. Une dimension non calculable est EXCLUE et son poids redistribue;
la liste des dimensions exclues figure dans le rapport.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import HealthConfig
from ..domain.quality import DataQualityReport
from .evidence import (
    NO_COMPARISON, VS_PREVIOUS_PERIOD, Evidence, not_observed, observed, to_dicts,
)
from .kpi import Metric, product_performance
from .profitability import ProfitabilityResult
from .timeseries import Comparison

#: (valeur_seuil, score) tries par valeur croissante.
#: AN-16: les cinq baremes vivent desormais dans `HealthConfig`, qui valide leur monotonie.
Thresholds = Sequence[Tuple[float, float]]

#: composantes de cout sans lesquelles une "marge" n'a pas de sens en e-commerce
#: une marge qui ignore le cout d'achat ou des remboursements connus mais non ramenables a la base
#: du CA (D-047) recompenserait l'absence de donnee: la dimension est exclue, pas approximee
CRITICAL_COST_COMPONENTS = ("cogs", "refunds")


def _bands(thresholds: Optional[Thresholds]) -> Optional[List[List[float]]]:
    """Bareme en JSON: `[[seuil, score], ...]`.

    K-6: le champ publiait `str(cfg.revenue_growth_thresholds)`, soit
    `"((-0.25, 0.0), (-0.1, 40.0), ...)"` -- une valeur structuree figee dans du texte, dernier
    vestige du genre apres AN-15. Les valeurs viennent de `HealthConfig`, inchangees.
    """
    if thresholds is None:
        return None
    return [[float(threshold), float(score)] for threshold, score in thresholds]


def interpolate(value: float, thresholds: Thresholds) -> float:
    if value <= thresholds[0][0]:
        return thresholds[0][1]
    if value >= thresholds[-1][0]:
        return thresholds[-1][1]
    for (x0, y0), (x1, y1) in zip(thresholds, thresholds[1:]):
        if x0 <= value <= x1:
            span = x1 - x0
            return y0 if span == 0 else y0 + (value - x0) * (y1 - y0) / span
    return thresholds[-1][1]


@dataclass
class DimensionScore:
    key: str
    label: str
    score: Optional[float]
    weight: float
    available: bool
    formula: str
    #: K-6: bareme publie comme STRUCTURE, plus comme `repr()` Python. `None` pour une
    #: dimension sans bareme -- la qualite de donnee est une application LINEAIRE, et
    #: `HealthConfig` ne definit aucune echelle pour elle (AN-16: les seuils vivent dans la
    #: configuration, inventer une echelle ici lui donnerait un second proprietaire).
    thresholds: Optional[Thresholds]
    #: AN-15 / D-090 = A: le critere d'acceptation << aucun nombre dans une chaine de
    #: preuve >> ne distingue pas les producteurs, et `llm/context.py` passait ces
    #: chaines au modele COMME PREUVES. `health.py` est donc dans le perimetre.
    evidence: List[Evidence] = field(default_factory=list)
    reason_unavailable: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label,
            "score": round(self.score, 1) if self.score is not None else None,
            "weight": round(self.weight, 4), "available": self.available,
            "formula": self.formula, "thresholds": _bands(self.thresholds),
            "evidence": to_dicts(self.evidence), "reason_unavailable": self.reason_unavailable,
        }


@dataclass
class BusinessHealth:
    score: Optional[int]
    interpretation: str
    dimensions: List[DimensionScore]
    excluded_dimensions: List[str]
    weights_renormalised: bool
    methodology: str

    def to_dict(self) -> dict:
        return {
            "score": self.score, "interpretation": self.interpretation,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "excluded_dimensions": self.excluded_dimensions,
            "weights_renormalised": self.weights_renormalised,
            "methodology": self.methodology,
        }


def interpret(score: Optional[int]) -> str:
    if score is None:
        return "Score non calculable: aucune dimension exploitable."
    if score >= 80:
        return "Sante solide: pas de probleme structurel detecte sur les dimensions mesurees."
    if score >= 60:
        return "Sante correcte avec des points de vigilance identifies."
    if score >= 40:
        return "Sante degradee: au moins une dimension necessite une action rapide."
    return "Sante critique sur les dimensions mesurees: action immediate requise."


def compute_business_health(
    kpis: Dict[str, Metric],
    comparisons: Dict[str, Dict[str, Comparison]],
    profitability: ProfitabilityResult,
    dataset,
    quality: DataQualityReport,
    cfg: HealthConfig,
) -> BusinessHealth:
    dims: List[DimensionScore] = []
    w = cfg.weights

    # 1. croissance du CA
    growth = next((c for c in comparisons.get("revenue", {}).values() if c.available and c.pct_change is not None), None)
    if growth:
        dims.append(DimensionScore(
            "revenue_growth", "Croissance du CA", interpolate(growth.pct_change, cfg.revenue_growth_thresholds),
            w["revenue_growth"], True,
            "interpolation lineaire de la variation du CA vs periode precedente",
            cfg.revenue_growth_thresholds,
            [observed("health.revenue_growth.change", "evidence.health.revenue_growth",
                      metric_key="revenue", source="shopify_orders", unit="ratio",
                      period=growth.current_period, current=growth.pct_change,
                      comparison=VS_PREVIOUS_PERIOD)],
        ))
    else:
        dims.append(DimensionScore("revenue_growth", "Croissance du CA", None, w["revenue_growth"], False,
                                   "-", cfg.revenue_growth_thresholds, [],
                                   "aucune periode de comparaison exploitable"))

    # 2. profitabilite
    # Regle: une marge partielle amputee d'une composante MAJEURE (COGS) n'est pas
    # une marge. La noter reviendrait a recompenser l'absence de donnee. La
    # dimension est alors exclue, pas approximee.
    margin = profitability.contribution_margin
    blocking = [c for c in CRITICAL_COST_COMPONENTS if c in profitability.missing_components]
    if margin is not None:
        dims.append(DimensionScore(
            "profitability", "Profitabilite", interpolate(margin, cfg.profitability_thresholds),
            w["profitability"], True,
            "interpolation lineaire de la marge de contribution complete",
            cfg.profitability_thresholds,
            [observed("health.profitability.full_margin", "evidence.health.full_margin",
                      metric_key="contribution_profit", source="analytics", unit="ratio",
                      current=margin)],
        ))
    elif blocking:
        dims.append(DimensionScore(
            "profitability", "Profitabilite", None, w["profitability"], False,
            "-", cfg.profitability_thresholds,
            ([observed("health.profitability.partial_margin",
                       "evidence.health.partial_margin_not_scored",
                       metric_key="contribution_profit", source="analytics", unit="ratio",
                       current=profitability.partial_contribution_margin)]
             + [not_observed(f"cost.{key}", "evidence.cost.not_observed", metric_key=key,
                             source="analytics") for key in blocking]
             if profitability.partial_contribution_margin is not None else
             [not_observed(f"cost.{key}", "evidence.cost.not_observed", metric_key=key,
                           source="analytics") for key in blocking]),
            "composante de cout majeure manquante: " + ", ".join(blocking)
            + " - noter cette dimension surestimerait la sante reelle",
        ))
    elif profitability.partial_contribution_margin is not None:
        dims.append(DimensionScore(
            "profitability", "Profitabilite",
            interpolate(profitability.partial_contribution_margin, cfg.profitability_thresholds),
            w["profitability"], True,
            "interpolation lineaire de la marge de contribution partielle (couts majeurs presents)",
            cfg.profitability_thresholds,
            [observed("health.profitability.partial_margin", "evidence.health.partial_margin",
                      metric_key="contribution_profit", source="analytics", unit="ratio",
                      current=profitability.partial_contribution_margin)]
            + [not_observed(f"cost.{key}", "evidence.cost.not_observed", metric_key=key,
                            source="analytics") for key in profitability.missing_components],
        ))
    else:
        dims.append(DimensionScore("profitability", "Profitabilite", None, w["profitability"], False,
                                   "-", cfg.profitability_thresholds, [], "aucune composante de cout disponible"))

    # 3. efficacite marketing
    roas = kpis.get("roas")
    if roas and roas.available:
        dims.append(DimensionScore(
            "marketing_efficiency", "Efficacite marketing", interpolate(roas.value, cfg.roas_thresholds),
            w["marketing_efficiency"], True, "interpolation lineaire du ROAS global",
            cfg.roas_thresholds,
            [observed("health.marketing_efficiency.roas", "evidence.kpi.value",
                      metric_key="roas", source="google_ads", unit="ratio",
                      current=roas.value)],
        ))
    else:
        dims.append(DimensionScore("marketing_efficiency", "Efficacite marketing", None,
                                   w["marketing_efficiency"], False, "-", cfg.roas_thresholds, [],
                                   "aucune depense publicitaire connue sur la periode"))

    # 4. sante client
    repeat = kpis.get("repeat_purchase_rate")
    concentration = kpis.get("customer_concentration_top5")
    if repeat and repeat.available:
        base = interpolate(repeat.value, cfg.repeat_thresholds)
        evidence = [observed("health.customer_health.repeat_rate", "evidence.kpi.value",
                             metric_key="repeat_purchase_rate", source="shopify_orders",
                             unit="ratio", current=repeat.value)]
        if concentration and concentration.available and concentration.value >= cfg.concentration_warning:
            base = max(0.0, base - cfg.concentration_penalty)
            evidence.append(observed(
                "health.customer_health.concentration", "evidence.health.concentration_penalty",
                metric_key="customer_concentration_top5", source="shopify_orders", unit="ratio",
                current=concentration.value, previous=cfg.concentration_warning,
                comparison=NO_COMPARISON))
        dims.append(DimensionScore("customer_health", "Sante client", base, w["customer_health"], True,
                                   "interpolation du taux de reachat, moins une penalite de concentration",
                                   cfg.repeat_thresholds, evidence))
    else:
        dims.append(DimensionScore("customer_health", "Sante client", None, w["customer_health"], False,
                                   "-", cfg.repeat_thresholds, [], "aucun client identifie sur la periode"))

    # 5. sante produit
    products = product_performance(dataset)
    with_margin = [p for p in products if p["gross_margin"] is not None]
    if with_margin:
        covered_revenue = sum(p["revenue"] for p in with_margin)
        weighted = sum(p["gross_margin"] * p["revenue"] for p in with_margin) / covered_revenue
        total_revenue_all = sum(p["revenue"] for p in products) or 1.0
        coverage = covered_revenue / total_revenue_all
        evidence = [
            observed("health.product_health.weighted_margin", "evidence.kpi.value",
                     metric_key="product_gross_margin", source="shopify_products", unit="ratio",
                     current=weighted),
            observed("health.product_health.coverage", "evidence.health.cost_coverage",
                     metric_key="data_quality_coverage", source="shopify_products", unit="ratio",
                     current=coverage),
        ]
        if coverage < cfg.min_product_coverage:
            # une marge calculee sur une minorite du catalogue ne represente pas
            # le catalogue: on n'extrapole pas, on exclut.
            dims.append(DimensionScore(
                "product_health", "Sante produit", None, w["product_health"], False,
                "-", cfg.product_margin_thresholds, evidence,
                f"couverture cout insuffisante ({coverage:.0%} < {cfg.min_product_coverage:.0%} du CA article)",
            ))
        else:
            dims.append(DimensionScore(
                "product_health", "Sante produit", interpolate(weighted, cfg.product_margin_thresholds),
                w["product_health"], True,
                "marge brute ponderee par le CA des produits dont le cout est connu",
                cfg.product_margin_thresholds, evidence,
            ))
    else:
        dims.append(DimensionScore("product_health", "Sante produit", None, w["product_health"], False,
                                   "-", cfg.product_margin_thresholds, [],
                                   "aucun cout produit renseigne: marge produit non calculable"))

    # 6. qualite de donnee
    ratio = quality.reliable_ratio()
    if ratio is not None:
        dims.append(DimensionScore("data_quality", "Qualite de la donnee", ratio * 100.0,
                                   w["data_quality"], True,
                                   "part des champs suivis classes 'reliable'", None,
                                   [observed("health.data_quality.reliable_share",
                                             "evidence.health.reliable_fields",
                                             metric_key="data_quality_coverage",
                                             source="analytics", unit="ratio", current=ratio)]))
    else:
        dims.append(DimensionScore("data_quality", "Qualite de la donnee", None, w["data_quality"], False,
                                   "-", None, [], "aucun champ suivi"))

    available = [d for d in dims if d.available and d.score is not None]
    excluded = [d.key for d in dims if not d.available]
    if not available:
        return BusinessHealth(None, interpret(None), dims, excluded, False, _METHODOLOGY)
    total_weight = sum(d.weight for d in available)
    score = round(sum(d.score * d.weight for d in available) / total_weight)
    return BusinessHealth(int(score), interpret(int(score)), dims, excluded, bool(excluded), _METHODOLOGY)


_METHODOLOGY = (
    "Score = somme ponderee des dimensions calculables, renormalisee sur la somme de leurs poids. "
    "Chaque dimension est notee 0-100 par interpolation lineaire entre seuils declares. "
    "Les dimensions non calculables sont exclues et listees. Aucun LLM n'intervient dans ce calcul."
)
