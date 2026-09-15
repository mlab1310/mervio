"""Couche d'import et de validation des fichiers.

La validation REUTILISE les connecteurs d'ingestion: elle ne reimplemente
aucune regle d'acceptation. Un fichier est valide si, et seulement si, le
connecteur reel sait le lire.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..domain.quality import DataQualityReport
from ..errors import MervioError
from ..ingestion import (
    ingest_google_ads, ingest_shopify_orders, ingest_shopify_products, ingest_stripe,
)
from ..ingestion.base import read_csv, sniff_delimiter
from .sensitive import SensitiveFinding, scan_rows

#: signatures de colonnes permettant la detection automatique de source
SOURCE_SIGNATURES: Dict[str, tuple] = {
    "shopify_orders": ("Name", "Lineitem quantity", "Lineitem price"),
    "shopify_products": ("SKU", "Title"),
    "stripe": ("id", "Amount", "Status"),
    "google_ads": ("Day", "Campaign", "Cost"),
}

SOURCE_LABELS = {
    "shopify_orders": "Commandes Shopify",
    "shopify_products": "Produits Shopify (couts)",
    "stripe": "Transactions Stripe",
    "google_ads": "Campagnes Google Ads",
}

#: sources sans lesquelles aucune analyse n'est possible
REQUIRED_SOURCES = ("shopify_orders",)


class UnknownSourceError(MervioError):
    """Impossible d'identifier la source d'un fichier."""


@dataclass
class FileValidation:
    source: str
    status: str  # valid | valid_with_issues | invalid
    rows: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    issues: List[dict] = field(default_factory=list)
    path: Optional[str] = None
    columns: List[str] = field(default_factory=list)
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    currency: Optional[str] = None
    delimiter: str = ","
    sensitive_findings: List[dict] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.status != "invalid"

    def to_dict(self) -> dict:
        return {
            "source": self.source, "status": self.status, "rows": self.rows,
            "accepted_rows": self.accepted_rows, "rejected_rows": self.rejected_rows,
            "issues": self.issues, "file": Path(self.path).name if self.path else None,
            "columns": self.columns,
            "period": {"start": self.period_start, "end": self.period_end},
            "currency": self.currency, "delimiter": self.delimiter,
            "sensitive_findings": self.sensitive_findings, "error": self.error,
        }


def detect_source(path: str | Path) -> str:
    """Identifie la source d'un CSV a partir de son en-tete."""
    try:
        rows = read_csv(path, "detection")
    except MervioError as exc:
        raise UnknownSourceError(f"fichier illisible: {exc}") from exc
    if not rows:
        raise UnknownSourceError("fichier sans ligne de donnee: source indeterminable")
    columns = set(rows[0].keys())
    best, best_score = None, 0
    for source, signature in SOURCE_SIGNATURES.items():
        score = sum(1 for column in signature if column in columns)
        if score == len(signature) and score > best_score:
            best, best_score = source, score
    if best is None:
        raise UnknownSourceError(
            "aucune signature de colonnes reconnue. Sources supportees: "
            + ", ".join(SOURCE_SIGNATURES)
        )
    return best


def _period_of(values: List[date]) -> tuple:
    if not values:
        return None, None
    return min(values).isoformat(), max(values).isoformat()


def validate_file(path: str | Path, source: Optional[str] = None) -> FileValidation:
    """Charge un fichier avec son vrai connecteur et rapporte ce qui passe."""
    resolved = source
    if resolved is None:
        try:
            resolved = detect_source(path)
        except UnknownSourceError as exc:
            return FileValidation(source="unknown", status="invalid", path=str(path), error=str(exc))

    quality = DataQualityReport()
    columns: List[str] = []
    delimiter = ","
    findings: List[SensitiveFinding] = []
    try:
        # lecture brute SANS rapport qualite: le connecteur relit le fichier
        # juste apres et c'est lui qui journalise les incidents. Passer
        # `quality` ici produirait chaque incident en double.
        raw_rows = read_csv(path, resolved)
        if raw_rows:
            columns = list(raw_rows[0].keys())
            findings = scan_rows(raw_rows)
        delimiter = sniff_delimiter(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except (MervioError, OSError):
        pass

    for finding in findings:
        # message sans valeur: seuls la colonne et le compte sont exposes
        quality.add_issue(resolved, "sensitive_data", "warning", finding.message())

    try:
        days: List[date] = []
        if resolved == "shopify_orders":
            orders, _, currency = ingest_shopify_orders(path, quality)
            days = [o.created_at.date() for o in orders]
            key = "shopify_orders"
        elif resolved == "shopify_products":
            ingest_shopify_products(path, quality)
            currency, key = None, "shopify_products"
        elif resolved == "stripe":
            payments, _ = ingest_stripe(path, quality)
            days = [p.created_at.date() for p in payments]
            currency = (quality.observed_currencies.get("stripe") or [None])[0]
            key = "stripe"
        elif resolved == "google_ads":
            _, performance = ingest_google_ads(path, quality)
            days = [a.day for a in performance]
            currency, key = None, "google_ads"
        else:
            return FileValidation(source=resolved, status="invalid", path=str(path),
                                  error=f"source non supportee: {resolved}")
    except MervioError as exc:
        return FileValidation(source=resolved, status="invalid", path=str(path),
                              columns=columns, error=str(exc))

    counts = quality.row_counts.get(key)
    issues = [i.to_dict() for i in quality.issues]
    blocking = any(i["severity"] == "error" for i in issues)
    rejected = counts.rejected if counts else 0
    start, end = _period_of(days)

    if counts and counts.accepted == 0:
        status = "invalid"
    elif rejected or issues:
        status = "valid_with_issues"
    else:
        status = "valid"

    return FileValidation(
        source=resolved, status=status,
        rows=counts.total if counts else 0,
        accepted_rows=counts.accepted if counts else 0,
        rejected_rows=rejected, issues=issues, path=str(path), columns=columns,
        period_start=start, period_end=end,
        currency=(currency.upper() if isinstance(currency, str) else None),
        delimiter=delimiter, sensitive_findings=[f.to_dict() for f in findings],
        error="lignes rejetees pour donnee invalide" if blocking and status == "invalid" else None,
    )


def _has_rows(path: str | Path, source: str) -> bool:
    try:
        return bool(read_csv(path, source))
    except MervioError:
        return False


def validate_many(files: Dict[str, Optional[str]]) -> List[FileValidation]:
    """files: {source_or_None: path}. Retourne une validation par fichier."""
    out: List[FileValidation] = []
    for source, path in files.items():
        if not path:
            continue
        out.append(validate_file(path, None if source == "auto" else source))
    return out
