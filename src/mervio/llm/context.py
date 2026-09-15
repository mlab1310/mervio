"""Interface AnalyticsResult -> contexte LLM.

AUCUN appel LLM ici. Ce module prepare uniquement le contexte que la couche IA
consommera a la mission suivante. Le contrat est volontairement restrictif: le
LLM recoit des faits deja calcules et n'a pas le droit de produire un chiffre.
"""
from __future__ import annotations

from typing import Any, Dict, List

SYSTEM_CONTRACT = (
    "Tu interpretes des resultats analytiques deja calcules. "
    "Regles absolues: (1) ne calcule aucun chiffre, reutilise uniquement ceux fournis; "
    "(2) ne presente jamais une hypothese comme une causalite prouvee; "
    "(3) si une donnee est marquee unavailable, dis-le explicitement au lieu de l'estimer; "
    "(4) chaque affirmation doit pouvoir etre reliee a un element du contexte."
)


def build_llm_context(report: Dict[str, Any], max_items: int = 5) -> Dict[str, Any]:
    """Extrait un contexte compact et factuel a partir du rapport structure."""
    kpis = report.get("kpis", {})
    facts: List[Dict[str, Any]] = [
        {
            "metric": key, "label": metric["label"], "value": metric["value"],
            "unit": metric["unit"], "period": metric["period"],
            "data_quality": metric["data_quality"], "notes": metric["notes"],
        }
        for key, metric in kpis.items()
        if metric.get("value") is not None
    ]
    unavailable = [
        {"metric": key, "label": metric["label"], "reason": "; ".join(metric["notes"]) or "donnee absente"}
        for key, metric in kpis.items()
        if metric.get("value") is None
    ]
    return {
        "system_contract": SYSTEM_CONTRACT,
        "period": report.get("period", {}),
        "currency": report.get("_meta", {}).get("currency"),
        "business_health": {
            "score": report.get("business_health_score", {}).get("score"),
            "interpretation": report.get("business_health_score", {}).get("interpretation"),
            "excluded_dimensions": report.get("business_health_score", {}).get("excluded_dimensions", []),
        },
        "facts": facts,
        "unavailable_metrics": unavailable,
        "anomalies": report.get("anomalies", [])[:max_items],
        "root_causes": report.get("root_causes", []),
        "critical_issues": report.get("critical_issues", [])[:max_items],
        "warnings": report.get("warnings", [])[:max_items],
        "opportunities": report.get("opportunities", [])[:max_items],
        "limitations": report.get("limitations", []),
    }
