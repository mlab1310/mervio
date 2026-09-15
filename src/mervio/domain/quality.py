"""Suivi de la qualite de la donnee.

Principe non negociable: l'utilisateur ne doit jamais croire que Mervio connait
une donnee qu'il ne possede pas. Chaque champ exploite par le moteur porte un
statut explicite, et chaque rapport expose ses limites.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class QualityStatus(str, Enum):
    RELIABLE = "reliable"      # vert
    INCOMPLETE = "incomplete"  # orange
    UNAVAILABLE = "unavailable"  # rouge

    @property
    def icon(self) -> str:
        return {"reliable": "GREEN", "incomplete": "AMBER", "unavailable": "RED"}[self.value]


@dataclass
class FieldQuality:
    field_name: str
    status: QualityStatus
    coverage: float  # 0.0 -> 1.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "field": self.field_name,
            "status": self.status.value,
            "coverage": round(self.coverage, 4),
            "note": self.note,
        }


@dataclass
class DataIssue:
    source: str
    kind: str
    severity: str  # info | warning | error
    message: str
    count: int = 1

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "count": self.count,
        }


@dataclass
class RowCount:
    """Comptage brut par source: combien de lignes lues, combien exploitees."""

    source: str
    total: int
    accepted: int

    @property
    def rejected(self) -> int:
        return max(0, self.total - self.accepted)

    def to_dict(self) -> dict:
        return {
            "source": self.source, "rows": self.total,
            "accepted_rows": self.accepted, "rejected_rows": self.rejected,
        }


@dataclass
class DataQualityReport:
    fields: Dict[str, FieldQuality] = field(default_factory=dict)
    issues: List[DataIssue] = field(default_factory=list)
    sources_loaded: List[str] = field(default_factory=list)
    row_counts: Dict[str, RowCount] = field(default_factory=dict)
    #: devises observees par source, pour detecter une incoherence multi-source
    observed_currencies: Dict[str, List[str]] = field(default_factory=dict)

    # -- ecriture -------------------------------------------------------
    def set_field(
        self,
        field_name: str,
        *,
        covered: int,
        total: int,
        reliable_coverage: float = 0.95,
        note: str = "",
    ) -> FieldQuality:
        coverage = (covered / total) if total else 0.0
        if total == 0 or coverage <= 0.0:
            status = QualityStatus.UNAVAILABLE
        elif coverage >= reliable_coverage:
            status = QualityStatus.RELIABLE
        else:
            status = QualityStatus.INCOMPLETE
        fq = FieldQuality(field_name, status, coverage, note)
        self.fields[field_name] = fq
        return fq

    def set_status(self, field_name: str, status: QualityStatus, note: str = "") -> FieldQuality:
        coverage = {
            QualityStatus.RELIABLE: 1.0,
            QualityStatus.INCOMPLETE: 0.5,
            QualityStatus.UNAVAILABLE: 0.0,
        }[status]
        fq = FieldQuality(field_name, status, coverage, note)
        self.fields[field_name] = fq
        return fq

    def add_issue(self, source: str, kind: str, severity: str, message: str, count: int = 1) -> None:
        for issue in self.issues:
            if issue.source == source and issue.kind == kind and issue.message == message:
                issue.count += count
                return
        self.issues.append(DataIssue(source, kind, severity, message, count))

    def set_rows(self, source: str, *, total: int, accepted: int) -> RowCount:
        count = RowCount(source, total, accepted)
        self.row_counts[source] = count
        return count

    def observe_currency(self, source: str, currency: str) -> None:
        if not currency:
            return
        bucket = self.observed_currencies.setdefault(source, [])
        code = currency.strip().upper()
        if code and code not in bucket:
            bucket.append(code)

    def distinct_currencies(self) -> List[str]:
        seen: List[str] = []
        for codes in self.observed_currencies.values():
            for code in codes:
                if code not in seen:
                    seen.append(code)
        return seen

    def issues_for(self, source: str) -> List["DataIssue"]:
        return [i for i in self.issues if i.source == source]

    def mark_source(self, source: str) -> None:
        if source not in self.sources_loaded:
            self.sources_loaded.append(source)

    # -- lecture --------------------------------------------------------
    def status_of(self, field_name: str) -> QualityStatus:
        fq = self.fields.get(field_name)
        return fq.status if fq else QualityStatus.UNAVAILABLE

    def coverage_of(self, field_name: str) -> float:
        fq = self.fields.get(field_name)
        return fq.coverage if fq else 0.0

    def reliable_ratio(self) -> Optional[float]:
        if not self.fields:
            return None
        reliable = sum(1 for f in self.fields.values() if f.status is QualityStatus.RELIABLE)
        return reliable / len(self.fields)

    def to_dict(self) -> dict:
        return {
            "sources_loaded": list(self.sources_loaded),
            "row_counts": {k: v.to_dict() for k, v in self.row_counts.items()},
            "observed_currencies": {k: list(v) for k, v in self.observed_currencies.items()},
            "fields": [f.to_dict() for f in self.fields.values()],
            "issues": [i.to_dict() for i in self.issues],
            "reliable_field_ratio": (
                round(self.reliable_ratio(), 4) if self.reliable_ratio() is not None else None
            ),
        }
