"""KPI Engine deterministe.

Regle absolue: aucun LLM n'intervient ici. Chaque metrique porte sa definition,
sa formule, ses sources, sa periode et son statut de qualite, afin qu'un insight
puisse toujours etre relie a sa donnee.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..domain.models import Dataset
from ..domain.quality import QualityStatus
from .periods import Period


@dataclass
class Metric:
    key: str
    label: str
    value: Optional[float]
    unit: str  # currency | count | ratio | percent
    definition: str
    formula: str
    sources: List[str]
    period: str
    data_quality: str = QualityStatus.RELIABLE.value
    notes: List[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": round(self.value, 4) if self.value is not None else None,
            "unit": self.unit,
            "definition": self.definition,
            "formula": self.formula,
            "sources": self.sources,
            "period": self.period,
            "data_quality": self.data_quality,
            "notes": self.notes,
        }


def _m(key, label, value, unit, definition, formula, sources, period, quality=QualityStatus.RELIABLE, notes=None):
    return Metric(key, label, value, unit, definition, formula, sources, period,
                  quality.value if isinstance(quality, QualityStatus) else quality, notes or [])


# --- agregats primitifs -------------------------------------------------
def total_revenue(ds: Dataset) -> float:
    return sum(o.net_revenue for o in ds.orders)


def total_refunds(ds: Dataset) -> float:
    return sum(r.amount for r in ds.refunds if r.source == "shopify")


def total_order_value(ds: Dataset) -> float:
    """Somme des Total de commande (port et taxes compris): base du taux de remboursement (D-045)."""
    return sum(o.total for o in ds.orders)


def total_units(ds: Dataset) -> int:
    return sum(o.units for o in ds.orders)


def total_ad_spend(ds: Dataset) -> float:
    return sum(a.spend for a in ds.ad_performance)


def total_clicks(ds: Dataset) -> int:
    return sum(a.clicks for a in ds.ad_performance)


def total_payment_fees(ds: Dataset) -> Optional[float]:
    fees = [p.fee for p in ds.payments if p.fee is not None]
    return sum(fees) if fees else None


def known_cogs(ds: Dataset) -> tuple[float, float, float]:
    """Retourne (cogs_connu, ca_couvert, ca_total) sur les articles vendus."""
    cogs = covered = total = 0.0
    for order in ds.orders:
        for item in order.items:
            total += item.line_revenue
            product = ds.product_for(item.sku)
            if product and product.has_cogs:
                cogs += item.quantity * (product.unit_cogs or 0.0)
                covered += item.line_revenue
    return cogs, covered, total


def new_customers(ds: Dataset, period: Period) -> int:
    ids = {o.customer_id for o in ds.orders}
    return sum(
        1 for cid in ids
        if (first := ds.customer_first_order.get(cid)) and period.start <= first < period.end
    )


# --- moteur -------------------------------------------------------------
def compute_kpis(ds: Dataset, period: Period) -> Dict[str, Metric]:
    p = period.label
    q = ds.quality
    kpis: Dict[str, Metric] = {}

    revenue = total_revenue(ds)
    orders_count = len(ds.orders)
    units = total_units(ds)
    refunds = total_refunds(ds)
    spend = total_ad_spend(ds)
    clicks = total_clicks(ds)

    # un CA negatif ne vient que de montants source negatifs: il est conserve et signale, jamais ramene a zero
    kpis["revenue"] = _m("revenue", "Chiffre d'affaires net", revenue, "currency",
                         "CA produit net de remises, avant remboursements et annulations, hors frais de port et hors taxes.",
                         "sum(order.subtotal) [subtotal deja net de remise, D-041]", ["shopify_orders"], p,
                         QualityStatus.INCOMPLETE if revenue < 0 else QualityStatus.RELIABLE,
                         ["CA negatif sur la periode: montants source negatifs a verifier, valeur non corrigee"]
                         if revenue < 0 else [])
    kpis["orders"] = _m("orders", "Commandes", float(orders_count), "count",
                        "Nombre de commandes creees sur la periode.",
                        "count(orders)", ["shopify_orders"], p)
    kpis["units"] = _m("units", "Unites vendues", float(units), "count",
                       "Somme des quantites de tous les articles.",
                       "sum(item.quantity)", ["shopify_orders"], p)
    kpis["aov"] = _m("aov", "Panier moyen", (revenue / orders_count) if orders_count else None, "currency",
                     "CA net divise par le nombre de commandes.",
                     "revenue / orders", ["shopify_orders"], p,
                     QualityStatus.RELIABLE if orders_count else QualityStatus.UNAVAILABLE,
                     [] if orders_count else ["aucune commande sur la periode"])
    # D-045: remboursements et Total sont sur la meme base (montant facture, port et taxes compris)
    # et la meme date (creation de la commande: l'export ne date pas les remboursements)
    refund_notes = ["remboursements rattaches a la date de creation de la commande, pas a la date du remboursement"]
    kpis["refunds"] = _m("refunds", "Remboursements", refunds, "currency",
                         "Montant rembourse sur les commandes creees dans la periode (Refunded Amount Shopify, "
                         "port et taxes eventuels compris).",
                         "sum(refund.amount where source=shopify)", ["shopify_orders"], p,
                         notes=list(refund_notes))
    order_value = total_order_value(ds)
    total_incomplete = any(i.kind == "missing_order_total" for i in q.issues)
    kpis["refund_rate"] = _m("refund_rate", "Taux de remboursement",
                             (refunds / order_value) if order_value else None, "ratio",
                             "Part du montant facture (port et taxes compris) des commandes de la periode remboursee.",
                             "refunds / sum(order.total)", ["shopify_orders"], p,
                             (QualityStatus.INCOMPLETE if total_incomplete else QualityStatus.RELIABLE)
                             if order_value else QualityStatus.UNAVAILABLE,
                             refund_notes
                             + (["Total absent sur une partie des commandes: base incomplete"] if total_incomplete else [])
                             + ([] if order_value else ["aucun montant facture sur la periode"]))
    kpis["ad_spend"] = _m("ad_spend", "Depense publicitaire", spend, "currency",
                          "Depense Google Ads uniquement.", "sum(ad.spend)", ["google_ads"], p,
                          QualityStatus.INCOMPLETE,
                          ["Meta Ads et autres regies non connectes: la depense reelle peut etre superieure"])
    kpis["clicks"] = _m("clicks", "Clics payants", float(clicks), "count",
                        "Clics Google Ads, utilises comme proxy de trafic paye.",
                        "sum(ad.clicks)", ["google_ads"], p, QualityStatus.INCOMPLETE,
                        ["proxy: le trafic organique et direct n'est pas mesure (GA4 non connecte)"])

    cvr = (orders_count / clicks) if clicks else None
    kpis["paid_conversion_rate"] = _m("paid_conversion_rate", "Taux de conversion (trafic paye)", cvr, "ratio",
                                      "Commandes rapportees aux clics payants.",
                                      "orders / paid_clicks", ["shopify_orders", "google_ads"], p,
                                      QualityStatus.INCOMPLETE if clicks else QualityStatus.UNAVAILABLE,
                                      ["approximation: toutes les commandes ne proviennent pas du trafic paye"])

    n_new = new_customers(ds, period)
    kpis["new_customers"] = _m("new_customers", "Nouveaux clients", float(n_new), "count",
                               "Clients dont la toute premiere commande tombe dans la periode.",
                               "count(customer where first_order in period)", ["shopify_orders"], p,
                               q.status_of("customer_identity"))
    # Un CAC de 0.00 affirmerait "le cout d'acquisition est nul". Sans aucune
    # ligne de depense publicitaire, la bonne reponse est "inconnu", pas zero.
    has_ad_data = bool(ds.ad_performance)
    cac_value = (spend / n_new) if (n_new and has_ad_data) else None
    kpis["cac"] = _m("cac", "CAC", cac_value, "currency",
                     "Cout d'acquisition client sur depense publicitaire connue.",
                     "ad_spend / new_customers", ["google_ads", "shopify_orders"], p,
                     QualityStatus.INCOMPLETE if cac_value is not None else QualityStatus.UNAVAILABLE,
                     ["ne couvre que Google Ads: CAC reel probablement superieur"]
                     + ([] if n_new else ["aucun nouveau client sur la periode"])
                     + ([] if has_ad_data else ["aucune donnee publicitaire: CAC inconnu, pas nul"]))
    kpis["roas"] = _m("roas", "ROAS", (revenue / spend) if spend else None, "ratio",
                      "CA net rapporte a la depense publicitaire.", "revenue / ad_spend",
                      ["shopify_orders", "google_ads"], p,
                      QualityStatus.INCOMPLETE if spend else QualityStatus.UNAVAILABLE,
                      ["ROAS global, non attribue: mesure l'efficacite du mix, pas d'une campagne"])

    cogs, covered, total_item_revenue = known_cogs(ds)
    coverage = (covered / total_item_revenue) if total_item_revenue else 0.0
    if coverage <= 0:
        gm, gm_quality, gm_notes = None, QualityStatus.UNAVAILABLE, ["aucun cout produit renseigne"]
    else:
        gm = (covered - cogs) / covered
        gm_quality = QualityStatus.RELIABLE if coverage >= 0.95 else QualityStatus.INCOMPLETE
        gm_notes = [f"calculee sur {coverage:.0%} du CA article (produits dont le cout est connu)"]
    kpis["gross_margin"] = _m("gross_margin", "Marge brute", gm, "ratio",
                              "Marge brute sur la part du CA dont le cout produit est connu.",
                              "(revenue_covered - cogs) / revenue_covered",
                              ["shopify_orders", "shopify_products"], p, gm_quality, gm_notes)
    kpis["cogs"] = _m("cogs", "Cout des marchandises vendues", cogs if coverage > 0 else None, "currency",
                      "Somme des couts unitaires connus multiplies par les quantites.",
                      "sum(qty * product.unit_cogs)", ["shopify_products"], p,
                      gm_quality, gm_notes)

    fees = total_payment_fees(ds)
    kpis["payment_fees"] = _m("payment_fees", "Frais de paiement", fees, "currency",
                              "Frais Stripe reels.", "sum(payment.fee)", ["stripe"], p,
                              q.status_of("payment_fees"))

    customers = ds.customers()
    repeat = sum(1 for c in customers.values() if c.orders_count > 1)
    kpis["repeat_purchase_rate"] = _m(
        "repeat_purchase_rate", "Taux de reachat", (repeat / len(customers)) if customers else None, "ratio",
        "Part des clients ayant passe plus d'une commande DANS la periode analysee.",
        "customers_with_2plus_orders / customers", ["shopify_orders"], p,
        QualityStatus.INCOMPLETE if customers else QualityStatus.UNAVAILABLE,
        ["mesure intra-periode: sous-estime la retention reelle sur un historique court"])

    top5 = sorted((c.revenue for c in customers.values()), reverse=True)[:5]
    kpis["customer_concentration_top5"] = _m(
        "customer_concentration_top5", "Concentration client (top 5)",
        (sum(top5) / revenue) if revenue else None, "ratio",
        "Part du CA realisee par les 5 plus gros clients.",
        "sum(top5_customer_revenue) / revenue", ["shopify_orders"], p,
        q.status_of("customer_identity"))

    return kpis


def product_performance(ds: Dataset) -> List[dict]:
    acc: Dict[str, dict] = {}
    for order in ds.orders:
        for item in order.items:
            entry = acc.setdefault(item.sku, {
                "sku": item.sku, "title": item.title, "revenue": 0.0, "units": 0,
                "cogs": None, "gross_profit": None, "gross_margin": None, "cogs_known": False,
            })
            entry["revenue"] += item.line_revenue
            entry["units"] += item.quantity
            product = ds.product_for(item.sku)
            if product and product.has_cogs:
                entry["cogs"] = (entry["cogs"] or 0.0) + item.quantity * (product.unit_cogs or 0.0)
                entry["cogs_known"] = True
    for entry in acc.values():
        if entry["cogs_known"] and entry["revenue"]:
            entry["gross_profit"] = entry["revenue"] - entry["cogs"]
            entry["gross_margin"] = entry["gross_profit"] / entry["revenue"]
    return sorted(acc.values(), key=lambda e: e["revenue"], reverse=True)


def campaign_performance(ds: Dataset) -> List[dict]:
    acc: Dict[str, dict] = {}
    for row in ds.ad_performance:
        campaign = ds.campaigns.get(row.campaign_id)
        entry = acc.setdefault(row.campaign_id, {
            "campaign_id": row.campaign_id,
            "name": campaign.name if campaign else row.campaign_id,
            "spend": 0.0, "impressions": 0, "clicks": 0,
            "conversions": 0.0, "conversion_value": 0.0,
        })
        entry["spend"] += row.spend
        entry["impressions"] += row.impressions
        entry["clicks"] += row.clicks
        entry["conversions"] += row.conversions
        entry["conversion_value"] += row.conversion_value
    for entry in acc.values():
        entry["roas"] = (entry["conversion_value"] / entry["spend"]) if entry["spend"] else None
        entry["cpc"] = (entry["spend"] / entry["clicks"]) if entry["clicks"] else None
        entry["cvr"] = (entry["conversions"] / entry["clicks"]) if entry["clicks"] else None
        entry["cpa"] = (entry["spend"] / entry["conversions"]) if entry["conversions"] else None
    return sorted(acc.values(), key=lambda e: e["spend"], reverse=True)
