"""Worker: prend un travail, l'execute, enregistre son sort.

    run_once()          un travail au plus, puis rendre la main
    run_until_empty()   jusqu'a ce que plus rien ne soit disponible
    run_forever()       boucle avec attente entre deux files vides

Un worker sert une LISTE EXPLICITE de sessions de tenant (ADR-004.2-002). Il ne
decouvre jamais le travail d'une organisation qu'on ne lui a pas confiee: chaque
prise passe par la `TenantSession` de cette organisation, donc par la verification
d'appartenance ET par RLS. Prendre le travail d'un autre tenant n'est pas
"interdit par le code": c'est hors de portee de la requete.

Transactions (§30 de la mission):

    [transaction courte]  prise: queued -> running, bail pose, audit job.claimed
    [hors transaction]    execution du gestionnaire: peut durer des minutes
    [transaction courte]  resultat: -> succeeded | failed | queued, audit

Un arret brutal entre les deux laisse un travail `running`; son bail expire et
`recover_stale_jobs` le remet en file. L'execution est donc au moins une fois.

L'audit n'est jamais ecrit "a cote": chaque evenement d'etat part dans la MEME
transaction que la transition qu'il decrit (crochet `hook` du depot). Un travail
commis sans sa trace, ou une trace sans son travail, est impossible.
"""
from __future__ import annotations

import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..observability.logging import EventLogger, correlation_scope, get_event_logger
from ..persistence import audit, jobs
from ..persistence.audit import Action, ActorType, Outcome, ResourceType
from ..persistence.errors import JobLeaseLost
from ..persistence.jobs import DEFAULT_LEASE_SECONDS, JobRecord, JobType
from ..persistence.tenancy import TenantSession
from .errors import classify
from .handlers import HandlerRegistry, JobContext, default_registry


#: sentinelle: distinguer "acteur non precise" de "acteur volontairement absent"
_UNSET = object()


def default_worker_id() -> str:
    """Identite lisible d'un worker: machine, processus, tirage. 100 caracteres au plus."""
    return f"{socket.gethostname()[:40]}/{os.getpid()}/{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class JobOutcome:
    """Ce qu'une boucle a fait d'un travail."""

    job: JobRecord
    #: succeeded | failed | requeued | lease_lost
    outcome: str
    duration_ms: int
    error_code: Optional[str] = None
    result: Optional[dict] = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"


@dataclass
class Worker:
    """Boucle d'execution sur une liste explicite de sessions de tenant."""

    sessions: Sequence[TenantSession]
    registry: HandlerRegistry = field(default_factory=default_registry)
    worker_id: str = field(default_factory=default_worker_id)
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    job_types: Optional[Sequence] = None
    #: horloge injectable: les tests fixent l'instant plutot que d'attendre
    clock: Optional[Callable[[], datetime]] = None
    log: EventLogger = field(default_factory=lambda: get_event_logger("worker"))
    #: index de rotation entre tenants: chaque tour commence par le suivant (equite)
    _cursor: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.sessions:
            raise ValueError("un worker sert au moins une session de tenant")
        if not self.worker_id or len(self.worker_id) > 100:
            raise ValueError("worker_id non vide, 100 caracteres au plus")
        self.log = self.log.bind(worker_id=self.worker_id)

    # -- horloge ---------------------------------------------------------------------------
    def _now(self) -> Optional[datetime]:
        return self.clock() if self.clock is not None else None

    # -- prise -----------------------------------------------------------------------------
    def claim(self) -> Optional[tuple]:
        """Prend un travail dans la premiere session qui en a un. Rotation equitable."""
        count = len(self.sessions)
        for offset in range(count):
            session = self.sessions[(self._cursor + offset) % count]
            job = jobs.claim_next_job(
                session, worker_id=self.worker_id, job_types=self.job_types,
                lease_seconds=self.lease_seconds, now=self._now(),
                hook=lambda conn, claimed: self._audit(
                    conn, session, claimed, Action.JOB_CLAIMED, Outcome.STARTED, ActorType.WORKER,
                    {"attempt": claimed.attempts, "max_attempts": claimed.max_attempts,
                     "queue_wait_ms": _milliseconds(claimed.available_at, claimed.started_at)}))
            if job is not None:
                self._cursor = (self._cursor + offset + 1) % count
                return session, job
        self._cursor = (self._cursor + 1) % count
        return None

    # -- boucles ---------------------------------------------------------------------------
    def run_once(self) -> Optional[JobOutcome]:
        """Prend et execute un travail au plus. None si la file est vide."""
        claimed = self.claim()
        if claimed is None:
            return None
        session, job = claimed
        return self.execute(session, job)

    def run_until_empty(self, *, max_jobs: int = 10000) -> List[JobOutcome]:
        """Vide la file (travaux disponibles maintenant). Borne dure pour ne jamais boucler sans fin."""
        done: List[JobOutcome] = []
        while len(done) < max_jobs:
            outcome = self.run_once()
            if outcome is None:
                break
            done.append(outcome)
        return done

    def run_forever(self, *, idle_sleep: float = 1.0, stop: Optional[Callable[[], bool]] = None,
                    max_jobs: Optional[int] = None) -> List[JobOutcome]:
        """Boucle jusqu'a `stop()`. `idle_sleep` n'est attendu que sur une file vide."""
        done: List[JobOutcome] = []
        while not (stop() if stop is not None else False):
            outcome = self.run_once()
            if outcome is None:
                if max_jobs is not None and len(done) >= max_jobs:
                    break
                time.sleep(idle_sleep)
                continue
            done.append(outcome)
            if max_jobs is not None and len(done) >= max_jobs:
                break
        return done

    # -- execution -------------------------------------------------------------------------
    def execute(self, session: TenantSession, job: JobRecord) -> JobOutcome:
        """Execute un travail DEJA pris et enregistre son sort. Ne leve pas."""
        queue_wait_ms = _milliseconds(job.available_at, job.started_at)
        log = self.log.bind(job_id=str(job.id), job_type=job.job_type,
                            organization_id=str(job.organization_id),
                            store_id=str(job.store_id) if job.store_id else None,
                            correlation_id=str(job.correlation_id))
        with correlation_scope(str(job.correlation_id)):
            log.info("job.claimed", attempt=job.attempts, max_attempts=job.max_attempts,
                     queue_wait_ms=queue_wait_ms)
            started = time.perf_counter()
            try:
                handler = self.registry.resolve(job.kind)
                result = handler(JobContext(session=session, job=job, correlation_id=str(job.correlation_id),
                                            log=log, now=self._now()))
            except BaseException as exc:  # noqa: BLE001 - tout echec devient un etat de travail, jamais un silence
                duration_ms = int((time.perf_counter() - started) * 1000)
                outcome = self._record_failure(session, job, exc, duration_ms, log)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                return outcome
            duration_ms = int((time.perf_counter() - started) * 1000)
            return self._record_success(session, job, result, duration_ms, log)

    def _record_success(self, session: TenantSession, job: JobRecord, result: Optional[Dict[str, Any]],
                        duration_ms: int, log: EventLogger) -> JobOutcome:
        document = dict(result or {})
        document.setdefault("duration_ms", duration_ms)
        try:
            finished = jobs.mark_succeeded(
                session, job, result=document, worker_id=self.worker_id, now=self._now(),
                hook=lambda conn, done: self._audit(
                    conn, session, done, Action.JOB_SUCCEEDED, Outcome.SUCCEEDED, ActorType.WORKER,
                    {"duration_ms": duration_ms, "attempt": done.attempts}))
        except JobLeaseLost:
            log.warning("job.lease_lost", duration_ms=duration_ms, attempt=job.attempts)
            return JobOutcome(job, "lease_lost", duration_ms, error_code="lease_lost")
        log.info("job.succeeded", duration_ms=duration_ms, attempt=finished.attempts)
        return JobOutcome(finished, "succeeded", duration_ms, result=finished.result)

    def _record_failure(self, session: TenantSession, job: JobRecord, exc: BaseException, duration_ms: int,
                        log: EventLogger) -> JobOutcome:
        failure = classify(exc)
        if isinstance(exc, JobLeaseLost):
            log.warning("job.lease_lost", duration_ms=duration_ms, attempt=job.attempts)
            return JobOutcome(job, "lease_lost", duration_ms, error_code="lease_lost")
        def audit_failure(conn, final: JobRecord) -> None:
            requeue = final.state is jobs.JobStatus.QUEUED
            self._audit(conn, session, final,
                        Action.JOB_REQUEUED if requeue else Action.JOB_FAILED, Outcome.FAILED, ActorType.WORKER,
                        {"error_code": failure.code, "attempt": final.attempts,
                         "max_attempts": final.max_attempts, "retryable": failure.retryable,
                         "duration_ms": duration_ms,
                         "available_at": final.available_at.isoformat() if requeue else None})

        try:
            final = jobs.mark_failed(session, job, error_code=failure.code, error=failure.message,
                                     retryable=failure.retryable, worker_id=self.worker_id, now=self._now(),
                                     hook=audit_failure)
        except JobLeaseLost:
            log.warning("job.lease_lost", duration_ms=duration_ms, attempt=job.attempts)
            return JobOutcome(job, "lease_lost", duration_ms, error_code="lease_lost")
        requeued = final.state is jobs.JobStatus.QUEUED
        log.error("job.requeued" if requeued else "job.failed", error_code=failure.code,
                  error_type=type(exc).__name__, attempt=final.attempts, max_attempts=final.max_attempts,
                  retryable=failure.retryable, duration_ms=duration_ms)
        return JobOutcome(final, "requeued" if requeued else "failed", duration_ms, error_code=failure.code)

    # -- recuperation ----------------------------------------------------------------------
    def recover_stale(self, *, limit: int = 100) -> List[JobRecord]:
        """Remet en file les travaux dont le bail a expire, dans toutes ses sessions."""
        recovered: List[JobRecord] = []
        for session in self.sessions:
            found = jobs.recover_stale_jobs(
                session, now=self._now(), limit=limit,
                hook=lambda conn, job, s=session: self._audit(
                    conn, s, job, Action.JOB_RECOVERED, Outcome.FAILED, ActorType.SYSTEM,
                    {"status": job.status, "attempt": job.attempts, "max_attempts": job.max_attempts,
                     "reason": "lease_expired"}, actor_id=None))
            for job in found:
                with correlation_scope(str(job.correlation_id)):
                    self.log.warning("job.recovered", job_id=str(job.id), job_type=job.job_type,
                                     organization_id=str(job.organization_id), status=job.status,
                                     attempt=job.attempts, correlation_id=str(job.correlation_id))
            recovered.extend(found)
        return recovered

    # -- audit -----------------------------------------------------------------------------
    def _audit(self, conn, session: TenantSession, job: JobRecord, action, outcome, actor_type,
               metadata: Dict[str, Any], *, actor_id: Any = _UNSET) -> None:
        """Trace un changement d'etat DANS la transaction qui le realise."""
        audit.record(conn, organization_id=session.organization_id, action=action,
                     resource_type=ResourceType.JOB, resource_id=job.id,
                     correlation_id=job.correlation_id, outcome=outcome, actor_type=actor_type,
                     actor_id=session.user_id if actor_id is _UNSET else actor_id, store_id=job.store_id,
                     metadata={**metadata, "job_type": job.job_type, "worker_id": self.worker_id})


# -- mise en file cote appelant -----------------------------------------------------------

def enqueue(session: TenantSession, *, job_type, payload, store_id=None, priority: int = 0,
            max_attempts: int = jobs.DEFAULT_MAX_ATTEMPTS, idempotency_key: Optional[str] = None,
            available_at: Optional[datetime] = None, correlation_id=None,
            log: Optional[EventLogger] = None) -> JobRecord:
    """Met un travail en file ET trace `job.enqueued`, dans la meme unite logique.

    L'acteur est l'utilisateur, pas le worker: c'est lui qui demande le travail.
    """
    def trace(conn, created: JobRecord) -> None:
        audit.record(conn, organization_id=session.organization_id, action=Action.JOB_ENQUEUED,
                     resource_type=ResourceType.JOB, resource_id=created.id,
                     correlation_id=created.correlation_id, outcome=Outcome.STARTED, actor_type=ActorType.USER,
                     actor_id=session.user_id, store_id=created.store_id,
                     metadata={"job_type": created.job_type, "priority": created.priority,
                               "max_attempts": created.max_attempts,
                               "idempotency_key": created.idempotency_key})

    job = jobs.enqueue_job(session, job_type=JobType(job_type), payload=payload, store_id=store_id,
                           priority=priority, max_attempts=max_attempts, idempotency_key=idempotency_key,
                           available_at=available_at, correlation_id=correlation_id, hook=trace)
    (log or get_event_logger("worker")).info(
        "job.enqueued", job_id=str(job.id), job_type=job.job_type, organization_id=str(job.organization_id),
        store_id=str(job.store_id) if job.store_id else None, priority=job.priority,
        correlation_id=str(job.correlation_id))
    return job


def cancel(session: TenantSession, job_id, *, log: Optional[EventLogger] = None) -> JobRecord:
    """Annule un travail encore en file et trace `job.cancelled`."""
    def trace(conn, stopped: JobRecord) -> None:
        audit.record(conn, organization_id=session.organization_id, action=Action.JOB_CANCELLED,
                     resource_type=ResourceType.JOB, resource_id=stopped.id,
                     correlation_id=stopped.correlation_id, outcome=Outcome.SUCCEEDED, actor_type=ActorType.USER,
                     actor_id=session.user_id, store_id=stopped.store_id,
                     metadata={"job_type": stopped.job_type, "attempt": stopped.attempts})

    job = jobs.cancel_job(session, job_id, hook=trace)
    (log or get_event_logger("worker")).info("job.cancelled", job_id=str(job.id), job_type=job.job_type,
                                             organization_id=str(job.organization_id),
                                             correlation_id=str(job.correlation_id))
    return job


def _milliseconds(start: Optional[datetime], end: Optional[datetime]) -> int:
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


__all__ = ["Worker", "JobOutcome", "enqueue", "cancel", "default_worker_id"]
