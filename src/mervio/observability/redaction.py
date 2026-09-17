"""Redaction des valeurs publiees (logs, audit, erreurs de travaux) - Mission 004.3.

Deux filtres complementaires:

- par CLE (`forbidden_key`): une cle qui evoque un secret, une PII, un chemin ou un
  contenu est entierement masquee, quelle que soit sa valeur;
- par CONTENU (`redact_text`): dans un texte libre (message, erreur, valeur), on masque
  ce qui ressemble a un secret meme sous une cle anodine:

      identifiants d'URL        postgresql://mervio:s3cret@db/x   -> postgresql://mervio:[redacted]@db/x
      paires cle=valeur         password=s3cret, "token": "abc"  -> password=[redacted]
      jetons Bearer             Authorization: Bearer eyJ...      -> Bearer [redacted]
      jetons connus             sk-..., ghp_..., AKIA..., shpat_..., xox?-..., JWT
      chemins absolus           /Users/x/export.csv, C:\\data\\x   -> [redacted-path]
      adresses e-mail           alice@example.com                 -> [redacted-email]

Choix assumes:
- les empreintes hexadecimales (sha256) ne sont PAS masquees: elles sont publiees
  volontairement (provenance) et ne revelent rien;
- un chemin RELATIF n'est pas reconnaissable dans un texte libre: la regle reste de ne
  jamais journaliser une charge utile de travail (elle localise des fichiers);
- le filtre est large: perdre un diagnostic coute moins cher que publier un jeton.

Bibliotheque standard uniquement: importable par le moteur, le worker et la persistance.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"
REDACTED_PATH = "[redacted-path]"
REDACTED_EMAIL = "[redacted-email]"

#: Fragments de cle interdits. Comparaison sur la cle en minuscules, par inclusion.
FORBIDDEN_KEY_FRAGMENTS = (
    "password", "passwd", "secret", "token", "credential", "authorization",
    "api_key", "apikey", "private_key", "cookie", "signature",
    "email", "phone", "customer", "address", "prompt", "completion",
    "path", "filename", "file_name", "content", "body", "raw",
    # 004.3: chaines de connexion et equivalents
    "dsn", "database_url", "conninfo", "access_key", "session_key",
)

_SECRET_WORDS = (r"password|passwd|pwd|secret|client_secret|token|access_token|refresh_token|api[_-]?key|"
                 r"apikey|authorization|access[_-]?key|private[_-]?key|session[_-]?key|cookie|signature")

_PATTERNS = (
    # identifiants dans une URL: on garde le schema, l'utilisateur et l'hote
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]*):[^\s@/]+@"), r"\1:" + REDACTED + "@"),
    # Bearer / Basic
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}"), r"\1 " + REDACTED),
    # cle=valeur, cle: valeur, "cle": "valeur" (y compris libpq: password=...)
    (re.compile(r"""(?i)(["']?\b(?:""" + _SECRET_WORDS + r""")\b["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;&}]+)"""),
     r"\1" + REDACTED),
    # jetons de fournisseurs reconnaissables
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"), REDACTED),   # JWT
    (re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test)?[-_]?[A-Za-z0-9]{12,}\b"), REDACTED),       # Stripe, OpenAI...
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b"), REDACTED),                                  # Anthropic
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),                                 # GitHub
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),                                  # AWS
    (re.compile(r"\bshp(?:at|ss|ca|pa)_[A-Fa-f0-9]{20,}\b"), REDACTED),                        # Shopify
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), REDACTED),                               # Slack
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), REDACTED),                                     # Google
    # chemins absolus (POSIX, Windows, repertoire personnel)
    # au moins deux segments (/a/b) hors d'une URL (le `/` d'une URL suit un mot ou `:`), ou ~/...
    (re.compile(r"(?<![\w.:/~-])(?:~|/[^\s/'\",;:)\]}]+)(?:/[^\s'\",;:)\]}]*)+"), REDACTED_PATH),
    (re.compile(r"(?<![\w])[A-Za-z]:\\[^\s'\",;)\]}]*"), REDACTED_PATH),
    (re.compile(r"\\\\[^\s\\]+\\[^\s'\",;)\]}]*"), REDACTED_PATH),
    # adresses e-mail
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"), REDACTED_EMAIL),
)


def forbidden_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in FORBIDDEN_KEY_FRAGMENTS)


def looks_like_absolute_path(text: str) -> bool:
    return text.startswith("/") or text.startswith("\\\\") or (len(text) > 3 and text[1:3] == ":\\")


def redact_text(text: str) -> str:
    """Le texte, avec tout ce qui ressemble a un secret, un chemin absolu ou un e-mail masque."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def describe_exception(exc: BaseException) -> str:
    """Ce qu'on publie d'une exception: son type, jamais son message (ADR-004.2-006)."""
    return type(exc).__name__


__all__ = [
    "FORBIDDEN_KEY_FRAGMENTS", "REDACTED", "REDACTED_EMAIL", "REDACTED_PATH", "describe_exception",
    "forbidden_key", "looks_like_absolute_path", "redact_text",
]
