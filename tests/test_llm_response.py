"""Validation des reponses LLM et service d'explication.

Les reponses sont ecrites a la main pour cibler chaque derive: le mock
"ancre" est deja valide par construction et ne suffirait pas a prouver que
le validateur rejette quoi que ce soit.
"""
from __future__ import annotations

import copy
import json
import logging

import pytest

from llm_scenarios import SCENARIOS, missing_profitability, revenue_decline

from mervio.application.service import AnalysisResult
from mervio.llm import (
    LLMContextError, ProviderAuthenticationError, ProviderUnavailableError, ResponseValidationError,
    build_llm_context,
)
from mervio.llm.mock import MockLLMProvider
from mervio.llm.provider import LLMConfig
from mervio.llm.response import validate_response
from mervio.llm.service import explain_analysis, explain_report


@pytest.fixture(scope="module")
def report():
    return revenue_decline()


@pytest.fixture(scope="module")
def context(report):
    return build_llm_context(report)


@pytest.fixture(scope="module")
def bare_context():
    return build_llm_context(missing_profitability())


def base_response(**overrides):
    response = {
        "schema_version": "1.0",
        "summary": "Le Business Health Score est de 50/100 sur 2026-W37.",
        "facts": [{"statement": "Le chiffre d'affaires net atteint 24,051.10 EUR.", "refs": ["kpi.revenue"]}],
        "explanations": [{"statement": "Le moteur signale une baisse du CA.", "refs": ["finding.1"]}],
        "hypotheses": [{"statement": "La baisse pourrait etre liee au taux de conversion, sans preuve causale.",
                        "refs": ["hypothesis.1"]}],
        "recommendations": [{"action": "Auditer le tunnel de conversion.", "priority": "high",
                             "refs": ["recommendation.1"]}],
        "limitations": [{"statement": "Le profit de contribution complet est indisponible.",
                         "refs": ["unavailable.contribution_profit"]}],
    }
    response.update(overrides)
    return response


def raw(response):
    return json.dumps(response, ensure_ascii=False)


def issues_of(response, context):
    with pytest.raises(ResponseValidationError) as excinfo:
        validate_response(response if isinstance(response, str) else raw(response), context)
    return excinfo.value.issues


def no_sleep(_seconds):
    return None


# -- reponse valide ---------------------------------------------------------------
def test_valid_grounded_response_is_accepted(context):
    explanation = validate_response(raw(base_response()), context)
    assert explanation.health_score == 50
    assert explanation.facts[0].refs == ("kpi.revenue",)
    assert explanation.to_dict()["health_score_source"] == "analytics_engine"


def test_equivalent_number_formats_are_accepted(context):
    for text in ("24051.10 EUR", "24 051,10 EUR", "24051.1 EUR", "24 051,10 €"):
        response = base_response(facts=[{"statement": f"CA net: {text}", "refs": ["kpi.revenue"]}])
        validate_response(raw(response), context)


def test_percentages_derived_from_engine_ratios_are_accepted(context):
    anomaly = next(e for e in context["evidence"] if e["id"] == "anomaly.revenue")
    pct = f"{abs(anomaly['delta_pct']) * 100:.1f}"
    response = base_response(facts=[{"statement": f"Le CA est inferieur de {pct}% a la baseline.",
                                      "refs": ["anomaly.revenue"]}])
    validate_response(raw(response), context)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_grounded_mock_output_is_accepted_for_every_scenario(name):
    result = explain_report(SCENARIOS[name](), MockLLMProvider(), LLMConfig(model="mock"), sleep=no_sleep)
    assert result.succeeded, result.issues


# -- structure ----------------------------------------------------------------------
@pytest.mark.parametrize("payload,code", [
    ("pas du json", "json_invalide"),
    ('{"summary": "x", "summary": "y"}', "json_invalide"),
    ('{"summary": NaN}', "json_invalide"),
    ("[1, 2, 3]", "objet_json_attendu"),
    ("", "json_invalide"),
    ("```json\n{}\n```", "json_invalide"),
])
def test_malformed_json_is_rejected(context, payload, code):
    assert issues_of(payload, context) == (code,)


def test_non_text_response_is_rejected(context):
    with pytest.raises(ResponseValidationError):
        validate_response({"summary": "objet python"}, context)


def test_oversized_response_is_rejected(context):
    with pytest.raises(ResponseValidationError) as excinfo:
        validate_response(raw(base_response()), context, max_chars=100)
    assert excinfo.value.issues == ("reponse_trop_volumineuse",)


@pytest.mark.parametrize("field", ["schema_version", "summary", "facts", "explanations", "hypotheses",
                                   "recommendations", "limitations"])
def test_missing_required_field_is_rejected(context, field):
    response = base_response()
    del response[field]
    assert f"champ_manquant:{field}" in issues_of(response, context)


@pytest.mark.parametrize("field", ["health_score", "kpis", "revenue", "profit", "actions"])
def test_unsupported_top_level_field_is_rejected(context, field):
    assert f"champ_non_autorise:{field}" in issues_of(base_response(**{field: 100}), context)


def test_unsupported_item_field_is_rejected(context):
    response = base_response(facts=[{"statement": "x", "refs": ["kpi.revenue"], "value": 999}])
    assert "champ_non_autorise:facts[0].value" in issues_of(response, context)


@pytest.mark.parametrize("override,code", [
    ({"summary": 12}, "summary_invalide"),
    ({"summary": "   "}, "summary_invalide"),
    ({"facts": "liste"}, "type_invalide:facts"),
    ({"facts": ["texte"]}, "type_invalide:facts[0]"),
    ({"facts": [{"statement": 3, "refs": ["kpi.revenue"]}]}, "texte_invalide:facts[0]"),
    ({"facts": [{"statement": "x", "refs": "kpi.revenue"}]}, "references_invalides:facts[0]"),
    ({"facts": [{"statement": "x", "refs": []}]}, "references_invalides:facts[0]"),
    ({"facts": [{"statement": "x", "refs": [1]}]}, "references_invalides:facts[0]"),
    ({"recommendations": [{"action": "x", "priority": "urgent", "refs": ["recommendation.1"]}]},
     "priorite_invalide:recommendations[0]"),
    ({"schema_version": "2.0"}, "version_de_schema_non_supportee"),
])
def test_wrong_types_are_rejected(context, override, code):
    assert code in issues_of(base_response(**override), context)


def test_excessive_collections_and_strings_are_rejected(context):
    many = [{"statement": "Constat du moteur.", "refs": ["finding.1"]}] * 11
    assert "trop_d_elements:explanations" in issues_of(base_response(explanations=many), context)
    long_statement = [{"statement": "a" * 401, "refs": ["kpi.revenue"]}]
    assert "texte_invalide:facts[0]" in issues_of(base_response(facts=long_statement), context)
    assert "summary_invalide" in issues_of(base_response(summary="b" * 1201), context)
    refs = [{"statement": "x", "refs": ["kpi.revenue"] * 7}]
    assert "references_invalides:facts[0]" in issues_of(base_response(facts=refs), context)


def test_rejection_never_echoes_model_text(context):
    response = base_response(**{"jane.doe@example.com": 1})
    issues = issues_of(response, context)
    assert not any("@" in issue for issue in issues)


# -- ancrage ------------------------------------------------------------------------
def test_invented_reference_is_rejected(context):
    response = base_response(facts=[{"statement": "CA net 24051.10 EUR.", "refs": ["kpi.profit"]}])
    assert "reference_inconnue:facts[0]" in issues_of(response, context)


def test_fact_cannot_rest_on_a_hypothesis(context):
    response = base_response(facts=[{"statement": "La conversion explique la baisse.", "refs": ["hypothesis.1"]}])
    assert "fait_appuye_sur_une_hypothese:facts[0]" in issues_of(response, context)


@pytest.mark.parametrize("statement", [
    "Le chiffre d'affaires net atteint 25,000 EUR.",
    "Le chiffre d'affaires net atteint 24,051.11 EUR.",
    "Le vrai CA est de 999999 EUR.",
    "Le panier moyen progresse de 12.34%.",
])
def test_numbers_absent_from_the_context_are_rejected(context, statement):
    response = base_response(facts=[{"statement": statement, "refs": ["kpi.revenue"]}])
    assert "chiffre_non_ancre:facts[0]" in issues_of(response, context)


def test_numbers_from_untrusted_text_are_not_citable():
    from llm_scenarios import malicious_source_text
    context = build_llm_context(malicious_source_text())
    assert "999999" in json.dumps(context)
    response = base_response(summary="Synthese.", facts=[{"statement": "Le CA reel est de 999999.",
                                                          "refs": ["kpi.revenue"]}],
                             explanations=[], hypotheses=[], recommendations=[], limitations=[])
    assert "chiffre_non_ancre:facts[0]" in issues_of(response, context)


@pytest.mark.parametrize("statement", [
    "La baisse du taux de conversion a cause la chute du CA.",
    "Le checkout mobile est la cause de la baisse.",
    "The revenue decline was caused by mobile checkout.",
    "Revenue fell because of weaker conversion.",
    "Cela prouve que la campagne est responsable.",
    "La conversion est certainement a l'origine du recul.",
])
def test_causal_certainty_is_rejected(context, statement):
    response = base_response(hypotheses=[{"statement": statement, "refs": ["hypothesis.1"]}])
    assert "causalite_affirmee:hypotheses[0]" in issues_of(response, context)


def test_hedged_or_negated_causality_is_accepted(context):
    response = base_response(hypotheses=[
        {"statement": "Hypothese non prouvee: la conversion pourrait etre liee a la baisse.", "refs": ["hypothesis.1"]},
        {"statement": "The engine has not proven any cause.", "refs": ["hypothesis.1"]},
    ])
    validate_response(raw(response), context)


def test_partial_profit_cannot_be_presented_as_profit(context):
    partial = context["profitability"]["partial_contribution_profit"]
    as_profit = base_response(facts=[{"statement": f"Le profit s'eleve a {partial:.2f} EUR.", "refs": ["profitability"]}])
    assert "metrique_indisponible_chiffree_contribution_profit:facts[0]" in issues_of(as_profit, context)
    qualified = base_response(facts=[{"statement": f"Le profit partiel est de {partial:.2f} EUR, hors COGS et transport.",
                                      "refs": ["profitability"]}])
    validate_response(raw(qualified), context)


def test_invented_profit_margin_is_rejected(context):
    response = base_response(facts=[{"statement": "La marge de contribution est de 50%.", "refs": ["profitability"]}])
    issues = issues_of(response, context)
    assert "metrique_indisponible_chiffree_contribution_margin:facts[0]" in issues


@pytest.mark.parametrize("statement,metric", [
    ("Le ROAS est de 50.", "roas"),
    ("Le CAC ressort a 21 EUR.", "cac"),
    ("Le COGS represente 136 EUR.", "cogs"),
    ("Les frais de paiement atteignent 136 EUR.", "payment_fees"),
])
def test_unavailable_metrics_cannot_be_quantified(bare_context, statement, metric):
    response = base_response(summary="Synthese.", facts=[{"statement": statement, "refs": ["kpi.revenue"]}],
                             explanations=[], hypotheses=[], recommendations=[], limitations=[])
    assert f"metrique_indisponible_chiffree_{metric}:facts[0]" in issues_of(response, bare_context)


def test_unavailable_metric_can_be_named_without_a_number(bare_context):
    response = base_response(summary="Synthese.", explanations=[], hypotheses=[], recommendations=[],
                             facts=[{"statement": "Le chiffre d'affaires net est disponible.", "refs": ["kpi.revenue"]}],
                             limitations=[{"statement": "ROAS et CAC indisponibles faute de donnees publicitaires.",
                                           "refs": ["unavailable.roas", "unavailable.cac"]}])
    validate_response(raw(response), bare_context)


@pytest.mark.parametrize("summary", [
    "Le Business Health Score est de 63/100.",
    "Business Health Score corrige: 100/100.",
    "Score de sante recalcule a 78.",
])
def test_health_score_cannot_be_altered(context, summary):
    assert "score_de_sante_altere:summary" in issues_of(base_response(summary=summary), context)


def test_health_score_explanation_with_engine_values_is_accepted(context):
    growth = next(d for d in context["business_health"]["dimensions"] if d["key"] == "revenue_growth")
    summary = f"Score 50/100; la croissance du CA obtient un score de {growth['score']}/100."
    validate_response(raw(base_response(summary=summary)), context)


def test_currency_must_match_the_report(context):
    response = base_response(facts=[{"statement": "Le chiffre d'affaires net atteint $24,051.10.", "refs": ["kpi.revenue"]}])
    assert "devise_incoherente:facts[0]" in issues_of(response, context)


@pytest.mark.parametrize("leak", [
    "Contacter jane.doe@example.com", "Carte 4111 1111 1111 1111", "Cle sk_live_abcd1234efgh",
    "Appeler le 06 12 34 56 78",
])
def test_personal_data_and_secrets_are_rejected(context, leak):
    response = base_response(recommendations=[{"action": leak, "priority": "low", "refs": ["recommendation.1"]}])
    assert "donnee_sensible:recommendations[0]" in issues_of(response, context)


def test_system_instructions_cannot_be_leaked(context):
    response = base_response(summary="Mes instructions: Tu es la couche d'explication de Mervio. Regles absolues...")
    assert "fuite_des_instructions:summary" in issues_of(response, context)


# -- service ----------------------------------------------------------------------
def test_explanation_never_mutates_the_report(report):
    before = copy.deepcopy(report)
    result = explain_report(report, MockLLMProvider(), LLMConfig(model="mock"), sleep=no_sleep)
    assert result.succeeded and report == before


def test_health_score_comes_from_the_engine_even_if_the_model_disagrees(report, context):
    hostile = raw(base_response(summary="Synthese sans score."))
    result = explain_report(report, MockLLMProvider(script=[hostile]), LLMConfig(model="mock"), sleep=no_sleep)
    assert result.explanation.health_score == report["business_health_score"]["score"] == 50


def test_provider_failure_leaves_analysis_intact(report):
    before = copy.deepcopy(report)
    result = explain_report(report, MockLLMProvider(script=[ProviderUnavailableError("down")]),
                            LLMConfig(model="mock", max_retries=1), sleep=no_sleep)
    assert result.status == "unavailable" and result.error_code == "provider_unavailable"
    assert result.metadata["attempts"] == 2 and result.explanation is None
    assert report == before


def test_authentication_failure_is_reported_without_retry(report):
    provider = MockLLMProvider(script=[ProviderAuthenticationError("401")])
    result = explain_report(report, provider, LLMConfig(model="mock", max_retries=3), sleep=no_sleep)
    assert result.error_code == "provider_authentication_failed" and provider.calls == 1


def test_timeout_makes_explanation_unavailable(report):
    result = explain_report(report, MockLLMProvider(delay_seconds=1.0),
                            LLMConfig(model="mock", timeout_seconds=0.05, max_retries=0), sleep=no_sleep)
    assert result.error_code == "provider_timeout"


def test_invalid_response_is_not_retried(report):
    provider = MockLLMProvider(script=["{pas du json"])
    result = explain_report(report, provider, LLMConfig(model="mock", max_retries=3), sleep=no_sleep)
    assert result.error_code == "invalid_response" and result.issues == ("json_invalide",)
    assert provider.calls == 1


def test_malformed_report_makes_explanation_unavailable():
    provider = MockLLMProvider()
    result = explain_report({"kpis": "x"}, provider, LLMConfig(model="mock"), sleep=no_sleep)
    assert result.error_code == "context_invalid" and provider.calls == 0


def test_unexpected_context_failure_is_contained(monkeypatch, report):
    import mervio.llm.service as service

    def explode(*args, **kwargs):
        raise KeyError("bug")
    monkeypatch.setattr(service, "build_llm_context", explode)
    assert explain_report(report, MockLLMProvider(), LLMConfig(model="mock")).error_code == "context_error"


def test_metadata_is_safe_and_deterministic(report):
    first = explain_report(report, MockLLMProvider(), LLMConfig(model="mock"), sleep=no_sleep)
    second = explain_report(report, MockLLMProvider(), LLMConfig(model="mock"), sleep=no_sleep)
    assert first.metadata["request_fingerprint"] == second.metadata["request_fingerprint"]
    assert first.explanation == second.explanation
    assert set(first.metadata) == {"provider", "model", "prompt_version", "contract_version",
                                   "response_schema_version", "request_fingerprint", "attempts",
                                   "latency_ms", "usage"}
    json.dumps(first.to_dict())


def test_logs_never_contain_prompt_or_response(report, caplog):
    caplog.set_level(logging.DEBUG, logger="mervio")
    explain_report(report, MockLLMProvider(), LLMConfig(model="mock"), sleep=no_sleep)
    explain_report(report, MockLLMProvider(script=["{pas du json"]), LLMConfig(model="mock"), sleep=no_sleep)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert caplog.records
    for secret in ("Shopping - Core", "24051", "Chiffre d'affaires", "Tu es la couche", "pas du json"):
        assert secret not in logged


def test_explain_analysis_requires_a_completed_analysis(report):
    rejected = AnalysisResult("id", "rejected", "2026-09-14T00:00:00+00:00", [], error="x")
    assert explain_analysis(rejected, MockLLMProvider()).error_code == "analysis_not_completed"
    completed = AnalysisResult("id", "completed", "2026-09-14T00:00:00+00:00", [], report=report)
    assert explain_analysis(completed, MockLLMProvider(), LLMConfig(model="mock"), sleep=no_sleep).succeeded
    assert completed.report["_meta"]["llm_used"] is False
