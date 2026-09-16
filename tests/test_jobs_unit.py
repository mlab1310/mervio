"""Unites de la file de travaux, sans base de donnees (Mission 004.2).

Machine a etats, reprise, classification des erreurs, registre de gestionnaires,
retention: tout ce qui se decide sans PostgreSQL se teste sans PostgreSQL.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from mervio.errors import IngestionError, InsufficientDataError, MervioError  # noqa: E402
from mervio.persistence.errors import (  # noqa: E402
    JobLeaseLost, NotFound, PayloadRejected, PermissionDenied,
)
from mervio.persistence.jobs import (  # noqa: E402
    ALLOWED_TRANSITIONS, BACKOFF_CAP_SECONDS, DEFAULT_MAX_ATTEMPTS, ENQUEUE_PERMISSION, TERMINAL_STATUSES,
    JobStatus, JobType, backoff_seconds, validate_payload,
)
from mervio.persistence.retry import RETRYABLE_SQLSTATES, database_error_code, retryable_database_error  # noqa: E402
from mervio.persistence.tenancy import MINIMUM_ROLE, Permission, Role, role_allows  # noqa: E402
from mervio.workers.errors import PermanentJobError, RetryableJobError, classify  # noqa: E402
from mervio.workers.handlers import HandlerRegistry, default_registry  # noqa: E402
from mervio.workers.retention import AUDIT_FLOOR_DAYS, RetentionPolicy  # noqa: E402

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


# -- machine a etats ------------------------------------------------------------------------

def test_every_status_has_an_explicit_transition_set():
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)


def test_terminal_states_have_no_successor():
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset(), status


def test_the_only_states_that_move_are_queued_and_running():
    assert ALLOWED_TRANSITIONS[JobStatus.QUEUED] == {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.QUEUED}
    assert ALLOWED_TRANSITIONS[JobStatus.RUNNING] == {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.QUEUED}


def test_a_queued_job_never_reaches_a_result_without_running():
    assert JobStatus.SUCCEEDED not in ALLOWED_TRANSITIONS[JobStatus.QUEUED]
    assert JobStatus.FAILED not in ALLOWED_TRANSITIONS[JobStatus.QUEUED]


def test_terminal_statuses_are_exactly_the_three_finished_ones():
    assert TERMINAL_STATUSES == {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


# -- reprise --------------------------------------------------------------------------------

def test_backoff_doubles_from_thirty_seconds():
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]


def test_backoff_is_capped():
    assert backoff_seconds(20) == BACKOFF_CAP_SECONDS
    assert backoff_seconds(100) == BACKOFF_CAP_SECONDS


def test_backoff_is_deterministic():
    assert {backoff_seconds(3) for _ in range(50)} == {120}


def test_backoff_refuses_a_zero_attempt():
    with pytest.raises(ValueError):
        backoff_seconds(0)


@pytest.mark.parametrize("base, cap", [(0, 60), (30, 10)])
def test_backoff_refuses_inconsistent_bounds(base, cap):
    with pytest.raises(ValueError):
        backoff_seconds(1, base=base, cap=cap)


# -- charge utile ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["password", "api_key", "access_token", "SECRET", "shop_credential",
                                 "authorization", "private_key"])
def test_a_payload_carrying_a_secret_is_refused(key):
    with pytest.raises(PayloadRejected):
        validate_payload({key: "x"})


def test_a_secret_nested_in_the_payload_is_refused():
    with pytest.raises(PayloadRejected):
        validate_payload({"connection": {"api_key": "x"}})


def test_a_payload_never_carries_the_content_of_a_source():
    with pytest.raises(PayloadRejected):
        validate_payload({"sources": "a" * 5000})


def test_a_payload_locating_files_is_accepted():
    payload = {"store_id": "s", "sources": {"shopify_orders": "/tmp/orders.csv"}}
    assert validate_payload(payload) == payload


def test_a_payload_is_an_object():
    with pytest.raises(PayloadRejected):
        validate_payload(["not", "an", "object"])


# -- permissions ----------------------------------------------------------------------------

def test_enqueueing_requires_the_permission_of_the_work_itself():
    assert ENQUEUE_PERMISSION[JobType.IMPORT] is Permission.IMPORT_DATA
    assert ENQUEUE_PERMISSION[JobType.ANALYSIS] is Permission.RUN_ANALYSIS
    assert ENQUEUE_PERMISSION[JobType.PURGE] is Permission.PURGE_DATA


def test_every_job_type_has_an_enqueue_permission():
    assert set(ENQUEUE_PERMISSION) == set(JobType)


def test_new_permissions_follow_the_documented_role_matrix():
    # section 8 de MISSION_004_0_ARCHITECTURE.md: audit -> admin, suppression -> owner
    assert MINIMUM_ROLE[Permission.READ_AUDIT] is Role.ADMIN
    assert MINIMUM_ROLE[Permission.PURGE_DATA] is Role.OWNER
    assert MINIMUM_ROLE[Permission.RUN_JOBS] is Role.ANALYST


def test_a_viewer_neither_runs_nor_purges_nor_reads_the_audit():
    for permission in (Permission.RUN_JOBS, Permission.READ_AUDIT, Permission.PURGE_DATA):
        assert not role_allows(Role.VIEWER, permission), permission


def test_only_the_owner_purges():
    assert role_allows(Role.OWNER, Permission.PURGE_DATA)
    for role in (Role.ADMIN, Role.ANALYST, Role.VIEWER):
        assert not role_allows(role, Permission.PURGE_DATA), role


def test_every_permission_has_a_minimum_role():
    assert set(MINIMUM_ROLE) == set(Permission)


# -- classification des erreurs --------------------------------------------------------------

def test_a_validation_error_is_never_retried():
    assert classify(IngestionError("colonne absente", source="shopify_orders")).retryable is False
    assert classify(InsufficientDataError("pas assez d'historique")).retryable is False
    assert classify(NotFound("snapshot")).retryable is False
    assert classify(PermissionDenied("run_analysis", "viewer")).retryable is False


def test_a_deadlock_is_retried():
    error = psycopg.errors.DeadlockDetected("deadlock")
    assert retryable_database_error(error) is True
    assert classify(error).retryable is True


def test_a_constraint_violation_is_not_retried():
    error = psycopg.errors.UniqueViolation("doublon")
    assert retryable_database_error(error) is False
    assert classify(error).retryable is False


def test_a_lost_connection_is_retried():
    assert retryable_database_error(psycopg.OperationalError("connexion coupee")) is True


def test_every_retryable_sqlstate_is_five_characters():
    assert all(len(state) == 5 for state in RETRYABLE_SQLSTATES)


def test_a_lost_lease_is_never_retried_by_the_worker():
    failure = classify(JobLeaseLost("id"))
    assert (failure.retryable, failure.code) == (False, "lease_lost")


def test_an_unknown_error_is_retried_within_the_attempt_budget():
    assert classify(RuntimeError("panne inconnue")).retryable is True


def test_an_unclassified_business_error_is_not_retried():
    assert classify(MervioError("regle metier")).retryable is False


def test_handler_errors_carry_their_own_verdict():
    assert classify(RetryableJobError("locked", "verrou")).retryable is True
    assert classify(PermanentJobError("payload_invalid", "grain")).retryable is False


def test_an_error_code_is_publishable_and_carries_no_data():
    failure = classify(IngestionError("valeur x=42 invalide", source="stripe"))
    assert failure.code == "ingestion_error"
    assert "42" not in failure.code


def test_an_error_message_never_leaks_an_absolute_path():
    assert classify(RuntimeError("/Users/someone/private/export.csv")).message == "[redacted]"


def test_a_database_error_code_uses_its_sqlstate():
    assert database_error_code(psycopg.errors.DeadlockDetected("x")) == "sqlstate_40P01"


# -- registre -------------------------------------------------------------------------------

def test_the_default_registry_covers_every_declared_job_type():
    assert set(default_registry().types()) == set(JobType)


def test_dispatch_returns_the_registered_handler():
    registry = HandlerRegistry().register(JobType.IMPORT, lambda context: {"ok": True})
    assert registry.resolve(JobType.IMPORT)(None) == {"ok": True}
    assert registry.resolve("import") is registry.resolve(JobType.IMPORT)


def test_dispatch_on_an_unregistered_type_is_a_permanent_failure():
    registry = HandlerRegistry()
    with pytest.raises(PermanentJobError) as excinfo:
        registry.resolve(JobType.PURGE)
    assert classify(excinfo.value).retryable is False


def test_a_type_cannot_be_registered_twice():
    registry = HandlerRegistry().register(JobType.IMPORT, lambda context: {})
    with pytest.raises(ValueError):
        registry.register(JobType.IMPORT, lambda context: {})


def test_a_copied_registry_is_independent():
    original = HandlerRegistry().register(JobType.IMPORT, lambda context: {})
    copy = original.copy()
    copy.register(JobType.PURGE, lambda context: {})
    assert set(original.types()) == {JobType.IMPORT}
    assert set(copy.types()) == {JobType.IMPORT, JobType.PURGE}


def test_an_unknown_job_type_is_rejected():
    with pytest.raises(ValueError):
        HandlerRegistry().resolve("explanation")


# -- retention ------------------------------------------------------------------------------

def test_default_retention_keeps_failures_longer_than_successes():
    policy = RetentionPolicy()
    assert policy.failed_jobs_days > policy.succeeded_jobs_days


def test_audit_retention_never_goes_under_the_database_floor():
    with pytest.raises(ValueError):
        RetentionPolicy(audit_events_days=AUDIT_FLOOR_DAYS - 1)


def test_audit_always_outlives_the_jobs_it_describes():
    with pytest.raises(ValueError):
        RetentionPolicy(audit_events_days=30, failed_jobs_days=60)


def test_retention_cutoffs_are_computed_from_the_given_instant():
    policy = RetentionPolicy(succeeded_jobs_days=7, failed_jobs_days=30, cancelled_jobs_days=3)
    cutoffs = policy.job_cutoffs(NOW)
    assert cutoffs[JobStatus.SUCCEEDED] == NOW - timedelta(days=7)
    assert cutoffs[JobStatus.FAILED] == NOW - timedelta(days=30)
    assert cutoffs[JobStatus.CANCELLED] == NOW - timedelta(days=3)
    assert policy.audit_cutoff(NOW) == NOW - timedelta(days=365)


def test_every_terminal_status_has_a_retention():
    assert set(RetentionPolicy().job_cutoffs(NOW)) == TERMINAL_STATUSES


def test_a_retention_document_round_trips():
    policy = RetentionPolicy(succeeded_jobs_days=2, failed_jobs_days=5, cancelled_jobs_days=1)
    assert RetentionPolicy.from_document(policy.to_document()) == policy


def test_an_unknown_retention_key_is_refused():
    with pytest.raises(ValueError):
        RetentionPolicy.from_document({"snapshots_days": 10})


@pytest.mark.parametrize("value", [0, -1, "7", True, 1.5])
def test_retention_days_must_be_a_positive_integer(value):
    with pytest.raises(ValueError):
        RetentionPolicy(succeeded_jobs_days=value)


def test_the_batch_limit_stays_within_the_repository_bounds():
    with pytest.raises(ValueError):
        RetentionPolicy(batch_limit=0)
    with pytest.raises(ValueError):
        RetentionPolicy(batch_limit=1001)


def test_the_default_attempt_budget_is_bounded():
    assert 1 <= DEFAULT_MAX_ATTEMPTS <= 20
