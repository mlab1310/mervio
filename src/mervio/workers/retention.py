"""Politique de retention de la purge (Mission 004.2).

Les durees ci-dessous sont des DEFAUTS OPERATIONNELS, pas une politique metier ni
une politique de conformite. Elles bornent la croissance des tables de service.
La retention des donnees d'un client (instantanes, rapports, provenance) n'est PAS
decidee ici: cette purge n'a pas le droit de les supprimer (ADR-004.2-004).

Ordre de suppression (§21 de la mission):

    travaux termines   ->   evenements d'audit
    (aucune ligne ne les reference)

Rien d'autre n'est supprime. `reports`, `analysis_runs`, `data_snapshots`,
`snapshot_sources` et les lignes canoniques restent hors d'atteinte du role
applicatif (aucun droit DELETE depuis 004.1): la chaine de provenance
rapport -> execution -> instantane -> source ne peut pas etre coupee par une purge.

L'audit survit toujours aux travaux qu'il decrit: `audit_events_days` doit rester
superieur ou egal a la plus longue retention de travaux, sinon l'histoire d'un
travail disparaitrait avant le travail lui-meme.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping

from ..persistence.jobs import JobStatus

#: Plancher impose par la base (migration 0005): rien de plus recent que 30 jours
#: ne peut etre supprime du journal, quelle que soit la configuration.
AUDIT_FLOOR_DAYS = 30

DEFAULT_SUCCEEDED_JOBS_DAYS = 7
DEFAULT_FAILED_JOBS_DAYS = 30
DEFAULT_CANCELLED_JOBS_DAYS = 7
DEFAULT_AUDIT_EVENTS_DAYS = 365
DEFAULT_BATCH_LIMIT = 1000


@dataclass(frozen=True)
class RetentionPolicy:
    """Duree de conservation par categorie, en jours."""

    succeeded_jobs_days: int = DEFAULT_SUCCEEDED_JOBS_DAYS
    failed_jobs_days: int = DEFAULT_FAILED_JOBS_DAYS
    cancelled_jobs_days: int = DEFAULT_CANCELLED_JOBS_DAYS
    audit_events_days: int = DEFAULT_AUDIT_EVENTS_DAYS
    #: lignes supprimees par appel et par categorie (une purge se repete sans dommage)
    batch_limit: int = DEFAULT_BATCH_LIMIT

    def __post_init__(self) -> None:
        for name in ("succeeded_jobs_days", "failed_jobs_days", "cancelled_jobs_days", "audit_events_days"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name}: nombre de jours entier positif attendu")
        if self.audit_events_days < AUDIT_FLOOR_DAYS:
            raise ValueError(f"audit_events_days: {AUDIT_FLOOR_DAYS} jours au minimum (plancher de la base)")
        if self.audit_events_days < max(self.succeeded_jobs_days, self.failed_jobs_days, self.cancelled_jobs_days):
            raise ValueError("audit_events_days doit couvrir la plus longue retention de travaux")
        if not isinstance(self.batch_limit, int) or not 1 <= self.batch_limit <= 1000:
            raise ValueError("batch_limit entre 1 et 1000")

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "RetentionPolicy":
        known = {f: document[f] for f in
                 ("succeeded_jobs_days", "failed_jobs_days", "cancelled_jobs_days", "audit_events_days",
                  "batch_limit") if f in document}
        unknown = set(document) - {"succeeded_jobs_days", "failed_jobs_days", "cancelled_jobs_days",
                                   "audit_events_days", "batch_limit"}
        if unknown:
            raise ValueError("cle de retention inconnue: " + ", ".join(sorted(unknown)))
        return cls(**known)

    def to_document(self) -> Dict[str, int]:
        return {"succeeded_jobs_days": self.succeeded_jobs_days, "failed_jobs_days": self.failed_jobs_days,
                "cancelled_jobs_days": self.cancelled_jobs_days, "audit_events_days": self.audit_events_days,
                "batch_limit": self.batch_limit}

    def job_cutoffs(self, now: datetime) -> Dict[JobStatus, datetime]:
        """Instant avant lequel un travail termine devient eligible, par etat."""
        return {
            JobStatus.SUCCEEDED: now - timedelta(days=self.succeeded_jobs_days),
            JobStatus.FAILED: now - timedelta(days=self.failed_jobs_days),
            JobStatus.CANCELLED: now - timedelta(days=self.cancelled_jobs_days),
        }

    def audit_cutoff(self, now: datetime) -> datetime:
        return now - timedelta(days=self.audit_events_days)
