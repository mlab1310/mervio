"""Fournisseur LLM deterministe, sans reseau ni cle.

Deux usages:
- sans script: produit une reponse STRUCTUREE et ANCREE, construite
  uniquement a partir des identifiants et des valeurs numeriques du contexte
  (demonstration du flux complet, tests de bout en bout);
- avec script: rejoue une sequence de sorties brutes, d'exceptions ou de
  fonctions, pour tester validation, erreurs et tentatives.

Il ne recopie jamais un texte non fiable du contexte dans sa reponse.
"""
from __future__ import annotations

import json
import time
from typing import Callable, List, Optional, Sequence, Union

from .contract import RESPONSE_SCHEMA_VERSION
from .prompt import LLMRequest, extract_context
from .provider import LLMProvider, ProviderResponse

ScriptStep = Union[str, BaseException, Callable[[LLMRequest], str]]

_PRIORITY = {"high": "high", "medium": "medium", "low": "low", "info": "low"}


class MockLLMProvider(LLMProvider):
    name = "mock"

    def __init__(self, script: Optional[Sequence[ScriptStep]] = None, delay_seconds: float = 0.0) -> None:
        self._script = list(script or [])
        self._delay = delay_seconds
        self.requests: List[LLMRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def generate(self, request: LLMRequest, *, model: str, timeout_seconds: float,
                 max_output_chars: int) -> ProviderResponse:
        index = len(self.requests)
        self.requests.append(request)
        if self._delay:
            time.sleep(self._delay)
        if not self._script:
            return ProviderResponse(grounded_response(request), usage={"input_chars": len(request.user_prompt)})
        step = self._script[min(index, len(self._script) - 1)]
        if isinstance(step, BaseException):
            raise step
        text = step(request) if callable(step) else step
        return ProviderResponse(text)


def grounded_response(request: LLMRequest) -> str:
    """Reponse valide par construction: ids et nombres repris du contexte."""
    context = extract_context(request)
    health = context["business_health"]
    currency = context.get("currency") or ""
    period = (context.get("analysis_period") or {}).get("current") or {}
    summary = (f"Business Health Score {health['score']}/100, calcule par le moteur analytique."
               if health["score"] is not None else "Business Health Score non calculable a partir des donnees.")
    if period.get("label"):
        summary = f"Periode {period['label']}. {summary}"

    facts = [{"statement": f"{fact['label']}: {fact['value']}" + (f" {currency}" if fact["unit"] == "currency" else ""),
              "refs": [fact["id"]]}
             for fact in context["facts"][:3]]
    facts.append({"statement": summary, "refs": [health["id"]]})
    response = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "summary": summary,
        "facts": facts,
        "explanations": [{"statement": "Constat etabli par le moteur analytique, voir la reference.",
                          "refs": [finding["id"]]} for finding in context["findings"][:3]],
        "hypotheses": [{"statement": "Hypothese non prouvee fournie par le moteur, a verifier dans l'outil source.",
                        "refs": [hypothesis["id"]]} for hypothesis in context["hypotheses"][:3]],
        "recommendations": [{"action": "Appliquer la recommandation referencee apres verification dans l'outil source.",
                             "priority": _PRIORITY.get(reco["severity"], "low"), "refs": [reco["id"]]}
                            for reco in context["recommendations"][:3]],
        "limitations": [{"statement": f"{item['label']}: indisponible, non estime.", "refs": [item["id"]]}
                        for item in context["unavailable_metrics"][:5]],
    }
    return json.dumps(response, ensure_ascii=False)
