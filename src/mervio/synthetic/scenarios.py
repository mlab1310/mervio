"""Scenarios metier deterministes et verite terrain.

Chaque scenario decrit:
- `baseline`: le fonctionnement normal de la boutique;
- `injected`: la condition injectee et sa fenetre;
- `ground_truth`: ce qui s'est reellement passe (cause, direction, categorie de
  recommandation). C'est la verite du generateur, pas une sortie du moteur;
- `expectations`: ce que le moteur ACTUEL doit observer. `MATCH` = le moteur
  doit retrouver la valeur; `KNOWN_GAP` = la valeur est la verite terrain que
  le moteur ne sait pas encore retrouver (angle mort documente). Si le moteur
  progresse et la retrouve, le test echoue volontairement: le scenario doit
  alors etre mis a jour, pas le test affaibli.

Fenetres: `last_week` = les 7 derniers jours (semaine ISO close analysee par le
moteur); `ramp` = effet croissant lineairement sur tout l'horizon; `always` =
effet constant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Effects:
    window: str = "last_week"
    paid_traffic: float = 1.0
    organic_traffic: float = 1.0
    cvr: float = 1.0
    campaign_cvr: Tuple[Tuple[str, float], ...] = ()      # (nom de campagne, multiplicateur)
    single_line_shift: float = 0.0                        # 0..1: bascule des paniers multi-articles vers 1 article
    cheap_mix: float = 0.0                                # >0: demande deplacee vers les articles moins chers
    discount_probability: Optional[float] = None
    discount_rate: Optional[float] = None
    refund_probability_multiplier: float = 1.0
    product_demand: Tuple[Tuple[int, float], ...] = ()    # (rang produit par poids, multiplicateur)
    product_refund_multiplier: Tuple[Tuple[int, float], ...] = ()
    stockout_products: Tuple[int, ...] = ()               # rangs produits indisponibles
    cost_multiplier: float = 1.0                          # cout unitaire (instantane de l'export produits)
    returning_share_multiplier: float = 1.0
    seasonal_amplitude: Optional[float] = None
    seasonal_peak_day: int = 350                          # jour de l'annee du pic saisonnier
    late_delivery_share: Optional[float] = None

    def __post_init__(self) -> None:
        if self.window not in ("last_week", "ramp", "always"):
            raise ValueError(f"fenetre inconnue: {self.window}")


@dataclass(frozen=True)
class Expectation:
    check: str
    expected: Any
    status: str = "MATCH"          # MATCH | KNOWN_GAP
    note: str = ""


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    baseline: str
    injected: str
    effects: Effects
    ground_truth: Dict[str, Any]
    expectations: List[Expectation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scenario": self.name, "baseline": self.baseline, "injected": self.injected,
            "window": self.effects.window, "ground_truth": self.ground_truth,
            "engine_expectations": [e.__dict__ for e in self.expectations],
        }


BASELINE = ("boutique stable: saisonnalite hebdomadaire et annuelle faible, tendance +6 %/an, "
            "trafic paye et organique constants, remises et remboursements a leur taux habituel")

_ORGANIC_GAP = "le moteur ne mesure que les clics Google Ads: une baisse organique apparait comme une baisse de conversion"
_CUSTOMER_GAP = "le moteur n'a ni attrition ni cohorte: la perte de clients recurrents apparait comme une baisse de conversion"


def _gt(primary: str, revenue: str, orders: str, category: str, secondary: Optional[List[str]] = None, **extra) -> Dict[str, Any]:
    return {"primary_root_cause": primary, "revenue_direction": revenue, "orders_direction": orders,
            "possible_secondary_causes": secondary or [], "recommendation_category": category, **extra}


SCENARIOS: Dict[str, ScenarioSpec] = {s.name: s for s in [
    ScenarioSpec(
        "healthy_store", BASELINE, "aucune condition injectee",
        Effects(window="always"),
        _gt("none", "flat", "flat", "none"),
        [Expectation("revenue_wow", "flat"), Expectation("no_anomaly", "revenue"),
         Expectation("no_anomaly", "orders"), Expectation("no_critical_issue", True)],
    ),
    ScenarioSpec(
        "revenue_drop", BASELINE, "budget Google Ads coupe de 40 % sur la derniere semaine",
        Effects(paid_traffic=0.60),
        _gt("order_volume_decline", "down", "down", "acquisition", ["paid_traffic_decline"], mechanism="paid_budget_cut"),
        [Expectation("revenue_wow", "down"), Expectation("orders_wow", "down"),
         Expectation("anomaly", "revenue:down"), Expectation("primary_factor", "paid_traffic"),
         Expectation("recommendation_category", "acquisition")],
    ),
    ScenarioSpec(
        "order_volume_drop", BASELINE, "trafic organique -60 % (referencement), trafic paye inchange, derniere semaine",
        Effects(organic_traffic=0.40),
        _gt("organic_traffic_decline", "down", "down", "acquisition", ["order_volume_decline"]),
        [Expectation("revenue_wow", "down"), Expectation("orders_wow", "down"),
         Expectation("anomaly", "orders:down"),
         Expectation("primary_factor", "organic_traffic", "KNOWN_GAP", _ORGANIC_GAP)],
    ),
    ScenarioSpec(
        "AOV_decline", BASELINE, "paniers mono-article et mix vers les articles bon marche, derniere semaine",
        Effects(single_line_shift=0.9, cheap_mix=2.5),
        _gt("aov_decline", "down", "flat", "pricing_merchandising"),
        [Expectation("aov_wow", "down"), Expectation("orders_wow", "flat"),
         Expectation("primary_factor", "average_order_value"),
         Expectation("recommendation_category", "pricing_merchandising")],
    ),
    ScenarioSpec(
        "discount_explosion", BASELINE, "80 % des commandes remisees a 35 %, derniere semaine",
        Effects(discount_probability=0.80, discount_rate=0.35),
        _gt("discount_increase", "down", "flat", "discount_policy", ["aov_decline", "margin_erosion"]),
        [Expectation("aov_wow", "down"), Expectation("orders_wow", "flat"),
         Expectation("primary_factor", "average_order_value"),
         Expectation("recommendation_category", "discount_policy", "KNOWN_GAP",
                     "le moteur ne suit pas le taux de remise: il recommande sur le panier moyen")],
    ),
    ScenarioSpec(
        "margin_collapse", BASELINE, "cout unitaire de tous les produits x2,4 (instantane de l'export produits)",
        Effects(window="always", cost_multiplier=2.4),
        _gt("cost_increase", "flat", "flat", "cost_supplier"),
        [Expectation("revenue_wow", "flat"), Expectation("kpi_below", ["gross_margin", 0.20]),
         Expectation("insight_present", "low_margin_product"),
         Expectation("margin_trend", "down", "KNOWN_GAP",
                     "l'export produits ne porte qu'un cout courant: aucune evolution de marge n'est observable")],
    ),
    ScenarioSpec(
        "product_failure", BASELINE, "produit n°1 du CA: 90 % des demandes abandonnees et remboursements x8, derniere semaine",
        Effects(product_demand=((0, 0.10),), product_refund_multiplier=((0, 8.0),)),
        _gt("product_demand_collapse", "down", "down", "product_quality", ["refund_increase"]),
        [Expectation("revenue_wow", "down"), Expectation("top_product_contributor", [0]),
         Expectation("primary_factor", "product_demand_collapse", "KNOWN_GAP",
                     "la decomposition du moteur ne connait que trafic, conversion et panier moyen")],
    ),
    ScenarioSpec(
        "customer_churn", BASELINE, "les clients recurrents cessent d'acheter (part x0,05), derniere semaine",
        Effects(returning_share_multiplier=0.05, organic_traffic=0.55),
        _gt("customer_churn", "down", "down", "retention_crm", ["organic_traffic_decline"]),
        [Expectation("orders_wow", "down"), Expectation("new_customer_share_above", 0.90),
         Expectation("primary_factor", "customer_churn", "KNOWN_GAP", _CUSTOMER_GAP)],
    ),
    ScenarioSpec(
        "retention_decline", BASELINE, "part des clients recurrents divisee par 10 progressivement sur tout l'horizon",
        Effects(window="ramp", returning_share_multiplier=0.10),
        _gt("retention_decline", "flat", "flat", "retention_crm"),
        [Expectation("no_anomaly", "revenue"),
         Expectation("retention_trend", "down", "KNOWN_GAP",
                     "taux de reachat intra-periode uniquement: aucune tendance de retention n'est calculee")],
    ),
    ScenarioSpec(
        "marketing_campaign_failure", BASELINE, "campagne 'Shopping' : conversion x0,05 a depense constante, derniere semaine",
        Effects(campaign_cvr=(("Shopping", 0.05),)),
        _gt("campaign_failure", "down", "down", "campaign_optimization", ["conversion_decline"]),
        [Expectation("revenue_wow", "down"), Expectation("top_campaign_decline", "Shopping"),
         Expectation("primary_factor", "conversion_rate"),
         Expectation("recommendation_category", "campaign_optimization", "KNOWN_GAP",
                     "la recommandation du moteur vise le tunnel de conversion; la campagne n'est citee qu'en detail")],
    ),
    ScenarioSpec(
        "traffic_drop", BASELINE, "trafic paye et organique -35 %, conversion inchangee, derniere semaine",
        Effects(paid_traffic=0.65, organic_traffic=0.65),
        _gt("traffic_decline", "down", "down", "acquisition"),
        [Expectation("revenue_wow", "down"), Expectation("orders_wow", "down"),
         Expectation("anomaly", "paid_clicks:down"), Expectation("primary_factor", "paid_traffic"),
         Expectation("recommendation_category", "acquisition")],
    ),
    ScenarioSpec(
        "conversion_drop", BASELINE, "conversion -40 % sur tous les canaux, trafic inchange, derniere semaine",
        Effects(cvr=0.60),
        _gt("conversion_decline", "down", "down", "conversion_checkout"),
        [Expectation("revenue_wow", "down"), Expectation("anomaly", "paid_conversion_rate:down"),
         Expectation("primary_factor", "conversion_rate"),
         Expectation("recommendation_category", "conversion_checkout")],
    ),
    ScenarioSpec(
        "refund_spike", BASELINE, "probabilite de remboursement x6, derniere semaine",
        Effects(refund_probability_multiplier=6.0),
        _gt("refund_increase", "flat", "flat", "returns_quality"),
        [Expectation("no_anomaly", "revenue"), Expectation("anomaly", "refunds:up"),
         Expectation("kpi_above", ["refund_rate", 0.08])],
    ),
    ScenarioSpec(
        "seasonal_business", BASELINE, "saisonnalite forte (amplitude 0,8, pic mi-juillet): decrue saisonniere normale en septembre, aucun incident",
        Effects(window="always", seasonal_amplitude=0.8, seasonal_peak_day=196),
        _gt("seasonality", "down", "down", "seasonal_planning", engine_coverage="gap_exposure"),
        [Expectation("no_anomaly", "revenue", "KNOWN_GAP",
                     "aucun modele saisonnier: la decrue normale est comparee a une moyenne glissante haute et signalee"),
         Expectation("seasonality_awareness", "seasonal", "KNOWN_GAP",
                     "le moteur n'a pas de saisonnalite (limite documentee de PROJECT_STATE)")],
    ),
    ScenarioSpec(
        "stockout", BASELINE, "produits n°1 et n°2 du CA en rupture (substitution partielle), derniere semaine",
        Effects(stockout_products=(0, 1)),
        _gt("stockout", "down", "down", "inventory_replenishment", ["conversion_decline"]),
        [Expectation("revenue_wow", "down"), Expectation("top_product_contributor", [0, 1]),
         Expectation("primary_factor", "stockout", "KNOWN_GAP",
                     "aucune donnee de stock ingeree: la rupture est lue comme une baisse de panier moyen ou de "
                     "conversion selon la part des paniers multi-articles"),
         Expectation("recommendation_category", "inventory_replenishment", "KNOWN_GAP",
                     "aucune recommandation de reapprovisionnement n'existe dans le moteur")],
    ),
    ScenarioSpec(
        "shipping_problem", BASELINE, "60 % de livraisons en retard, remboursements x4, conversion -10 %, derniere semaine",
        Effects(late_delivery_share=0.60, refund_probability_multiplier=4.0, cvr=0.90),
        _gt("fulfillment_delay", "down", "down", "fulfillment_logistics", ["refund_increase"]),
        [Expectation("anomaly", "refunds:up"),
         Expectation("primary_factor", "fulfillment_delay", "KNOWN_GAP",
                     "aucune donnee d'expedition ingeree par le moteur")],
    ),
    ScenarioSpec(
        "mixed_root_causes", BASELINE, "trafic paye -35 %, panier moyen en legere baisse et remboursements x4, derniere semaine",
        Effects(paid_traffic=0.65, single_line_shift=0.35, cheap_mix=0.5, refund_probability_multiplier=4.0),
        _gt("mixed", "down", "down", "acquisition", ["paid_traffic_decline", "aov_decline", "refund_increase"]),
        [Expectation("revenue_wow", "down"), Expectation("primary_factor", "paid_traffic"),
         Expectation("secondary_factor", "average_order_value"), Expectation("anomaly", "refunds:up")],
    ),
]}


def get_scenario(name: str) -> ScenarioSpec:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise ValueError(f"scenario inconnu: {name} (connus: {', '.join(sorted(SCENARIOS))})") from None
