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

__all__ = [
    "CONTRACT_VERSION", "PROMPT_VERSION", "RESPONSE_SCHEMA_VERSION", "SYSTEM_CONTRACT",
    "ContextLimits", "LLMContextError", "LLMError", "ProviderAuthenticationError", "ProviderError",
    "ProviderRateLimitError", "ProviderRefusalError", "ProviderTimeoutError",
    "ProviderUnavailableError", "ResponseValidationError", "build_llm_context", "context_ids",
    "serialize_context",
]
