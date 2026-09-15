"""Primitives d'ingestion partagees par toutes les sources."""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from ..errors import InvalidDataError, MissingColumnsError

_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%m/%d/%Y",
)

_NULL_TOKENS = {"", "-", "--", "n/a", "na", "null", "none", "nan"}

#: encodages tentes dans l'ordre. Les exports Shopify/Stripe sont en UTF-8,
#: mais un passage par Excel sous Windows produit regulierement du cp1252.
_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

#: separateurs supportes. Un export genere sur un poste francais sort en ';'
#: et un fichier lu avec le mauvais separateur donne UNE colonne: illisible.
_DELIMITERS = (",", ";", "\t", "|")

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def redact(value: object, limit: int = 40) -> str:
    """Neutralise une valeur avant de la placer dans un message d'erreur.

    Une colonne decalee peut faire arriver un email ou un nom dans une colonne
    numerique. Les messages d'erreur finissent dans les logs: ils ne doivent
    jamais transporter de donnee personnelle.
    """
    text = str(value)
    text = _EMAIL_RE.sub("[email masque]", text)
    return text if len(text) <= limit else text[:limit] + "..."


def sniff_delimiter(text: str) -> str:
    """Choisit le separateur qui decoupe le plus de colonnes dans l'en-tete.

    Plus fiable que csv.Sniffer sur des en-tetes courts contenant des virgules
    dans les libelles.
    """
    header = text.splitlines()[0] if text.splitlines() else ""
    if not header:
        return ","
    best, best_count = ",", header.count(",")
    for candidate in _DELIMITERS:
        count = header.count(candidate)
        if count > best_count:
            best, best_count = candidate, count
    return best


def read_csv(path: str | Path, source: str, quality=None) -> List[dict]:
    """Lit un CSV en liste de dicts, en nettoyant BOM et espaces d'en-tete."""
    p = Path(path)
    if not p.exists():
        raise InvalidDataError(f"fichier introuvable: {p}", source=source)
    text = None
    used_encoding = None
    for encoding in _ENCODINGS:
        try:
            text = p.read_text(encoding=encoding)
            used_encoding = encoding
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        raise InvalidDataError("encodage de fichier illisible (utf-8, cp1252 et latin-1 ont echoue)",
                               source=source)
    if used_encoding != "utf-8-sig" and quality is not None:
        quality.add_issue(source, "encoding_fallback", "warning",
                          f"fichier lu en {used_encoding} et non en UTF-8: verifier les accents")

    delimiter = sniff_delimiter(text)
    if delimiter != "," and quality is not None:
        quality.add_issue(source, "delimiter_detected", "info",
                          f"separateur detecte: {delimiter!r} au lieu de la virgule")

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        raise InvalidDataError("CSV vide ou sans en-tete", source=source)
    rows = []
    for raw in reader:
        rows.append({(k or "").strip(): (v.strip() if isinstance(v, str) else v)
                     for k, v in raw.items() if k is not None})
    return rows


def require_columns(rows: Sequence[dict], required: Iterable[str], source: str) -> None:
    if not rows:
        raise InvalidDataError("aucune ligne de donnee", source=source)
    present = set(rows[0].keys())
    missing = [c for c in required if c not in present]
    if missing:
        raise MissingColumnsError(missing, source=source)


def is_null(value: Optional[str]) -> bool:
    return value is None or str(value).strip().lower() in _NULL_TOKENS


def parse_float(
    value: Optional[str],
    *,
    source: str,
    column: str,
    row: Optional[int] = None,
    default: Optional[float] = None,
    required: bool = False,
) -> Optional[float]:
    """Parse un montant. Retourne default (souvent None) si vide.

    Ne fabrique jamais une valeur: un champ vide reste None, jamais 0.
    """
    if is_null(value):
        if required:
            raise InvalidDataError(f"colonne obligatoire vide: {column}", source=source, row=row)
        return default
    text = str(value).strip()
    for symbol in ("\u20ac", "$", "\u00a3", "\u00a0", " "):
        text = text.replace(symbol, "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    if "," in text and "." in text:
        text = text.replace(",", "") if text.rfind(".") > text.rfind(",") else text.replace(".", "").replace(",", ".")
    elif "," in text:
        decimals = len(text.split(",")[-1])
        text = text.replace(",", ".") if decimals in (1, 2) else text.replace(",", "")
    try:
        number = float(text)
    except ValueError as exc:
        raise InvalidDataError(
            f"valeur numerique invalide dans {column}: {redact(value)!r}", source=source, row=row
        ) from exc
    return -number if negative else number


def parse_int(
    value: Optional[str],
    *,
    source: str,
    column: str,
    row: Optional[int] = None,
    default: Optional[int] = None,
    required: bool = False,
) -> Optional[int]:
    number = parse_float(value, source=source, column=column, row=row, required=required)
    if number is None:
        return default
    return int(round(number))


def parse_datetime(
    value: Optional[str],
    *,
    source: str,
    column: str,
    row: Optional[int] = None,
    required: bool = True,
) -> Optional[datetime]:
    """Parse une date/heure et la normalise en naive UTC.

    Normaliser en UTC evite de comparer des commandes de fuseaux differents.
    """
    if is_null(value):
        if required:
            raise InvalidDataError(f"date obligatoire vide: {column}", source=source, row=row)
        return None
    text = str(value).strip()
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _DATETIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        if not required:
            # required=False signifie "cette ligne peut etre ignoree": on renvoie
            # None pour que le connecteur la rejette et la trace en data quality,
            # au lieu de faire echouer toute l'ingestion sur une ligne corrompue.
            return None
        raise InvalidDataError(f"date invalide dans {column}: {redact(value)!r}", source=source, row=row)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_date(
    value: Optional[str],
    *,
    source: str,
    column: str,
    row: Optional[int] = None,
    required: bool = True,
) -> Optional[date]:
    parsed = parse_datetime(value, source=source, column=column, row=row, required=required)
    return parsed.date() if parsed else None


def first_present(row: dict, candidates: Sequence[str]) -> Optional[str]:
    """Retourne la premiere colonne presente et non vide parmi candidates."""
    for name in candidates:
        if name in row and not is_null(row[name]):
            return row[name]
    return None
