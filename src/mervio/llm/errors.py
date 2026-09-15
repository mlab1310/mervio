"""Erreurs de la couche LLM.

Aucun message ne transporte de contenu metier ni de reponse brute du modele:
ils finissent dans des logs. Seuls un code stable et un chemin de champ sont
exposes.
"""
from __future__ import annotations

from typing import Sequence

from ..errors import MervioError


class LLMError(MervioError):
    """Erreur de base de la couche LLM."""


class LLMContextError(LLMError):
    """Le rapport ne permet pas de construire un contexte sur."""


class ProviderError(LLMError):
    """Echec d'un fournisseur LLM.

    `transient` indique si une nouvelle tentative a un sens. Une erreur
    d'authentification ou un refus ne se corrige pas en reessayant.
    """

    code = "provider_error"
    transient = False


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    transient = True


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"
    transient = True


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limited"
    transient = True


class ProviderAuthenticationError(ProviderError):
    code = "provider_authentication_failed"


class ProviderRefusalError(ProviderError):
    code = "provider_refusal"


class ResponseValidationError(LLMError):
    """Reponse du modele rejetee. Porte des codes, jamais le texte rejete."""

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("reponse LLM rejetee: " + "; ".join(self.issues[:10]))
