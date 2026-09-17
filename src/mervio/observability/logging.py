"""Logs JSON correles (Mission 004.2, unifies en 004.3; ADR-004-009).

Une ligne = un objet JSON sur une ligne, lisible par une machine:

    {"timestamp": "...", "level": "INFO", "service": "worker", "environment": "production",
     "event": "job.succeeded", "correlation_id": "...", "job_id": "...",
     "organization_id": "...", "job_type": "import", "duration_ms": 1234}

Sans dependance: la bibliotheque standard suffit. Pas d'OpenTelemetry (ADR-004.2-005).

Une seule configuration de processus: `configure(format="json" | "text")`. La CLI garde
sa sortie texte historique (`logging_config.configure_logging` delegue ici, format
identique, sans redaction ajoutee a ce que l'utilisateur lit dans son terminal).

Correlation: `correlation_scope` pose l'identifiant dans un `ContextVar`. Le formateur
le relit pour CHAQUE enregistrement du logger `mervio`, y compris ceux des modules du
moteur qui ignorent les travaux: une execution se recolle sans toucher a leur code.
Un fil d'execution n'herite pas du contexte: `bind_context` le lui transmet.

Redaction (`redaction.py`): une cle sensible est masquee, un texte est nettoye de ses
secrets, chemins absolus et e-mails, une exception ne publie que son type, une valeur
est tronquee. Le filtre est volontairement large: perdre un champ de diagnostic coute
moins cher que publier un jeton.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, Mapping, Optional

from .redaction import (
    FORBIDDEN_KEY_FRAGMENTS, REDACTED, describe_exception, forbidden_key, looks_like_absolute_path, redact_text,
)

#: Tronquage des chaines: un log n'est pas un stockage.
MAX_VALUE_LENGTH = 200
#: Profondeur maximale d'un objet imbrique publie dans un log.
MAX_DEPTH = 3

#: Format texte historique de la CLI (inchange depuis la mission 001).
TEXT_FORMAT = "%(levelname)s %(name)s: %(message)s"
FORMATS = ("json", "text")
_HANDLER_NAMES = ("mervio-json", "mervio-text")

#: Evenements publies par le worker et son environnement (contrat de 004.3). Les modules du
#: moteur publient des lignes `log` (evenement generique) avec un `message` nettoye.
EVENTS = frozenset({
    # cycle de vie du processus
    "worker.starting", "worker.config", "worker.database_connected", "worker.database_unavailable",
    "worker.database_recovered", "worker.schema_checked", "worker.ready", "worker.stopping",
    "worker.shutdown_forced", "worker.stopped", "worker.identity_refused", "worker.crashed", "worker.exit_forced",
    "worker.state_changed", "worker.callback_failed",
    # travaux (004.2 et 004.3)
    "job.enqueued", "job.claimed", "job.succeeded", "job.failed", "job.requeued", "job.recovered",
    "job.cancelled", "job.lease_renewed", "job.lease_renew_failed", "job.lease_lost", "job.result_unrecorded",
    "job.abandoned", "job.publish_retry", "job.organization_skipped", "job.release_failed", "job.lease_keeper_stuck", "job.lease_callback_failed",
    # gestionnaires
    "import.started", "import.succeeded", "import.failed", "analysis.started", "analysis.succeeded",
    "analysis.failed", "purge.started", "purge.completed",
})

_CORRELATION_ID: ContextVar[Optional[str]] = ContextVar("mervio_correlation_id", default=None)

#: Attributs standard de LogRecord, a ne pas recopier dans le JSON.
_RECORD_ATTRIBUTES = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)))


def new_correlation_id() -> str:
    """Nouvel identifiant de correlation (UUID4 textuel)."""
    return str(uuid.uuid4())


def current_correlation_id() -> Optional[str]:
    return _CORRELATION_ID.get()


@contextmanager
def correlation_scope(correlation_id: Optional[str] = None) -> Iterator[str]:
    """Pose l'identifiant de correlation pour la duree du bloc, puis le restaure."""
    resolved = str(correlation_id) if correlation_id else new_correlation_id()
    token = _CORRELATION_ID.set(resolved)
    try:
        yield resolved
    finally:
        _CORRELATION_ID.reset(token)


def bind_context(function: Callable[..., Any]) -> Callable[..., Any]:
    """`function` executee dans une COPIE du contexte courant (correlation comprise).

    Pour un fil d'execution: `threading.Thread(target=bind_context(work))`. La copie
    isole les deux fils: un scope ouvert dans l'un n'affecte pas l'autre.
    """
    context = contextvars.copy_context()

    def run(*args: Any, **kwargs: Any) -> Any:
        return context.run(function, *args, **kwargs)

    return run


# -- redaction ---------------------------------------------------------------------------

def _forbidden_key(key: str) -> bool:
    return forbidden_key(key)


def _scrub_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, BaseException):
        # le type, jamais le message: il peut contenir une valeur de donnee
        return describe_exception(value)
    if isinstance(value, Mapping):
        if depth >= MAX_DEPTH:
            return REDACTED
        return {str(k): (REDACTED if _forbidden_key(k) else _scrub_value(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        if depth >= MAX_DEPTH:
            return REDACTED
        return [_scrub_value(v, depth + 1) for v in list(value)[:20]]
    text = value if isinstance(value, str) else str(value)
    # une valeur qui EST un chemin absolu peut porter un nom de client ou de fichier exporte
    if looks_like_absolute_path(text):
        return REDACTED
    text = redact_text(text)
    if len(text) > MAX_VALUE_LENGTH:
        return text[:MAX_VALUE_LENGTH] + "..."
    return text


def scrub(fields: Mapping[str, Any]) -> Dict[str, Any]:
    """Champs publiables: cle sensible masquee, valeur nettoyee et tronquee."""
    return {str(k): (REDACTED if _forbidden_key(k) else _scrub_value(v))
            for k, v in fields.items() if v is not None}


def scrub_text(text: Any) -> str:
    """Un texte libre publiable (message d'erreur, detail): nettoye, jamais tronque."""
    return redact_text(str(text))


# -- formatage ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """Formate un enregistrement en un objet JSON d'une seule ligne."""

    def __init__(self, *, service: str = "worker", environment: Optional[str] = None) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        document: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": getattr(record, "event", None) or "log",
        }
        if self.environment:
            document["environment"] = self.environment
        correlation_id = getattr(record, "correlation_id", None) or current_correlation_id()
        if correlation_id:
            document["correlation_id"] = correlation_id
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - un format de message casse ne doit pas perdre la ligne
            message = str(record.msg)
        if message:
            document["message"] = _scrub_value(message)
        extra = {k: v for k, v in vars(record).items()
                 if k not in _RECORD_ATTRIBUTES and k not in ("event", "correlation_id")}
        document.update(scrub(extra))
        if record.exc_info and record.exc_info[0] is not None:
            # le type de l'exception, jamais son message ni sa trace: ils peuvent
            # contenir une valeur de donnee (ADR-004.2-006)
            document["error_type"] = record.exc_info[0].__name__
        return json.dumps(document, ensure_ascii=False, sort_keys=True, default=str)


class RedactingTextFormatter(logging.Formatter):
    """Format texte lisible, nettoye comme le JSON (developpement du worker)."""

    def __init__(self) -> None:
        super().__init__(TEXT_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        cleaned = logging.makeLogRecord(dict(vars(record), msg=_scrub_value(record.getMessage()), args=None,
                                             exc_info=None, exc_text=None, stack_info=None))
        line = super().format(cleaned)
        if record.exc_info and record.exc_info[0] is not None:
            line += f" [error_type={record.exc_info[0].__name__}]"
        return line


def configure(*, format: str = "json", level: int = logging.INFO, service: str = "worker",
              environment: Optional[str] = None, stream=None, redact: bool = True) -> logging.Handler:
    """Configure LE gestionnaire du logger `mervio` pour tout le processus.

    Idempotent: un nouvel appel remplace le gestionnaire precedent (JSON ou texte), sans
    jamais dupliquer les lignes. En JSON, `mervio` ne propage plus vers la racine: une
    ligne n'est ecrite qu'une fois et toujours nettoyee. En texte, la propagation est
    laissee telle quelle (outils de test, CLI).
    """
    if format not in FORMATS:
        raise ValueError(f"format de log inconnu: {format}")
    root = logging.getLogger("mervio")
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    if format == "json":
        handler.setFormatter(JsonFormatter(service=service, environment=environment))
        handler.set_name("mervio-json")
    else:
        handler.setFormatter(RedactingTextFormatter() if redact else logging.Formatter(TEXT_FORMAT))
        handler.set_name("mervio-text")
    for existing in list(root.handlers):
        if existing.get_name() in _HANDLER_NAMES:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    if format == "json":
        root.propagate = False
    return handler


def configure_json_logging(*, level: int = logging.INFO, service: str = "worker", stream=None,
                           environment: Optional[str] = None) -> logging.Handler:
    """Sortie JSON du logger `mervio` (API de 004.2, conservee)."""
    return configure(format="json", level=level, service=service, stream=stream, environment=environment)


# -- emission ----------------------------------------------------------------------------

class EventLogger:
    """Logger d'evenements nommes, avec des champs attaches.

    `bind` renvoie un NOUVEL objet: aucun etat mutable partage entre workers.
    """

    __slots__ = ("_logger", "_fields")

    def __init__(self, name: str, fields: Optional[Mapping[str, Any]] = None) -> None:
        self._logger = logging.getLogger(f"mervio.{name}") if not name.startswith("mervio.") else \
            logging.getLogger(name)
        self._fields: Dict[str, Any] = dict(fields or {})

    def bind(self, **fields: Any) -> "EventLogger":
        merged = dict(self._fields)
        merged.update({k: v for k, v in fields.items() if v is not None})
        return EventLogger(self._logger.name, merged)

    @property
    def fields(self) -> Dict[str, Any]:
        return dict(self._fields)

    def event(self, name: str, /, *, level: int = logging.INFO, exc_info: bool = False,
              **fields: Any) -> None:
        """Le nom de l'evenement est positionnel: un champ nomme `name` reste un champ."""
        merged = dict(self._fields)
        merged.update(fields)
        # une cle qui heurte un attribut standard de LogRecord ferait echouer logging
        extra = {k: v for k, v in scrub(merged).items() if k not in _RECORD_ATTRIBUTES and k != "message"}
        extra["event"] = name
        correlation_id = merged.get("correlation_id") or current_correlation_id()
        if correlation_id:
            extra["correlation_id"] = str(correlation_id)
        self._logger.log(level, "", extra=extra, exc_info=exc_info)

    def debug(self, name: str, /, **fields: Any) -> None:
        self.event(name, level=logging.DEBUG, **fields)

    def info(self, name: str, /, **fields: Any) -> None:
        self.event(name, level=logging.INFO, **fields)

    def warning(self, name: str, /, **fields: Any) -> None:
        self.event(name, level=logging.WARNING, **fields)

    def error(self, name: str, /, *, exc_info: bool = False, **fields: Any) -> None:
        self.event(name, level=logging.ERROR, exc_info=exc_info, **fields)


def get_event_logger(name: str, **fields: Any) -> EventLogger:
    return EventLogger(name, fields)


__all__ = [
    "EVENTS", "FORBIDDEN_KEY_FRAGMENTS", "FORMATS", "MAX_VALUE_LENGTH", "REDACTED", "TEXT_FORMAT", "EventLogger",
    "JsonFormatter", "RedactingTextFormatter", "bind_context", "configure", "configure_json_logging",
    "correlation_scope", "current_correlation_id", "get_event_logger", "new_correlation_id", "scrub", "scrub_text",
]
