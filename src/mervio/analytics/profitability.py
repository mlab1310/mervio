"""Moteur de profitabilite.

Revenue != Profit. Si une composante de cout manque, le profit de contribution
complet est declare INDISPONIBLE. Un profit partiel est expose separement, avec
la liste explicite des couts inclus et exclus. Aucune valeur n'est inventee.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..domain.models import Dataset
from .kpi import known_cogs, total_ad_spend, total_payment_fees, total_revenue

#: composantes requises pour un profit de contribution COMPLET
REQUIRED_COMPONENTS = ("cogs", "payment_fees", "advertising", "refunds", "shipping_cost")

#: arrondi independant des composantes du Total (meme tolerance que le connecteur Shopify)
_ROUNDING = 0.02


def refunds_on_revenue_basis(ds: Dataset) -> Tuple[Optional[float], int, float, float]:
    """Part produit des remboursements Shopify, sur la base du CA (Subtotal) (D-047).

    `Refunded Amount` peut inclure port et taxes; le CA non. L'export ne ventile
    pas le remboursement, mais chaque part est plafonnee par ce qui a ete facture:
    part produit dans [max(0, rembourse - (Total - Subtotal)), min(rembourse, Subtotal)].
    Quand ces bornes se rejoignent (aucun remboursement, remboursement integral,
    commande sans port ni taxes, commande a Subtotal nul), la part produit est
    connue. Sinon elle est indeterminable: rien n'est suppose.

    Retourne (valeur ou None, remboursements indetermines, borne basse, borne haute).
    """
    orders = {o.order_id: o for o in ds.orders}
    low_sum = high_sum = 0.0
    undetermined = 0
    for refund in ds.refunds:
        if refund.source != "shopify":
            continue
        order = orders.get(refund.order_id)
        if (order is None or order.subtotal < 0 or order.total + _ROUNDING < order.subtotal
                or refund.amount > order.total + _ROUNDING):
            # commande introuvable ou montants incoherents (Total absent compris): seule borne sure
            undetermined += 1
            high_sum += refund.amount
            continue
        low = max(0.0, refund.amount - (order.total - order.subtotal))
        high = min(refund.amount, order.subtotal)
        if high - low > _ROUNDING:
            undetermined += 1
        low_sum += min(low, high)
        high_sum += high
    return (None if undetermined else high_sum), undetermined, low_sum, high_sum


@dataclass
class CostComponent:
    key: str
    label: str
    value: Optional[float]
    source: str
    note: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label,
            "value": round(self.value, 2) if self.value is not None else None,
            "available": self.available, "source": self.source, "note": self.note,
        }


@dataclass
class ProfitabilityResult:
    period: str
    revenue: float
    components: Dict[str, CostComponent] = field(default_factory=dict)
    data_available: bool = False
    contribution_profit: Optional[float] = None
    contribution_margin: Optional[float] = None
    partial_contribution_profit: Optional[float] = None
    partial_contribution_margin: Optional[float] = None
    included_components: List[str] = field(default_factory=list)
    missing_components: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "period": self.period,
            "revenue": round(self.revenue, 2),
            "data_available": self.data_available,
            "contribution_profit": round(self.contribution_profit, 2) if self.contribution_profit is not None else None,
            "contribution_margin": round(self.contribution_margin, 4) if self.contribution_margin is not None else None,
            "partial_contribution_profit": round(self.partial_contribution_profit, 2) if self.partial_contribution_profit is not None else None,
            "partial_contribution_margin": round(self.partial_contribution_margin, 4) if self.partial_contribution_margin is not None else None,
            "included_components": self.included_components,
            "missing_components": self.missing_components,
            "components": {k: c.to_dict() for k, c in self.components.items()},
            "limitations": self.limitations,
        }


def compute_profitability(ds: Dataset, period_label: str) -> ProfitabilityResult:
    revenue = total_revenue(ds)
    cogs, covered, item_revenue = known_cogs(ds)
    coverage = (covered / item_revenue) if item_revenue else 0.0

    components: Dict[str, CostComponent] = {}
    components["cogs"] = CostComponent(
        "cogs", "Cout des marchandises vendues",
        cogs if coverage >= 0.999 else None, "shopify_products",
        "complet" if coverage >= 0.999 else
        f"cout connu sur {coverage:.0%} du CA article seulement: COGS total non calculable",
    )
    fees = total_payment_fees(ds)
    components["payment_fees"] = CostComponent("payment_fees", "Frais de paiement", fees, "stripe",
                                               "frais Stripe reels" if fees is not None else "aucun frais renseigne")
    spend = total_ad_spend(ds)
    components["advertising"] = CostComponent("advertising", "Publicite", spend if ds.ad_performance else None,
                                              "google_ads",
                                              "Google Ads uniquement: les autres regies ne sont pas connectees")
    # D-047: jamais "Refunded Amount" (port et taxes compris) retranche d'un CA qui les exclut
    refunds, undetermined, low, high = refunds_on_revenue_basis(ds)
    components["refunds"] = CostComponent(
        "refunds", "Remboursements (part produit)", refunds, "shopify_orders",
        "part produit des remboursements, ramenee a la base du CA (hors port et taxes)" if refunds is not None else
        f"part produit non determinable pour {undetermined} remboursement(s): l'export ne ventile pas port et "
        f"taxes rembourses (part produit de la periode comprise entre {low:,.2f} et {high:,.2f})",
    )
    components["shipping_cost"] = CostComponent(
        "shipping_cost", "Cout de transport reel", None, "non connecte",
        "l'export Shopify contient le port FACTURE au client, pas le cout transporteur",
    )

    missing = [k for k in REQUIRED_COMPONENTS if not components[k].available]
    included = [k for k in REQUIRED_COMPONENTS if components[k].available]

    result = ProfitabilityResult(period=period_label, revenue=revenue, components=components,
                                 included_components=included, missing_components=missing)

    partial_costs = sum(components[k].value or 0.0 for k in included)
    # sans aucun cout connu, un "profit partiel" ne serait que le CA avec une marge de 100 %
    if revenue and included:
        result.partial_contribution_profit = revenue - partial_costs
        result.partial_contribution_margin = result.partial_contribution_profit / revenue

    if not missing and revenue:
        result.data_available = True
        result.contribution_profit = result.partial_contribution_profit
        result.contribution_margin = result.partial_contribution_margin
    else:
        result.data_available = False
        for key in missing:
            result.limitations.append(
                f"{components[key].label}: {components[key].note or 'donnee absente'}"
            )
        excluded = ", ".join(components[k].label for k in missing)
        result.limitations.append(
            "Profit de contribution complet non calculable a partir des donnees disponibles. "
            + (f"Le profit partiel ci-dessus exclut: {excluded}." if result.partial_contribution_profit is not None
               else f"Aucun profit partiel: aucune composante de cout disponible ({excluded}).")
        )
    return result
