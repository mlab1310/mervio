"""Business Health Report: sortie JSON structuree.

Ce JSON est le contrat de sortie du moteur. Il doit pouvoir alimenter demain
un dashboard, un PDF, un email ou un contexte LLM sans transformation metier.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..domain.models import Customer
from .insights import CATEGORIES, SECTION_OF
from .kpi import campaign_performance, product_performance
from .timeseries import rolling_average

#: nombre de clients decrits dans `customers.top_customers` (meme perimetre que
#: `customer_concentration_top5`, dont les parts publiees sont les composantes)
TOP_CUSTOMERS = 5


def top_customers(customers: Dict[str, Customer], revenue: Optional[float]) -> List[dict]:
    """Les plus gros clients de la periode, DECRITS et non designes (AN-17).

    Le rapport exposait ici `customer_id = "cust_" + sha256(customer_ref)[:12]`. Ce n'etait
    pas un agregat mais un IDENTIFIANT: deterministe et recalculable par qui detient la cle
    d'identite de l'organisation, STABLE d'un rapport a l'autre -- donc suffisant pour suivre
    un client dans le temps -- et inscrit dans `reports`, table que `reports_forbid_update`
    (revision 0003) interdit de modifier. L'effacement client (D-056) remplace
    `orders.customer_ref` par un tombstone et detruit les octets bruts; il ne touche pas, et
    ne peut pas toucher, un rapport deja persiste. Le pseudonyme d'un client efface y
    survivait indefiniment.

    Le rang porte la meme information analytique sans designer personne: la concentration du
    CA se lit dans les parts, la fidelite dans le nombre de commandes. Le CA absolu reste
    derivable (`revenue_share` x `kpis.revenue.value`).

    `revenue_share` partage le denominateur de `customer_concentration_top5`: la somme des
    parts publiees EST cette concentration. Un CA de periode nul ne permet aucune part: la
    reponse est `null`, jamais 0.0, comme pour le KPI.

    L'ordre est celui du CA decroissant; a egalite, le tri stable de Python conserve l'ordre
    de premiere apparition dans les commandes. Il ne depend donc d'aucune reference client,
    et c'est ce qui rend le rapport independant de la cle d'identite (cf. D-058).
    """
    ranked = sorted(customers.values(), key=lambda c: c.revenue, reverse=True)[:TOP_CUSTOMERS]
    return [
        {
            "rank": rank,
            "orders": c.orders_count,
            "revenue_share": round(c.revenue / revenue, 4) if revenue else None,
        }
        for rank, c in enumerate(ranked, start=1)
    ]


SYNTHETIC_MARKER = (
    "Les jeux de donnees fournis dans data/sample sont SYNTHETIQUES et ne "
    "representent aucune entreprise reelle."
)


def _executive_summary(health, insights, kpis, period, currency, freshness=None) -> str:
    revenue = kpis.get("revenue")
    parts: List[str] = []
    # AN-05: un rapport perime le dit EN TETE, avant le score. Un lecteur qui s'arrete a la
    # premiere phrase doit savoir que les chiffres qui suivent sont vieux.
    if freshness and freshness.get("status") == "stale":
        parts.append(f"ATTENTION: donnee perimee. Derniere donnee {freshness['latest_data_day']}, "
                     f"analyse au {freshness['analysis_date']}, soit {freshness['delay_days']} "
                     f"jours de retard (seuil {freshness['threshold_days']}).")
    parts.append(f"Periode {period.label} ({period.start_date} au {period.end_date}).")
    if revenue and revenue.available:
        parts.append(f"{revenue.label}: {revenue.value:,.2f} {currency}.")
    if health.score is not None:
        parts.append(f"Business Health Score: {health.score}/100. {health.interpretation}")
    critical = [i for i in insights if i.category == "critical_issue"]
    if critical:
        parts.append(f"Point principal: {critical[0].fact}")
        if critical[0].hypothesis:
            parts.append(f"Hypothese: {critical[0].hypothesis}")
    else:
        parts.append("Aucun probleme critique detecte sur les dimensions mesurees.")
    return " ".join(parts)


def _limitations(dataset, profitability, root_cause, health) -> List[str]:
    out: List[str] = list(profitability.limitations)
    if root_cause and root_cause.limitations:
        out.extend(root_cause.limitations)
    if health.excluded_dimensions:
        out.append("Dimensions exclues du Business Health Score (non calculables): "
                   + ", ".join(health.excluded_dimensions))
    for issue in dataset.quality.issues:
        if issue.severity in ("warning", "error"):
            out.append(f"[{issue.source}] {issue.message}" + (f" (x{issue.count})" if issue.count > 1 else ""))
    out.append("Ce rapport ne couvre que les sources connectees; toute source absente "
               "peut modifier les conclusions.")
    seen, unique = set(), []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def assemble_report(dataset, window, period, prior, kpis, profitability, series,
                    anomalies, comparisons, root_cause, health, insights, cfg,
                    engine_version: str, freshness: Optional[dict] = None) -> dict:
    currency = dataset.currency
    customers = window.customers()
    revenue_metric = kpis.get("revenue")
    period_revenue = revenue_metric.value if revenue_metric is not None else None

    series_out = {}
    for name, points in series.items():
        values = [p.value for p in points]
        series_out[name] = {
            "points": [p.to_dict() for p in points],
            "rolling_average": [
                round(v, 4) if v is not None else None
                for v in rolling_average(values, cfg.rolling_window)
            ],
            "rolling_window": cfg.rolling_window,
        }

    return {
        "_meta": {
            "engine": "mervio-analytics",
            "engine_version": engine_version,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "currency": currency,
            "grain": cfg.grain,
            "llm_used": False,
            "note": "Tous les chiffres de ce rapport sont calcules de maniere deterministe. "
                    "Aucun LLM n'intervient dans le calcul.",
            "synthetic_data_warning": SYNTHETIC_MARKER,
        },
        "business_health_score": health.to_dict(),
        "period": {"current": period.to_dict(), "previous": prior.to_dict()},
        "executive_summary": _executive_summary(health, insights, kpis, period, currency,
                                                freshness),
        # AN-18 / D-084: une liste par categorie, dans l'ORDRE ratifie. L'ordre des cles est
        # l'ordre de lecture, et il survit a la serialisation: c'est elle qui circule.
        **{SECTION_OF[category]: [i.to_dict() for i in insights if i.category == category]
           for category in CATEGORIES},
        "kpis": {k: m.to_dict() for k, m in kpis.items()},
        "profitability": profitability.to_dict(),
        "customers": {
            "total_active": len(customers),
            "new_customers": kpis["new_customers"].to_dict() if "new_customers" in kpis else None,
            "repeat_purchase_rate": kpis["repeat_purchase_rate"].to_dict() if "repeat_purchase_rate" in kpis else None,
            "concentration_top5": kpis["customer_concentration_top5"].to_dict() if "customer_concentration_top5" in kpis else None,
            "top_customers": top_customers(customers, period_revenue),
        },
        "products": product_performance(window),
        "marketing": {
            "campaigns": campaign_performance(window),
            "ad_spend": kpis["ad_spend"].to_dict() if "ad_spend" in kpis else None,
            "roas": kpis["roas"].to_dict() if "roas" in kpis else None,
            "cac": kpis["cac"].to_dict() if "cac" in kpis else None,
        },
        "time_series": series_out,
        "comparisons": {
            metric: {kind: c.to_dict() for kind, c in kinds.items()}
            for metric, kinds in comparisons.items()
        },
        "anomalies": [a.to_dict() for a in anomalies],
        "root_causes": [root_cause.to_dict()] if root_cause else [],
        "recommendations": [
            {
                "source_insight": i.type, "severity": i.severity,
                "recommendation": i.recommendation,
                "confidence": i.confidence.to_dict() if i.confidence is not None else None,
                "estimated_impact": round(i.estimated_impact, 2) if i.estimated_impact is not None else None,
                "estimated_impact_basis": i.estimated_impact_basis,
            }
            for i in insights if i.recommendation
        ],
        "data_freshness": freshness or {},
        "data_quality": dataset.quality.to_dict(),
        "limitations": _limitations(dataset, profitability, root_cause, health),
    }
