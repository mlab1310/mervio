"""Primitives d'ingestion partagees par toutes les sources."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from ..errors import InvalidDataError, MissingColumnsError

_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)

#: ordre jour / mois d'une date "a barres" (D-042). Jamais devine: il est
#: declare par l'appelant, deduit du fichier, ou lisible dans la valeur elle-meme.
DAY_FIRST = "day_first"
MONTH_FIRST = "month_first"
#: ordre inconnu ET interdit: dates a barres d'une colonne contradictoire
REJECT_SLASH_DATES = "reject_slash_dates"
_SLASH_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$")

_NULL_TOKENS = {"", "-", "--", "n/a", "na", "null", "none", "nan"}

#: encodages tentes dans l'ordre. Les exports Shopify/Stripe sont en UTF-8,
#: mais un passage par Excel sous Windows produit regulierement du cp1252.
_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

#: separateurs supportes. Un export genere sur un poste francais sort en ';'
#: et un fichier lu avec le mauvais separateur donne UNE colonne: illisible.
_DELIMITERS = (",", ";", "\t", "|")

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

#: signatures de fichiers non CSV. latin-1 decode n'importe quel octet: sans ce
#: controle, un classeur .xlsx etait "lu" comme un CSV d'une colonne binaire.
_BINARY_SIGNATURES = (
    (b"PK\x03\x04", "classeur Excel .xlsx ou archive ZIP"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "classeur Excel .xls"),
    (b"%PDF", "document PDF"),
)


def unsupported_format(path: str | Path) -> Optional[str]:
    """Motif de refus si le fichier n'est manifestement pas un CSV texte."""
    with Path(path).open("rb") as handle:
        head = handle.read(8192)
    for signature, label in _BINARY_SIGNATURES:
        if head.startswith(signature):
            return f"format non supporte ({label}): exporter le fichier en CSV UTF-8"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "encodage UTF-16 non supporte: reexporter le fichier en CSV UTF-8"
    if b"\x00" in head:
        return "fichier binaire non supporte: exporter le fichier en CSV UTF-8"
    return None


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
    problem = unsupported_format(p)
    if problem:
        raise InvalidDataError(problem, source=source)
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


def slash_date_order(value: Optional[str]) -> Optional[str]:
    """Ordre lisible dans UNE valeur a barres: DAY_FIRST, MONTH_FIRST, "ambiguous", "invalid" ou None."""
    match = _SLASH_DATE_RE.match(str(value).strip()) if value is not None else None
    if not match:
        return None
    first, second = int(match.group(1)), int(match.group(2))
    if first > 12 and second > 12:
        return "invalid"
    if first > 12:
        return DAY_FIRST
    if second > 12:
        return MONTH_FIRST
    return "ambiguous"


@dataclass(frozen=True)
class DateConvention:
    """Ordre jour / mois d'une colonne, deduit de ses seules valeurs discriminantes."""

    status: str  # not_needed | resolved | ambiguous | conflict
    order: Optional[str] = None
    day_first_values: int = 0
    month_first_values: int = 0
    ambiguous_values: int = 0


def detect_date_order(values: Iterable[Optional[str]]) -> DateConvention:
    counts = {DAY_FIRST: 0, MONTH_FIRST: 0, "ambiguous": 0}
    for value in values:
        kind = slash_date_order(value)
        if kind in counts:
            counts[kind] += 1
    evidence = dict(day_first_values=counts[DAY_FIRST], month_first_values=counts[MONTH_FIRST],
                    ambiguous_values=counts["ambiguous"])
    if counts[DAY_FIRST] and counts[MONTH_FIRST]:
        return DateConvention("conflict", REJECT_SLASH_DATES, **evidence)
    if counts[DAY_FIRST] or counts[MONTH_FIRST]:
        return DateConvention("resolved", DAY_FIRST if counts[DAY_FIRST] else MONTH_FIRST, **evidence)
    if counts["ambiguous"]:
        return DateConvention("ambiguous", None, **evidence)
    return DateConvention("not_needed", None, **evidence)


def resolve_date_order(values: Iterable[Optional[str]], *, source: str, column: str, quality=None) -> Optional[str]:
    """Convention d'un fichier pour un connecteur, tracee en qualite de donnee. Ne leve pas."""
    convention = detect_date_order(values)
    if quality is not None:
        if convention.status == "conflict":
            quality.add_issue(source, "date_order_conflict", "error",
                              f"{column}: dates JJ/MM et MM/JJ melangees ({convention.day_first_values} jour d'abord, "
                              f"{convention.month_first_values} mois d'abord): dates a barres rejetees")
        elif convention.status == "ambiguous":
            quality.add_issue(source, "ambiguous_date_order", "error",
                              f"{column}: {convention.ambiguous_values} date(s) JJ/MM ou MM/JJ sans valeur discriminante: "
                              "ordre jour/mois non devine, lignes rejetees")
        elif convention.status == "resolved" and convention.ambiguous_values:
            label = "MM/JJ" if convention.order == MONTH_FIRST else "JJ/MM"
            quality.add_issue(source, "date_order_inferred", "info",
                              f"{column}: ordre {label} etabli par "
                              f"{convention.month_first_values or convention.day_first_values} valeur(s) discriminante(s), "
                              f"applique a {convention.ambiguous_values} date(s) ambigue(s) du meme fichier")
    return convention.order


def parse_datetime(
    value: Optional[str],
    *,
    source: str,
    column: str,
    row: Optional[int] = None,
    required: bool = True,
    date_order: Optional[str] = None,
) -> Optional[datetime]:
    """Parse une date/heure et la normalise en naive UTC.

    Normaliser en UTC evite de comparer des commandes de fuseaux differents.
    Formats: ISO 8601 (avec ou sans heure, secondes, fuseau) et dates a barres
    J/M/AAAA ou M/J/AAAA avec heure optionnelle (H:MM ou H:MM:SS). L'ordre
    jour/mois d'une date a barres vient de `date_order` ou de la valeur elle-meme
    (une composante > 12); une date ambigue sans convention n'est jamais devinee.
    Une date a barres n'a pas de fuseau: elle est traitee comme deja en UTC.
    """
    if is_null(value):
        if required:
            raise InvalidDataError(f"date obligatoire vide: {column}", source=source, row=row)
        return None
    text = str(value).strip()
    parsed: Optional[datetime] = None
    ambiguous = False
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
        parsed, ambiguous = _parse_slash_date(text, date_order)
    if parsed is None:
        if not required:
            # required=False signifie "cette ligne peut etre ignoree": on renvoie
            # None pour que le connecteur la rejette et la trace en data quality,
            # au lieu de faire echouer toute l'ingestion sur une ligne corrompue.
            return None
        reason = "date ambigue (JJ/MM ou MM/JJ)" if ambiguous else "date invalide"
        raise InvalidDataError(f"{reason} dans {column}: {redact(value)!r}", source=source, row=row)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_slash_date(text: str, date_order: Optional[str]) -> Tuple[Optional[datetime], bool]:
    """(date, ambigue). Jamais d'ordre par defaut."""
    match = _SLASH_DATE_RE.match(text)
    if not match or date_order == REJECT_SLASH_DATES:
        return None, False
    own = slash_date_order(text)
    if own in (DAY_FIRST, MONTH_FIRST):
        if date_order in (DAY_FIRST, MONTH_FIRST) and date_order != own:
            return None, False  # contredit la convention du fichier
        order = own
    elif own == "ambiguous" and date_order in (DAY_FIRST, MONTH_FIRST):
        order = date_order
    else:
        return None, own == "ambiguous"
    first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    day, month = (first, second) if order == DAY_FIRST else (second, first)
    try:
        return datetime(year, month, day, int(match.group(4) or 0), int(match.group(5) or 0), int(match.group(6) or 0)), False
    except ValueError:
        return None, False


def parse_date(
    value: Optional[str],
    *,
    source: str,
    column: str,
    row: Optional[int] = None,
    required: bool = True,
    date_order: Optional[str] = None,
) -> Optional[date]:
    parsed = parse_datetime(value, source=source, column=column, row=row, required=required, date_order=date_order)
    return parsed.date() if parsed else None


def first_present(row: dict, candidates: Sequence[str]) -> Optional[str]:
    """Retourne la premiere colonne presente et non vide parmi candidates."""
    for name in candidates:
        if name in row and not is_null(row[name]):
            return row[name]
    return None
