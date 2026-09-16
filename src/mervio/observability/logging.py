"""Logs JSON correles (Mission 004.2, ADR-004-009).

Une ligne = un objet JSON sur une ligne, lisible par une machine:

    {"timestamp": "...", "level": "INFO", "event": "job.succeeded",
     "correlation_id": "...", "job_id": "...", "organization_id": "...",
     "job_type": "import", "duration_ms": 1234}

Sans dependance: la bibliotheque standard suffit. Pas d'OpenTelemetry dans cette
mission (ADR-004.2-005).

Correlation: `correlation_scope` pose l'identifiant dans un `ContextVar`. Le
formateur le relit pour CHAQUE enregistrement du logger `mervio`, y compris ceux
des modules existants qui ne connaissent pas les jobs: une execution complete se
recolle sans toucher au code du moteur.

Redaction: `event()` refuse de publier une valeur dont la CLE evoque un secret ou
une PII, et tronque ou masque une valeur qui ressemble a un chemin de fichier ou
a un contenu. Le filtre est volontairement large: perdre un champ de diagnostic
coute moins cher que publier un jeton.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Mapping, Optional

REDACTED = "[redacted]"
#: Tronquage des chaines: un log n'est pas un stockage.
MAX_VALUE_LENGTH = 200
#: Profondeur maximale d'un objet imbrique publie dans un log.
MAX_DEPTH = 3

#: Fragments de cle interdits. Comparaison sur la cle en minuscules, par inclusion.
FORBIDDEN_KEY_FRAGMENTS = (
    "password", "passwd", "secret", "token", "credential", "authorization",
    "api_key", "apikey", "private_key", "cookie", "signature",
    "email", "phone", "customer", "address", "prompt", "completion",
    "path", "filename", "file_name", "content", "body", "raw",
)

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


# -- redaction ---------------------------------------------------------------------------

def _forbidden_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in FORBIDDEN_KEY_FRAGMENTS)


def _scrub_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
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
    # un chemin absolu peut porter un nom de client ou un nom de fichier exporte
    if text.startswith("/") or text.startswith("\\\\") or (len(text) > 3 and text[1:3] == ":\\"):
        return REDACTED
    if len(text) > MAX_VALUE_LENGTH:
        return text[:MAX_VALUE_LENGTH] + "..."
    return text


def scrub(fields: Mapping[str, Any]) -> Dict[str, Any]:
    """Champs publiables: cle sensible masquee, valeur tronquee, chemin masque."""
    return {str(k): (REDACTED if _forbidden_key(k) else _scrub_value(v))
            for k, v in fields.items() if v is not None}


# -- formatage ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """Formate un enregistrement en un objet JSON d'une seule ligne."""

    def __init__(self, *, service: str = "worker") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        document: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": getattr(record, "event", None) or "log",
        }
        correlation_id = getattr(record, "correlation_id", None) or current_correlation_id()
        if correlation_id:
            document["correlation_id"] = correlation_id
        message = record.getMessage()
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


def configure_json_logging(*, level: int = logging.INFO, service: str = "worker", stream=None) -> logging.Handler:
    """Remplace les gestionnaires du logger `mervio` par une sortie JSON.

    Idempotent par (service, flux): rappeler la fonction ne duplique pas les lignes.
    """
    root = logging.getLogger("mervio")
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter(service=service))
    handler.set_name("mervio-json")
    for existing in list(root.handlers):
        if existing.get_name() == "mervio-json":
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    return handler


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

    def info(self, name: str, /, **fields: Any) -> None:
        self.event(name, level=logging.INFO, **fields)

    def warning(self, name: str, /, **fields: Any) -> None:
        self.event(name, level=logging.WARNING, **fields)

    def error(self, name: str, /, *, exc_info: bool = False, **fields: Any) -> None:
        self.event(name, level=logging.ERROR, exc_info=exc_info, **fields)


def get_event_logger(name: str, **fields: Any) -> EventLogger:
    return EventLogger(name, fields)
