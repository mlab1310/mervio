"""Generation d'insights structures.

Contrat: FACT / EVIDENCE / HYPOTHESIS / RECOMMENDATION restent separes.
estimated_impact vaut null des que le chiffrage n'est pas calculable a partir
des donnees disponibles. Aucun impact financier n'est invente.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config import AnalyticsConfig
from .confidence import (
    NOT_APPLICABLE, Confidence, assess, sort_rank,
)
from .evidence import (
    NO_COMPARISON, VS_BASELINE, VS_PREVIOUS_PERIOD, Evidence, finding_id,
    not_observed, observed, to_dicts,
)
from .messages import FACTOR_RECOMMENDATION_KEY, Message
from .anomaly import Anomaly
from .kpi import Metric, product_performance
from .profitability import ProfitabilityResult
from .root_cause import RootCauseAnalysis

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

#: categories de constat, DANS L'ORDRE de lecture ratifie (AN-18 / D-084). `setup` n'est pas un
#: probleme de l'entreprise: c'est une donnee qui n'est pas branchee. Elle se lit apres ce qui va
#: mal et avant ce qu'il y a a saisir.
CATEGORIES = ("critical_issue", "warning", "setup", "opportunity")
#: liste du rapport portant chaque categorie. Le moteur possede cet ordre; le rapport s'y
#: conforme. Les consommateurs (`reporting/`, `llm/`) continuent de ne lire QUE le rapport.
SECTION_OF = {"critical_issue": "critical_issues", "warning": "warnings",
              "setup": "setup", "opportunity": "opportunities"}

#: metriques qui sont la DECOMPOSITION du CA, pas des problemes independants.
#: Quand une baisse de CA est detectee, "commandes -28%" et "conversion -34%"
#: decrivent le meme evenement: les emettre separement produit 5 alarmes pour
#: un seul incident et noie la cause racine. Elles deviennent des preuves.
DECOMPOSITION_METRICS = {"orders", "aov", "orders_per_paid_click", "paid_clicks", "roas"}



@dataclass
class Insight:
    type: str
    severity: str  # high | medium | low | info
    category: str  # critical_issue | warning | setup | opportunity
    #: AN-19 / D-085: un constat ne porte plus de prose, il porte une CLE et ses parametres.
    #: Le texte est derive (`Message.text`), donc il ne peut pas contredire sa cle.
    fact: Message
    #: precise QUEL objet le constat vise, quand le type peut se repeter (un SKU, une
    #: campagne). Sans lui, deux `low_margin_product` auraient le MEME identifiant.
    scope: Optional[str] = None
    evidence: List[Evidence] = field(default_factory=list)
    hypothesis: Optional[Message] = None
    recommendation: Optional[Message] = None
    estimated_impact: Optional[float] = None
    estimated_impact_basis: Optional[Message] = None
    confidence: Optional[Confidence] = None

    @property
    def finding_id(self) -> str:
        """Identite STABLE du constat (D-082). Elle vivait uniquement dans `llm/context.py`,
        frappee localement: le consommateur fabriquait l'identite de ce qu'il consommait."""
        return finding_id(self.type, self.scope)

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "type": self.type, "severity": self.severity, "category": self.category,
            **message_fields("fact", self.fact),
            "evidence": to_dicts(self.evidence),
            **message_fields("hypothesis", self.hypothesis),
            **message_fields("recommendation", self.recommendation),
            "estimated_impact": round(self.estimated_impact, 2) if self.estimated_impact is not None else None,
            **message_fields("estimated_impact_basis", self.estimated_impact_basis),
            "confidence": self.confidence.to_dict() if self.confidence is not None else None,
        }


def message_fields(name: str, message: Optional[Message]) -> Dict[str, object]:
    """Forme PLATE et ADDITIVE du contrat (D-3): `<name>`, `<name>_key`, `<name>_params`.

    Le champ texte EXISTANT garde son nom et son type, de sorte qu'un consommateur 1.x
    continue de le lire; la cle et les parametres s'ajoutent a cote. La cle est l'identite,
    le texte n'en est qu'une representation.
    """
    if message is None:
        return {name: None, f"{name}_key": None, f"{name}_params": None}
    return {name: message.text, f"{name}_key": message.key, f"{name}_params": dict(message.params)}


def build_insights(
    kpis: Dict[str, Metric],
    anomalies: List[Anomaly],
    root_cause: Optional[RootCauseAnalysis],
    profitability: ProfitabilityResult,
    dataset,
    currency: str = "EUR",
    config: Optional[AnalyticsConfig] = None,
) -> List[Insight]:
    # AN-16: deux seuils vivaient en dur ici. Celui de concentration DUPLIQUAIT
    # `HealthConfig.concentration_warning`: deux proprietaires pour une regle, donc une
    # divergence qui attend.
    cfg = config or AnalyticsConfig()
    # AN-15: chaque fait structure declare SA periode. Elle est deja portee par les
    # metriques: aucune nouvelle plomberie, et aucune invention de date.
    revenue_metric = kpis.get("revenue")
    period_label = revenue_metric.period if revenue_metric is not None else None
    insights: List[Insight] = []

    # 1. anomalie de CA + cause probable
    revenue_anomaly = next((a for a in anomalies if a.metric == "revenue"), None)
    if revenue_anomaly and revenue_anomaly.direction == "down":
        primary = root_cause.primary_factor() if root_cause and root_cause.available else None
        insight = Insight(
            type="revenue_decline", severity=revenue_anomaly.severity, category="critical_issue",
            fact=Message("finding.revenue_decline.fact",
                         {"period": revenue_anomaly.period,
                          # la valeur absolue est prise ICI, par le moteur: le rendu ne calcule rien
                          "delta_pct_abs": abs(revenue_anomaly.delta_pct or 0)}),
            evidence=list(revenue_anomaly.evidence),
            estimated_impact=revenue_anomaly.delta,
            estimated_impact_basis=Message("impact_basis.revenue_vs_baseline"),
            # AN-14: le constat HERITE la confiance du facteur primaire. Il repose
            # sur lui, donc il ne peut pas etre plus confiant que lui.
            confidence=primary.confidence if primary else None,
        )
        if primary:
            insight.evidence.append(observed(
                f"factor.{primary.factor}", "evidence.factor.change",
                metric_key=primary.factor, source="analytics", unit="ratio",
                period=root_cause.period, current=primary.factor_change_pct,
                comparison=VS_BASELINE,
            ) if primary.factor_change_pct is not None else not_observed(
                f"factor.{primary.factor}", "evidence.factor.not_computable",
                metric_key=primary.factor, source="analytics"))
            insight.evidence.append(observed(
                f"factor.{primary.factor}.contribution", "evidence.factor.contribution",
                metric_key="contribution", source="analytics", unit="ratio",
                period=root_cause.period, current=primary.contribution,
            ) if primary.contribution is not None else not_observed(
                f"factor.{primary.factor}.contribution",
                "evidence.factor.contribution_not_computable",
                metric_key="contribution", source="analytics"))
            insight.hypothesis = primary.hypothesis
            insight.recommendation = _recommendation_for(primary.factor, root_cause)
        else:
            insight.hypothesis = Message("finding.revenue_decline.hypothesis_not_identified")
            insight.recommendation = Message("finding.revenue_decline.recommendation_not_identified")
        insights.append(insight)

    # 2. autres anomalies, classees selon que l'ecart est favorable ou non
    revenue_insight = insights[0] if insights else None
    for anomaly in anomalies:
        if anomaly.metric == "revenue":
            continue
        # rattachement a l'insight de CA au lieu d'une alerte separee
        if revenue_insight is not None and anomaly.metric in DECOMPOSITION_METRICS:
            revenue_insight.evidence.append(observed(
                f"anomaly.{anomaly.metric}.delta_pct", "evidence.anomaly.delta_vs_baseline",
                metric_key=anomaly.metric, source="analytics", unit="ratio",
                period=anomaly.period, current=anomaly.delta_pct, comparison=VS_BASELINE,
            ) if anomaly.delta_pct is not None else not_observed(
                f"anomaly.{anomaly.metric}.delta_pct", "evidence.anomaly.delta_not_computable",
                metric_key=anomaly.metric, source="analytics"))
            continue
        # AN-19: le TYPE du constat reste derive de la metrique (`{metric}_anomaly`) -- c'est un
        # identifiant. La CLE DE MESSAGE, elle, est statique, et la metrique devient un parametre.
        if anomaly.assessment == "favorable":
            category, severity = "opportunity", "info"
            hypothesis = Message("finding.metric_anomaly.hypothesis_favorable")
            recommendation = Message("finding.metric_anomaly.recommendation_favorable",
                                     {"metric": anomaly.metric})
        elif anomaly.assessment == "neutral":
            category, severity = "warning", "low"
            hypothesis = Message("finding.metric_anomaly.hypothesis_neutral")
            recommendation = Message("finding.metric_anomaly.recommendation_neutral",
                                     {"metric": anomaly.metric})
        else:
            category = "critical_issue" if anomaly.severity == "high" else "warning"
            severity = anomaly.severity
            hypothesis = Message("finding.metric_anomaly.hypothesis_unfavorable")
            recommendation = Message("finding.metric_anomaly.recommendation_unfavorable",
                                     {"metric": anomaly.metric})
        insights.append(Insight(
            type=f"{anomaly.metric}_anomaly", severity=severity, category=category,
            fact=Message("finding.metric_anomaly.fact",
                         {"metric": anomaly.metric, "period": anomaly.period,
                          "delta_pct": anomaly.delta_pct})
            if anomaly.delta_pct is not None else
            Message("finding.metric_anomaly.fact_without_delta",
                    {"metric": anomaly.metric, "period": anomaly.period}),
            evidence=list(anomaly.evidence),
            hypothesis=hypothesis, recommendation=recommendation,
            estimated_impact=None, estimated_impact_basis=None,
            confidence=_kpi_confidence(dataset),
        ))

    # 3. profitabilite non calculable
    if not profitability.data_available:
        # AN-18 / D-084: `medium` + `warning` faisaient de ce constat une alerte business.
        # Il est emis par CONSTRUCTION, pas par exception: `REQUIRED_COMPONENTS` exige
        # `shipping_cost`, que `compute_profitability` pose a None sans condition (un export
        # Shopify porte le port FACTURE, jamais le cout transporteur). Aucune boutique
        # alimentee par CSV ne peut donc l'eviter: l'avertissement etait permanent, donc muet,
        # et il occupait la section ou le marchand cherche ce qui va mal cette semaine.
        # `info` + `setup` disent ce que c'est: une donnee a brancher, pas un probleme.
        insights.append(Insight(
            type="profitability_unavailable", severity="info", category="setup",
            fact=Message("finding.profitability_unavailable.fact"),
            # AN-15: les limitations etaient de la PROSE. Les faits, eux, sont les composantes
            # de cout manquantes -- deja typées sur `ProfitabilityResult`. Chacune devient un fait
            # NON OBSERVE: aucune valeur fabriquee, et la source manquante est nommee. Le texte
            # de limitation reste dans `limitations`, ou AN-19 le passera au catalogue.
            evidence=[
                not_observed(f"cost.{component.key}", "evidence.cost.not_observed",
                             metric_key=component.key, source=component.source)
                if not component.available else
                observed(f"cost.{component.key}", "evidence.cost.observed",
                         metric_key=component.key, source=component.source, unit="currency",
                         period=period_label, current=component.value)
                for component in profitability.components.values()
            ],
            hypothesis=None,
            recommendation=Message(
                "finding.profitability_unavailable.recommendation",
                {"components": ", ".join(profitability.missing_components)}),
            estimated_impact=None, estimated_impact_basis=None,
            confidence=_kpi_confidence(dataset),
        ))

    # 4. produits a faible marge
    for product in product_performance(dataset):
        if product["gross_margin"] is not None and product["gross_margin"] < cfg.insights.low_product_margin and product["revenue"] > 0:
            insights.append(Insight(
                type="low_margin_product", severity="medium", category="warning",
                fact=Message("finding.low_margin_product.fact",
                             {"title": product["title"], "sku": product["sku"],
                              "gross_margin": product["gross_margin"]}),
                scope=product["sku"],
                evidence=[
                    observed(f"product.{product['sku']}.revenue", "evidence.product.revenue",
                             metric_key="revenue", source="shopify_orders", unit="currency",
                             period=period_label, current=product["revenue"]),
                    observed(f"product.{product['sku']}.cogs", "evidence.product.cogs",
                             metric_key="cogs", source="shopify_products", unit="currency",
                             period=period_label, current=product["cogs"]),
                    observed(f"product.{product['sku']}.units", "evidence.product.units",
                             metric_key="units", source="shopify_orders", unit="count",
                             period=period_label, current=float(product["units"])),
                ],
                hypothesis=Message("finding.low_margin_product.hypothesis"),
                recommendation=Message("finding.low_margin_product.recommendation"),
                estimated_impact=None, estimated_impact_basis=None,
            confidence=_kpi_confidence(dataset),
            ))

    # 5. concentration client
    concentration = kpis.get("customer_concentration_top5")
    if concentration and concentration.available and concentration.value >= cfg.health.concentration_warning:
        insights.append(Insight(
            type="customer_concentration", severity="medium", category="warning",
            fact=Message("finding.customer_concentration.fact",
                         {"share": concentration.value}),
            evidence=[observed(
                "kpi.customer_concentration_top5", "evidence.kpi.value",
                metric_key="customer_concentration_top5", source="shopify_orders",
                unit="ratio", period=period_label, current=concentration.value)],
            hypothesis=Message("finding.customer_concentration.hypothesis"),
            recommendation=Message("finding.customer_concentration.recommendation"),
            estimated_impact=None, estimated_impact_basis=None,
            confidence=_kpi_confidence(dataset),
        ))

    # 6. opportunite: campagne la plus efficace
    if root_cause and root_cause.campaign_contributors:
        # cle TOTALE (F-01): a egalite de `conversions_delta`, `max` renverrait la premiere
        # rencontree, donc la campagne CITEE DANS LE CONSTAT dependrait de l'ordre d'entree.
        best = max(root_cause.campaign_contributors,
                   key=lambda c: (c["conversions_delta"], c["campaign_id"]))
        if best["conversions_delta"] > 0:
            insights.append(Insight(
                type="campaign_opportunity", severity="info", category="opportunity",
                fact=Message("finding.campaign_opportunity.fact",
                             {"name": best["name"],
                              "conversions_delta": best["conversions_delta"]}),
                scope=best["campaign_id"],
                evidence=[
                    observed(f"campaign.{best['campaign_id']}.spend",
                             "evidence.campaign.spend",
                             metric_key="campaign_spend", source="google_ads", unit="currency",
                             period=period_label, current=best["spend_current"],
                             previous=best["spend_previous"], comparison=VS_PREVIOUS_PERIOD),
                    observed(f"campaign.{best['campaign_id']}.conversions",
                             "evidence.campaign.conversions",
                             metric_key="campaign_conversions", source="google_ads", unit="count",
                             period=period_label, current=best["conversions_current"],
                             previous=best["conversions_previous"],
                             comparison=VS_PREVIOUS_PERIOD),
                ],
                hypothesis=Message("finding.campaign_opportunity.hypothesis"),
                recommendation=Message("finding.campaign_opportunity.recommendation"),
                estimated_impact=None,
                estimated_impact_basis=Message("impact_basis.campaign_not_quantifiable"),
                confidence=_kpi_confidence(dataset),
            ))

    # la baisse de CA porte la cause racine et la recommandation: elle passe
    # toujours en tete, quelle que soit la severite mecanique des autres ecarts
    # AN-14: passer de nombreux flottants a TROIS niveaux multiplie les egalites. La cle doit
    # donc rester TOTALE, sinon l'ordre des constats redeviendrait dependant de l'ordre
    # d'insertion -- la faute meme de F-01, dans un autre objet. L'ordre d'EMISSION est le
    # dernier recours: il est deterministe et ne depend d'aucun texte (donc immunise contre AN-19).
    ordered = sorted(enumerate(insights), key=lambda pair: (
        0 if pair[1].type == "revenue_decline" else 1,
        SEVERITY_ORDER.get(pair[1].severity, 9),
        -sort_rank(pair[1].confidence),
        pair[0],
    ))
    return [insight for _index, insight in ordered]


def _kpi_confidence(dataset) -> Confidence:
    """Confiance d'un constat adosse a UN SEUL KPI, pas a une decomposition.

    Les criteres de decomposition -- part expliquee, concurrent non observe -- sont NON
    APPLICABLES: ils n'ont pas d'objet ici, et un critere sans objet ne peut ni soutenir ni
    affaiblir la conclusion. Restent la longueur de baseline (sans objet egalement: le constat ne
    compare pas a une baseline) et la fiabilite des champs, qui, elle, s'applique.
    """
    from .root_cause import _fields_reliable
    reliable = _fields_reliable(dataset)
    confidence = assess(baseline_periods=0, min_baseline_periods=0, fields_reliable=reliable,
                        unobserved_competitor=None, contribution=None, share_applicable=False)
    return Confidence(confidence.level,
                      dict(confidence.criteria, baseline_sufficient=NOT_APPLICABLE))


def _recommendation_for(factor: str, analysis: RootCauseAnalysis) -> Message:
    """AN-02 / D-075: AUCUNE recommandation checkout tant que le trafic total n'est pas observe.

    La branche `conversion_rate` a disparu avec le facteur. Elle recommandait d'auditer le
    checkout des que << commandes totales / clics payants >> baissait -- ce qui arrive aussi quand
    le trafic organique chute, sans qu'aucun checkout soit en cause. La derivation des commandes
    non payantes depuis `ad_performance.conversions` est REJETEE en 004.5: la fenetre
    d'attribution de la regie n'est pas la date de commande Shopify.
    """
    if factor == "order_volume":
        worst = analysis.campaign_contributors[0] if analysis.campaign_contributors else None
        # AN-19: la variante avec campagne est une CLE A PART, pas une concatenation. Un suffixe
        # colle a une phrase ne serait traduisible, ni relisible, ni testable separement.
        if worst and worst["conversions_delta"] < 0:
            return Message("recommendation.factor.order_volume_with_campaign",
                           {"campaign": worst["name"]})
        return Message("recommendation.factor.order_volume")
    if factor in FACTOR_RECOMMENDATION_KEY:
        return Message(FACTOR_RECOMMENDATION_KEY[factor])
    return Message("recommendation.factor.fallback")
