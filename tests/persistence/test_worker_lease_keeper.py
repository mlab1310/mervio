"""Gardien de bail dans le worker reel, contre PostgreSQL (Mission 004.3).

Chaque scenario utilise le vrai role worker, le dispatcher, deux connexions par worker
et le jeton d'exclusion de la revision 0006. La semantique prouvee: execution AU MOINS
une fois, et JAMAIS de publication par une tentative qui a perdu son bail.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
from datetime import timedelta

import pytest

from mervio.observability.logging import configure
from mervio.persistence import jobs
from mervio.persistence.jobs import JobStatus
from mervio.persistence.service import ServiceSession, authorize_service
from mervio.workers.lease import LeaseKeeper

from .service_support import raw
from .worker_support import (
    AFTER_WORK, DispatchedWorker, actions, enqueue_scripted, later, running_statement, terminate_backends,
    wait_until,
)

PUBLISHED = {"job.succeeded", "job.failed", "job.requeued"}


@pytest.fixture
def authorized(tenant_a, principal):
    authorize_service(tenant_a.session, principal.id)
    return tenant_a


@pytest.fixture
def workers(pg, principal):
    created = []

    def make(worker_id, **options):
        worker = DispatchedWorker(pg, principal, worker_id=worker_id, **options)
        created.append(worker)
        return worker

    yield make
    for worker in created:
        worker.close()


@pytest.fixture
def json_logs():
    root = logging.getLogger("mervio")
    state = (list(root.handlers), root.propagate, root.level)
    buffer = io.StringIO()
    configure(format="json", level=logging.DEBUG, stream=buffer)
    yield buffer
    root.handlers, root.propagate, root.level = state


def events(buffer, name=None):
    lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
    return [line for line in lines if name is None or line["event"] == name]


class Background:
    def __init__(self, function):
        self.result = self.error = None
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, args=(function,), daemon=True)
        self.thread.start()

    def _run(self, function):
        try:
            self.result = function()
        except BaseException as exc:  # noqa: BLE001 - observe dans le fil principal
            self.error = exc
        finally:
            self.elapsed = time.monotonic() - self.started

    def join(self, timeout=60):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "le worker ne rend pas la main"
        assert self.error is None, self.error
        return self.result


def published_by_worker(tenant, job_id, principal_id):
    return [a for a, actor_type, actor, _ in actions(tenant, job_id) if a in PUBLISHED and actor == principal_id]


# -- 1. travail court -----------------------------------------------------------------------

def test_a_short_job_is_published_without_renewal(authorized, workers, principal):
    worker = workers("short")
    job = enqueue_scripted(authorized)
    outcome = worker.worker.run_once()
    assert outcome.succeeded and outcome.job.id == job.id
    [keeper] = worker.keepers
    assert keeper.renewals == 0 and not keeper.lost
    assert [(a, t, actor, behalf) for a, t, actor, behalf in actions(authorized, job.id)] == [
        ("job.claimed", "worker", principal.id, authorized.owner_id),
        ("job.succeeded", "worker", principal.id, authorized.owner_id)]


# -- 2 et 3. travail long, bail renouvele plusieurs fois, reprise concurrente -------------------

def test_a_long_job_is_renewed_and_never_stolen(authorized, workers):
    worker = workers("long", lease_seconds=2, renew_seconds=0.5)
    supervisor = workers("supervisor", keeper=False)
    job = enqueue_scripted(authorized, mode="checkpointed", seconds=5)
    stop, stolen = threading.Event(), []

    def supervise():
        while not stop.is_set():
            stolen.extend(supervisor.worker.recover_stale())
            time.sleep(0.1)

    watcher = threading.Thread(target=supervise, daemon=True)
    watcher.start()
    try:
        outcome = worker.worker.run_once()
    finally:
        stop.set()
        watcher.join(10)
    assert stolen == []
    [keeper] = worker.keepers
    assert keeper.renewals >= 8 and keeper.failures == 0 and not keeper.lost
    assert outcome.succeeded and outcome.job.attempts == 1 and outcome.job.id == job.id


# -- 4 et 5. bail perdu par une reprise concurrente: le gestionnaire est interrompu -----------

def test_a_lost_lease_interrupts_the_handler_and_publishes_nothing(pg, authorized, workers, principal):
    worker = workers("lost")
    job = enqueue_scripted(authorized, mode="sql", seconds=30)
    run = Background(worker.worker.run_once)
    wait_until(lambda: running_statement(pg, "test-main:lost"), message="le gestionnaire n'a pas demarre")
    assert [j.id for j in jobs.recover_stale_jobs(authorized.session, now=later(60))] == [job.id]
    outcome = run.join()
    assert run.elapsed < 10, "l'instruction longue aurait du etre annulee"
    assert (outcome.outcome, outcome.error_code) == ("lease_lost", "lease_lost")
    stored = jobs.get_job(authorized.session, job.id)
    assert (stored.status, stored.attempts, stored.last_error_code) == ("queued", 1, "lease_expired")
    assert published_by_worker(authorized, job.id, principal.id) == []


@pytest.mark.parametrize("same_id", [False, True])
def test_a_stale_worker_never_publishes_after_another_took_over(authorized, workers, principal, same_id):
    """A travaille sans jamais consulter son bail; B reprend et termine. A ne publie rien."""
    first = workers("stale-a" if not same_id else "same-id")
    moment = later(60)
    # B vit a l'horloge declaree par la reprise (le travail n'est disponible qu'a cet instant)
    second = workers("stale-b" if not same_id else "same-id", clock=lambda: moment + timedelta(seconds=1))
    job = enqueue_scripted(authorized, mode="blind", seconds=3, only_attempt=1)
    run = Background(first.worker.run_once)
    wait_until(lambda: first.keepers, message="A n'a rien pris")
    assert [j.id for j in jobs.recover_stale_jobs(authorized.session, now=moment)] == [job.id]
    taken = second.worker.run_once()
    assert taken.succeeded and taken.job.attempts == 2 and taken.result["attempt"] == 2
    outcome = run.join()
    assert outcome.outcome == "lease_lost"
    # perte constatee par le jeton au renouvellement suivant, pas par simple expiration locale
    assert first.keepers[0].lost and first.keepers[0].loss_reason == "lease_lost"
    assert first.keepers[0].failures == 0
    final = jobs.get_job(authorized.session, job.id)
    assert (final.status, final.attempts, final.result["attempt"]) == ("succeeded", 2, 2)
    succeeded = [a for a, *_ in actions(authorized, job.id) if a == "job.succeeded"]
    assert succeeded == ["job.succeeded"], "un seul publicateur"


# -- 9. expiration locale: bail non renouvelable, la base n'a encore rien repris ---------------

def test_a_lease_that_cannot_be_renewed_is_abandoned_before_its_expiry(owner, authorized, workers, principal):
    worker = workers("expiring", lease_seconds=2, renew_seconds=0.5)
    job = enqueue_scripted(authorized, mode="blind", seconds=3.5)
    run = Background(worker.worker.run_once)
    wait_until(lambda: worker.keepers, message="rien pris")
    with raw(owner, authorized.organization_id) as conn:
        conn.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (job.id,))  # bloque les renouvellements
        outcome = run.join()
    keeper = worker.keepers[0]
    assert (outcome.outcome, outcome.error_code) == ("lease_lost", "lease_expired")
    assert keeper.failures >= 1 and keeper.renewals == 0
    # la base aurait encore accepte le resultat: c'est le worker qui s'abstient
    assert jobs.get_job(authorized.session, job.id).status == JobStatus.RUNNING.value
    assert published_by_worker(authorized, job.id, principal.id) == []
    wait_until(lambda: jobs.recover_stale_jobs(authorized.session), timeout=5)
    assert jobs.get_job(authorized.session, job.id).status == JobStatus.QUEUED.value


# -- 8. arret pendant un renouvellement ----------------------------------------------------------

def test_stopping_during_a_blocked_renewal_waits_then_renews_nothing(owner, pg, authorized, worker_db, principal):
    enqueue_scripted(authorized)
    session = ServiceSession(worker_db, authorized.organization_id, principal)
    job = jobs.claim_next_job(session, worker_id="stopper", lease_seconds=30)
    from mervio.persistence.database import Database
    lease_db = Database(pg.url("worker"))
    lease_session = ServiceSession(lease_db, authorized.organization_id, principal)
    started, finished = [], []

    def renew(current):
        started.append(time.monotonic())
        try:
            return jobs.renew_lease(lease_session, current, worker_id="stopper", lease_seconds=30, timeout_ms=1500)
        finally:
            finished.append(time.monotonic())

    keeper = LeaseKeeper(job, renew=renew, lease_seconds=30, renew_seconds=0.2, heartbeat_seconds=0.05)
    try:
        with raw(owner, authorized.organization_id) as conn:
            conn.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (job.id,))
            keeper.start()
            wait_until(lambda: started, message="aucun renouvellement")
            time.sleep(0.2)
            assert not finished, "le renouvellement devrait etre bloque"
            stop_started = time.monotonic()
            assert keeper.stop()
            assert time.monotonic() - stop_started < 2.5
        calls = len(started)
        time.sleep(0.6)
        assert len(started) == calls == len(finished), "aucun renouvellement apres l'arret"
        assert keeper.failures >= 1 and not keeper.lost
    finally:
        keeper.stop()
        lease_db.close()


# -- 10. connexion du gardien coupee ------------------------------------------------------------

def test_the_keeper_survives_the_loss_of_its_connection(pg, authorized, workers):
    worker = workers("keeper-cut", lease_seconds=3, renew_seconds=0.5)
    job = enqueue_scripted(authorized, mode="checkpointed", seconds=4)
    run = Background(worker.worker.run_once)
    wait_until(lambda: worker.keepers and worker.keepers[0].renewals >= 1, message="aucun renouvellement")
    assert terminate_backends(pg, "test-lease:keeper-cut") >= 1
    outcome = run.join()
    keeper = worker.keepers[0]
    assert outcome.succeeded and outcome.job.id == job.id
    assert keeper.failures >= 1 and keeper.renewals >= 3 and not keeper.lost


# -- 11. connexion principale coupee pendant le gestionnaire, puis a la publication -----------

def test_a_cut_main_connection_during_the_handler_requeues_the_job(pg, authorized, workers):
    worker = workers("main-cut")
    job = enqueue_scripted(authorized, mode="sql", seconds=30)
    run = Background(worker.worker.run_once)
    wait_until(lambda: running_statement(pg, "test-main:main-cut"), message="le gestionnaire n'a pas demarre")
    assert terminate_backends(pg, "test-main:main-cut") >= 1
    outcome = run.join()
    assert outcome.outcome == "requeued"
    assert outcome.error_code in {"sqlstate_57P01", "OperationalError"}
    stored = jobs.get_job(authorized.session, job.id)
    assert (stored.status, stored.attempts, stored.last_error_code) == ("queued", 1, outcome.error_code)


def test_a_cut_main_connection_at_publication_is_retried(pg, authorized, workers, json_logs):
    worker = workers("publish-cut")
    job = enqueue_scripted(authorized, mode="after_work")
    AFTER_WORK.append(lambda context: terminate_backends(pg, "test-main:publish-cut"))
    try:
        outcome = worker.worker.run_once()
    finally:
        AFTER_WORK.clear()
    assert outcome.succeeded and outcome.job.id == job.id
    assert events(json_logs, "job.publish_retry")
    assert jobs.get_job(authorized.session, job.id).status == JobStatus.SUCCEEDED.value


# -- 12. exceptions du gestionnaire ------------------------------------------------------------

def test_handler_errors_keep_their_codes_and_retry_policy(authorized, workers):
    worker = workers("errors", backoff_base_seconds=7, backoff_cap_seconds=60)
    retryable = enqueue_scripted(authorized, mode="retryable")
    outcome = worker.worker.run_once()
    assert (outcome.outcome, outcome.error_code) == ("requeued", "locked")
    delay = (outcome.job.available_at - outcome.job.updated_at).total_seconds()
    assert 6 <= delay <= 8
    permanent = enqueue_scripted(authorized, mode="permanent")
    outcome = worker.worker.run_once()
    assert (outcome.outcome, outcome.error_code, outcome.job.id) == ("failed", "payload_invalid", permanent.id)
    assert jobs.get_job(authorized.session, retryable.id).last_error_code == "locked"


def test_a_leaky_error_is_stored_and_logged_without_its_secret(authorized, workers, json_logs):
    worker = workers("leaky")
    job = enqueue_scripted(authorized, mode="leaky")
    outcome = worker.worker.run_once()
    assert (outcome.outcome, outcome.error_code) == ("requeued", "runtime_error")
    stored = jobs.get_job(authorized.session, job.id)
    assert "s3cr3t" not in stored.last_error and "/Users/alice" not in stored.last_error
    raw_logs = json_logs.getvalue()
    assert "s3cr3t" not in raw_logs and "/Users/alice" not in raw_logs
    [line] = events(json_logs, "job.requeued")
    assert line["error_type"] == "RuntimeError" and line["error_code"] == "runtime_error"


# -- 13. travail rendu terminal pendant l'execution ---------------------------------------------

def test_a_job_made_terminal_by_recovery_is_abandoned(pg, authorized, workers, principal):
    worker = workers("terminal")
    job = enqueue_scripted(authorized, mode="sql", seconds=30, max_attempts=1)
    run = Background(worker.worker.run_once)
    wait_until(lambda: running_statement(pg, "test-main:terminal"), message="le gestionnaire n'a pas demarre")
    [recovered] = jobs.recover_stale_jobs(authorized.session, now=later(60))
    assert recovered.status == JobStatus.FAILED.value
    outcome = run.join()
    assert outcome.outcome == "lease_lost"
    stored = jobs.get_job(authorized.session, job.id)
    assert (stored.status, stored.last_error_code) == ("failed", "lease_expired")
    assert published_by_worker(authorized, job.id, principal.id) == []


# -- aucun double publicateur ---------------------------------------------------------------------

def test_two_workers_and_a_supervisor_publish_each_job_exactly_once(authorized, workers):
    expected = {enqueue_scripted(authorized).id for _ in range(30)}
    expected |= {enqueue_scripted(authorized, mode="checkpointed", seconds=0.6).id for _ in range(6)}
    runners = [workers(f"pair-{index}", lease_seconds=2, renew_seconds=0.5) for index in range(2)]
    supervisor = workers("pair-supervisor", keeper=False)
    stop, stolen = threading.Event(), []

    def supervise():
        while not stop.is_set():
            stolen.extend(supervisor.worker.recover_stale())
            time.sleep(0.05)

    watcher = threading.Thread(target=supervise, daemon=True)
    watcher.start()
    try:
        results = [Background(runner.worker.run_until_empty) for runner in runners]
        outcomes = [outcome for run in results for outcome in run.join()]
    finally:
        stop.set()
        watcher.join(10)
    assert stolen == []
    assert sorted(o.job.id for o in outcomes) == sorted(expected)
    assert all(o.succeeded for o in outcomes)
    for job_id in expected:
        assert [a for a, *_ in actions(authorized, job_id) if a == "job.succeeded"] == ["job.succeeded"]


def test_an_empty_queue_never_starts_a_keeper(authorized, workers):
    worker = workers("nothing")
    assert worker.worker.run_once() is None
    assert worker.keepers == [] and not worker.worker.busy
