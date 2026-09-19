"""Aides des tests du processus worker (Mission 004.3).

Importable hors de pytest: le lanceur de sous-processus (`worker_launcher.py`) s'en sert.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from mervio.persistence import jobs
from mervio.persistence.database import Database
from mervio.persistence.dispatch import Dispatcher
from mervio.persistence.jobs import JobType
from mervio.persistence.tenancy import Permission
from mervio.workers.errors import PermanentJobError, RetryableJobError
from mervio.workers.handlers import HandlerRegistry, default_registry
from mervio.workers.worker import Worker

from .persistence_support import TEST_MASTER_KEY

#: Crochet des tests en processus: appele par le gestionnaire `after_work` avec son contexte.
AFTER_WORK: List[Callable] = []


def scripted_handler(context):
    """Gestionnaire pilote par la charge utile: {"mode": ..., "seconds": ..., "only_attempt": ...}."""
    payload = context.payload
    mode = payload.get("mode", "quick")
    seconds = float(payload.get("seconds", 0))
    only_attempt = payload.get("only_attempt")
    if only_attempt is not None and context.job.attempts != int(only_attempt):
        mode = "quick"
    if mode == "checkpointed":
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            context.checkpoint()
            time.sleep(0.05)
    elif mode == "sql":
        # instruction longue sur la connexion PRINCIPALE, annulable par le gardien
        with context.session.transaction(Permission.RUN_ANALYSIS) as conn:
            conn.execute("SELECT pg_sleep(%s)", (seconds,))
    elif mode == "blind":
        time.sleep(seconds)  # n'appelle jamais checkpoint
    elif mode == "cpu":
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            sum(range(1000))
        return {"mode": mode, "attempt": context.job.attempts}
    elif mode == "retryable":
        raise RetryableJobError("locked", "verrou pris")
    elif mode == "permanent":
        raise PermanentJobError("payload_invalid", "charge refusee")
    elif mode == "leaky":
        raise RuntimeError("echec de " + "postgresql://svc:" + "Zq9-s3cr3t" + "@db/x lecture /Users/alice/x.csv")
    elif mode == "after_work":
        for hook in AFTER_WORK:
            hook(context)
    context.checkpoint()
    return {"mode": mode, "attempt": context.job.attempts}


def scripted_registry(*, with_defaults: bool = False) -> HandlerRegistry:
    """`analysis` pilote par la charge utile; avec `with_defaults`, import et purge reels de 004.2."""
    registry = HandlerRegistry()
    if with_defaults:
        for kind in (JobType.IMPORT, JobType.PURGE):
            registry.register(kind, default_registry(identity_master=TEST_MASTER_KEY).resolve(kind))
    return registry.register(JobType.ANALYSIS, scripted_handler)


def enqueue_scripted(tenant, *, session=None, max_attempts: int = 3, **payload):
    return jobs.enqueue_job(session or tenant.session, job_type=JobType.ANALYSIS, payload=payload,
                            max_attempts=max_attempts)


class DispatchedWorker:
    """Un worker dispatche avec SES deux connexions (principale et bail)."""

    def __init__(self, pg, principal, *, worker_id: str, lease_seconds: int = 2, renew_seconds: float = 0.5,
                 registry: Optional[HandlerRegistry] = None, keeper: bool = True, kind: str = "worker", **options):
        self.main = Database(pg.url(kind), application_name=f"test-main:{worker_id}")
        self.lease = Database(pg.url(kind), application_name=f"test-lease:{worker_id}") if keeper else None
        self.keepers = []
        user_start = options.pop("on_job_start", None)

        def on_start(job):
            if self.worker._active is not None:
                self.keepers.append(self.worker._active)
            if user_start is not None:
                user_start(job)

        self.worker = Worker(
            dispatcher=Dispatcher(self.main, principal), registry=registry or scripted_registry(), worker_id=worker_id,
            lease_seconds=lease_seconds, lease_database=self.lease, renew_seconds=renew_seconds if keeper else None,
            heartbeat_seconds=0.2, on_job_start=on_start, **options)

    def close(self):
        for database in (self.main, self.lease):
            if database is not None:
                database.close()


def wait_until(predicate: Callable[[], bool], timeout: float = 15.0, interval: float = 0.02,
               message: str = "condition jamais remplie"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(message)


def terminate_backends(pg, application_name: str) -> int:
    with pg.admin() as conn:
        return conn.execute(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = %s AND application_name = %s", (pg.database, application_name)).fetchone()[0]


def running_statement(pg, application_name: str) -> bool:
    with pg.admin() as conn:
        return bool(conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND application_name = %s "
            "AND state = 'active' AND query LIKE %s", (pg.database, application_name, "%pg_sleep%")).fetchone()[0])


def later(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def actions(tenant, job_id) -> list:
    from mervio.persistence import audit
    return [(event.action, event.actor_type, event.actor_id, event.on_behalf_of)
            for event in audit.list_events(tenant.session, resource_id=job_id, limit=200)]
