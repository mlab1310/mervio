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
from .kpi import Metric, product_performance
from .profitability import ProfitabilityResult
from .timeseries import Comparison

#: (valeur_seuil, score) tries par valeur croissante
Thresholds = Sequence[Tuple[float, float]]

REVENUE_GROWTH_THRESHOLDS: Thresholds = ((-0.25, 0.0), (-0.10, 40.0), (0.0, 70.0), (0.10, 100.0))
PROFITABILITY_THRESHOLDS: Thresholds = ((-0.05, 0.0), (0.0, 20.0), (0.05, 45.0), (0.15, 75.0), (0.25, 100.0))
ROAS_THRESHOLDS: Thresholds = ((0.5, 0.0), (1.0, 25.0), (2.0, 55.0), (3.0, 80.0), (4.0, 100.0))
REPEAT_THRESHOLDS: Thresholds = ((0.0, 10.0), (0.10, 45.0), (0.20, 75.0), (0.30, 100.0))
PRODUCT_MARGIN_THRESHOLDS: Thresholds = ((0.10, 0.0), (0.25, 45.0), (0.40, 75.0), (0.55, 100.0))

#: composantes de cout sans lesquelles une "marge" n'a pas de sens en e-commerce
CRITICAL_COST_COMPONENTS = ("cogs",)
#: couverture minimale du CA article pour que la marge produit represente le catalogue
MIN_PRODUCT_COVERAGE = 0.80


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
    thresholds: str
    evidence: List[str] = field(default_factory=list)
    reason_unavailable: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label,
            "score": round(self.score, 1) if self.score is not None else None,
            "weight": round(self.weight, 4), "available": self.available,
            "formula": self.formula, "thresholds": self.thresholds,
            "evidence": self.evidence, "reason_unavailable": self.reason_unavailable,
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
            "revenue_growth", "Croissance du CA", interpolate(growth.pct_change, REVENUE_GROWTH_THRESHOLDS),
            w["revenue_growth"], True,
            "interpolation lineaire de la variation du CA vs periode precedente",
            str(REVENUE_GROWTH_THRESHOLDS),
            [f"CA {growth.pct_change:+.1%} ({growth.current_period} vs {growth.previous_period})"],
        ))
    else:
        dims.append(DimensionScore("revenue_growth", "Croissance du CA", None, w["revenue_growth"], False,
                                   "-", str(REVENUE_GROWTH_THRESHOLDS), [],
                                   "aucune periode de comparaison exploitable"))

    # 2. profitabilite
    # Regle: une marge partielle amputee d'une composante MAJEURE (COGS) n'est pas
    # une marge. La noter reviendrait a recompenser l'absence de donnee. La
    # dimension est alors exclue, pas approximee.
    margin = profitability.contribution_margin
    blocking = [c for c in CRITICAL_COST_COMPONENTS if c in profitability.missing_components]
    if margin is not None:
        dims.append(DimensionScore(
            "profitability", "Profitabilite", interpolate(margin, PROFITABILITY_THRESHOLDS),
            w["profitability"], True,
            "interpolation lineaire de la marge de contribution complete",
            str(PROFITABILITY_THRESHOLDS),
            [f"marge de contribution complete: {margin:.1%}"],
        ))
    elif blocking:
        dims.append(DimensionScore(
            "profitability", "Profitabilite", None, w["profitability"], False,
            "-", str(PROFITABILITY_THRESHOLDS),
            ([f"marge partielle observee: {profitability.partial_contribution_margin:.1%} "
              f"(non notee: elle exclut {', '.join(blocking)})"]
             if profitability.partial_contribution_margin is not None else []),
            "composante de cout majeure manquante: " + ", ".join(blocking)
            + " - noter cette dimension surestimerait la sante reelle",
        ))
    elif profitability.partial_contribution_margin is not None:
        dims.append(DimensionScore(
            "profitability", "Profitabilite",
            interpolate(profitability.partial_contribution_margin, PROFITABILITY_THRESHOLDS),
            w["profitability"], True,
            "interpolation lineaire de la marge de contribution partielle (couts majeurs presents)",
            str(PROFITABILITY_THRESHOLDS),
            [f"marge de contribution PARTIELLE: {profitability.partial_contribution_margin:.1%}",
             "couts exclus (mineurs): " + ", ".join(profitability.missing_components)],
        ))
    else:
        dims.append(DimensionScore("profitability", "Profitabilite", None, w["profitability"], False,
                                   "-", str(PROFITABILITY_THRESHOLDS), [], "aucune composante de cout disponible"))

    # 3. efficacite marketing
    roas = kpis.get("roas")
    if roas and roas.available:
        dims.append(DimensionScore(
            "marketing_efficiency", "Efficacite marketing", interpolate(roas.value, ROAS_THRESHOLDS),
            w["marketing_efficiency"], True, "interpolation lineaire du ROAS global",
            str(ROAS_THRESHOLDS), [f"ROAS {roas.value:.2f}"],
        ))
    else:
        dims.append(DimensionScore("marketing_efficiency", "Efficacite marketing", None,
                                   w["marketing_efficiency"], False, "-", str(ROAS_THRESHOLDS), [],
                                   "aucune depense publicitaire connue sur la periode"))

    # 4. sante client
    repeat = kpis.get("repeat_purchase_rate")
    concentration = kpis.get("customer_concentration_top5")
    if repeat and repeat.available:
        base = interpolate(repeat.value, REPEAT_THRESHOLDS)
        evidence = [f"taux de reachat intra-periode {repeat.value:.1%}"]
        if concentration and concentration.available and concentration.value >= cfg.concentration_warning:
            base = max(0.0, base - cfg.concentration_penalty)
            evidence.append(
                f"penalite de concentration: top 5 clients = {concentration.value:.1%} du CA "
                f"(seuil {cfg.concentration_warning:.0%}, -{cfg.concentration_penalty:.0f} pts)"
            )
        dims.append(DimensionScore("customer_health", "Sante client", base, w["customer_health"], True,
                                   "interpolation du taux de reachat, moins une penalite de concentration",
                                   str(REPEAT_THRESHOLDS), evidence))
    else:
        dims.append(DimensionScore("customer_health", "Sante client", None, w["customer_health"], False,
                                   "-", str(REPEAT_THRESHOLDS), [], "aucun client identifie sur la periode"))

    # 5. sante produit
    products = product_performance(dataset)
    with_margin = [p for p in products if p["gross_margin"] is not None]
    if with_margin:
        covered_revenue = sum(p["revenue"] for p in with_margin)
        weighted = sum(p["gross_margin"] * p["revenue"] for p in with_margin) / covered_revenue
        total_revenue_all = sum(p["revenue"] for p in products) or 1.0
        coverage = covered_revenue / total_revenue_all
        evidence = [f"marge brute ponderee {weighted:.1%}",
                    f"couverture cout: {coverage:.0%} du CA article"]
        if coverage < MIN_PRODUCT_COVERAGE:
            # une marge calculee sur une minorite du catalogue ne represente pas
            # le catalogue: on n'extrapole pas, on exclut.
            dims.append(DimensionScore(
                "product_health", "Sante produit", None, w["product_health"], False,
                "-", str(PRODUCT_MARGIN_THRESHOLDS), evidence,
                f"couverture cout insuffisante ({coverage:.0%} < {MIN_PRODUCT_COVERAGE:.0%} du CA article)",
            ))
        else:
            dims.append(DimensionScore(
                "product_health", "Sante produit", interpolate(weighted, PRODUCT_MARGIN_THRESHOLDS),
                w["product_health"], True,
                "marge brute ponderee par le CA des produits dont le cout est connu",
                str(PRODUCT_MARGIN_THRESHOLDS), evidence,
            ))
    else:
        dims.append(DimensionScore("product_health", "Sante produit", None, w["product_health"], False,
                                   "-", str(PRODUCT_MARGIN_THRESHOLDS), [],
                                   "aucun cout produit renseigne: marge produit non calculable"))

    # 6. qualite de donnee
    ratio = quality.reliable_ratio()
    if ratio is not None:
        dims.append(DimensionScore("data_quality", "Qualite de la donnee", ratio * 100.0,
                                   w["data_quality"], True,
                                   "part des champs suivis classes 'reliable'", "lineaire 0-100",
                                   [f"{ratio:.0%} des champs suivis sont fiables"]))
    else:
        dims.append(DimensionScore("data_quality", "Qualite de la donnee", None, w["data_quality"], False,
                                   "-", "lineaire 0-100", [], "aucun champ suivi"))

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
