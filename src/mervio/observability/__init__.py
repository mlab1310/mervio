"""Observabilite: logs JSON correles (Mission 004.2).

Paquet sans dependance et sans acces base: importable par le moteur comme par
le worker.
"""
from .logging import (
    EventLogger,
    JsonFormatter,
    configure_json_logging,
    correlation_scope,
    current_correlation_id,
    get_event_logger,
    new_correlation_id,
    scrub,
)

__all__ = [
    "EventLogger", "JsonFormatter", "configure_json_logging", "correlation_scope",
    "current_correlation_id", "get_event_logger", "new_correlation_id", "scrub",
]
