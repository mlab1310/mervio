"""Worker: prend un travail, l'execute, enregistre son sort.

    run_once()          un travail au plus, puis rendre la main
    run_until_empty()   jusqu'a ce que plus rien ne soit disponible
    run_forever()       boucle avec attente entre deux files vides

Deux facons de trouver du travail, jamais melangees:

- LISTE EXPLICITE de sessions de tenant (004.2, ADR-004.2-002): chaque prise passe par
  la `TenantSession` d'un humain membre; outil de test et de bibliotheque;
- DISPATCHER (004.3): le service connecte ne sert que les organisations qui l'ont
  autorise. L'etat du travail passe par une `ServiceSession`; le travail metier par la
  session du DEMANDEUR (`delegated_session`), dont l'appartenance et le role sont relus
  au moment de l'execution. L'audit nomme le principal du service et, pour le compte de
  qui il agit, le demandeur.

Transactions (§30 de la mission 004.2):

    [transaction courte]  prise: queued -> running, bail pose, audit job.claimed
    [hors transaction]    execution du gestionnaire: peut durer des minutes
    [transaction courte]  resultat: -> succeeded | failed | queued, audit

Bail (004.3): quand une connexion de bail est fournie, un `LeaseKeeper` renouvelle le
bail pendant l'execution. Si la tentative perd son bail, le worker n'ecrit PLUS RIEN:
ni resultat, ni echec, ni remise en file; la base refuserait de toute facon (jeton
d'exclusion), mais le worker ne tente meme pas. Une coupure transitoire de la base au
moment de publier est retentee tant que le bail local est valide.

Execution AU MOINS une fois, jamais exactement une fois: un travail peut etre execute
deux fois apres une panne; un ancien worker ne peut jamais en publier le resultat.

L'audit n'est jamais ecrit "a cote": chaque evenement d'etat part dans la MEME
transaction que la transition qu'il decrit (crochet `hook` du depot).
"""
from __future__ import annotations

import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence
from uuid import UUID

from ..observability.logging import EventLogger, correlation_scope, get_event_logger
from ..persistence import audit, jobs
from ..persistence.audit import Action, ActorType, Outcome, ResourceType
from ..persistence.database import Database
from ..persistence.dispatch import Dispatcher
from ..persistence.errors import JobLeaseLost, TenantAccessDenied
from ..persistence.jobs import DEFAULT_LEASE_SECONDS, JobRecord, JobType
from ..persistence.retry import database_error_code, retryable_database_error
from ..persistence.service import ServiceSession, delegated_session
from ..persistence.tenancy import TenantSession
from .errors import classify
from .handlers import HandlerRegistry, JobContext, default_registry
from .lease import DEFAULT_HEARTBEAT_SECONDS, LeaseKeeper

#: sentinelle: distinguer "acteur non precise" de "acteur volontairement absent"
_UNSET = object()
#: marge sous l'echeance du bail en deca de laquelle on ne tente plus de publier
PUBLISH_MARGIN_SECONDS = 1.0


def default_worker_id(name: Optional[str] = None) -> str:
    """Identite lisible d'un worker: nom (ou machine), processus, tirage. 100 caracteres au plus."""
    return f"{(name or socket.gethostname())[:40]}/{os.getpid()}/{uuid.uuid4().hex[:8]}"


def _noop(*args, **kwargs) -> None:
    return None


@dataclass(frozen=True)
class JobOutcome:
    """Ce qu'une boucle a fait d'un travail."""

    job: JobRecord
    #: succeeded | failed | requeued | lease_lost | unrecorded
    outcome: str
    duration_ms: int
    error_code: Optional[str] = None
    result: Optional[dict] = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"

    @property
    def published(self) -> bool:
        return self.outcome in ("succeeded", "failed", "requeued")


@dataclass
class Worker:
    """Boucle d'execution: liste explicite de sessions, ou dispatcher de service."""

    sessions: Sequence[TenantSession] = ()
    registry: HandlerRegistry = field(default_factory=default_registry)
    worker_id: str = field(default_factory=default_worker_id)
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    job_types: Optional[Sequence] = None
    #: horloge injectable: les tests fixent l'instant plutot que d'attendre
    clock: Optional[Callable[[], datetime]] = None
    log: EventLogger = field(default_factory=lambda: get_event_logger("worker"))
    #: 004.3: organisations autorisees du service connecte (exclusif de `sessions`)
    dispatcher: Optional[Dispatcher] = None
    #: 004.3: connexion DEDIEE au gardien de bail; sans elle, aucun renouvellement (004.2)
    lease_database: Optional[Database] = None
    renew_seconds: Optional[float] = None
    heartbeat: Callable[[], None] = _noop
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    backoff_base_seconds: int = jobs.BACKOFF_BASE_SECONDS
    backoff_cap_seconds: int = jobs.BACKOFF_CAP_SECONDS
    #: rappels du processus: debut et fin d'un travail (etat BUSY, compteur de sante)
    on_job_start: Callable[[JobRecord], None] = _noop
    on_job_end: Callable[[JobOutcome], None] = _noop
    #: index de rotation entre tenants: chaque tour commence par le suivant (equite)
    _cursor: int = field(default=0, init=False, repr=False)
    _claimed_at: Dict[UUID, float] = field(default_factory=dict, init=False, repr=False)
    _active: Optional[LeaseKeeper] = field(default=None, init=False, repr=False)
    #: organisations du dernier lot du dispatcher pas encore essayees (tour de role complet)
    _pending: List[UUID] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.dispatcher is None and not self.sessions:
            raise ValueError("un worker sert au moins une session de tenant")
        if self.dispatcher is not None and self.sessions:
            raise ValueError("liste explicite de sessions OU dispatcher, jamais les deux")
        if not self.worker_id or len(self.worker_id) > 100:
            raise ValueError("worker_id non vide, 100 caracteres au plus")
        if self.lease_database is not None:
            if self.renew_seconds is None or not 0 < self.renew_seconds < self.lease_seconds:
                raise ValueError("0 < renew_seconds < lease_seconds attendu avec une connexion de bail")
        self.log = self.log.bind(worker_id=self.worker_id)

    @property
    def dispatched(self) -> bool:
        return self.dispatcher is not None

    @property
    def service_id(self) -> Optional[UUID]:
        return self.dispatcher.principal.id if self.dispatcher is not None else None

    # -- horloge ---------------------------------------------------------------------------
    def _now(self) -> Optional[datetime]:
        return self.clock() if self.clock is not None else None

    # -- prise -----------------------------------------------------------------------------
    def claim(self) -> Optional[tuple]:
        """Prend un travail dans la premiere organisation qui en a un. Rotation equitable."""
        if self.dispatcher is not None:
            fetched = False
            while True:
                if not self._pending:
                    if fetched:
                        return None
                    self._pending = list(self.dispatcher.next_organizations(now=self._now()))
                    fetched = True
                    if not self._pending:
                        return None
                organization_id = self._pending.pop(0)
                try:
                    claimed = self._claim_in(self.dispatcher.session(organization_id))
                except TenantAccessDenied:
                    # autorisation revoquee entre la liste et la prise: organisation hors de portee
                    self.log.debug("job.organization_skipped", organization_id=str(organization_id))
                    continue
                if claimed is not None:
                    return claimed
        count = len(self.sessions)
        for offset in range(count):
            claimed = self._claim_in(self.sessions[(self._cursor + offset) % count])
            if claimed is not None:
                self._cursor = (self._cursor + offset + 1) % count
                return claimed
        self._cursor = (self._cursor + 1) % count
        return None

    def _claim_in(self, session: TenantSession) -> Optional[tuple]:
        started = time.monotonic()
        job = jobs.claim_next_job(
            session, worker_id=self.worker_id, job_types=self.job_types,
            lease_seconds=self.lease_seconds, now=self._now(),
            hook=lambda conn, claimed: self._audit(
                conn, session, claimed, Action.JOB_CLAIMED, Outcome.STARTED, ActorType.WORKER,
                {"attempt": claimed.attempts, "max_attempts": claimed.max_attempts,
                 "queue_wait_ms": _milliseconds(claimed.available_at, claimed.started_at)}))
        if job is None:
            return None
        self._claimed_at[job.id] = started
        return session, job

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
        """Execute un travail DEJA pris et enregistre son sort. Ne leve pas (sauf interruption)."""
        claimed_at = self._claimed_at.pop(job.id, time.monotonic())
        queue_wait_ms = _milliseconds(job.available_at, job.started_at)
        log = self.log.bind(job_id=str(job.id), job_type=job.job_type,
                            organization_id=str(job.organization_id),
                            store_id=str(job.store_id) if job.store_id else None,
                            correlation_id=str(job.correlation_id))
        outcome: Optional[JobOutcome] = None
        with correlation_scope(str(job.correlation_id)):
            keeper = self._start_keeper(session, job, claimed_at, log)
            self._active = keeper
            try:
                self._safe_callback(self.on_job_start, job, log)
                log.info("job.claimed", attempt=job.attempts, max_attempts=job.max_attempts,
                         queue_wait_ms=queue_wait_ms, lease_keeper=keeper is not None)
                started = time.perf_counter()
                error: Optional[BaseException] = None
                result: Optional[Dict[str, Any]] = None
                try:
                    work_session = delegated_session(session, job) if self.dispatched else session
                    handler = self.registry.resolve(job.kind)
                    result = handler(JobContext(
                        session=work_session, job=job, correlation_id=str(job.correlation_id), log=log,
                        now=self._now(), checkpoint=keeper.checkpoint if keeper is not None else (lambda: None),
                        actor_id=self.service_id, on_behalf_of=job.enqueued_by if self.dispatched else None))
                except BaseException as exc:  # noqa: BLE001 - tout echec devient un etat de travail, jamais un silence
                    error = exc
                duration_ms = int((time.perf_counter() - started) * 1000)
                if keeper is not None:
                    keeper.stop()
                if keeper is not None and keeper.lost:
                    # la tentative est perdue: on ne publie RIEN, pas meme l'echec
                    log.warning("job.abandoned", reason=keeper.loss_reason, duration_ms=duration_ms,
                                error_type=type(error).__name__ if error is not None else None)
                    outcome = JobOutcome(keeper.job, "lease_lost", duration_ms, error_code=keeper.loss_reason)
                elif error is None:
                    outcome = self._record_success(session, job, result, duration_ms, log, keeper, claimed_at)
                else:
                    outcome = self._record_failure(session, job, error, duration_ms, log, keeper, claimed_at)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise error
                return outcome
            finally:
                if keeper is not None:
                    keeper.stop()
                self._active = None
                if outcome is not None:
                    self._safe_callback(self.on_job_end, outcome, log)

    def force_release(self, error_code: str = "worker_shutdown") -> bool:
        """Arret force: remet en file la tentative en cours (depuis un AUTRE fil). Vrai si liberee."""
        keeper = self._active
        return keeper.release(error_code) if keeper is not None else False

    @property
    def busy(self) -> bool:
        return self._active is not None

    def _start_keeper(self, session: TenantSession, job: JobRecord, claimed_at: float,
                      log: EventLogger) -> Optional[LeaseKeeper]:
        if self.lease_database is None:
            return None
        if isinstance(session, ServiceSession):
            lease_session: TenantSession = ServiceSession(self.lease_database, job.organization_id, session.principal)
        else:
            lease_session = TenantSession(self.lease_database, session.context)
        main_database = session.database
        # une instruction du gardien ne peut pas attendre plus qu'un intervalle de renouvellement
        timeout_ms = max(100, int(float(self.renew_seconds) * 1000))

        def renew(current: JobRecord) -> JobRecord:
            return jobs.renew_lease(lease_session, current, worker_id=self.worker_id,
                                    lease_seconds=int(self.lease_seconds), timeout_ms=timeout_ms)

        def release(current: JobRecord, error_code: str) -> JobRecord:
            return jobs.requeue(
                lease_session, current, error_code=error_code, worker_id=self.worker_id, delay_seconds=0,
                timeout_ms=timeout_ms, hook=lambda conn, queued: self._audit(
                    conn, lease_session, queued, Action.JOB_REQUEUED, Outcome.FAILED, ActorType.WORKER,
                    {"error_code": error_code, "attempt": queued.attempts, "max_attempts": queued.max_attempts,
                     "retryable": True, "reason": error_code}))

        def on_lost(reason: str) -> None:
            # le gestionnaire est interrompu: sa transaction en cours echoue et s'annule
            main_database.cancel_current_statement()

        return LeaseKeeper(job, renew=renew, release=release, reset=self.lease_database.close, on_lost=on_lost,
                           heartbeat=self.heartbeat, heartbeat_seconds=self.heartbeat_seconds,
                           lease_seconds=self.lease_seconds, renew_seconds=float(self.renew_seconds),
                           started_at=claimed_at, log=log).start()

    # -- publication -----------------------------------------------------------------------
    def _publish(self, call: Callable[[], JobRecord], session: TenantSession, keeper: Optional[LeaseKeeper],
                 claimed_at: float, log: EventLogger) -> JobRecord:
        """Publie le sort de la tentative; retente une panne transitoire tant que le bail local tient."""
        deadline = keeper.deadline if keeper is not None else claimed_at + self.lease_seconds
        delay = 0.1
        while True:
            try:
                return call()
            except JobLeaseLost:
                raise
            except Exception as exc:  # noqa: BLE001 - trie ci-dessous
                if not retryable_database_error(exc) or time.monotonic() + delay >= deadline - PUBLISH_MARGIN_SECONDS:
                    raise
                log.warning("job.publish_retry", error_type=type(exc).__name__, error_code=database_error_code(exc),
                            retry_in_ms=int(delay * 1000))
                session.database.close()
                time.sleep(delay)
                delay = min(delay * 2, 2.0)

    def _record_success(self, session: TenantSession, job: JobRecord, result: Optional[Dict[str, Any]],
                        duration_ms: int, log: EventLogger, keeper: Optional[LeaseKeeper] = None,
                        claimed_at: Optional[float] = None) -> JobOutcome:
        document = dict(result or {})
        document.setdefault("duration_ms", duration_ms)
        try:
            finished = self._publish(lambda: jobs.mark_succeeded(
                session, job, result=document, worker_id=self.worker_id, now=self._now(),
                hook=lambda conn, done: self._audit(
                    conn, session, done, Action.JOB_SUCCEEDED, Outcome.SUCCEEDED, ActorType.WORKER,
                    {"duration_ms": duration_ms, "attempt": done.attempts})),
                session, keeper, time.monotonic() if claimed_at is None else claimed_at, log)
        except JobLeaseLost:
            log.warning("job.lease_lost", duration_ms=duration_ms, attempt=job.attempts)
            return JobOutcome(job, "lease_lost", duration_ms, error_code="lease_lost")
        except Exception as exc:  # noqa: BLE001 - le travail sera repris apres expiration du bail
            return self._unrecorded(job, exc, duration_ms, log)
        log.info("job.succeeded", duration_ms=duration_ms, attempt=finished.attempts)
        return JobOutcome(finished, "succeeded", duration_ms, result=finished.result)

    def _record_failure(self, session: TenantSession, job: JobRecord, exc: BaseException, duration_ms: int,
                        log: EventLogger, keeper: Optional[LeaseKeeper] = None,
                        claimed_at: Optional[float] = None) -> JobOutcome:
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
            final = self._publish(lambda: jobs.mark_failed(
                session, job, error_code=failure.code, error=failure.message, retryable=failure.retryable,
                worker_id=self.worker_id, now=self._now(), hook=audit_failure,
                backoff_base=self.backoff_base_seconds, backoff_cap=self.backoff_cap_seconds),
                session, keeper, time.monotonic() if claimed_at is None else claimed_at, log)
        except JobLeaseLost:
            log.warning("job.lease_lost", duration_ms=duration_ms, attempt=job.attempts)
            return JobOutcome(job, "lease_lost", duration_ms, error_code="lease_lost")
        except Exception as publish_error:  # noqa: BLE001 - le travail sera repris apres expiration du bail
            return self._unrecorded(job, publish_error, duration_ms, log)
        requeued = final.state is jobs.JobStatus.QUEUED
        log.error("job.requeued" if requeued else "job.failed", error_code=failure.code,
                  error_type=type(exc).__name__, attempt=final.attempts, max_attempts=final.max_attempts,
                  retryable=failure.retryable, duration_ms=duration_ms)
        return JobOutcome(final, "requeued" if requeued else "failed", duration_ms, error_code=failure.code)

    def _unrecorded(self, job: JobRecord, exc: BaseException, duration_ms: int, log: EventLogger) -> JobOutcome:
        code = database_error_code(exc)
        log.error("job.result_unrecorded", error_type=type(exc).__name__, error_code=code,
                  attempt=job.attempts, duration_ms=duration_ms)
        return JobOutcome(job, "unrecorded", duration_ms, error_code=code)

    # -- recuperation ----------------------------------------------------------------------
    def recover_stale(self, *, limit: int = 100) -> List[JobRecord]:
        """Remet en file les travaux dont le bail a expire, dans ses organisations."""
        if self.dispatcher is not None:
            sessions = [self.dispatcher.session(organization_id)
                        for organization_id in self.dispatcher.expired_organizations(now=self._now())]
        else:
            sessions = list(self.sessions)
        recovered: List[JobRecord] = []
        for session in sessions:
            try:
                found = self._recover_in(session, limit)
            except TenantAccessDenied:
                if not self.dispatched:
                    raise  # 004.2: une session explicite sans droit reste une erreur de l'appelant
                continue
            for job in found:
                with correlation_scope(str(job.correlation_id)):
                    self.log.warning("job.recovered", job_id=str(job.id), job_type=job.job_type,
                                     organization_id=str(job.organization_id), status=job.status,
                                     attempt=job.attempts, correlation_id=str(job.correlation_id))
            recovered.extend(found)
        return recovered

    def _recover_in(self, session: TenantSession, limit: int) -> List[JobRecord]:
        return jobs.recover_stale_jobs(
            session, now=self._now(), limit=limit,
            hook=lambda conn, job, s=session: self._audit(
                conn, s, job, Action.JOB_RECOVERED, Outcome.FAILED, ActorType.SYSTEM,
                {"status": job.status, "attempt": job.attempts, "max_attempts": job.max_attempts,
                 "reason": "lease_expired"}, actor_id=self.service_id, on_behalf_of=None))

    # -- audit -----------------------------------------------------------------------------
    def _audit(self, conn, session: TenantSession, job: JobRecord, action, outcome, actor_type,
               metadata: Dict[str, Any], *, actor_id: Any = _UNSET, on_behalf_of: Any = _UNSET) -> None:
        """Trace un changement d'etat DANS la transaction qui le realise.

        Mode dispatcher: l'acteur est le principal du service, pour le compte du demandeur.
        Mode liste explicite (004.2): l'acteur est l'utilisateur de la session.
        """
        if actor_id is _UNSET:
            actor_id = self.service_id if self.dispatched else session.user_id
        if on_behalf_of is _UNSET:
            on_behalf_of = job.enqueued_by if self.dispatched else None
        audit.record(conn, organization_id=session.organization_id, action=action,
                     resource_type=ResourceType.JOB, resource_id=job.id,
                     correlation_id=job.correlation_id, outcome=outcome, actor_type=actor_type,
                     actor_id=actor_id, store_id=job.store_id, on_behalf_of=on_behalf_of,
                     metadata={**metadata, "job_type": job.job_type, "worker_id": self.worker_id})

    def _safe_callback(self, callback: Callable, argument: Any, log: EventLogger) -> None:
        try:
            callback(argument)
        except Exception as exc:  # noqa: BLE001 - un rappel du processus ne fait pas echouer un travail
            log.error("worker.callback_failed", error_type=type(exc).__name__)


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


__all__ = ["JobOutcome", "PUBLISH_MARGIN_SECONDS", "Worker", "cancel", "default_worker_id", "enqueue"]
