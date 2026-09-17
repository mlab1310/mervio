"""Cycle de vie du processus et gardien de bail, sans base (Mission 004.3).

Les comportements qui dependent de PostgreSQL (jeton d'exclusion, courses, connexions)
sont prouves dans tests/persistence/test_worker_*.py.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from mervio.observability.health import WorkerState
from mervio.persistence.errors import JobLeaseLost
from mervio.persistence.jobs import JobRecord
from mervio.workers.lease import LeaseKeeper
from mervio.workers.lifecycle import (
    HEALTH_STATE, TERMINAL, TRANSITIONS, IllegalTransition, Lifecycle, ProcessState,
)
from mervio.workers.worker import Worker, default_worker_id

S = ProcessState
ALL = list(ProcessState)
ALLOWED = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}


# =============================================================================================
# Cycle de vie
# =============================================================================================

def reach(state: ProcessState) -> Lifecycle:
    path = {S.STARTING: [], S.READY: [S.READY], S.BUSY: [S.READY, S.BUSY], S.DEGRADED: [S.READY, S.DEGRADED],
            S.DRAINING: [S.DRAINING], S.STOPPED: [S.DRAINING, S.STOPPED], S.FORCED: [S.FORCED]}[state]
    lifecycle = Lifecycle()
    for step in path:
        lifecycle.transition(step)
    assert lifecycle.state is state
    return lifecycle


@pytest.mark.parametrize("current, target", sorted(ALLOWED, key=lambda p: (p[0].value, p[1].value)))
def test_every_documented_transition_is_allowed(current, target):
    lifecycle = reach(current)
    assert lifecycle.transition(target) is current
    assert lifecycle.state is target


@pytest.mark.parametrize("current, target", sorted(
    {(a, b) for a in ALL for b in ALL} - ALLOWED, key=lambda p: (p[0].value, p[1].value)))
def test_every_other_transition_is_refused(current, target):
    lifecycle = reach(current)
    with pytest.raises(IllegalTransition):
        lifecycle.transition(target)
    assert lifecycle.state is current
    assert not lifecycle.transition_if_allowed(target)


def test_the_documented_graph():
    assert TRANSITIONS[S.STARTING] == {S.READY, S.DRAINING, S.STOPPED, S.FORCED}
    assert TRANSITIONS[S.READY] == {S.BUSY, S.DEGRADED, S.DRAINING, S.FORCED}
    assert TRANSITIONS[S.BUSY] == {S.READY, S.DEGRADED, S.DRAINING, S.FORCED}
    assert TRANSITIONS[S.DEGRADED] == {S.READY, S.DRAINING, S.FORCED}
    assert TRANSITIONS[S.DRAINING] == {S.STOPPED, S.FORCED}
    assert TERMINAL == {S.STOPPED, S.FORCED}
    for state in TERMINAL:
        assert TRANSITIONS[state] == frozenset()
    for state in set(ALL) - TERMINAL:
        assert S.FORCED in TRANSITIONS[state], "l'arret force doit etre possible depuis tout etat vivant"


def test_every_process_state_is_published_to_the_health_file():
    assert {state.value for state in HEALTH_STATE} == {state.value for state in WorkerState}
    assert HEALTH_STATE[S.FORCED] is WorkerState.FORCED


def test_changes_are_notified_and_recorded():
    seen = []
    lifecycle = Lifecycle(on_change=lambda previous, current: seen.append((previous, current)))
    lifecycle.transition(S.READY)
    lifecycle.transition(S.BUSY)
    lifecycle.transition(S.DRAINING)
    lifecycle.transition(S.STOPPED)
    assert seen == [(S.STARTING, S.READY), (S.READY, S.BUSY), (S.BUSY, S.DRAINING), (S.DRAINING, S.STOPPED)]
    assert lifecycle.history == [S.STARTING, S.READY, S.BUSY, S.DRAINING, S.STOPPED]
    assert lifecycle.terminal


def test_concurrent_transitions_never_break_the_graph():
    lifecycle = Lifecycle()
    lifecycle.transition(S.READY)
    results = []

    def attempt(target):
        results.append(lifecycle.transition_if_allowed(target))

    threads = [threading.Thread(target=attempt, args=(target,)) for target in (S.FORCED, S.DRAINING) * 20]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert lifecycle.state is S.FORCED
    for previous, current in zip(lifecycle.history, lifecycle.history[1:]):
        assert current in TRANSITIONS[previous]


# =============================================================================================
# Identite et configuration du worker
# =============================================================================================

def test_a_worker_id_is_bounded_and_unique():
    first, second = default_worker_id("w" * 80), default_worker_id("w" * 80)
    assert first != second and len(first) <= 100
    assert first.split("/")[0] == "w" * 40


def test_a_worker_uses_either_sessions_or_a_dispatcher():
    with pytest.raises(ValueError):
        Worker()
    with pytest.raises(ValueError):
        Worker(sessions=[object()], dispatcher=object())


@pytest.mark.parametrize("renew", [None, 0, 30, 31])
def test_a_lease_keeper_needs_a_renewal_shorter_than_the_lease(renew):
    with pytest.raises(ValueError):
        Worker(dispatcher=object(), lease_database=object(), lease_seconds=30, renew_seconds=renew)


# =============================================================================================
# Gardien de bail
# =============================================================================================

def job_record(**changes) -> JobRecord:
    now = datetime.now(timezone.utc)
    base = JobRecord(id=uuid.uuid4(), organization_id=uuid.uuid4(), store_id=None, job_type="analysis",
                     status="running", priority=0, attempts=1, max_attempts=3, idempotency_key=None,
                     correlation_id=uuid.uuid4(), enqueued_by=uuid.uuid4(), available_at=now, locked_at=now,
                     locked_by="w", lease_expires_at=now + timedelta(seconds=2), started_at=now,
                     finished_at=None, last_error_code=None, last_error=None, payload={}, result=None,
                     created_at=now, updated_at=now)
    return replace(base, **changes)


class FakeRenewal:
    """Renouvellement scripte: une suite de reponses (record, exception ou attente)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.released = []
        self.lock = threading.Lock()

    def renew(self, job):
        with self.lock:
            self.calls.append(time.monotonic())
            step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, (int, float)):
            time.sleep(step)
            step = "ok"
        return replace(job, lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=2))

    def release(self, job, code):
        self.released.append(code)
        return replace(job, status="queued", locked_by=None)


def keeper(renewal: FakeRenewal, *, lease=1.0, renew=0.2, **options) -> LeaseKeeper:
    return LeaseKeeper(job_record(), renew=renewal.renew, release=renewal.release, lease_seconds=lease,
                       renew_seconds=renew, heartbeat_seconds=0.05, **options)


def test_the_keeper_renews_periodically_and_stops_cleanly():
    renewal = FakeRenewal()
    beats = []
    guard = keeper(renewal, heartbeat=lambda: beats.append(1)).start()
    time.sleep(1.1)
    assert guard.stop()
    count = len(renewal.calls)
    assert 4 <= count <= 7, count
    assert guard.renewals == count and not guard.lost and guard.failures == 0
    intervals = [b - a for a, b in zip(renewal.calls, renewal.calls[1:])]
    assert all(0.15 <= interval <= 0.4 for interval in intervals), intervals
    assert len(beats) >= 10
    time.sleep(0.4)
    assert len(renewal.calls) == count, "aucun renouvellement apres l'arret"


def test_a_short_job_never_renews():
    renewal = FakeRenewal()
    guard = keeper(renewal).start()
    assert guard.stop()
    assert renewal.calls == [] and not guard.lost


def test_a_lost_lease_is_final_and_signalled_once():
    renewal = FakeRenewal(JobLeaseLost("x"))
    signals = []
    guard = keeper(renewal, on_lost=signals.append).start()
    assert guard._lost.wait(2)
    time.sleep(0.5)
    assert signals == ["lease_lost"] and guard.loss_reason == "lease_lost"
    assert len(renewal.calls) == 1, "plus aucun renouvellement apres la perte"
    with pytest.raises(JobLeaseLost):
        guard.checkpoint()
    assert guard.stop()


def test_transient_failures_are_retried_until_the_lease_is_renewed():
    renewal = FakeRenewal(ConnectionError("coupure"), ConnectionError("coupure"))
    resets = []
    guard = keeper(renewal, reset=lambda: resets.append(1)).start()
    time.sleep(1.2)
    guard.stop()
    assert guard.failures == 2 and len(resets) == 2
    assert guard.renewals >= 2 and not guard.lost


def test_the_lease_is_lost_when_it_cannot_be_renewed_before_its_deadline():
    renewal = FakeRenewal(*[ConnectionError("base partie")] * 50)
    signals = []
    started = time.monotonic()
    guard = keeper(renewal, lease=1.0, renew=0.2, on_lost=signals.append, started_at=started).start()
    assert guard._lost.wait(3)
    lost_after = time.monotonic() - started
    wait = time.monotonic() + 2
    while not signals and time.monotonic() < wait:  # le rappel suit le marquage de la perte
        time.sleep(0.01)
    assert signals == ["lease_expired"]
    assert 0.95 <= lost_after <= 1.6
    guard.stop()


def test_a_slow_renewal_counts_from_its_start():
    """L'echeance locale part de l'ENVOI de la demande: pessimiste, jamais optimiste."""
    renewal = FakeRenewal(0.3)
    guard = keeper(renewal, lease=2.0, renew=0.5).start()
    wait = time.monotonic() + 1.5
    while guard.renewals == 0 and time.monotonic() < wait:
        time.sleep(0.01)
    assert guard.renewals == 1
    sent = renewal.calls[0]
    assert guard.deadline == pytest.approx(sent + 2.0, abs=0.02)
    assert guard.stop()


def test_stopping_during_a_renewal_waits_for_it_and_renews_nothing_more():
    renewal = FakeRenewal(0.6)
    guard = keeper(renewal).start()
    time.sleep(0.3)
    assert len(renewal.calls) == 1
    started = time.monotonic()
    assert guard.stop()
    assert 0.3 <= time.monotonic() - started <= 1.0
    assert guard.renewals == 1
    time.sleep(0.5)
    assert len(renewal.calls) == 1


def test_a_release_requeues_once_and_declares_the_attempt_lost():
    renewal = FakeRenewal()
    signals = []
    guard = keeper(renewal, lease=30, renew=10, on_lost=signals.append).start()
    assert guard.release("worker_shutdown")
    assert not guard.release("worker_shutdown")
    assert renewal.released == ["worker_shutdown"]
    assert guard.lost and guard.loss_reason == "worker_shutdown" and signals == ["worker_shutdown"]
    assert guard.job.status == "queued"
    guard.stop()


def test_a_release_of_an_already_lost_attempt_is_refused():
    renewal = FakeRenewal(JobLeaseLost("x"))
    guard = keeper(renewal).start()
    assert guard._lost.wait(2)
    assert not guard.release()
    assert renewal.released == []
    guard.stop()


def test_a_failing_callback_never_stops_the_keeper():
    renewal = FakeRenewal()

    def broken():
        raise RuntimeError("sante indisponible")

    guard = keeper(renewal, heartbeat=broken).start()
    time.sleep(0.5)
    guard.stop()
    assert guard.renewals >= 1 and not guard.lost


def test_the_keeper_carries_the_correlation_id(monkeypatch):
    from mervio.observability.logging import correlation_scope, current_correlation_id
    seen = []
    renewal = FakeRenewal()
    renewal.renew = lambda job, original=renewal.renew: (seen.append(current_correlation_id()), original(job))[1]
    with correlation_scope("corr-lease-1"):
        guard = keeper(renewal).start()
    time.sleep(0.5)
    guard.stop()
    assert seen and set(seen) == {"corr-lease-1"}


@pytest.mark.parametrize("lease, renew", [(1, 0), (1, 1), (1, 2), (0, 0.5)])
def test_the_keeper_refuses_inconsistent_timings(lease, renew):
    with pytest.raises(ValueError):
        LeaseKeeper(job_record(), renew=lambda job: job, lease_seconds=lease, renew_seconds=renew)


def test_a_keeper_starts_once():
    guard = keeper(FakeRenewal()).start()
    with pytest.raises(RuntimeError):
        guard.start()
    guard.stop()
