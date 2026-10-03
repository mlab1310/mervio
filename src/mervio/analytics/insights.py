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
from .anomaly import Anomaly
from .kpi import Metric, product_performance
from .profitability import ProfitabilityResult
from .root_cause import RootCauseAnalysis

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

#: metriques qui sont la DECOMPOSITION du CA, pas des problemes independants.
#: Quand une baisse de CA est detectee, "commandes -28%" et "conversion -34%"
#: decrivent le meme evenement: les emettre separement produit 5 alarmes pour
#: un seul incident et noie la cause racine. Elles deviennent des preuves.
DECOMPOSITION_METRICS = {"orders", "aov", "orders_per_paid_click", "paid_clicks", "roas"}



@dataclass
class Insight:
    type: str
    severity: str  # high | medium | low | info
    category: str  # critical_issue | warning | opportunity
    fact: str
    evidence: List[str] = field(default_factory=list)
    hypothesis: Optional[str] = None
    recommendation: Optional[str] = None
    estimated_impact: Optional[float] = None
    estimated_impact_basis: Optional[str] = None
    confidence: Optional[Confidence] = None

    def to_dict(self) -> dict:
        return {
            "type": self.type, "severity": self.severity, "category": self.category,
            "fact": self.fact, "evidence": self.evidence, "hypothesis": self.hypothesis,
            "recommendation": self.recommendation,
            "estimated_impact": round(self.estimated_impact, 2) if self.estimated_impact is not None else None,
            "estimated_impact_basis": self.estimated_impact_basis,
            "confidence": self.confidence.to_dict() if self.confidence is not None else None,
        }


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
    insights: List[Insight] = []

    # 1. anomalie de CA + cause probable
    revenue_anomaly = next((a for a in anomalies if a.metric == "revenue"), None)
    if revenue_anomaly and revenue_anomaly.direction == "down":
        primary = root_cause.primary_factor() if root_cause and root_cause.available else None
        insight = Insight(
            type="revenue_decline", severity=revenue_anomaly.severity, category="critical_issue",
            fact=f"Le CA de {revenue_anomaly.period} est inferieur de "
                 f"{abs(revenue_anomaly.delta_pct or 0):.1%} a la moyenne des periodes precedentes.",
            evidence=list(revenue_anomaly.evidence),
            estimated_impact=revenue_anomaly.delta,
            estimated_impact_basis="ecart de CA observe vs baseline glissante, en devise",
            # AN-14: le constat HERITE la confiance du facteur primaire. Il repose
            # sur lui, donc il ne peut pas etre plus confiant que lui.
            confidence=primary.confidence if primary else None,
        )
        if primary:
            insight.evidence.append(primary.observed_fact)
            insight.evidence.append(
                f"contribution estimee a la variation: {primary.contribution:.0%}"
                if primary.contribution is not None else "contribution non calculable"
            )
            insight.hypothesis = primary.hypothesis
            insight.recommendation = _recommendation_for(primary.factor, root_cause)
        else:
            insight.hypothesis = "Cause non identifiee a partir des donnees disponibles."
            insight.recommendation = "Verifier manuellement trafic, checkout et disponibilite produit."
        insights.append(insight)

    # 2. autres anomalies, classees selon que l'ecart est favorable ou non
    revenue_insight = insights[0] if insights else None
    for anomaly in anomalies:
        if anomaly.metric == "revenue":
            continue
        # rattachement a l'insight de CA au lieu d'une alerte separee
        if revenue_insight is not None and anomaly.metric in DECOMPOSITION_METRICS:
            arrow = "+" if anomaly.direction == "up" else ""
            revenue_insight.evidence.append(
                f"{anomaly.metric}: {arrow}{anomaly.delta_pct:.1%} vs baseline"
                if anomaly.delta_pct is not None else f"{anomaly.metric}: ecart detecte"
            )
            continue
        if anomaly.assessment == "favorable":
            category, severity = "opportunity", "info"
            hypothesis = "Ecart favorable: comprendre ce qui l'a produit pour tenter de le reproduire."
            recommendation = f"Identifier ce qui explique l'amelioration de {anomaly.metric} avant qu'elle ne se dissipe."
        elif anomaly.assessment == "neutral":
            category, severity = "warning", "low"
            hypothesis = "Ecart inhabituel dont le signe n'est ni bon ni mauvais en soi."
            recommendation = f"Verifier que la variation de {anomaly.metric} est intentionnelle."
        else:
            category = "critical_issue" if anomaly.severity == "high" else "warning"
            severity = anomaly.severity
            hypothesis = "Variation statistiquement inhabituelle: la cause n'est pas etablie par ce moteur."
            recommendation = f"Verifier {anomaly.metric} dans la source avant toute decision."
        insights.append(Insight(
            type=f"{anomaly.metric}_anomaly", severity=severity, category=category,
            fact=f"{anomaly.metric} sur {anomaly.period}: {anomaly.delta_pct:+.1%} vs baseline."
                 if anomaly.delta_pct is not None else f"{anomaly.metric}: ecart detecte sur {anomaly.period}.",
            evidence=list(anomaly.evidence),
            hypothesis=hypothesis, recommendation=recommendation,
            estimated_impact=None, estimated_impact_basis=None,
            confidence=_kpi_confidence(dataset),
        ))

    # 3. profitabilite non calculable
    if not profitability.data_available:
        insights.append(Insight(
            type="profitability_unavailable", severity="medium", category="warning",
            fact="Le profit de contribution complet n'est pas calculable a partir des donnees fournies.",
            evidence=profitability.limitations,
            hypothesis=None,
            recommendation="Renseigner les couts manquants ("
                           + ", ".join(profitability.missing_components)
                           + ") pour obtenir une profitabilite reelle plutot qu'un CA.",
            estimated_impact=None, estimated_impact_basis=None,
            confidence=_kpi_confidence(dataset),
        ))

    # 4. produits a faible marge
    for product in product_performance(dataset):
        if product["gross_margin"] is not None and product["gross_margin"] < cfg.insights.low_product_margin and product["revenue"] > 0:
            insights.append(Insight(
                type="low_margin_product", severity="medium", category="warning",
                fact=f"{product['title']} ({product['sku']}) degage une marge brute de "
                     f"{product['gross_margin']:.1%}.",
                evidence=[f"CA periode: {product['revenue']:,.2f} {currency}",
                          f"cout marchandise: {product['cogs']:,.2f} {currency}",
                          f"unites vendues: {product['units']}"],
                hypothesis="Marge faible: prix, cout d'achat ou remises appliquees.",
                recommendation="Reevaluer le prix ou le cout d'achat de cette reference.",
                estimated_impact=None, estimated_impact_basis=None,
            confidence=_kpi_confidence(dataset),
            ))

    # 5. concentration client
    concentration = kpis.get("customer_concentration_top5")
    if concentration and concentration.available and concentration.value >= cfg.health.concentration_warning:
        insights.append(Insight(
            type="customer_concentration", severity="medium", category="warning",
            fact=f"Les 5 plus gros clients representent {concentration.value:.1%} du CA de la periode.",
            evidence=[concentration.definition],
            hypothesis="Dependance commerciale a un petit nombre de comptes.",
            recommendation="Diversifier l'acquisition pour reduire le risque de concentration.",
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
                fact=f"La campagne {best['name']} a augmente ses conversions de "
                     f"{best['conversions_delta']:.0f} sur la periode.",
                evidence=[f"depense: {best['spend_current']:,.2f} vs {best['spend_previous']:,.2f} {currency}",
                          f"conversions: {best['conversions_current']:.0f} vs {best['conversions_previous']:.0f}"],
                hypothesis="Cette campagne absorbe la demande mieux que les autres sur la periode.",
                recommendation="Tester une hausse de budget maitrisee sur cette campagne et mesurer le CPA.",
                estimated_impact=None,
                estimated_impact_basis="impact non chiffrable: aucune donnee de marge par campagne",
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


def _recommendation_for(factor: str, analysis: RootCauseAnalysis) -> str:
    """AN-02 / D-075: AUCUNE recommandation checkout tant que le trafic total n'est pas observe.

    La branche `conversion_rate` a disparu avec le facteur. Elle recommandait d'auditer le
    checkout des que << commandes totales / clics payants >> baissait -- ce qui arrive aussi quand
    le trafic organique chute, sans qu'aucun checkout soit en cause. La derivation des commandes
    non payantes depuis `ad_performance.conversions` est REJETEE en 004.5: la fenetre
    d'attribution de la regie n'est pas la date de commande Shopify.
    """
    if factor == "order_volume":
        worst = analysis.campaign_contributors[0] if analysis.campaign_contributors else None
        suffix = (f" La baisse de conversions est la plus marquee sur {worst['name']}."
                  if worst and worst["conversions_delta"] < 0 else "")
        return ("Chercher d'ou vient la demande manquante: le trafic total et la conversion du "
                "site ne sont pas observes, donc la cause n'est pas etablie. Connecter une mesure "
                "d'audience pour la trancher." + suffix)
    if factor == "paid_traffic":
        return ("Verifier budgets, enchares et taux d'impressions perdues dans Google Ads: "
                "la variation vient du volume de trafic achete.")
    if factor == "average_order_value":
        return ("Analyser le mix produit et le niveau de remise: la variation vient de la valeur "
                "moyenne des commandes, pas du volume.")
    return "Verifier le facteur identifie dans l'outil source avant toute decision."
