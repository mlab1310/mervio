"""Observabilite: logs JSON correles, redaction, sante du worker (Missions 004.2 et 004.3).

Paquet sans dependance et sans acces base: importable par le moteur comme par
le worker.
"""
from .health import HealthReporter, HealthVerdict, WorkerState, check_health
from .logging import (
    EventLogger,
    JsonFormatter,
    bind_context,
    configure,
    configure_json_logging,
    correlation_scope,
    current_correlation_id,
    get_event_logger,
    new_correlation_id,
    scrub,
    scrub_text,
)
from .redaction import redact_text

__all__ = [
    "EventLogger", "HealthReporter", "HealthVerdict", "JsonFormatter", "WorkerState", "bind_context",
    "check_health", "configure", "configure_json_logging", "correlation_scope", "current_correlation_id",
    "get_event_logger", "new_correlation_id", "redact_text", "scrub", "scrub_text",
]
