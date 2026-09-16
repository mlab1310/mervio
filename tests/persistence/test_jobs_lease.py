"""Bail renouvelable et jeton d'exclusion (Mission 004.3, revision 0006).

Trois niveaux de preuve, tous contre un vrai PostgreSQL:
1. le trigger, en SQL brut (role applicatif et proprietaire);
2. le depot (`renew_lease`, `mark_*`, `requeue`, `recover_stale_jobs`);
3. les courses, avec de vraies connexions concurrentes et le verrouillage reel de
   PostgreSQL: l'attente d'un verrou est constatee dans `pg_stat_activity`, pas supposee.

Semantique prouvee: au plus UN detenteur publie le sort d'une tentative. L'execution
reste AU MOINS une fois; rien ici ne pretend l'exactement-une-fois.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from mervio.persistence import jobs, migrate
from mervio.persistence.database import Database
from mervio.persistence.errors import JobLeaseLost, NotFound, PermissionDenied
from mervio.persistence.jobs import JobStatus, JobType
from mervio.persistence.tenancy import Role, TenantContext, TenantSession, add_member, ensure_user

WORKER_A = "worker-a"
WORKER_B = "worker-b"
#: instant fixe du passe: un bail pose a cet instant est deja expire pour l'horloge de la base
PAST = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(tenant, **kwargs):
    return jobs.enqueue_job(tenant.session, job_type=JobType.ANALYSIS, payload={"k": "v"}, **kwargs)


def claim_live(tenant, worker=WORKER_A, lease=60, session=None):
    """Prise a l'horloge reelle: le bail est valide pour la base pendant `lease` secondes."""
    enqueue(tenant)
    job = jobs.claim_next_job(session or tenant.session, worker_id=worker, lease_seconds=lease)
    assert job is not None
    return job


def after_expiry(job) -> datetime:
    """Instant de reprise declare: juste apres l'echeance du bail."""
    return job.lease_expires_at + timedelta(seconds=1)


def current(tenant, job):
    return jobs.get_job(tenant.session, job.id)


@pytest.fixture
def second_session(pg, tenant_a):
    """Meme tenant, AUTRE connexion PostgreSQL: deux acteurs reellement concurrents."""
    database = Database(pg.url("app"))
    yield TenantSession(database, TenantContext(tenant_a.organization_id, tenant_a.owner_id))
    database.close()


def raw_update(conn, tenant, statement, params=(), *, token=None, recovery_at=None) -> int:
    """UPDATE brut dans une transaction, avec le contexte que la transaction declare."""
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true), set_config('app.user_id', %s, true)",
                     (str(tenant.organization_id), str(tenant.owner_id)))
        if token is not None:
            conn.execute("SELECT set_config('app.job_lease_token', %s, true)", (token,))
        if recovery_at is not None:
            conn.execute("SELECT set_config('app.job_lease_recovery_at', %s, true)", (recovery_at.isoformat(),))
        return conn.execute(statement, params).rowcount


def token_of(job) -> str:
    return jobs.lease_token(job.attempts, job.locked_by)


def refused(conn, tenant, statement, params=(), *, message, **declared):
    with pytest.raises(psycopg.errors.RestrictViolation) as error:
        raw_update(conn, tenant, statement, params, **declared)
    assert error.value.diag.message_primary == message


# =============================================================================================
# 1. Trigger, en SQL brut
# =============================================================================================

RENEW_SQL = "UPDATE jobs SET lease_expires_at = lease_expires_at + interval '5 minutes' WHERE id = %s"


def test_the_holder_renews_a_valid_lease_in_raw_sql(app_conn, tenant_a):
    job = claim_live(tenant_a)
    assert raw_update(app_conn, tenant_a, RENEW_SQL, (job.id,), token=token_of(job)) == 1
    renewed = current(tenant_a, job)
    assert renewed.lease_expires_at == job.lease_expires_at + timedelta(minutes=5)
    assert (renewed.status, renewed.attempts, renewed.locked_by) == ("running", 1, WORKER_A)


@pytest.mark.parametrize("token", [None, "", "1:worker-b", "2:worker-a", "0:worker-a", "1:worker-a ", "worker-a"])
def test_running_to_running_without_the_exact_token_is_refused(app_conn, tenant_a, token):
    job = claim_live(tenant_a)
    refused(app_conn, tenant_a, RENEW_SQL, (job.id,), token=token, message="only the lease holder renews a lease")
    assert current(tenant_a, job).lease_expires_at == job.lease_expires_at


@pytest.mark.parametrize("assignment", [
    "locked_by = 'worker-b'",
    "locked_at = locked_at - interval '1 second'",
    "started_at = started_at - interval '1 second'",
    "available_at = available_at + interval '1 second'",
    "priority = 5",
    "last_error_code = 'x'",
    "last_error = 'x'",
    "result = '{}'::jsonb",
    "finished_at = now()",
])
def test_a_renewal_changes_nothing_but_the_lease(app_conn, tenant_a, assignment):
    job = claim_live(tenant_a)
    refused(app_conn, tenant_a, f"UPDATE jobs SET {assignment} WHERE id = %s", (job.id,), token=token_of(job),
            message="a running job only changes by renewing its lease")


def test_a_lease_is_never_shortened(app_conn, tenant_a):
    job = claim_live(tenant_a)
    refused(app_conn, tenant_a, "UPDATE jobs SET lease_expires_at = lease_expires_at - interval '1 second' "
            "WHERE id = %s", (job.id,), token=token_of(job), message="a lease is never shortened")


def test_a_lease_is_renewed_for_one_day_at_most(app_conn, tenant_a):
    job = claim_live(tenant_a)
    refused(app_conn, tenant_a, "UPDATE jobs SET lease_expires_at = now() + interval '2 days' WHERE id = %s",
            (job.id,), token=token_of(job), message="a lease is renewed for one day at most")
    assert raw_update(app_conn, tenant_a, "UPDATE jobs SET lease_expires_at = now() + interval '23 hours' "
                      "WHERE id = %s", (job.id,), token=token_of(job)) == 1


def test_an_expired_lease_cannot_be_renewed_even_by_its_holder(app_conn, tenant_a):
    enqueue(tenant_a, available_at=PAST)
    job = jobs.claim_next_job(tenant_a.session, worker_id=WORKER_A, now=PAST, lease_seconds=60)
    refused(app_conn, tenant_a, RENEW_SQL, (job.id,), token=token_of(job),
            message="an expired lease cannot be renewed")


@pytest.mark.parametrize("statement", [
    "UPDATE jobs SET status = 'succeeded', finished_at = now(), locked_at = NULL, locked_by = NULL, "
    "lease_expires_at = NULL WHERE id = %s",
    "UPDATE jobs SET status = 'failed', finished_at = now(), locked_at = NULL, locked_by = NULL, "
    "lease_expires_at = NULL, last_error_code = 'boom' WHERE id = %s",
    "UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL, lease_expires_at = NULL WHERE id = %s",
])
@pytest.mark.parametrize("token", [None, "1:worker-b", "2:worker-a"])
def test_only_the_holder_finishes_a_running_job(app_conn, owner, tenant_a, statement, token):
    job = claim_live(tenant_a)
    refused(app_conn, tenant_a, statement, (job.id,), token=token,
            message="only the lease holder finishes a running job")
    # le proprietaire des tables est soumis au meme trigger
    refused(owner, tenant_a, statement, (job.id,), token=token,
            message="only the lease holder finishes a running job")
    assert raw_update(app_conn, tenant_a, statement, (job.id,), token=token_of(job)) == 1


def test_a_recovery_declared_before_the_expiry_is_refused(app_conn, tenant_a):
    job = claim_live(tenant_a)
    refused(app_conn, tenant_a,
            "UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL, lease_expires_at = NULL, "
            "last_error_code = 'lease_expired' WHERE id = %s", (job.id,),
            recovery_at=job.lease_expires_at - timedelta(seconds=1),
            message="only the lease holder finishes a running job")


@pytest.mark.parametrize("statement", [
    "UPDATE jobs SET status = 'succeeded', finished_at = now(), locked_at = NULL, locked_by = NULL, "
    "lease_expires_at = NULL, last_error_code = 'lease_expired' WHERE id = %s",
    "UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL, lease_expires_at = NULL, "
    "last_error_code = 'boom' WHERE id = %s",
    "UPDATE jobs SET status = 'failed', finished_at = now(), locked_at = NULL, locked_by = NULL, "
    "lease_expires_at = NULL WHERE id = %s",
])
def test_an_expired_lease_is_recovered_never_completed(app_conn, tenant_a, statement):
    job = claim_live(tenant_a)
    refused(app_conn, tenant_a, statement, (job.id,), recovery_at=after_expiry(job),
            message="an expired lease is recovered, never completed")


def test_a_raw_recovery_of_an_expired_lease_is_accepted(app_conn, tenant_a):
    job = claim_live(tenant_a)
    assert raw_update(app_conn, tenant_a,
                      "UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL, "
                      "lease_expires_at = NULL, last_error_code = 'lease_expired' WHERE id = %s", (job.id,),
                      recovery_at=after_expiry(job)) == 1
    assert current(tenant_a, job).attempts == 1


def test_the_token_does_not_survive_its_transaction(app_conn, tenant_a):
    job = claim_live(tenant_a)
    assert raw_update(app_conn, tenant_a, RENEW_SQL, (job.id,), token=token_of(job)) == 1
    refused(app_conn, tenant_a, RENEW_SQL, (job.id,), message="only the lease holder renews a lease")


@pytest.mark.parametrize("statement, message", [
    ("UPDATE jobs SET status = 'running', attempts = attempts + 2, locked_at = now(), locked_by = 'w', "
     "lease_expires_at = now() + interval '1 minute', started_at = now() WHERE id = %s",
     "a claim consumes exactly one attempt"),
    ("UPDATE jobs SET status = 'running', locked_at = now(), locked_by = 'w', "
     "lease_expires_at = now() + interval '1 minute', started_at = now() WHERE id = %s",
     "a claim consumes exactly one attempt"),
    ("UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_at = now(), locked_by = 'w', "
     "lease_expires_at = now(), started_at = now() WHERE id = %s",
     "a lease ends after it starts"),
    ("UPDATE jobs SET attempts = attempts + 1 WHERE id = %s", "only a claim changes the attempt counter"),
    ("UPDATE jobs SET status = 'cancelled', finished_at = now(), attempts = attempts + 1 WHERE id = %s",
     "only a claim changes the attempt counter"),
])
def test_queued_jobs_keep_their_attempt_invariants(app_conn, tenant_a, statement, message):
    job = enqueue(tenant_a)
    refused(app_conn, tenant_a, statement, (job.id,), message=message)


def test_the_holder_never_changes_the_attempt_counter(app_conn, tenant_a):
    job = claim_live(tenant_a)
    refused(app_conn, tenant_a, "UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL, "
            "lease_expires_at = NULL, attempts = attempts + 1 WHERE id = %s", (job.id,), token=token_of(job),
            message="only a claim changes the attempt counter")


def test_a_running_job_is_still_never_cancelled(app_conn, tenant_a):
    job = claim_live(tenant_a)
    refused(app_conn, tenant_a, "UPDATE jobs SET status = 'cancelled', finished_at = now(), locked_at = NULL, "
            "locked_by = NULL, lease_expires_at = NULL WHERE id = %s", (job.id,), token=token_of(job),
            message="illegal job state transition")


def test_the_identity_of_a_running_job_stays_immutable_with_a_valid_token(owner, tenant_a):
    """Le proprietaire a le droit de colonne: c'est le trigger, pas un GRANT, qui refuse."""
    job = claim_live(tenant_a)
    refused(owner, tenant_a, "UPDATE jobs SET payload = '{}'::jsonb WHERE id = %s", (job.id,),
            token=token_of(job), message="job identity is immutable")


def test_a_finished_job_stays_immutable_even_with_its_old_token(app_conn, tenant_a):
    job = claim_live(tenant_a)
    token = token_of(job)
    jobs.mark_succeeded(tenant_a.session, job, worker_id=WORKER_A)
    refused(app_conn, tenant_a, RENEW_SQL, (job.id,), token=token, message="a finished job is immutable")


# =============================================================================================
# 2. Depot
# =============================================================================================

def comparable(job):
    return dataclasses.replace(job, lease_expires_at=None, updated_at=None)


def test_renewal_extends_the_lease_and_nothing_else(tenant_a):
    job = claim_live(tenant_a, lease=30)
    before = utcnow()
    renewed = jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A, lease_seconds=600)
    assert renewed.lease_expires_at >= before + timedelta(seconds=599)
    assert renewed.lease_expires_at > job.lease_expires_at
    assert comparable(renewed) == comparable(job)


def test_renewal_never_shortens_a_longer_lease(tenant_a):
    job = claim_live(tenant_a, lease=600)
    renewed = jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A, lease_seconds=30)
    assert renewed.lease_expires_at == job.lease_expires_at


def test_renewal_defaults_to_the_holder_of_the_record(tenant_a):
    job = claim_live(tenant_a, lease=30)
    assert jobs.renew_lease(tenant_a.session, job, lease_seconds=120).lease_expires_at > job.lease_expires_at


def test_renewal_by_another_worker_is_refused(tenant_a):
    job = claim_live(tenant_a)
    with pytest.raises(JobLeaseLost):
        jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_B)
    assert current(tenant_a, job) == job


@pytest.mark.parametrize("attempts", [0, 2])
def test_renewal_for_another_attempt_is_refused(tenant_a, attempts):
    job = claim_live(tenant_a)
    with pytest.raises(JobLeaseLost):
        jobs.renew_lease(tenant_a.session, dataclasses.replace(job, attempts=attempts), worker_id=WORKER_A)
    assert current(tenant_a, job) == job


def test_renewal_of_a_queued_job_is_refused(tenant_a):
    queued = enqueue(tenant_a)
    with pytest.raises(JobLeaseLost):
        jobs.renew_lease(tenant_a.session, queued, worker_id=WORKER_A)
    with pytest.raises(JobLeaseLost):
        jobs.renew_lease(tenant_a.session, queued)
    assert current(tenant_a, queued).status == JobStatus.QUEUED.value


@pytest.mark.parametrize("finish", ["succeeded", "failed", "cancelled"])
def test_renewal_of_a_finished_job_is_refused(tenant_a, finish):
    if finish == "cancelled":
        job = enqueue(tenant_a)
        jobs.cancel_job(tenant_a.session, job.id)
        record = dataclasses.replace(job, status="running", locked_by=WORKER_A, attempts=1)
    else:
        record = claim_live(tenant_a)
        if finish == "succeeded":
            jobs.mark_succeeded(tenant_a.session, record, worker_id=WORKER_A)
        else:
            jobs.mark_failed(tenant_a.session, record, error_code="boom", retryable=False, worker_id=WORKER_A)
    with pytest.raises(JobLeaseLost):
        jobs.renew_lease(tenant_a.session, record, worker_id=WORKER_A)
    assert current(tenant_a, record).status == finish


def test_an_expired_lease_is_lost_for_renewal_but_still_recoverable(tenant_a):
    enqueue(tenant_a, available_at=PAST)
    job = jobs.claim_next_job(tenant_a.session, worker_id=WORKER_A, now=PAST, lease_seconds=60)
    with pytest.raises(JobLeaseLost):
        jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A)
    assert current(tenant_a, job).status == JobStatus.RUNNING.value
    [recovered] = jobs.recover_stale_jobs(tenant_a.session)
    assert (recovered.status, recovered.attempts, recovered.last_error_code) == ("queued", 1, "lease_expired")


def test_renewal_after_the_lease_was_recovered_is_refused(tenant_a):
    job = claim_live(tenant_a)
    jobs.recover_stale_jobs(tenant_a.session, now=after_expiry(job))
    with pytest.raises(JobLeaseLost):
        jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A)
    assert current(tenant_a, job).status == JobStatus.QUEUED.value


def test_renewal_of_another_tenant_job_is_not_found(tenant_a, tenant_b):
    job = claim_live(tenant_b)
    with pytest.raises(NotFound):
        jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A)
    assert current(tenant_b, job) == job


def test_renewal_requires_the_right_to_run_jobs(db, tenant_a):
    viewer_id = ensure_user(db, "test|lease|viewer")
    add_member(tenant_a.session, user_id=viewer_id, role=Role.VIEWER)
    viewer = TenantSession(db, TenantContext(tenant_a.organization_id, viewer_id))
    job = claim_live(tenant_a)
    with pytest.raises(PermissionDenied):
        jobs.renew_lease(viewer, job, worker_id=WORKER_A)


@pytest.mark.parametrize("lease", [0, -1, jobs.MAX_LEASE_SECONDS + 1, True, "60", 1.5])
def test_renewal_rejects_an_invalid_duration(tenant_a, lease):
    job = claim_live(tenant_a)
    with pytest.raises(ValueError):
        jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A, lease_seconds=lease)


def test_a_failing_renewal_hook_cancels_the_renewal(tenant_a):
    job = claim_live(tenant_a, lease=30)

    def broken(conn, renewed):
        assert renewed.lease_expires_at > job.lease_expires_at
        raise RuntimeError("hook")

    with pytest.raises(RuntimeError):
        jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A, lease_seconds=600, hook=broken)
    assert current(tenant_a, job).lease_expires_at == job.lease_expires_at


def test_a_renewed_lease_is_not_recovered_at_its_old_expiry(tenant_a):
    job = claim_live(tenant_a, lease=30)
    renewed = jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A, lease_seconds=600)
    assert jobs.recover_stale_jobs(tenant_a.session, now=after_expiry(job)) == []
    assert jobs.recover_stale_jobs(tenant_a.session, now=after_expiry(renewed)) != []


def test_a_renewed_job_is_finished_normally_by_its_holder(tenant_a):
    job = claim_live(tenant_a, lease=30)
    renewed = jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A, lease_seconds=600)
    done = jobs.mark_succeeded(tenant_a.session, renewed, result={"ok": True}, worker_id=WORKER_A)
    assert (done.status, done.attempts, done.result) == ("succeeded", 1, {"ok": True})


def test_a_holder_whose_lease_expired_unnoticed_may_still_publish(tenant_a):
    """Personne n'a repris le travail: accepter le resultat evite un rejeu inutile."""
    enqueue(tenant_a, available_at=PAST)
    job = jobs.claim_next_job(tenant_a.session, worker_id=WORKER_A, now=PAST, lease_seconds=60)
    done = jobs.mark_succeeded(tenant_a.session, job, worker_id=WORKER_A)
    assert done.status == JobStatus.SUCCEEDED.value


def test_the_lease_token_format():
    assert jobs.lease_token(3, "host/42/ab:cd") == "3:host/42/ab:cd"


# =============================================================================================
# 3. Ancien worker: scenario complet de fencing
# =============================================================================================

@pytest.mark.parametrize("worker_a, worker_b", [(WORKER_A, WORKER_B), ("worker-same", "worker-same")])
def test_a_stale_worker_can_no_longer_write_anything(app_conn, tenant_a, second_session, worker_a, worker_b):
    # 1-3. A prend, commence, renouvelle
    job_a = claim_live(tenant_a, worker=worker_a, lease=30)
    job_a = jobs.renew_lease(tenant_a.session, job_a, worker_id=worker_a, lease_seconds=60)
    # 4-5. A devient lent: son bail est declare expire, B reprend le travail
    moment = after_expiry(job_a)
    [recovered] = jobs.recover_stale_jobs(second_session, now=moment)
    assert recovered.status == JobStatus.QUEUED.value
    # 6. B obtient une NOUVELLE tentative (meme horloge declaree que la reprise)
    job_b = jobs.claim_next_job(second_session, worker_id=worker_b, lease_seconds=60, now=moment)
    assert (job_b.id, job_b.attempts, job_b.locked_by) == (job_a.id, 2, worker_b)
    before = current(tenant_a, job_b)

    # 7-8. A revient: le depot refuse tout
    for attempt in (
        lambda: jobs.renew_lease(tenant_a.session, job_a, worker_id=worker_a),
        lambda: jobs.mark_succeeded(tenant_a.session, job_a, result={"stale": True}, worker_id=worker_a),
        lambda: jobs.mark_failed(tenant_a.session, job_a, error_code="boom", retryable=False, worker_id=worker_a),
        lambda: jobs.mark_failed(tenant_a.session, job_a, error_code="boom", retryable=True, worker_id=worker_a),
        lambda: jobs.requeue(tenant_a.session, job_a, error_code="boom", worker_id=worker_a),
        lambda: jobs.mark_succeeded(tenant_a.session, job_a),
    ):
        with pytest.raises(JobLeaseLost):
            attempt()

    # ... et le SQL brut aussi, avec le jeton de A
    stale_token = jobs.lease_token(job_a.attempts, worker_a)
    finishing = {
        "renouveler": ("UPDATE jobs SET lease_expires_at = lease_expires_at + interval '1 hour' WHERE id = %s",
                       "only the lease holder renews a lease"),
        "terminer": ("UPDATE jobs SET status = 'succeeded', finished_at = now(), locked_at = NULL, "
                     "locked_by = NULL, lease_expires_at = NULL, result = '{\"stale\": true}' WHERE id = %s",
                     "only the lease holder finishes a running job"),
        "echouer": ("UPDATE jobs SET status = 'failed', finished_at = now(), locked_at = NULL, locked_by = NULL, "
                    "lease_expires_at = NULL, last_error_code = 'boom' WHERE id = %s",
                    "only the lease holder finishes a running job"),
        "remettre en file": ("UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL, "
                             "lease_expires_at = NULL WHERE id = %s",
                             "only the lease holder finishes a running job"),
    }
    for statement, message in finishing.values():
        refused(app_conn, tenant_a, statement, (job_a.id,), token=stale_token, message=message)
    # un ancien filtre d'UPDATE ne voit plus la ligne du tout
    assert raw_update(app_conn, tenant_a,
                      "UPDATE jobs SET lease_expires_at = lease_expires_at + interval '1 hour' "
                      "WHERE id = %s AND locked_by = %s AND attempts = %s",
                      (job_a.id, worker_a, job_a.attempts), token=stale_token) == 0

    # rien n'a bouge pour B, qui garde la main
    assert current(tenant_a, job_b) == before
    renewed_b = jobs.renew_lease(second_session, job_b, worker_id=worker_b, lease_seconds=120)
    done = jobs.mark_succeeded(second_session, renewed_b, result={"by": "b"}, worker_id=worker_b,
                               now=moment + timedelta(seconds=1))
    assert (done.status, done.attempts, done.result) == ("succeeded", 2, {"by": "b"})


# =============================================================================================
# 4. Courses reelles
# =============================================================================================

class Actor:
    """Execute une operation dans un fil; garde son resultat ou son exception."""

    def __init__(self, operation):
        self.result = self.error = None
        self.thread = threading.Thread(target=self._run, args=(operation,), daemon=True)
        self.thread.start()

    def _run(self, operation):
        try:
            self.result = operation()
        except BaseException as exc:  # noqa: BLE001 - l'exception est la donnee observee
            self.error = exc

    def join(self):
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "operation bloquee"
        return self


class Pause:
    """Crochet de transition qui garde la transaction (et ses verrous) ouverte."""

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()

    def __call__(self, conn, job):
        self.entered.set()
        assert self.release.wait(30)

    def wait_entered(self):
        assert self.entered.wait(30), "la transaction n'a pas atteint le crochet"


def lock_waiters(pg) -> int:
    with pg.admin() as conn:
        return conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND wait_event_type = 'Lock'",
                            (pg.database,)).fetchone()[0]


def wait_for_lock_waiter(pg) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if lock_waiters(pg) >= 1:
            return
        time.sleep(0.02)
    raise AssertionError("aucune session n'attend de verrou: la course n'est pas reelle")


def holder_operation(kind, session, job):
    if kind == "renew":
        return lambda hook=None: jobs.renew_lease(session, job, worker_id=WORKER_A, lease_seconds=600, hook=hook)
    if kind == "succeed":
        return lambda hook=None: jobs.mark_succeeded(session, job, result={"ok": 1}, worker_id=WORKER_A, hook=hook)
    if kind == "fail":
        return lambda hook=None: jobs.mark_failed(session, job, error_code="boom", retryable=False,
                                                  worker_id=WORKER_A, hook=hook)
    if kind == "requeue":
        return lambda hook=None: jobs.mark_failed(session, job, error_code="deadlock", retryable=True,
                                                  worker_id=WORKER_A, hook=hook)
    raise AssertionError(kind)


HOLDER_OUTCOME = {"renew": "running", "succeed": "succeeded", "fail": "failed", "requeue": "queued"}
OPERATIONS = sorted(HOLDER_OUTCOME)


@pytest.mark.parametrize("kind", OPERATIONS)
def test_recovery_first_the_holder_waits_then_loses(pg, tenant_a, second_session, kind):
    """La reprise tient le verrou; l'ecriture du detenteur ATTEND, puis voit une tentative perimee."""
    job = claim_live(tenant_a, lease=60)
    pause = Pause()
    recovery = Actor(lambda: jobs.recover_stale_jobs(second_session, now=after_expiry(job), hook=pause))
    pause.wait_entered()
    holder = Actor(holder_operation(kind, tenant_a.session, job))
    wait_for_lock_waiter(pg)
    pause.release.set()
    recovery.join()
    holder.join()
    assert recovery.error is None and [j.id for j in recovery.result] == [job.id]
    assert isinstance(holder.error, JobLeaseLost), holder.error
    final = current(tenant_a, job)
    assert (final.status, final.attempts, final.last_error_code, final.result) == ("queued", 1, "lease_expired", None)


@pytest.mark.parametrize("kind", OPERATIONS)
def test_holder_first_the_recovery_skips_the_locked_job(pg, tenant_a, second_session, kind):
    """Le detenteur tient le verrou; la reprise ne l'attend pas (SKIP LOCKED) et ne prend rien."""
    job = claim_live(tenant_a, lease=60)
    pause = Pause()
    holder = Actor(lambda: holder_operation(kind, tenant_a.session, job)(hook=pause))
    pause.wait_entered()
    recovery = Actor(lambda: jobs.recover_stale_jobs(second_session, now=after_expiry(job))).join()
    assert recovery.error is None and recovery.result == []
    pause.release.set()
    holder.join()
    assert holder.error is None, holder.error
    final = current(tenant_a, job)
    assert final.status == HOLDER_OUTCOME[kind] and final.attempts == 1
    # une fois le detenteur commis, la reprise ne trouve toujours rien a reprendre
    assert jobs.recover_stale_jobs(second_session, now=after_expiry(job)) == []


def test_two_recoveries_and_a_holder_never_both_win(pg, tenant_a, second_session):
    """La reprise tient le verrou; une seconde reprise l'ignore, le detenteur attend puis perd."""
    job = claim_live(tenant_a, lease=60)
    third = Database(pg.url("app"))
    try:
        other = TenantSession(third, TenantContext(tenant_a.organization_id, tenant_a.owner_id))
        pause = Pause()
        first = Actor(lambda: jobs.recover_stale_jobs(second_session, now=after_expiry(job), hook=pause))
        pause.wait_entered()
        second = Actor(lambda: jobs.recover_stale_jobs(other, now=after_expiry(job))).join()
        holder = Actor(holder_operation("succeed", tenant_a.session, job))
        wait_for_lock_waiter(pg)
        pause.release.set()
        first.join()
        holder.join()
    finally:
        third.close()
    assert len(first.result) == 1 and second.result == []
    assert isinstance(holder.error, JobLeaseLost)


def test_concurrent_renewals_by_the_holder_are_serialized(pg, tenant_a, second_session):
    job = claim_live(tenant_a, lease=30)
    pause = Pause()
    first = Actor(lambda: jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A, lease_seconds=300,
                                           hook=pause))
    pause.wait_entered()
    second = Actor(lambda: jobs.renew_lease(second_session, job, worker_id=WORKER_A, lease_seconds=600))
    wait_for_lock_waiter(pg)
    pause.release.set()
    first.join()
    second.join()
    assert first.error is None and second.error is None
    assert second.result.lease_expires_at >= first.result.lease_expires_at
    assert current(tenant_a, job).lease_expires_at == second.result.lease_expires_at


@pytest.mark.parametrize("kind", OPERATIONS)
def test_unordered_races_never_have_two_winners(tenant_a, second_session, kind):
    """Sans ordre impose: a chaque essai, exactement l'un des deux l'emporte."""
    winners = {"holder": 0, "recovery": 0}
    for _ in range(15):
        job = claim_live(tenant_a, lease=60)
        start = threading.Barrier(2)

        def as_holder(operation=holder_operation(kind, tenant_a.session, job)):
            start.wait()
            return operation()

        def as_recovery(moment=after_expiry(job)):
            start.wait()
            return jobs.recover_stale_jobs(second_session, now=moment)

        holder, recovery = Actor(as_holder), Actor(as_recovery)
        holder.join()
        recovery.join()
        assert recovery.error is None, recovery.error
        holder_won = holder.error is None
        recovery_won = bool(recovery.result)
        assert holder_won != recovery_won, (holder.error, recovery.result)
        if not holder_won:
            assert isinstance(holder.error, JobLeaseLost), holder.error
        final = current(tenant_a, job)
        assert final.attempts == 1
        assert final.status == (HOLDER_OUTCOME[kind] if holder_won else "queued")
        winners["holder" if holder_won else "recovery"] += 1
        if final.status == "running":
            jobs.mark_succeeded(tenant_a.session, final, worker_id=WORKER_A)
        elif final.status == "queued":
            jobs.cancel_job(tenant_a.session, final.id)
    assert sum(winners.values()) == 15


def test_a_job_longer_than_its_lease_is_not_stolen_while_renewed(tenant_a, second_session):
    """Bail de 2 s, travail de 5 s, reprise tentee toutes les 100 ms a l'horloge reelle."""
    job = claim_live(tenant_a, lease=2)
    stop = threading.Event()
    stolen = []

    def supervisor():
        while not stop.is_set():
            stolen.extend(jobs.recover_stale_jobs(second_session))
            time.sleep(0.1)

    watcher = threading.Thread(target=supervisor, daemon=True)
    watcher.start()
    renewals = 0
    try:
        started = time.monotonic()
        while time.monotonic() - started < 5:
            time.sleep(0.5)
            job = jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A, lease_seconds=2)
            renewals += 1
    finally:
        stop.set()
        watcher.join(timeout=10)
    assert stolen == []
    assert renewals >= 8
    done = jobs.mark_succeeded(tenant_a.session, job, worker_id=WORKER_A)
    assert (done.status, done.attempts) == ("succeeded", 1)


def test_without_renewal_the_same_job_is_recovered(tenant_a, second_session):
    """Temoin: sans renouvellement, le meme scenario perd le travail."""
    job = claim_live(tenant_a, lease=1)
    deadline = time.monotonic() + 10
    recovered = []
    while not recovered and time.monotonic() < deadline:
        time.sleep(0.2)
        recovered = jobs.recover_stale_jobs(second_session)
    assert [j.id for j in recovered] == [job.id]
    with pytest.raises(JobLeaseLost):
        jobs.renew_lease(tenant_a.session, job, worker_id=WORKER_A)


# =============================================================================================
# 5. Migration 0006: montee, descente, remontee
# =============================================================================================

def guard_definition(url) -> str:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT pg_get_functiondef('jobs_guard_transition'::regproc)").fetchone()[0]


def test_0006_downgrades_to_the_exact_0005_guard_and_upgrades_again(pg):
    reference = pg.create_empty_database("lease_ref")
    subject = pg.create_empty_database("lease_down")
    try:
        reference_url, url = pg.url("migrator", reference), pg.url("migrator", subject)
        migrate.upgrade(reference_url, "0005_audit")
        migrate.upgrade(url, "0006_job_leases")
        head_guard = guard_definition(url)
        assert "app.job_lease_token" in head_guard

        migrate.downgrade(url, "0005_audit")
        assert migrate.current_revision(url) == "0005_audit"
        assert guard_definition(url) == guard_definition(reference_url)

        migrate.upgrade(url, "0006_job_leases")
        assert migrate.current_revision(url) == "0006_job_leases"
        assert guard_definition(url) == head_guard == guard_definition(pg.url("migrator"))
    finally:
        pg.drop_database(reference)
        pg.drop_database(subject)
