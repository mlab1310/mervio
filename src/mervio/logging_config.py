"""Logging centralise.

Depuis la mission 004.3, la configuration passe par un seul point
(`mervio.observability.logging.configure`). Le comportement de la CLI est inchange:
sortie texte `NIVEAU logger: message` sur stderr, configuree une seule fois par
processus, sans redaction ajoutee a ce que l'utilisateur lit dans son terminal.
"""
from __future__ import annotations

import logging
import sys

from .observability.logging import configure

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    configure(format="text", level=level, service="cli", stream=sys.stderr, redact=False)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mervio.{name}")
