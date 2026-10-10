"""Instantanes (golden) du contexte LLM sur des scenarios metier representatifs.

Un ecart signifie que le contrat du contexte a change: soit c'est une
regression, soit c'est volontaire et CONTRACT_VERSION doit etre incremente.

Regenerer apres une evolution volontaire du contrat:
    MERVIO_UPDATE_GOLDEN=1 python -m pytest tests/test_llm_golden.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llm_scenarios import SCENARIOS

from mervio.llm import CONTRACT_VERSION, build_llm_context, serialize_context

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "llm_context"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_context_matches_golden_snapshot(name):
    context = json.loads(serialize_context(build_llm_context(SCENARIOS[name]())))
    path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("MERVIO_UPDATE_GOLDEN") == "1":
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert expected["contract_version"] == CONTRACT_VERSION
    assert context == expected


def test_golden_semantics_healthy_business():
    context = build_llm_context(SCENARIOS["healthy_business"]())
    assert context["business_health"]["score"] >= 70
    assert not any(f["category"] == "critical_issue" for f in context["findings"])
    assert not [e for e in context["evidence"] if e["kind"] == "anomaly"]


def test_golden_semantics_revenue_decline():
    """AN-02 / D-075: le facteur `conversion_rate` n'existe plus, et l'attente suit l'arbitrage.

    Il confondait une baisse ORGANIQUE avec un probleme de checkout: << commandes totales /
    clics PAYANTS >> baisse aussi quand le trafic gratuit chute, sans qu'aucun checkout soit en
    cause. La decomposition est desormais CA = commandes x panier moyen (couche A), les commandes
    etant expliquees par les clics payants (couche B); le trafic organique et la conversion du
    site sont DECLARES non observes (D-073).

    L'attente n'est pas affaiblie: le facteur primaire est NOMME, l'ancien est INTERDIT, les deux
    angles morts sont EXIGES avec leurs valeurs nulles, et aucune recommandation checkout n'est
    admise -- la regle meme que D-075 ratifie.
    """
    context = build_llm_context(SCENARIOS["revenue_decline"]())
    decline = next(f for f in context["findings"] if f["type"] == "revenue_decline")
    assert decline["category"] == "critical_issue" and decline["estimated_impact"] < 0
    analysis = context["root_causes"][0]
    assert analysis["primary_factor"] == "order_volume"
    assert analysis["causality_established"] is False

    factors = {item["factor"]: item for item in context["evidence"]
               if item["kind"] == "root_cause_factor"}
    assert "conversion_rate" not in factors, "AN-02 a supprime ce facteur"
    assert factors["order_volume"]["layer"] == "a"
    assert factors["paid_traffic"]["layer"] == "b"
    assert factors["paid_traffic"]["explains"] == "order_volume"
    for blind_spot in ("organic_traffic", "site_conversion_rate"):
        item = factors[blind_spot]
        assert item["observability"] == "not_observed"
        assert item["contribution"] is None and item["explains_share"] is None
        assert item["missing_source"] == "web_analytics_sessions"

    actions = " ".join(item["action"] for item in context["recommendations"]).lower()
    assert "checkout" not in actions and "tunnel de conversion" not in actions


def test_golden_semantics_missing_profitability():
    context = build_llm_context(SCENARIOS["missing_profitability"]())
    unavailable = {u["metric"] for u in context["unavailable_metrics"]}
    assert {"contribution_profit", "cogs", "roas", "cac"} <= unavailable
    assert context["profitability"]["data_available"] is False


def test_golden_semantics_marketing_deterioration():
    context = build_llm_context(SCENARIOS["marketing_deterioration"]())
    roas = next(e for e in context["evidence"] if e["id"] == "anomaly.roas")
    assert roas["direction"] == "down" and roas["assessment"] == "unfavorable"


def test_golden_semantics_customer_churn_risk():
    context = build_llm_context(SCENARIOS["customer_churn_risk"]())
    assert any(f["type"] == "customer_concentration" for f in context["findings"])
    assert "churn" not in serialize_context(context).lower()  # le moteur ne calcule pas de churn


def test_golden_semantics_malicious_source_text():
    context = build_llm_context(SCENARIOS["malicious_source_text"]())
    assert context["untrusted_content"]["flagged_count"] >= 4
    serialized = serialize_context(context)
    assert "jane.doe" not in serialized and "4111" not in serialized


def test_golden_semantics_minimal_report():
    context = build_llm_context(SCENARIOS["minimal_report"]())
    assert context["business_health"]["score"] is None
    assert context["facts"] == [] and context["findings"] == []
