"""Abstraction fournisseur LLM et execution bornee.

Le moteur analytique ne connait aucun fournisseur. Un fournisseur (OpenAI,
Claude, local...) implemente LLMProvider.generate() et rien d'autre; aucune
implementation reelle n'existe dans cette version, et aucun SDK n'est requis.

Garanties d'execution, quel que soit le fournisseur:
- delai fini: l'appel s'execute dans un thread demon attendu au plus
  `timeout_seconds`; au-dela, ProviderTimeoutError. Un fournisseur reseau doit
  en plus appliquer ce delai a sa propre connexion;
- tentatives bornees: uniquement sur erreur transitoire, jamais sur une
  erreur d'authentification, un refus ou une reponse invalide;
- taille de sortie bornee avant toute analyse.
"""
from __future__ import annotations

import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from ..errors import ConfigurationError
from ..logging_config import get_logger
from .errors import ProviderError, ProviderTimeoutError
from .prompt import LLMRequest

log = get_logger("llm.provider")

MAX_TIMEOUT_SECONDS = 120.0
MAX_RETRIES = 3


@dataclass(frozen=True)
class LLMConfig:
    #: identifiant du modele, transmis tel quel au fournisseur
    model: str = "unconfigured"
    timeout_seconds: float = 30.0
    #: tentatives supplementaires sur erreur transitoire (0 = une seule tentative)
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    max_output_chars: int = 20_000

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ConfigurationError("modele LLM non renseigne")
        if not _finite(self.timeout_seconds) or not 0 < self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ConfigurationError(f"timeout LLM invalide: doit etre fini, dans ]0, {MAX_TIMEOUT_SECONDS}]")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) \
                or not 0 <= self.max_retries <= MAX_RETRIES:
            raise ConfigurationError(f"max_retries invalide: entier dans [0, {MAX_RETRIES}]")
        if not _finite(self.retry_backoff_seconds) or not 0 <= self.retry_backoff_seconds <= 10:
            raise ConfigurationError("retry_backoff_seconds invalide: dans [0, 10]")
        if isinstance(self.max_output_chars, bool) or not isinstance(self.max_output_chars, int) \
                or not 1 <= self.max_output_chars <= 200_000:
            raise ConfigurationError("max_output_chars invalide: dans [1, 200000]")


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    #: consommation de tokens si le fournisseur la communique
    usage: Optional[Dict[str, int]] = None


class LLMProvider(ABC):
    """Contrat minimal d'un fournisseur."""

    name: str = "abstract"

    @abstractmethod
    def generate(self, request: LLMRequest, *, model: str, timeout_seconds: float,
                 max_output_chars: int) -> ProviderResponse:
        """Retourne la reponse brute du modele ou leve une ProviderError."""


class ProviderOutputTooLargeError(ProviderError):
    code = "provider_output_too_large"


class ProviderInvalidResponseError(ProviderError):
    code = "provider_invalid_response"


@dataclass(frozen=True)
class ProviderCall:
    response: ProviderResponse
    attempts: int


def call_provider(
    provider: LLMProvider,
    request: LLMRequest,
    config: LLMConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> ProviderCall:
    """Appelle le fournisseur avec delai et tentatives bornes.

    Leve une ProviderError portant `attempts`. Toute exception inattendue est
    convertie en ProviderError non transitoire, sans reprendre son message.
    """
    last_attempt = config.max_retries + 1
    for attempt in range(1, last_attempt + 1):
        try:
            response = _run_with_timeout(
                lambda: provider.generate(request, model=config.model, timeout_seconds=config.timeout_seconds,
                                          max_output_chars=config.max_output_chars),
                config.timeout_seconds,
            )
            if not isinstance(response, ProviderResponse) or not isinstance(response.text, str):
                raise ProviderInvalidResponseError("reponse fournisseur de type invalide")
            if len(response.text) > config.max_output_chars:
                raise ProviderOutputTooLargeError("reponse fournisseur trop volumineuse")
            return ProviderCall(response, attempt)
        except ProviderError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 - un fournisseur tiers peut lever n'importe quoi
            error = ProviderError(f"erreur inattendue du fournisseur ({type(exc).__name__})")
        error.attempts = attempt
        if not error.transient or attempt == last_attempt:
            log.warning("fournisseur %s: echec %s apres %s tentative(s)", provider.name, error.code, attempt)
            raise error
        log.info("fournisseur %s: %s, nouvelle tentative %s/%s", provider.name, error.code, attempt + 1, last_attempt)
        sleep(config.retry_backoff_seconds * attempt)
    raise AssertionError("inatteignable")  # pragma: no cover


def _run_with_timeout(fn: Callable[[], ProviderResponse], timeout: float) -> ProviderResponse:
    box: dict = {}
    done = threading.Event()

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - relaye au thread appelant
            box["error"] = exc
        finally:
            done.set()

    # thread demon: un fournisseur bloque ne peut empecher ni le retour de
    # l'appelant ni l'arret du processus
    threading.Thread(target=target, name="mervio-llm-call", daemon=True).start()
    if not done.wait(timeout):
        raise ProviderTimeoutError("delai d'appel du fournisseur depasse")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
