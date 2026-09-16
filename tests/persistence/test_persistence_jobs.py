"""File de travaux PostgreSQL (Mission 004.2): cycle de vie, reprise, bail, purge, isolation.

Aucun test n'attend en temps reel: toutes les dates sont fournies explicitement
(`now=`, `available_at=`), donc chaque scenario temporel est deterministe.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from mervio.persistence import jobs
from mervio.persistence.errors import (
    JobLeaseLost, JobStateError, NotFound, PayloadRejected, PermissionDenied,
)
from mervio.persistence.jobs import JobStatus, JobType
from mervio.persistence.tenancy import Role, TenantContext, TenantSession, add_member, ensure_user

#: Instant de reference, volontairement dans le passe: la politique RESTRICTIVE de
#: la base refuse de supprimer un travail termine depuis moins d'une heure, donc un
#: scenario de purge ancre sur "maintenant" serait fragile.
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
WORKER = "worker-a"
OTHER_WORKER = "worker-b"


def enqueue(tenant, *, kind=JobType.ANALYSIS, payload=None, **kwargs):
    """Met en file a `NOW` par defaut: tout le scenario temporel reste deterministe."""
    kwargs.setdefault("available_at", NOW)
    return jobs.enqueue_job(tenant.session, job_type=kind, payload=payload if payload is not None else {"k": "v"},
                            **kwargs)


def claim(tenant, *, worker_id=WORKER, **kwargs):
    return jobs.claim_next_job(tenant.session, worker_id=worker_id, **kwargs)


# -- mise en file ---------------------------------------------------------------------------

def test_a_new_job_is_queued_with_no_attempt_and_no_result(tenant_a):
    job = enqueue(tenant_a)
    assert (job.status, job.attempts, job.result) == (JobStatus.QUEUED.value, 0, None)
    assert (job.locked_at, job.locked_by, job.lease_expires_at, job.finished_at) == (None, None, None, None)
    assert job.organization_id == tenant_a.organization_id
    assert job.enqueued_by == tenant_a.owner_id
    assert job.max_attempts == jobs.DEFAULT_MAX_ATTEMPTS


def test_a_job_carries_its_store_and_its_payload(tenant_a):
    job = enqueue(tenant_a, store_id=tenant_a.store_id, payload={"snapshot_id": "x"})
    assert job.store_id == tenant_a.store_id
    assert job.payload == {"snapshot_id": "x"}


def test_a_job_on_an_unknown_store_is_refused(tenant_a):
    with pytest.raises(NotFound):
        enqueue(tenant_a, store_id=uuid.uuid4())


def test_a_job_on_another_tenant_store_is_not_found(tenant_a, tenant_b):
    with pytest.raises(NotFound):
        enqueue(tenant_a, store_id=tenant_b.store_id)


def test_a_payload_carrying_a_secret_never_reaches_the_database(tenant_a):
    with pytest.raises(PayloadRejected):
        enqueue(tenant_a, payload={"api_key": "sk-live-1234"})
    assert jobs.list_jobs(tenant_a.session) == []


def test_a_correlation_id_is_assigned_when_none_is_given(tenant_a):
    assert isinstance(enqueue(tenant_a).correlation_id, uuid.UUID)


def test_a_given_correlation_id_is_preserved(tenant_a):
    correlation = uuid.uuid4()
    assert enqueue(tenant_a, correlation_id=correlation).correlation_id == correlation


@pytest.mark.parametrize("field, value", [("priority", 500), ("max_attempts", 0), ("max_attempts", 99)])
def test_out_of_range_options_are_refused(tenant_a, field, value):
    with pytest.raises(ValueError):
        enqueue(tenant_a, **{field: value})


# -- idempotence de la mise en file ----------------------------------------------------------

def test_the_same_idempotency_key_enqueues_one_job(tenant_a):
    first = enqueue(tenant_a, idempotency_key="import:abc")
    second = enqueue(tenant_a, idempotency_key="import:abc")
    assert first.id == second.id
    assert len(jobs.list_jobs(tenant_a.session)) == 1


def test_the_same_key_on_another_job_type_is_another_job(tenant_a):
    first = enqueue(tenant_a, kind=JobType.ANALYSIS, idempotency_key="k")
    second = enqueue(tenant_a, kind=JobType.IMPORT, idempotency_key="k")
    assert first.id != second.id


def test_two_tenants_share_an_idempotency_key_without_seeing_each_other(tenant_a, tenant_b):
    """Cle prefixee par l'organisation: aucune violation d'unicite ne sert d'oracle d'existence."""
    first = enqueue(tenant_a, idempotency_key="import:abc")
    second = enqueue(tenant_b, idempotency_key="import:abc")
    assert first.id != second.id
    assert first.organization_id != second.organization_id


def test_jobs_without_a_key_are_never_merged(tenant_a):
    assert enqueue(tenant_a).id != enqueue(tenant_a).id


# -- prise ----------------------------------------------------------------------------------

def test_claiming_moves_the_job_to_running_and_records_the_lease(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a, now=NOW, lease_seconds=300)
    assert (job.status, job.attempts, job.locked_by) == (JobStatus.RUNNING.value, 1, WORKER)
    assert job.locked_at == NOW
    assert job.started_at == NOW
    assert job.lease_expires_at == NOW + timedelta(seconds=300)


def test_an_empty_queue_yields_nothing(tenant_a):
    assert claim(tenant_a) is None


def test_a_job_is_never_claimed_twice(tenant_a):
    enqueue(tenant_a)
    assert claim(tenant_a, worker_id=WORKER) is not None
    assert claim(tenant_a, worker_id=OTHER_WORKER) is None


def test_the_highest_priority_goes_first(tenant_a):
    low = enqueue(tenant_a, priority=0)
    high = enqueue(tenant_a, priority=10)
    assert claim(tenant_a).id == high.id
    assert claim(tenant_a).id == low.id


def test_at_equal_priority_the_oldest_goes_first(tenant_a):
    first = enqueue(tenant_a)
    second = enqueue(tenant_a)
    assert [claim(tenant_a).id, claim(tenant_a).id] == [first.id, second.id]


def test_a_job_scheduled_for_later_is_not_claimed_yet(tenant_a):
    enqueue(tenant_a, available_at=NOW + timedelta(minutes=5))
    assert claim(tenant_a, now=NOW) is None
    assert claim(tenant_a, now=NOW + timedelta(minutes=5)) is not None


def test_a_worker_claims_only_the_types_it_serves(tenant_a):
    analysis = enqueue(tenant_a, kind=JobType.ANALYSIS)
    enqueue(tenant_a, kind=JobType.IMPORT)
    assert claim(tenant_a, job_types=[JobType.ANALYSIS]).id == analysis.id
    assert claim(tenant_a, job_types=[JobType.ANALYSIS]) is None


def test_a_specific_job_can_be_targeted(tenant_a):
    enqueue(tenant_a, priority=10)
    wanted = enqueue(tenant_a, priority=0)
    assert claim(tenant_a, job_id=wanted.id).id == wanted.id


def test_a_worker_identity_is_required(tenant_a):
    with pytest.raises(ValueError):
        claim(tenant_a, worker_id="")


def test_an_absurd_lease_is_refused(tenant_a):
    with pytest.raises(ValueError):
        claim(tenant_a, lease_seconds=0)


# -- succes ---------------------------------------------------------------------------------

def test_a_successful_job_keeps_its_result(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a, now=NOW)
    done = jobs.mark_succeeded(tenant_a.session, job, result={"snapshot_id": "s1"}, worker_id=WORKER,
                               now=NOW + timedelta(seconds=5))
    assert done.status == JobStatus.SUCCEEDED.value
    assert done.result == {"snapshot_id": "s1"}
    assert done.finished_at == NOW + timedelta(seconds=5)
    assert (done.locked_at, done.locked_by, done.lease_expires_at) == (None, None, None)


def test_a_finished_job_is_immutable(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a)
    done = jobs.mark_succeeded(tenant_a.session, job, worker_id=WORKER)
    with pytest.raises((JobStateError, JobLeaseLost)):
        jobs.mark_succeeded(tenant_a.session, done, worker_id=WORKER)


def test_a_result_carrying_a_secret_is_refused(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a)
    with pytest.raises(PayloadRejected):
        jobs.mark_succeeded(tenant_a.session, job, result={"access_token": "t"}, worker_id=WORKER)


# -- echec et reprise -------------------------------------------------------------------------

def test_a_retryable_failure_returns_the_job_to_the_queue_with_a_backoff(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a, now=NOW)
    again = jobs.mark_failed(tenant_a.session, job, error_code="deadlock", error="verrou", retryable=True,
                             worker_id=WORKER, now=NOW)
    assert again.status == JobStatus.QUEUED.value
    assert again.attempts == 1
    assert again.available_at == NOW + timedelta(seconds=jobs.backoff_seconds(1))
    assert (again.last_error_code, again.finished_at) == ("deadlock", None)


def test_a_permanent_failure_never_returns_to_the_queue(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a, now=NOW)
    failed = jobs.mark_failed(tenant_a.session, job, error_code="payload_invalid", retryable=False,
                              worker_id=WORKER, now=NOW)
    assert failed.status == JobStatus.FAILED.value
    assert failed.attempts == 1 and failed.attempts_left == 2
    assert failed.finished_at == NOW


def test_attempts_are_exhausted_then_the_job_fails_terminally(tenant_a):
    enqueue(tenant_a, max_attempts=3)
    moment = NOW
    for attempt in (1, 2):
        job = claim(tenant_a, now=moment)
        assert job.attempts == attempt
        job = jobs.mark_failed(tenant_a.session, job, error_code="boom", retryable=True, worker_id=WORKER,
                               now=moment)
        assert job.status == JobStatus.QUEUED.value
        moment = job.available_at
    job = claim(tenant_a, now=moment)
    assert job.attempts == 3
    final = jobs.mark_failed(tenant_a.session, job, error_code="boom", retryable=True, worker_id=WORKER, now=moment)
    assert final.status == JobStatus.FAILED.value
    assert final.attempts == final.max_attempts
    assert claim(tenant_a, now=moment + timedelta(days=1)) is None


def test_the_backoff_grows_between_attempts(tenant_a):
    enqueue(tenant_a, max_attempts=3)
    delays = []
    moment = NOW
    for _ in range(2):
        job = claim(tenant_a, now=moment)
        job = jobs.mark_failed(tenant_a.session, job, error_code="boom", retryable=True, worker_id=WORKER, now=moment)
        delays.append((job.available_at - moment).total_seconds())
        moment = job.available_at
    assert delays == [30, 60]


def test_an_error_message_is_stored_without_line_breaks_or_absolute_paths(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a)
    failed = jobs.mark_failed(tenant_a.session, job, error_code="boom", error="ligne 1\nligne 2",
                              retryable=False, worker_id=WORKER)
    assert failed.last_error == "ligne 1 ligne 2"
    enqueue(tenant_a)
    other = claim(tenant_a)
    hidden = jobs.mark_failed(tenant_a.session, other, error_code="boom", error="/Users/x/export.csv",
                              retryable=False, worker_id=WORKER)
    assert hidden.last_error == "[redacted]"


def test_a_requeued_job_is_claimed_again_by_another_worker(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a, worker_id=WORKER, now=NOW)
    jobs.mark_failed(tenant_a.session, job, error_code="boom", retryable=True, worker_id=WORKER, now=NOW)
    again = claim(tenant_a, worker_id=OTHER_WORKER, now=NOW + timedelta(minutes=10))
    assert again is not None and again.locked_by == OTHER_WORKER and again.attempts == 2


# -- bail -----------------------------------------------------------------------------------

def test_a_worker_that_lost_its_lease_cannot_publish_a_result(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a, worker_id=WORKER, now=NOW, lease_seconds=60)
    jobs.recover_stale_jobs(tenant_a.session, now=NOW + timedelta(seconds=61))
    stolen = claim(tenant_a, worker_id=OTHER_WORKER, now=NOW + timedelta(seconds=62))
    assert stolen.locked_by == OTHER_WORKER
    with pytest.raises(JobLeaseLost):
        jobs.mark_succeeded(tenant_a.session, job, worker_id=WORKER)


def test_a_worker_that_lost_its_lease_cannot_fail_the_job_either(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a, worker_id=WORKER, now=NOW, lease_seconds=60)
    jobs.recover_stale_jobs(tenant_a.session, now=NOW + timedelta(seconds=61))
    claim(tenant_a, worker_id=OTHER_WORKER, now=NOW + timedelta(seconds=62))
    with pytest.raises(JobLeaseLost):
        jobs.mark_failed(tenant_a.session, job, error_code="boom", retryable=False, worker_id=WORKER)


def test_a_still_valid_lease_is_not_recovered(tenant_a):
    enqueue(tenant_a)
    claim(tenant_a, now=NOW, lease_seconds=300)
    assert jobs.recover_stale_jobs(tenant_a.session, now=NOW + timedelta(seconds=299)) == []


def test_an_expired_lease_returns_the_job_to_the_queue_without_duplicating_it(tenant_a):
    original = enqueue(tenant_a)
    claim(tenant_a, now=NOW, lease_seconds=60)
    recovered = jobs.recover_stale_jobs(tenant_a.session, now=NOW + timedelta(seconds=61))
    assert [job.id for job in recovered] == [original.id]
    assert recovered[0].status == JobStatus.QUEUED.value
    assert recovered[0].attempts == 1  # l'historique des tentatives est conserve
    assert recovered[0].last_error_code == "lease_expired"
    assert len(jobs.list_jobs(tenant_a.session)) == 1


def test_a_recovered_job_is_claimable_again(tenant_a):
    enqueue(tenant_a)
    claim(tenant_a, worker_id=WORKER, now=NOW, lease_seconds=60)
    jobs.recover_stale_jobs(tenant_a.session, now=NOW + timedelta(seconds=61))
    again = claim(tenant_a, worker_id=OTHER_WORKER, now=NOW + timedelta(seconds=62))
    assert again.locked_by == OTHER_WORKER and again.attempts == 2


def test_a_stale_job_with_no_attempt_left_fails_terminally(tenant_a):
    enqueue(tenant_a, max_attempts=1)
    claim(tenant_a, now=NOW, lease_seconds=60)
    recovered = jobs.recover_stale_jobs(tenant_a.session, now=NOW + timedelta(seconds=61))
    assert recovered[0].status == JobStatus.FAILED.value
    assert recovered[0].last_error_code == "lease_expired"
    assert claim(tenant_a, now=NOW + timedelta(days=1)) is None


def test_recovery_never_touches_a_queued_job(tenant_a):
    enqueue(tenant_a)
    assert jobs.recover_stale_jobs(tenant_a.session, now=NOW + timedelta(days=1)) == []


# -- annulation -----------------------------------------------------------------------------

def test_a_queued_job_can_be_cancelled(tenant_a):
    job = enqueue(tenant_a)
    cancelled = jobs.cancel_job(tenant_a.session, job.id)
    assert cancelled.status == JobStatus.CANCELLED.value
    assert cancelled.finished_at is not None
    assert claim(tenant_a) is None


def test_a_running_job_cannot_be_cancelled(tenant_a):
    job = enqueue(tenant_a)
    claim(tenant_a)
    with pytest.raises(JobStateError):
        jobs.cancel_job(tenant_a.session, job.id)


def test_cancelling_an_unknown_job_is_not_found(tenant_a):
    with pytest.raises(NotFound):
        jobs.cancel_job(tenant_a.session, uuid.uuid4())


# -- lecture --------------------------------------------------------------------------------

def test_a_job_is_read_back_by_its_identifier(tenant_a):
    job = enqueue(tenant_a, store_id=tenant_a.store_id, payload={"a": 1})
    assert jobs.get_job(tenant_a.session, job.id) == job


def test_a_malformed_identifier_is_treated_as_missing(tenant_a):
    with pytest.raises(NotFound):
        jobs.get_job(tenant_a.session, "pas-un-uuid")


def test_jobs_are_listed_from_the_most_recent(tenant_a):
    first = enqueue(tenant_a)
    second = enqueue(tenant_a)
    assert [job.id for job in jobs.list_jobs(tenant_a.session)] == [second.id, first.id]


def test_jobs_are_filtered_by_status_type_and_store(tenant_a):
    enqueue(tenant_a, kind=JobType.IMPORT, store_id=tenant_a.store_id)
    enqueue(tenant_a, kind=JobType.ANALYSIS)
    claim(tenant_a, job_types=[JobType.IMPORT])
    assert len(jobs.list_jobs(tenant_a.session, status=[JobStatus.RUNNING])) == 1
    assert len(jobs.list_jobs(tenant_a.session, job_type=[JobType.ANALYSIS])) == 1
    assert len(jobs.list_jobs(tenant_a.session, store_id=tenant_a.store_id)) == 1


def test_queue_statistics_count_by_status_and_type(tenant_a):
    enqueue(tenant_a, kind=JobType.IMPORT)
    enqueue(tenant_a, kind=JobType.ANALYSIS)
    claim(tenant_a, job_types=[JobType.IMPORT])
    statistics = jobs.queue_statistics(tenant_a.session)
    assert statistics["jobs_total"] == 2
    assert statistics["by_status"] == {"queued": 1, "running": 1}
    assert statistics["by_type"] == {"analysis": 1, "import": 1}
    assert statistics["queue_oldest_age_ms"] >= 0


# -- isolation: depots ------------------------------------------------------------------------

def test_a_tenant_never_reads_another_tenant_job(tenant_a, tenant_b):
    job = enqueue(tenant_b)
    with pytest.raises(NotFound):
        jobs.get_job(tenant_a.session, job.id)
    assert jobs.list_jobs(tenant_a.session) == []


def test_a_tenant_never_claims_another_tenant_job(tenant_a, tenant_b):
    enqueue(tenant_b)
    assert claim(tenant_a) is None


def test_targeting_another_tenant_job_by_identifier_claims_nothing(tenant_a, tenant_b):
    job = enqueue(tenant_b)
    assert claim(tenant_a, job_id=job.id) is None
    assert jobs.get_job(tenant_b.session, job.id).status == JobStatus.QUEUED.value


def test_a_tenant_never_completes_another_tenant_job(tenant_a, tenant_b):
    enqueue(tenant_b)
    running = claim(tenant_b)
    with pytest.raises(NotFound):
        jobs.mark_succeeded(tenant_a.session, running)


def test_a_tenant_never_recovers_another_tenant_stale_job(tenant_a, tenant_b):
    enqueue(tenant_b)
    claim(tenant_b, now=NOW, lease_seconds=60)
    assert jobs.recover_stale_jobs(tenant_a.session, now=NOW + timedelta(hours=1)) == []
    assert jobs.get_job(tenant_b.session, jobs.list_jobs(tenant_b.session)[0].id).status == JobStatus.RUNNING.value


def test_a_tenant_never_cancels_another_tenant_job(tenant_a, tenant_b):
    job = enqueue(tenant_b)
    with pytest.raises(NotFound):
        jobs.cancel_job(tenant_a.session, job.id)


def test_statistics_count_only_the_current_tenant(tenant_a, tenant_b):
    enqueue(tenant_b)
    enqueue(tenant_b)
    enqueue(tenant_a)
    assert jobs.queue_statistics(tenant_a.session)["jobs_total"] == 1


# -- isolation: SQL brut ----------------------------------------------------------------------

def set_tenant(conn, organization_id, user_id):
    conn.execute("SELECT set_config('app.organization_id', %s, false), set_config('app.user_id', %s, false)",
                 (str(organization_id), str(user_id)))


def test_raw_sql_sees_no_foreign_job(app_conn, tenant_a, tenant_b):
    """RLS seule, sans la barriere applicative des depots."""
    enqueue(tenant_b, store_id=tenant_b.store_id)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    assert app_conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_raw_sql_cannot_claim_a_foreign_job(app_conn, tenant_a, tenant_b):
    job = enqueue(tenant_b)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    updated = app_conn.execute(
        "UPDATE jobs SET status = 'running', locked_at = now(), locked_by = 'pirate', "
        "lease_expires_at = now() + interval '5 minutes', started_at = now() WHERE id = %s", (job.id,)).rowcount
    assert updated == 0
    assert jobs.get_job(tenant_b.session, job.id).status == JobStatus.QUEUED.value


def test_raw_sql_cannot_insert_a_job_for_another_organization(app_conn, tenant_a, tenant_b):
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(
            "INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, payload, available_at) "
            "VALUES (%s, %s, 'analysis', 'queued', %s, '{}'::jsonb, now())",
            (uuid.uuid4(), tenant_b.organization_id, uuid.uuid4()))


def test_without_a_tenant_context_nothing_is_visible(app_conn, tenant_a):
    enqueue(tenant_a)
    app_conn.execute("SELECT set_config('app.organization_id', '', false)")
    assert app_conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


# -- machine a etats imposee par la base --------------------------------------------------------

def test_the_database_refuses_an_illegal_transition(app_conn, tenant_a):
    job = enqueue(tenant_a)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    with pytest.raises(psycopg.errors.RestrictViolation):
        app_conn.execute("UPDATE jobs SET status = 'succeeded', finished_at = now() WHERE id = %s", (job.id,))


def test_the_database_refuses_to_revive_a_finished_job(app_conn, tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a)
    jobs.mark_succeeded(tenant_a.session, job, worker_id=WORKER)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    with pytest.raises(psycopg.errors.RestrictViolation):
        app_conn.execute("UPDATE jobs SET status = 'queued', finished_at = NULL WHERE id = %s", (job.id,))


def test_the_database_refuses_to_lower_the_attempt_counter(app_conn, tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    with pytest.raises(psycopg.errors.RestrictViolation):
        app_conn.execute("UPDATE jobs SET attempts = 0 WHERE id = %s", (job.id,))


def test_the_application_role_cannot_rewrite_a_job_payload(owner):
    assert not owner.execute(
        "SELECT has_column_privilege('mervio_app', 'jobs', 'payload', 'UPDATE')").fetchone()[0]
    for column in ("organization_id", "store_id", "job_type", "idempotency_key", "correlation_id",
                   "enqueued_by", "max_attempts", "created_at"):
        assert not owner.execute(
            "SELECT has_column_privilege('mervio_app', 'jobs', %s, 'UPDATE')", (column,)).fetchone()[0], column


def test_a_job_is_always_created_queued(app_conn, tenant_a):
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    with pytest.raises(psycopg.errors.RestrictViolation):
        app_conn.execute(
            "INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, payload, available_at, "
            "locked_at, locked_by, lease_expires_at) "
            "VALUES (%s, %s, 'analysis', 'running', %s, '{}'::jsonb, now(), now(), 'x', now())",
            (uuid.uuid4(), tenant_a.organization_id, uuid.uuid4()))


# -- purge ----------------------------------------------------------------------------------

def finish(tenant, *, status=JobStatus.SUCCEEDED, **kwargs):
    enqueue(tenant, **kwargs)
    job = claim(tenant, now=NOW)
    if status is JobStatus.SUCCEEDED:
        return jobs.mark_succeeded(tenant.session, job, worker_id=WORKER, now=NOW)
    return jobs.mark_failed(tenant.session, job, error_code="boom", retryable=False, worker_id=WORKER, now=NOW)


def test_purge_removes_only_jobs_finished_before_the_cutoff(tenant_a):
    old = finish(tenant_a)
    assert jobs.purge_terminal_jobs(tenant_a.session, before=NOW - timedelta(days=1),
                                    statuses=[JobStatus.SUCCEEDED]) == 0
    assert jobs.purge_terminal_jobs(tenant_a.session, before=NOW + timedelta(days=1),
                                    statuses=[JobStatus.SUCCEEDED]) == 1
    with pytest.raises(NotFound):
        jobs.get_job(tenant_a.session, old.id)


def test_purge_never_removes_a_queued_job(tenant_a):
    job = enqueue(tenant_a)
    assert jobs.purge_terminal_jobs(tenant_a.session, before=NOW + timedelta(days=365),
                                    statuses=list(jobs.TERMINAL_STATUSES)) == 0
    assert jobs.get_job(tenant_a.session, job.id).status == JobStatus.QUEUED.value


def test_purge_never_removes_a_running_job(tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a, now=NOW)
    assert jobs.purge_terminal_jobs(tenant_a.session, before=NOW + timedelta(days=365),
                                    statuses=list(jobs.TERMINAL_STATUSES)) == 0
    assert jobs.get_job(tenant_a.session, job.id).status == JobStatus.RUNNING.value


def test_purge_refuses_a_non_terminal_status(tenant_a):
    for status in (JobStatus.QUEUED, JobStatus.RUNNING):
        with pytest.raises(ValueError):
            jobs.purge_terminal_jobs(tenant_a.session, before=NOW, statuses=[status])


def test_purge_distinguishes_successes_from_failures(tenant_a):
    finish(tenant_a, status=JobStatus.SUCCEEDED)
    failed = finish(tenant_a, status=JobStatus.FAILED)
    assert jobs.purge_terminal_jobs(tenant_a.session, before=NOW + timedelta(days=1),
                                    statuses=[JobStatus.SUCCEEDED]) == 1
    assert jobs.get_job(tenant_a.session, failed.id).status == JobStatus.FAILED.value


def test_purge_is_repeatable_without_damage(tenant_a):
    finish(tenant_a)
    kept = enqueue(tenant_a)
    for _ in range(3):
        jobs.purge_terminal_jobs(tenant_a.session, before=NOW + timedelta(days=1),
                                 statuses=list(jobs.TERMINAL_STATUSES))
    assert [job.id for job in jobs.list_jobs(tenant_a.session)] == [kept.id]


def test_purge_touches_only_the_current_tenant(tenant_a, tenant_b):
    theirs = finish(tenant_b)
    finish(tenant_a)
    assert jobs.purge_terminal_jobs(tenant_a.session, before=NOW + timedelta(days=1),
                                    statuses=list(jobs.TERMINAL_STATUSES)) == 1
    assert jobs.get_job(tenant_b.session, theirs.id).id == theirs.id


def test_the_database_floor_forbids_deleting_a_job_finished_a_moment_ago(app_conn, tenant_a):
    """Politique RESTRICTIVE: meme en SQL brut, meme pour son propre tenant."""
    enqueue(tenant_a)
    job = claim(tenant_a)
    jobs.mark_succeeded(tenant_a.session, job, worker_id=WORKER)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    assert app_conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,)).rowcount == 0
    assert jobs.get_job(tenant_a.session, job.id).status == JobStatus.SUCCEEDED.value


def test_raw_sql_cannot_delete_a_running_job(app_conn, tenant_a):
    enqueue(tenant_a)
    job = claim(tenant_a)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    assert app_conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,)).rowcount == 0


def test_purge_preserves_the_provenance_tables(owner):
    """Aucune purge ne peut couper rapport -> execution -> instantane -> source."""
    for table in ("reports", "analysis_runs", "data_snapshots", "snapshot_sources", "orders", "order_lines"):
        assert not owner.execute("SELECT has_table_privilege('mervio_app', %s, 'DELETE')",
                                 (table,)).fetchone()[0], table


# -- roles ----------------------------------------------------------------------------------

def member_session(database, tenant, role: Role, label: str) -> TenantSession:
    user_id = ensure_user(database, f"test|{label}|{role.value}")
    add_member(tenant.session, user_id=user_id, role=role)
    return TenantSession(database, TenantContext(tenant.organization_id, user_id))


def test_a_viewer_cannot_enqueue_an_analysis(db, tenant_a):
    session = member_session(db, tenant_a, Role.VIEWER, "jobs")
    with pytest.raises(PermissionDenied):
        jobs.enqueue_job(session, job_type=JobType.ANALYSIS, payload={})


def test_an_analyst_cannot_enqueue_a_purge(db, tenant_a):
    session = member_session(db, tenant_a, Role.ANALYST, "jobs")
    with pytest.raises(PermissionDenied):
        jobs.enqueue_job(session, job_type=JobType.PURGE, payload={})


def test_an_admin_cannot_purge(db, tenant_a):
    session = member_session(db, tenant_a, Role.ADMIN, "jobs")
    with pytest.raises(PermissionDenied):
        jobs.purge_terminal_jobs(session, before=NOW, statuses=[JobStatus.SUCCEEDED])


def test_a_viewer_cannot_claim_a_job(db, tenant_a):
    enqueue(tenant_a)
    session = member_session(db, tenant_a, Role.VIEWER, "jobs")
    with pytest.raises(PermissionDenied):
        jobs.claim_next_job(session, worker_id=WORKER)


def test_a_non_member_sees_no_job_at_all(db, tenant_a, tenant_b):
    enqueue(tenant_a)
    stranger = TenantSession(db, TenantContext(tenant_a.organization_id, tenant_b.owner_id))
    with pytest.raises(NotFound):
        jobs.list_jobs(stranger)
