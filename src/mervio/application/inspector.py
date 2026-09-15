"""Inspection d'un CSV inconnu, AVANT toute tentative de mapping.

Ce module ne fait aucune hypothese sur la source et ne transforme rien. Il
decrit un fichier: structure, types, trous, doublons, periode, devises,
colonnes potentiellement personnelles.

Regle stricte: aucune VALEUR de cellule n'est retournee ni journalisee. On
expose des noms de colonnes, des types deduits et des comptages. Les seules
valeurs restituees sont les codes devise (ISO 4217), qui ne sont pas des
donnees personnelles.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..errors import MervioError
from ..ingestion.base import (
    _ENCODINGS, is_null, parse_datetime, parse_float, sniff_delimiter, unsupported_format,
)
from .sensitive import scan_rows

#: noms de colonnes qui trahissent une donnee personnelle
#: hints precis. Des hints trop larges ("name", "shipping") signalaient un
#: montant de port et un libelle produit comme donnees personnelles, ce qui
#: rend l'alerte inutilisable.
_PII_COLUMN_HINTS = (
    "email", "e-mail", "mail", "phone", "telephone", "mobile", "first_name",
    "last_name", "firstname", "lastname", "full_name", "customer_name",
    "nom", "prenom", "address", "adresse", "street", "rue", "city", "ville",
    "zip", "postal", "billing", "shipping_address", "ip_address", "customer_id",
)
#: hints appliques meme sur une colonne numerique
_STRONG_PII_HINTS = ("email", "mail", "phone", "telephone", "mobile", "ip_address",
                     "customer_id", "client_id", "user_id")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[ .-]?)?(?:\(?\d{2,4}\)?[ .-]?){3,5}")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_CURRENCY_COLUMN_HINTS = ("currency", "devise", "curr")

#: lignes echantillonnees pour le TYPAGE et la detection de donnees personnelles.
#: Les comptages (lignes, manquants, distinct, doublons) portent sur la TOTALITE
#: du fichier: une cardinalite calculee sur un echantillon fausse la granularite.
PROFILE_ROWS = 2000
#: plafond de suivi des valeurs distinctes, pour borner la memoire
DISTINCT_CAP = 200_000


@dataclass
class ColumnProfile:
    name: str
    inferred_type: str  # integer | decimal | date | boolean | currency_code | text | empty
    missing: int
    missing_pct: float
    distinct: int  # -1 = au-dela du plafond de suivi
    distinct_capped: bool = False
    looks_personal: bool = False
    personal_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "column": self.name, "inferred_type": self.inferred_type,
            "missing": self.missing, "missing_pct": round(self.missing_pct, 4),
            "distinct": None if self.distinct_capped else self.distinct,
            "distinct_capped": self.distinct_capped,
            "looks_personal": self.looks_personal,
            "personal_reason": self.personal_reason,
        }


@dataclass
class FileInspection:
    path: str
    readable: bool
    encoding: Optional[str] = None
    delimiter: Optional[str] = None
    size_bytes: int = 0
    row_count: int = 0
    column_count: int = 0
    columns: List[ColumnProfile] = field(default_factory=list)
    duplicate_rows: int = 0
    date_columns: List[str] = field(default_factory=list)
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    currency_columns: List[str] = field(default_factory=list)
    currencies: List[str] = field(default_factory=list)
    personal_data_columns: List[str] = field(default_factory=list)
    sensitive_findings: List[dict] = field(default_factory=list)
    period_rows: int = 0
    granularity: str = "indetermine"
    granularity_evidence: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "file": Path(self.path).name, "readable": self.readable,
            "encoding": self.encoding, "delimiter": self.delimiter,
            "size_bytes": self.size_bytes,
            "rows": self.row_count, "columns_count": self.column_count,
            "columns": [c.to_dict() for c in self.columns],
            "duplicate_rows": self.duplicate_rows,
            "date_columns": self.date_columns,
            "period": {"start": self.period_start, "end": self.period_end,
                       "dated_rows": self.period_rows},
            "currency_columns": self.currency_columns, "currencies": self.currencies,
            "personal_data_columns": self.personal_data_columns,
            "sensitive_findings": self.sensitive_findings,
            "granularity": {"guess": self.granularity, "evidence": self.granularity_evidence},
            "error": self.error,
        }


def _read(path: Path) -> tuple:
    problem = unsupported_format(path)
    if problem:
        raise MervioError(problem)
    for encoding in _ENCODINGS:
        try:
            return path.read_text(encoding=encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    raise MervioError("encodage illisible (utf-8, cp1252, latin-1 ont echoue)")


def _infer_type(values: List[str]) -> str:
    present = [v for v in values if not is_null(v)]
    if not present:
        return "empty"
    sample = present[:400]

    def ratio(predicate) -> float:
        return sum(1 for v in sample if predicate(v)) / len(sample)

    def is_number(value: str) -> bool:
        try:
            parse_float(value, source="inspect", column="c")
            return True
        except MervioError:
            return False

    def is_date(value: str) -> bool:
        try:
            return parse_datetime(value, source="inspect", column="c", required=False) is not None
        except MervioError:
            return False

    if ratio(lambda v: bool(_CURRENCY_RE.match(v.strip()))) > 0.9:
        return "currency_code"
    if ratio(lambda v: v.strip().lower() in {"true", "false", "yes", "no", "oui", "non", "0", "1"}) > 0.95:
        return "boolean"
    if ratio(is_date) > 0.9 and not ratio(lambda v: v.strip().lstrip("-").isdigit()) > 0.9:
        return "date"
    if ratio(is_number) > 0.9:
        whole = all("." not in v and "," not in v for v in sample)
        return "integer" if whole else "decimal"
    return "text"


def _looks_personal(name: str, values: List[str], inferred_type: str = "text") -> tuple:
    lowered = name.strip().lower().replace(" ", "_")
    numeric = inferred_type in ("integer", "decimal", "currency_code", "boolean")
    for hint in _PII_COLUMN_HINTS:
        if hint in lowered and (not numeric or hint in _STRONG_PII_HINTS):
            return True, f"nom de colonne contenant '{hint}'"
    sample = [v for v in values[:200] if isinstance(v, str) and v]
    if sample and sum(1 for v in sample if _EMAIL_RE.search(v)) / len(sample) > 0.3:
        return True, "valeurs au format email"
    # heuristique telephone reservee aux colonnes TEXTE: appliquee aux dates et
    # aux identifiants numeriques, elle prenait "2026-06-22" pour un telephone
    if inferred_type == "text" and sample:
        phones = sum(1 for v in sample
                     if len(v) > 7 and _PHONE_RE.fullmatch(v.strip())
                     and (v.startswith("+") or any(c in v for c in " .-")))
        if phones / len(sample) > 0.5:
            return True, "valeurs au format telephone"
    return False, ""


def _guess_granularity(total: int, columns: List[ColumnProfile]) -> tuple:
    """Heuristique. Toujours presentee comme une supposition, jamais un fait."""
    if not total:
        return "indetermine", "fichier vide"
    id_like = [c for c in columns
               if any(token in c.name.lower() for token in ("order", "commande", "transaction", "invoice"))
               and c.inferred_type in ("integer", "text")]
    for column in id_like:
        if column.distinct_capped:
            continue
        if column.distinct and column.distinct < total * 0.95:
            return ("ligne d'article (plusieurs lignes par commande)",
                    f"'{column.name}' compte {column.distinct} valeurs distinctes pour {total} lignes")
        if column.distinct >= total * 0.95:
            return ("commande (une ligne par commande)",
                    f"'{column.name}' est quasi unique ({column.distinct}/{total})")
    date_columns = [c for c in columns if c.inferred_type == "date" and not c.distinct_capped]
    if date_columns and date_columns[0].distinct and date_columns[0].distinct < total * 0.3:
        return ("agrege par periode (plusieurs lignes par date)",
                f"'{date_columns[0].name}' ne compte que {date_columns[0].distinct} dates distinctes")
    return "indetermine", "aucune colonne d'identifiant exploitable"


def inspect_file(path: str | Path) -> FileInspection:
    target = Path(path)
    if not target.exists():
        return FileInspection(path=str(target), readable=False, error="fichier introuvable")

    try:
        text, encoding = _read(target)
    except MervioError as exc:
        return FileInspection(path=str(target), readable=False, error=str(exc))

    import csv
    import io

    delimiter = sniff_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        return FileInspection(path=str(target), readable=False, encoding=encoding,
                              delimiter=delimiter, error="CSV vide ou sans en-tete")

    names = [(n or "").strip() for n in reader.fieldnames]
    rows: List[dict] = []
    signatures = Counter()
    distinct: Dict[str, set] = {name: set() for name in names}
    capped: Dict[str, bool] = {name: False for name in names}
    missing: Dict[str, int] = {name: 0 for name in names}

    for raw in reader:
        row = {(k or "").strip(): (v.strip() if isinstance(v, str) else v)
               for k, v in raw.items() if k is not None}
        signatures[tuple(sorted(row.items()))] += 1
        for name in names:
            value = row.get(name, "")
            if is_null(value):
                missing[name] += 1
            elif not capped[name]:
                distinct[name].add(value)
                if len(distinct[name]) > DISTINCT_CAP:
                    capped[name] = True
        if len(rows) < PROFILE_ROWS:
            rows.append(row)
    row_count = sum(signatures.values())
    duplicates = sum(count - 1 for count in signatures.values() if count > 1)

    inspection = FileInspection(
        path=str(target), readable=True, encoding=encoding, delimiter=delimiter,
        size_bytes=target.stat().st_size, row_count=row_count,
        column_count=len(reader.fieldnames), duplicate_rows=duplicates,
    )
    if not rows:
        inspection.error = "en-tete present mais aucune ligne de donnee"
        return inspection

    for name in names:
        values = [r.get(name, "") for r in rows]  # echantillon: typage uniquement
        profile = ColumnProfile(
            name=name, inferred_type=_infer_type(values), missing=missing[name],
            missing_pct=missing[name] / row_count if row_count else 0.0,
            distinct=-1 if capped[name] else len(distinct[name]),
            distinct_capped=capped[name],
        )
        profile.looks_personal, profile.personal_reason = _looks_personal(
            name, values, profile.inferred_type)
        inspection.columns.append(profile)

    inspection.date_columns = [c.name for c in inspection.columns if c.inferred_type == "date"]
    if inspection.date_columns:
        # second passage sur la TOTALITE du fichier: une periode calculee sur un
        # echantillon tronque annoncerait une date de fin fausse
        column = inspection.date_columns[0]
        full = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        days = []
        for raw in full:
            value = raw.get(column)
            try:
                parsed = parse_datetime(value.strip() if isinstance(value, str) else value,
                                        source="inspect", column="date", required=False)
            except MervioError:
                parsed = None
            if parsed:
                days.append(parsed.date())
        if days:
            inspection.period_start, inspection.period_end = min(days).isoformat(), max(days).isoformat()
            inspection.period_rows = len(days)

    inspection.currency_columns = [
        c.name for c in inspection.columns
        if c.inferred_type == "currency_code"
        or any(hint in c.name.lower() for hint in _CURRENCY_COLUMN_HINTS)
    ]
    codes = set()
    for name in inspection.currency_columns:
        for row in rows:
            value = (row.get(name) or "").strip().upper()
            if _CURRENCY_RE.match(value):
                codes.add(value)
    inspection.currencies = sorted(codes)

    inspection.personal_data_columns = [c.name for c in inspection.columns if c.looks_personal]
    inspection.sensitive_findings = [f.to_dict() for f in scan_rows(rows)]
    inspection.granularity, inspection.granularity_evidence = _guess_granularity(
        row_count, inspection.columns)
    return inspection
