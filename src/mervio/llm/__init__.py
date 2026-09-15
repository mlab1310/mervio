"""Couche d'interpretation LLM, au-dessus du rapport deterministe.

Le moteur analytique reste la source de verite: cette couche construit un
contexte borne, l'envoie a un fournisseur interchangeable et valide la
reponse structuree. Elle ne modifie jamais le rapport.
"""
from .context import build_llm_context, context_ids, serialize_context
from .contract import (
    CONTRACT_VERSION, PROMPT_VERSION, RESPONSE_SCHEMA_VERSION, SYSTEM_CONTRACT, ContextLimits,
)
from .errors import (
    LLMContextError, LLMError, ProviderAuthenticationError, ProviderError, ProviderRateLimitError,
    ProviderRefusalError, ProviderTimeoutError, ProviderUnavailableError, ResponseValidationError,
)
from .mock import MockLLMProvider
from .prompt import LLMRequest, build_request
from .provider import LLMConfig, LLMProvider, ProviderResponse, call_provider
from .response import BusinessExplanation, validate_response
from .service import ExplanationResult, explain_analysis, explain_report

__all__ = [
    "CONTRACT_VERSION", "PROMPT_VERSION", "RESPONSE_SCHEMA_VERSION", "SYSTEM_CONTRACT",
    "BusinessExplanation", "ContextLimits", "ExplanationResult", "LLMConfig", "LLMContextError",
    "LLMError", "LLMProvider", "LLMRequest", "MockLLMProvider", "ProviderAuthenticationError",
    "ProviderError", "ProviderRateLimitError", "ProviderRefusalError", "ProviderResponse",
    "ProviderTimeoutError", "ProviderUnavailableError", "ResponseValidationError", "build_llm_context",
    "build_request", "call_provider", "context_ids", "explain_analysis", "explain_report",
    "serialize_context", "validate_response",
]
