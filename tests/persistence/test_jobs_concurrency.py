"""Concurrence reelle: plusieurs workers, plusieurs connexions, un seul preneur par travail.

Ces tests n'inspectent pas le SQL: ils LANCENT des workers en parallele, chacun sur
sa propre connexion PostgreSQL, et verifient le resultat observable.

Ce que `FOR UPDATE SKIP LOCKED` garantit et que l'on mesure ici:
- un travail est pris par un worker au plus;
- un worker ne reste pas bloque derriere le travail qu'un autre tient deja;
- aucun travail disponible n'est laisse en file quand des workers tournent.
"""
from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from mervio.persistence import audit, jobs
from mervio.persistence.database import Database
from mervio.persistence.jobs import JobStatus, JobType
from mervio.persistence.tenancy import TenantContext, TenantSession
from mervio.workers.handlers import HandlerRegistry
from mervio.workers.worker import Worker

WORKERS = 10
JOBS = 100


@pytest.fixture
def connections(clean):
    """Une connexion applicative distincte par worker: la concurrence est reelle, pas simulee."""
    opened = []

    def open_session(organization_id, user_id) -> TenantSession:
        database = Database(clean.url("app"))
        opened.append(database)
        return TenantSession(database, TenantContext(organization_id, user_id))

    yield open_session
    for database in opened:
        database.close()


def parallel_registry(seen, lock) -> HandlerRegistry:
    def handler(context):
        with lock:
            seen.append(context.job.id)
        return {"job": str(context.job.id)}

    return HandlerRegistry().register(JobType.ANALYSIS, handler)


def run_workers(sessions, registry, *, expected: int):
    """Lance un worker par session, en parallele, jusqu'a ce que la file soit vide."""
    workers = [Worker(sessions=[session], registry=registry, worker_id=f"worker-{index}")
               for index, session in enumerate(sessions)]
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        futures = [pool.submit(worker.run_until_empty, max_jobs=expected) for worker in workers]
        return [outcome for future in futures for outcome in future.result()]


def test_a_hundred_jobs_and_ten_workers_run_each_job_exactly_once(tenant_a, connections):
    """100 travaux, 10 workers concurrents: 100 executions, aucune en double."""
    for index in range(JOBS):
        jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={"n": index})
    seen, lock = [], threading.Lock()
    sessions = [connections(tenant_a.organization_id, tenant_a.owner_id) for _ in range(WORKERS)]

    outcomes = run_workers(sessions, parallel_registry(seen, lock), expected=JOBS)

    assert len(outcomes) == JOBS
    assert all(outcome.succeeded for outcome in outcomes)
    assert len(set(seen)) == JOBS == len(seen)  # chaque travail execute une seule fois
    assert jobs.queue_statistics(tenant_a.session)["by_status"] == {"succeeded": JOBS}


def test_no_job_is_left_behind_by_concurrent_workers(tenant_a, connections):
    for index in range(JOBS):
        jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={"n": index})
    sessions = [connections(tenant_a.organization_id, tenant_a.owner_id) for _ in range(WORKERS)]

    run_workers(sessions, parallel_registry([], threading.Lock()), expected=JOBS)

    assert jobs.list_jobs(tenant_a.session, status=[JobStatus.QUEUED], limit=1) == []
    assert jobs.list_jobs(tenant_a.session, status=[JobStatus.RUNNING], limit=1) == []


def test_the_work_is_spread_across_workers(tenant_a, connections):
    """Si un seul worker prenait tout, SKIP LOCKED ne servirait a rien."""
    for index in range(JOBS):
        jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={"n": index})
    barrier = threading.Barrier(WORKERS, timeout=30)
    started = threading.Lock()

    def handler(context):
        with started:
            pass
        return {}

    sessions = [connections(tenant_a.organization_id, tenant_a.owner_id) for _ in range(WORKERS)]
    registry = HandlerRegistry().register(JobType.ANALYSIS, handler)
    workers = [Worker(sessions=[session], registry=registry, worker_id=f"worker-{index}")
               for index, session in enumerate(sessions)]

    def work(worker):
        barrier.wait()
        return worker.run_until_empty(max_jobs=JOBS)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        counts = [len(future.result()) for future in [pool.submit(work, worker) for worker in workers]]

    assert sum(counts) == JOBS
    assert len([count for count in counts if count]) >= 2


def test_a_locked_job_does_not_block_the_next_worker(tenant_a, connections):
    """Deux travaux, deux workers: le second ne patiente pas derriere le verrou du premier."""
    first = jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    second = jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    holder = connections(tenant_a.organization_id, tenant_a.owner_id)
    other = connections(tenant_a.organization_id, tenant_a.owner_id)

    with holder.transaction(jobs.Permission.RUN_JOBS) as conn:
        # verrou reel tenu sur la premiere ligne, transaction ouverte
        conn.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (first.id,))
        claimed = jobs.claim_next_job(other, worker_id="worker-other")
        assert claimed is not None and claimed.id == second.id

    assert jobs.get_job(tenant_a.session, first.id).status == JobStatus.QUEUED.value


def test_every_concurrent_execution_leaves_a_complete_audit_trail(tenant_a, connections):
    count = 20
    for index in range(count):
        jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={"n": index})
    sessions = [connections(tenant_a.organization_id, tenant_a.owner_id) for _ in range(5)]

    run_workers(sessions, parallel_registry([], threading.Lock()), expected=count)

    actions = Counter(event.action for event in audit.list_events(tenant_a.session, limit=1000))
    assert actions["job.claimed"] == count
    assert actions["job.succeeded"] == count
    assert actions["job.failed"] == 0


def test_concurrent_workers_of_two_tenants_never_cross(tenant_a, tenant_b, connections):
    """Chaque worker ne sert qu'un tenant: le travail de l'autre lui est inaccessible."""
    for _ in range(20):
        jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
        jobs.enqueue_job(tenant_b.session, job_type=JobType.ANALYSIS, payload={})
    seen, lock = [], threading.Lock()

    def handler(context):
        with lock:
            seen.append((context.session.organization_id, context.job.organization_id))
        return {}

    registry = HandlerRegistry().register(JobType.ANALYSIS, handler)
    sessions = ([connections(tenant_a.organization_id, tenant_a.owner_id) for _ in range(3)]
                + [connections(tenant_b.organization_id, tenant_b.owner_id) for _ in range(3)])

    outcomes = run_workers(sessions, registry, expected=40)

    assert len(outcomes) == 40
    assert all(worker_org == job_org for worker_org, job_org in seen)
    assert {organization for organization, _ in seen} == {tenant_a.organization_id, tenant_b.organization_id}


def test_concurrent_claims_of_the_same_job_yield_one_winner(tenant_a, connections):
    """Dix workers visent explicitement LE MEME travail: un seul l'obtient."""
    job = jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    sessions = [connections(tenant_a.organization_id, tenant_a.owner_id) for _ in range(WORKERS)]
    barrier = threading.Barrier(WORKERS, timeout=30)

    def claim(index):
        barrier.wait()
        return jobs.claim_next_job(sessions[index], worker_id=f"worker-{index}", job_id=job.id)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = [future.result() for future in [pool.submit(claim, index) for index in range(WORKERS)]]

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].attempts == 1


def test_concurrent_recoveries_requeue_a_stale_job_only_once(tenant_a, connections):
    """Plusieurs superviseurs simultanes ne dupliquent ni ne perdent un travail expire."""
    from datetime import datetime, timedelta, timezone

    job = jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    jobs.claim_next_job(tenant_a.session, worker_id="worker-dead", lease_seconds=1)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    sessions = [connections(tenant_a.organization_id, tenant_a.owner_id) for _ in range(5)]
    barrier = threading.Barrier(5, timeout=30)

    def recover(session):
        barrier.wait()
        return jobs.recover_stale_jobs(session, now=later)

    with ThreadPoolExecutor(max_workers=5) as pool:
        recovered = [record for future in [pool.submit(recover, session) for session in sessions]
                     for record in future.result()]

    assert [record.id for record in recovered] == [job.id]
    assert jobs.get_job(tenant_a.session, job.id).status == JobStatus.QUEUED.value
    assert len(jobs.list_jobs(tenant_a.session)) == 1
