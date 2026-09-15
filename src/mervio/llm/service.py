"""Orchestration: rapport deterministe -> explication validee.

Comme analyze_dataset() (D-022), ce service ne leve pas: il rapporte. Un echec
de la couche LLM (contexte refuse, fournisseur indisponible, reponse rejetee)
rend l'explication INDISPONIBLE et laisse le rapport analytique intact: une
analyse reussie ne devient jamais une analyse echouee a cause du modele.

Le rapport n'est jamais modifie. L'explication est un artefact separe; le
score qu'elle porte est recopie du contexte deterministe.

Journalisation: metadonnees uniquement (fournisseur, modele, versions,
empreinte, statut, tentatives, latence). Jamais le prompt ni la reponse.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from ..logging_config import get_logger
from .context import build_llm_context
from .contract import CONTRACT_VERSION, PROMPT_VERSION, RESPONSE_SCHEMA_VERSION, ContextLimits
from .errors import LLMContextError, ProviderError, ResponseValidationError
from .prompt import build_request
from .provider import LLMConfig, LLMProvider, call_provider
from .response import BusinessExplanation, validate_response

log = get_logger("llm.service")


@dataclass(frozen=True)
class ExplanationResult:
    status: str  # completed | unavailable
    explanation: Optional[BusinessExplanation] = None
    error_code: Optional[str] = None
    #: codes de validation, sans le texte rejete
    issues: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" and self.explanation is not None

    def to_dict(self) -> dict:
        return {
            "status": self.status, "error_code": self.error_code, "issues": list(self.issues),
            "explanation": self.explanation.to_dict() if self.explanation else None,
            "metadata": dict(self.metadata),
        }


def explain_report(
    report: Mapping[str, Any],
    provider: LLMProvider,
    config: Optional[LLMConfig] = None,
    *,
    limits: Optional[ContextLimits] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ExplanationResult:
    config = config or LLMConfig()
    metadata: Dict[str, Any] = {
        "provider": getattr(provider, "name", type(provider).__name__), "model": config.model,
        "prompt_version": PROMPT_VERSION, "contract_version": CONTRACT_VERSION,
        "response_schema_version": RESPONSE_SCHEMA_VERSION, "request_fingerprint": None,
        "attempts": 0, "latency_ms": None, "usage": None,
    }

    def unavailable(code: str, issues: Tuple[str, ...] = ()) -> ExplanationResult:
        log.warning("explication indisponible (%s) fournisseur=%s modele=%s tentatives=%s",
                    code, metadata["provider"], metadata["model"], metadata["attempts"])
        return ExplanationResult("unavailable", error_code=code, issues=issues, metadata=metadata)

    try:
        context = build_llm_context(report, limits)
        request = build_request(context)
    except LLMContextError:
        return unavailable("context_invalid")
    except Exception as exc:  # noqa: BLE001 - le rapport doit survivre a tout echec de cette couche
        log.error("construction du contexte: erreur inattendue %s", type(exc).__name__)
        return unavailable("context_error")
    metadata["request_fingerprint"] = request.fingerprint

    started = clock()
    try:
        call = call_provider(provider, request, config, sleep=sleep)
    except ProviderError as exc:
        metadata["attempts"] = getattr(exc, "attempts", 1)
        metadata["latency_ms"] = round((clock() - started) * 1000)
        return unavailable(exc.code)
    metadata.update(attempts=call.attempts, latency_ms=round((clock() - started) * 1000),
                    usage=dict(call.response.usage) if call.response.usage else None)

    try:
        explanation = validate_response(call.response.text, context, max_chars=config.max_output_chars)
    except ResponseValidationError as exc:
        # pas de nouvelle tentative: une reponse non ancree ne se corrige pas en reessayant
        return unavailable("invalid_response", exc.issues)
    except Exception as exc:  # noqa: BLE001
        log.error("validation: erreur inattendue %s", type(exc).__name__)
        return unavailable("validation_error")

    log.info("explication produite fournisseur=%s modele=%s empreinte=%s tentatives=%s latence_ms=%s",
             metadata["provider"], metadata["model"], request.fingerprint[:12], call.attempts,
             metadata["latency_ms"])
    return ExplanationResult("completed", explanation=explanation, metadata=metadata)


def explain_analysis(result: Any, provider: LLMProvider, config: Optional[LLMConfig] = None,
                     **kwargs: Any) -> ExplanationResult:
    """Explique un AnalysisResult reussi; ne touche jamais au resultat."""
    if not getattr(result, "succeeded", False):
        return ExplanationResult("unavailable", error_code="analysis_not_completed",
                                 metadata={"provider": getattr(provider, "name", None),
                                           "contract_version": CONTRACT_VERSION})
    return explain_report(result.report, provider, config, **kwargs)
