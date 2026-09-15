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
    context = build_llm_context(SCENARIOS["revenue_decline"]())
    decline = next(f for f in context["findings"] if f["type"] == "revenue_decline")
    assert decline["category"] == "critical_issue" and decline["estimated_impact"] < 0
    assert context["root_causes"][0]["primary_factor"] == "conversion_rate"
    assert context["root_causes"][0]["causality_established"] is False


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
