"""Business Health Report: sortie JSON structuree.

Ce JSON est le contrat de sortie du moteur. Il doit pouvoir alimenter demain
un dashboard, un PDF, un email ou un contexte LLM sans transformation metier.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Dict, List

from .kpi import campaign_performance, product_performance
from .timeseries import rolling_average

def pseudonymise(customer_id: str) -> str:
    """Identifiant stable et non reversible pour la sortie.

    En interne, l'email sert de cle de jointure entre commandes. Dans un
    livrable, il n'a rien a faire: un rapport circule par email, se depose sur
    un Drive et finit dans un ticket de support. Le pseudonyme reste stable
    d'un rapport a l'autre, ce qui suffit a suivre un client dans le temps.
    """
    digest = hashlib.sha256(customer_id.encode("utf-8")).hexdigest()
    return f"cust_{digest[:12]}"


SYNTHETIC_MARKER = (
    "Les jeux de donnees fournis dans data/sample sont SYNTHETIQUES et ne "
    "representent aucune entreprise reelle."
)


def _executive_summary(health, insights, kpis, period, currency) -> str:
    revenue = kpis.get("revenue")
    parts = [f"Periode {period.label} ({period.start_date} au {period.end_date})."]
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
                    engine_version: str) -> dict:
    currency = dataset.currency
    customers = window.customers()
    top_customers = sorted(customers.values(), key=lambda c: c.revenue, reverse=True)[:5]

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
        "executive_summary": _executive_summary(health, insights, kpis, period, currency),
        "critical_issues": [i.to_dict() for i in insights if i.category == "critical_issue"],
        "warnings": [i.to_dict() for i in insights if i.category == "warning"],
        "opportunities": [i.to_dict() for i in insights if i.category == "opportunity"],
        "kpis": {k: m.to_dict() for k, m in kpis.items()},
        "profitability": profitability.to_dict(),
        "customers": {
            "total_active": len(customers),
            "new_customers": kpis["new_customers"].to_dict() if "new_customers" in kpis else None,
            "repeat_purchase_rate": kpis["repeat_purchase_rate"].to_dict() if "repeat_purchase_rate" in kpis else None,
            "concentration_top5": kpis["customer_concentration_top5"].to_dict() if "customer_concentration_top5" in kpis else None,
            "top_customers": [
                {"customer_id": pseudonymise(c.customer_id),
                 "orders": c.orders_count, "revenue": round(c.revenue, 2)}
                for c in top_customers
            ],
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
                "recommendation": i.recommendation, "confidence": round(i.confidence, 2),
                "estimated_impact": round(i.estimated_impact, 2) if i.estimated_impact is not None else None,
                "estimated_impact_basis": i.estimated_impact_basis,
            }
            for i in insights if i.recommendation
        ],
        "data_quality": dataset.quality.to_dict(),
        "limitations": _limitations(dataset, profitability, root_cause, health),
    }
