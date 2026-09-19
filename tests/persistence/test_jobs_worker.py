"""Worker et gestionnaires (Mission 004.2): import, analyse, purge, audit, idempotence, pannes.

Ces tests executent la vraie chaine 004.1 depuis un travail de fond: les fichiers
CSV de `data/sample` passent par les connecteurs reels, l'instantane est scelle en
base et le rapport est produit par le moteur deterministe inchange.
"""
from __future__ import annotations

import io
import json
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from mervio.observability.logging import configure_json_logging
from mervio.persistence import analyses, audit, jobs, snapshots
from mervio.persistence.codec import serialize_report
from mervio.persistence.errors import PermissionDenied
from mervio.persistence.jobs import JobStatus, JobType
from mervio.persistence.tenancy import Role, TenantContext, TenantSession, add_member, ensure_user
from mervio.workers.errors import PermanentJobError, RetryableJobError
from mervio.workers.handlers import HandlerRegistry, default_registry
from mervio.workers.retention import RetentionPolicy
from mervio.workers.worker import Worker, cancel, enqueue

from .persistence_support import SAMPLE_FILES, TEST_MASTER_KEY, analysis_day, engine_files

PAST = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
#: `_meta.generated_at` est la seule valeur non deterministe d'un rapport (precision:
#: la seconde). Toute comparaison octet pour octet la fige, comme en 004.1.
FROZEN = datetime(2026, 9, 14, 6, 30, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN if tz is not None else FROZEN.replace(tzinfo=None)


@pytest.fixture
def frozen_clock(monkeypatch):
    import mervio.analytics.report as report_module
    monkeypatch.setattr(report_module, "datetime", _FrozenDatetime)


@pytest.fixture
def json_logs():
    """Capture la sortie JSON du worker et restaure le logger apres le test."""
    root = logging.getLogger("mervio")
    previous, propagate, level = list(root.handlers), root.propagate, root.level
    buffer = io.StringIO()
    configure_json_logging(stream=buffer, service="worker", level=logging.DEBUG)
    yield buffer
    root.handlers = previous
    root.propagate, root.level = propagate, level


def documents(buffer) -> list:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def worker(tenant, *, registry=None, **kwargs) -> Worker:
    return Worker(sessions=[tenant.session], registry=registry or default_registry(identity_master=TEST_MASTER_KEY),
                  worker_id=kwargs.pop("worker_id", "worker-test"), **kwargs)


def import_payload(tenant, files=None) -> dict:
    return {"store_id": str(tenant.store_id), "connection_id": str(tenant.connection_id),
            "sources": dict(files or SAMPLE_FILES)}


def enqueue_import(tenant, files=None, **kwargs):
    return enqueue(tenant.session, job_type=JobType.IMPORT, payload=import_payload(tenant, files),
                   store_id=tenant.store_id, **kwargs)


def enqueue_analysis(tenant, snapshot_id, **payload):
    return enqueue(tenant.session, job_type=JobType.ANALYSIS, store_id=tenant.store_id,
                   payload={"store_id": str(tenant.store_id), "snapshot_id": str(snapshot_id), **payload})


def audit_actions(tenant, **filters) -> list:
    return [event.action for event in audit.list_events(tenant.session, limit=100, **filters)]


# -- travail d'import -------------------------------------------------------------------------

def test_an_import_job_seals_a_snapshot(tenant_a):
    job = enqueue_import(tenant_a)
    outcome = worker(tenant_a).run_once()
    assert outcome.succeeded
    assert outcome.job.status == JobStatus.SUCCEEDED.value
    assert outcome.job.result["status"] == "completed"
    snapshot = snapshots.get_snapshot(tenant_a.session, tenant_a.store_id,
                                      uuid.UUID(outcome.job.result["snapshot_id"]))
    assert snapshot.status == "completed"
    assert snapshot.row_count > 0
    assert jobs.get_job(tenant_a.session, job.id).status == JobStatus.SUCCEEDED.value


def test_an_import_job_reuses_the_004_1_pipeline_without_duplicating_a_snapshot(tenant_a):
    """Idempotence d'import: la meme empreinte ne scelle jamais deux instantanes."""
    enqueue_import(tenant_a)
    first = worker(tenant_a).run_once()
    enqueue_import(tenant_a)
    second = worker(tenant_a).run_once()
    assert second.job.result["status"] == "reused"
    assert second.job.result["snapshot_id"] == first.job.result["snapshot_id"]
    assert len(snapshots.list_snapshots(tenant_a.session, tenant_a.store_id)) == 1


def test_a_replayed_import_job_keeps_one_snapshot_after_a_simulated_crash(tenant_a):
    """Crash entre l'execution et le resultat: le bail expire, un autre worker rejoue."""
    enqueue_import(tenant_a)
    crashed = worker(tenant_a, worker_id="worker-dead")
    session, job = crashed.claim()
    crashed.registry.resolve(job.kind)(_context(crashed, session, job))  # travail fait, resultat jamais ecrit

    reviver = worker(tenant_a, worker_id="worker-alive", clock=lambda: datetime.now(timezone.utc)
                     + timedelta(seconds=jobs.DEFAULT_LEASE_SECONDS + 1))
    assert len(reviver.recover_stale()) == 1
    outcome = reviver.run_once()
    assert outcome.succeeded
    assert outcome.job.result["status"] == "reused"
    assert outcome.job.attempts == 2
    assert len(snapshots.list_snapshots(tenant_a.session, tenant_a.store_id)) == 1


def _context(runner, session, job):
    from mervio.observability.logging import get_event_logger
    from mervio.workers.handlers import JobContext
    return JobContext(session=session, job=job, correlation_id=str(job.correlation_id),
                      log=get_event_logger("worker"), now=None)


def test_an_invalid_source_fails_the_import_permanently(tenant_a, tmp_path):
    broken = tmp_path / "shopify_orders.csv"
    broken.write_text("colonne_inconnue\n1\n", encoding="utf-8")
    enqueue_import(tenant_a, {"shopify_orders": str(broken)})
    outcome = worker(tenant_a).run_once()
    assert outcome.outcome == "failed"
    assert outcome.job.status == JobStatus.FAILED.value
    assert outcome.job.attempts == 1  # aucune reprise: la source ne deviendra pas valide
    assert outcome.error_code == "validation_failed"


def test_a_missing_file_fails_the_import_without_retrying(tenant_a, tmp_path):
    enqueue_import(tenant_a, {"shopify_orders": str(tmp_path / "absent.csv")})
    outcome = worker(tenant_a).run_once()
    assert outcome.job.status == JobStatus.FAILED.value
    assert outcome.job.attempts == 1


def test_an_import_payload_without_a_source_is_a_permanent_failure(tenant_a):
    enqueue(tenant_a.session, job_type=JobType.IMPORT, store_id=tenant_a.store_id,
            payload={"store_id": str(tenant_a.store_id), "connection_id": str(tenant_a.connection_id)})
    outcome = worker(tenant_a).run_once()
    assert (outcome.job.status, outcome.error_code) == (JobStatus.FAILED.value, "payload_incomplete")


def test_an_unknown_source_kind_is_refused(tenant_a):
    enqueue(tenant_a.session, job_type=JobType.IMPORT, store_id=tenant_a.store_id,
            payload={"store_id": str(tenant_a.store_id), "connection_id": str(tenant_a.connection_id),
                     "sources": {"amazon_orders": "/tmp/x.csv"}})
    outcome = worker(tenant_a).run_once()
    assert outcome.error_code == "payload_invalid"


# -- travail d'analyse ------------------------------------------------------------------------

def test_an_analysis_job_produces_the_report_of_the_unchanged_engine(tenant_a):
    enqueue_import(tenant_a)
    imported = worker(tenant_a).run_once()
    snapshot_id = uuid.UUID(imported.job.result["snapshot_id"])
    enqueue_analysis(tenant_a, snapshot_id)
    outcome = worker(tenant_a).run_once()
    assert outcome.succeeded
    stored = analyses.get_report(tenant_a.session, tenant_a.store_id, uuid.UUID(outcome.job.result["report_id"]))
    assert stored.record.payload_sha256 == outcome.job.result["payload_sha256"]
    assert stored.payload_json == serialize_report(stored.report())


def test_a_job_report_is_identical_to_the_direct_004_1_path(tenant_a, synthetic_set, frozen_clock):
    """Le wrapper de job ne change aucune valeur: memes octets que `analyze_snapshot` appele en direct.

    004.4.2: le chemin direct s'execute dans la MEME organisation (seconde boutique): les references client
    dependent de la cle d'identite de l'organisation (D-053), identique pour ses deux boutiques.
    """
    from mervio.application.persisted_analysis import analyze_snapshot
    from mervio.persistence.stores import create_csv_connection, create_store

    files = engine_files(synthetic_set.directory)
    today = analysis_day(synthetic_set.manifest)
    payload = {"sources": files, "synthetic": True}

    other_store = create_store(tenant_a.session, name="Boutique directe")
    other = replace(tenant_a, store_id=other_store.id,
                    connection_id=create_csv_connection(tenant_a.session, store_id=other_store.id,
                                                        label="Imports directs").id)
    direct_snapshot = _import_directly(other, files)
    direct = analyze_snapshot(other.session, store_id=other.store_id, snapshot_id=direct_snapshot.id,
                              today=today)

    enqueue(tenant_a.session, job_type=JobType.IMPORT, store_id=tenant_a.store_id,
            payload={**import_payload(tenant_a), **payload})
    imported = worker(tenant_a).run_once()
    enqueue_analysis(tenant_a, imported.job.result["snapshot_id"], as_of_date=today.isoformat())
    analysed = worker(tenant_a).run_once()

    by_job = analyses.get_report(tenant_a.session, tenant_a.store_id, uuid.UUID(analysed.job.result["report_id"]))
    by_hand = analyses.get_report(other.session, other.store_id, direct.report.id)
    assert by_job.record.payload_sha256 == by_hand.record.payload_sha256
    assert by_job.payload_json == by_hand.payload_json


def _import_directly(tenant, files):
    from mervio.application.persisted_analysis import SnapshotImportRequest, import_csv_snapshot
    result = import_csv_snapshot(tenant.session, store_id=tenant.store_id, connection_id=tenant.connection_id,
                                 request=SnapshotImportRequest(**files, synthetic=True), master_key=TEST_MASTER_KEY)
    assert result.status == "completed", result.error
    return result.snapshot


def test_a_replayed_analysis_job_never_writes_a_second_report(tenant_a):
    enqueue_import(tenant_a)
    snapshot_id = uuid.UUID(worker(tenant_a).run_once().job.result["snapshot_id"])
    enqueue_analysis(tenant_a, snapshot_id)
    first = worker(tenant_a).run_once()
    enqueue_analysis(tenant_a, snapshot_id)
    second = worker(tenant_a).run_once()
    assert second.job.result["status"] == "reused"
    assert second.job.result["report_id"] == first.job.result["report_id"]
    assert len(analyses.list_runs(tenant_a.session, tenant_a.store_id)) == 1


def test_an_analysis_on_an_unknown_snapshot_fails_permanently(tenant_a):
    enqueue_analysis(tenant_a, uuid.uuid4())
    outcome = worker(tenant_a).run_once()
    assert outcome.job.status == JobStatus.FAILED.value
    assert outcome.job.attempts == 1
    assert outcome.error_code == "not_found"


def test_an_analysis_on_another_tenant_snapshot_is_not_found(tenant_a, tenant_b):
    enqueue_import(tenant_b)
    theirs = worker(tenant_b).run_once().job.result["snapshot_id"]
    enqueue_analysis(tenant_a, theirs)
    outcome = worker(tenant_a).run_once()
    assert (outcome.job.status, outcome.error_code) == (JobStatus.FAILED.value, "not_found")


def test_an_engine_failure_fails_the_analysis_job_without_retrying(tenant_a, monkeypatch):
    """Injection de panne: le moteur leve, l'execution est marquee en echec et tracee."""
    from mervio.application import persisted_analysis
    from mervio.errors import InsufficientDataError

    enqueue_import(tenant_a)
    snapshot_id = worker(tenant_a).run_once().job.result["snapshot_id"]
    monkeypatch.setattr(persisted_analysis, "analyze_loaded_dataset",
                        _raise(InsufficientDataError("pas assez d'historique")))
    job = enqueue_analysis(tenant_a, snapshot_id)
    outcome = worker(tenant_a).run_once()
    assert outcome.job.status == JobStatus.FAILED.value
    assert outcome.job.attempts == 1  # une erreur d'historique ne devient pas vraie en la rejouant
    assert outcome.error_code == "insufficient_data"
    run = analyses.list_runs(tenant_a.session, tenant_a.store_id)[0]
    assert (run.status, run.failure_code) == ("failed", "insufficient_data")
    assert audit_actions(tenant_a, correlation_id=job.correlation_id) == [
        "job.enqueued", "job.claimed", "analysis.started", "analysis.failed", "job.failed"]


def _raise(exception):
    def fail(*args, **kwargs):
        raise exception
    return fail


@pytest.mark.parametrize("payload, code", [
    ({"grain": "daily"}, "payload_invalid"),
    ({"as_of_date": "pas-une-date"}, "payload_invalid"),
])
def test_an_invalid_analysis_payload_is_a_permanent_failure(tenant_a, payload, code):
    enqueue_analysis(tenant_a, uuid.uuid4(), **payload)
    assert worker(tenant_a).run_once().error_code == code


def test_the_monthly_grain_is_carried_by_the_payload(tenant_a):
    enqueue_import(tenant_a)
    snapshot_id = worker(tenant_a).run_once().job.result["snapshot_id"]
    enqueue_analysis(tenant_a, snapshot_id, grain="month")
    outcome = worker(tenant_a).run_once()
    run = analyses.get_run(tenant_a.session, tenant_a.store_id, uuid.UUID(outcome.job.result["analysis_run_id"]))
    assert run.grain == "month"


# -- audit d'une execution ---------------------------------------------------------------------

def test_a_whole_import_execution_is_auditable_under_one_correlation_id(tenant_a):
    job = enqueue_import(tenant_a)
    worker(tenant_a).run_once()
    events = audit.list_events(tenant_a.session, correlation_id=job.correlation_id, limit=100)
    assert [event.action for event in events] == [
        "job.enqueued", "job.claimed", "import.started", "import.succeeded", "job.succeeded"]
    assert {event.correlation_id for event in events} == {job.correlation_id}
    assert {event.organization_id for event in events} == {tenant_a.organization_id}


def test_a_failed_execution_is_audited_as_failed(tenant_a, tmp_path):
    broken = tmp_path / "shopify_orders.csv"
    broken.write_text("x\n1\n", encoding="utf-8")
    job = enqueue_import(tenant_a, {"shopify_orders": str(broken)})
    worker(tenant_a).run_once()
    events = audit.list_events(tenant_a.session, correlation_id=job.correlation_id, limit=100)
    assert [event.action for event in events] == [
        "job.enqueued", "job.claimed", "import.started", "import.failed", "job.failed"]
    assert events[-1].outcome == "failed"
    assert events[-1].metadata["error_code"] == "validation_failed"
    assert events[-1].metadata["retryable"] is False


def test_a_retry_is_audited_as_a_requeue(tenant_a):
    registry = HandlerRegistry().register(JobType.ANALYSIS, _always(RetryableJobError("locked", "verrou")))
    job = enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    worker(tenant_a, registry=registry).run_once()
    actions = audit_actions(tenant_a, correlation_id=job.correlation_id)
    assert actions == ["job.enqueued", "job.claimed", "job.requeued"]


def test_a_recovered_job_is_audited(tenant_a):
    job = enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    runner = worker(tenant_a)
    runner.claim()
    later = Worker(sessions=[tenant_a.session], worker_id="worker-2",
                   clock=lambda: datetime.now(timezone.utc) + timedelta(hours=1))
    assert len(later.recover_stale()) == 1
    actions = audit_actions(tenant_a, correlation_id=job.correlation_id)
    assert actions == ["job.enqueued", "job.claimed", "job.recovered"]
    recovery = audit.list_events(tenant_a.session, correlation_id=job.correlation_id, limit=100)[-1]
    assert (recovery.actor_type, recovery.actor_id) == ("system", None)
    assert recovery.metadata["reason"] == "lease_expired"


def test_a_cancellation_is_audited(tenant_a):
    job = enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    cancel(tenant_a.session, job.id)
    assert audit_actions(tenant_a, correlation_id=job.correlation_id) == ["job.enqueued", "job.cancelled"]


def test_the_audit_of_a_state_change_is_committed_with_it(tenant_a):
    """Le crochet d'audit echoue -> la transition est annulee: jamais d'etat sans trace."""
    job = enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})

    def refuse(conn, claimed):
        raise RuntimeError("audit indisponible")

    with pytest.raises(RuntimeError):
        jobs.claim_next_job(tenant_a.session, worker_id="w", hook=refuse)
    assert jobs.get_job(tenant_a.session, job.id).status == JobStatus.QUEUED.value
    assert jobs.get_job(tenant_a.session, job.id).attempts == 0


def test_an_enqueue_without_its_audit_leaves_no_job(tenant_a):
    def refuse(conn, created):
        raise RuntimeError("audit indisponible")

    with pytest.raises(RuntimeError):
        jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={}, hook=refuse)
    assert jobs.list_jobs(tenant_a.session) == []


# -- logs -------------------------------------------------------------------------------------

def test_an_execution_emits_correlated_json_logs(tenant_a, json_logs):
    job = enqueue_import(tenant_a)
    worker(tenant_a).run_once()
    emitted = documents(json_logs)
    events = [document["event"] for document in emitted]
    assert ["job.enqueued", "job.claimed", "import.started", "import.succeeded", "job.succeeded"] == [
        event for event in events if event.startswith(("job.", "import."))]
    correlated = [d for d in emitted if d["event"] != "job.enqueued"]
    assert {d["correlation_id"] for d in correlated} == {str(job.correlation_id)}
    success = next(d for d in emitted if d["event"] == "job.succeeded")
    assert success["job_id"] == str(job.id)
    assert success["organization_id"] == str(tenant_a.organization_id)
    assert success["job_type"] == "import"
    assert success["duration_ms"] >= 0
    assert success["worker_id"] == "worker-test"


def test_a_failure_log_carries_the_attempt_and_the_retry_decision(tenant_a, json_logs):
    registry = HandlerRegistry().register(JobType.ANALYSIS, _always(RetryableJobError("locked", "verrou")))
    enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    worker(tenant_a, registry=registry).run_once()
    failure = next(d for d in documents(json_logs) if d["event"] == "job.requeued")
    assert failure["level"] == "ERROR"
    assert (failure["attempt"], failure["max_attempts"], failure["retryable"]) == (1, 3, True)
    assert failure["error_code"] == "locked"
    assert failure["error_type"] == "RetryableJobError"


def test_no_absolute_path_reaches_the_logs(tenant_a, json_logs):
    enqueue_import(tenant_a)
    worker(tenant_a).run_once()
    text = json_logs.getvalue()
    for path in SAMPLE_FILES.values():
        assert path not in text


# -- reprises et pannes -------------------------------------------------------------------------

def _always(exception):
    def handler(context):
        raise exception
    return handler


def test_a_retryable_handler_error_puts_the_job_back_in_the_queue(tenant_a):
    registry = HandlerRegistry().register(JobType.ANALYSIS, _always(RetryableJobError("locked", "verrou")))
    enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    outcome = worker(tenant_a, registry=registry).run_once()
    assert outcome.outcome == "requeued"
    assert outcome.job.status == JobStatus.QUEUED.value
    assert outcome.job.available_at > datetime.now(timezone.utc)


def test_a_permanent_handler_error_never_retries(tenant_a):
    registry = HandlerRegistry().register(JobType.ANALYSIS, _always(PermanentJobError("bad_input", "non")))
    enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    outcome = worker(tenant_a, registry=registry).run_once()
    assert outcome.job.status == JobStatus.FAILED.value
    assert outcome.job.attempts == 1


def test_attempts_are_exhausted_then_the_job_is_terminally_failed(tenant_a):
    registry = HandlerRegistry().register(JobType.ANALYSIS, _always(RetryableJobError("locked", "verrou")))
    enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={}, max_attempts=2)
    moment = datetime.now(timezone.utc)
    statuses = []
    for _ in range(2):
        runner = worker(tenant_a, registry=registry, clock=lambda m=moment: m)
        outcome = runner.run_once()
        statuses.append(outcome.job.status)
        moment = outcome.job.available_at if outcome.job.available_at else moment
    assert statuses == [JobStatus.QUEUED.value, JobStatus.FAILED.value]


def test_an_unknown_exception_is_retried_within_the_budget(tenant_a):
    registry = HandlerRegistry().register(JobType.ANALYSIS, _always(RuntimeError("panne inconnue")))
    enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    outcome = worker(tenant_a, registry=registry).run_once()
    assert outcome.outcome == "requeued"


def test_a_worker_never_raises_a_handler_exception(tenant_a):
    registry = HandlerRegistry().register(JobType.ANALYSIS, _always(RuntimeError("panne")))
    enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    assert worker(tenant_a, registry=registry).run_once() is not None  # ne leve pas


def test_a_job_without_a_handler_fails_permanently(tenant_a):
    enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    outcome = worker(tenant_a, registry=HandlerRegistry()).run_once()
    assert (outcome.job.status, outcome.error_code) == (JobStatus.FAILED.value, "no_handler")


def test_a_worker_that_lost_its_lease_publishes_nothing(tenant_a):
    enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    slow = worker(tenant_a, worker_id="worker-slow", lease_seconds=1)
    session, job = slow.claim()
    Worker(sessions=[tenant_a.session], worker_id="worker-fast",
           clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=5)).recover_stale()
    outcome = slow.execute(session, job)
    assert outcome.outcome == "lease_lost"
    assert jobs.get_job(tenant_a.session, job.id).status == JobStatus.QUEUED.value


# -- boucles ------------------------------------------------------------------------------------

def test_run_until_empty_drains_the_queue(tenant_a):
    registry = HandlerRegistry().register(JobType.ANALYSIS, lambda context: {"ok": True})
    for _ in range(5):
        enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    outcomes = worker(tenant_a, registry=registry).run_until_empty()
    assert len(outcomes) == 5
    assert all(outcome.succeeded for outcome in outcomes)
    assert jobs.queue_statistics(tenant_a.session)["by_status"] == {"succeeded": 5}


def test_run_once_on_an_empty_queue_returns_nothing(tenant_a):
    assert worker(tenant_a).run_once() is None


def test_run_forever_stops_on_its_signal(tenant_a):
    registry = HandlerRegistry().register(JobType.ANALYSIS, lambda context: {"ok": True})
    for _ in range(3):
        enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    outcomes = worker(tenant_a, registry=registry).run_forever(idle_sleep=0.01, max_jobs=3)
    assert len(outcomes) == 3


def test_a_worker_serves_only_the_types_it_was_given(tenant_a):
    enqueue_import(tenant_a)
    enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    runner = worker(tenant_a, job_types=[JobType.IMPORT])
    assert runner.run_once().job.job_type == "import"
    assert runner.run_once() is None


def test_a_worker_alternates_between_its_tenants(tenant_a, tenant_b):
    registry = HandlerRegistry().register(JobType.ANALYSIS, lambda context: {"ok": True})
    for tenant in (tenant_a, tenant_a, tenant_b, tenant_b):
        enqueue(tenant.session, job_type=JobType.ANALYSIS, payload={})
    runner = Worker(sessions=[tenant_a.session, tenant_b.session], registry=registry, worker_id="multi")
    organizations = [outcome.job.organization_id for outcome in runner.run_until_empty()]
    assert organizations[:2] == [tenant_a.organization_id, tenant_b.organization_id]
    assert sorted(map(str, organizations)) == sorted(map(str, [tenant_a.organization_id] * 2
                                                         + [tenant_b.organization_id] * 2))


def test_a_worker_without_a_session_is_refused():
    with pytest.raises(ValueError):
        Worker(sessions=[])


def test_a_worker_never_reaches_a_tenant_it_does_not_serve(tenant_a, tenant_b):
    """Le travail de B est hors de portee d'un worker qui ne sert que A."""
    enqueue(tenant_b.session, job_type=JobType.ANALYSIS, payload={})
    assert worker(tenant_a).run_once() is None
    assert worker(tenant_a).recover_stale() == []
    assert jobs.list_jobs(tenant_b.session)[0].status == JobStatus.QUEUED.value


# -- travail de purge -----------------------------------------------------------------------------

def _finished_job(tenant, *, at, status=JobStatus.SUCCEEDED):
    job = jobs.enqueue_job(tenant.session, job_type=JobType.ANALYSIS, payload={}, available_at=at)
    running = jobs.claim_next_job(tenant.session, worker_id="w", now=at)
    if status is JobStatus.SUCCEEDED:
        return jobs.mark_succeeded(tenant.session, running, worker_id="w", now=at)
    return jobs.mark_failed(tenant.session, running, error_code="boom", retryable=False, worker_id="w", now=at)


def test_a_purge_job_removes_expired_jobs_and_audits_itself(tenant_a):
    _finished_job(tenant_a, at=PAST)
    kept = jobs.enqueue_job(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    job = enqueue(tenant_a.session, job_type=JobType.PURGE, payload={})
    outcome = worker(tenant_a, job_types=[JobType.PURGE]).run_once()
    assert outcome.succeeded
    assert outcome.job.result["jobs"]["succeeded"] == 1
    assert [j.id for j in jobs.list_jobs(tenant_a.session) if j.status == JobStatus.QUEUED.value] == [kept.id]
    assert audit_actions(tenant_a, correlation_id=job.correlation_id) == [
        "job.enqueued", "job.claimed", "purge.started", "purge.completed", "job.succeeded"]


def test_a_purge_never_removes_the_running_job_that_performs_it(tenant_a):
    job = enqueue(tenant_a.session, job_type=JobType.PURGE, payload={})
    outcome = worker(tenant_a).run_once()
    assert outcome.succeeded
    assert jobs.get_job(tenant_a.session, job.id).status == JobStatus.SUCCEEDED.value


def test_a_purge_honours_the_retention_it_is_given(tenant_a):
    _finished_job(tenant_a, at=datetime.now(timezone.utc) - timedelta(days=3))
    enqueue(tenant_a.session, job_type=JobType.PURGE,
            payload={"retention": RetentionPolicy(succeeded_jobs_days=30).to_document()})
    assert worker(tenant_a).run_once().job.result["jobs"]["succeeded"] == 0
    enqueue(tenant_a.session, job_type=JobType.PURGE,
            payload={"retention": RetentionPolicy(succeeded_jobs_days=1).to_document()})
    assert worker(tenant_a).run_once().job.result["jobs"]["succeeded"] == 1


def test_a_purge_with_an_invalid_retention_fails_permanently(tenant_a):
    enqueue(tenant_a.session, job_type=JobType.PURGE, payload={"retention": {"audit_events_days": 1}})
    outcome = worker(tenant_a).run_once()
    assert (outcome.job.status, outcome.error_code) == (JobStatus.FAILED.value, "payload_invalid")


def test_a_purge_never_breaks_the_provenance_chain(tenant_a):
    enqueue_import(tenant_a)
    snapshot_id = uuid.UUID(worker(tenant_a).run_once().job.result["snapshot_id"])
    enqueue_analysis(tenant_a, snapshot_id)
    report_id = uuid.UUID(worker(tenant_a).run_once().job.result["report_id"])
    enqueue(tenant_a.session, job_type=JobType.PURGE,
            payload={"retention": RetentionPolicy(succeeded_jobs_days=1, failed_jobs_days=30,
                                                  cancelled_jobs_days=1).to_document()})
    assert worker(tenant_a).run_once().succeeded
    # la chaine rapport -> execution -> instantane reste entiere
    stored = analyses.get_report(tenant_a.session, tenant_a.store_id, report_id)
    assert stored.record.snapshot_id == snapshot_id
    assert snapshots.get_snapshot(tenant_a.session, tenant_a.store_id, snapshot_id).status == "completed"
    assert snapshots.load_dataset(tenant_a.session, tenant_a.store_id, snapshot_id).orders


def test_a_purge_is_repeatable(tenant_a):
    _finished_job(tenant_a, at=PAST)
    for _ in range(3):
        enqueue(tenant_a.session, job_type=JobType.PURGE, payload={})
        assert worker(tenant_a).run_once().succeeded
    assert audit.count_events(tenant_a.session) > 0


def test_a_purge_job_of_another_tenant_never_touches_ours(tenant_a, tenant_b):
    mine = _finished_job(tenant_a, at=PAST)
    enqueue(tenant_b.session, job_type=JobType.PURGE, payload={})
    assert worker(tenant_b).run_once().succeeded
    assert jobs.get_job(tenant_a.session, mine.id).id == mine.id


def test_only_an_owner_enqueues_a_purge(db, tenant_a):
    admin_id = ensure_user(db, "test|purge|admin")
    add_member(tenant_a.session, user_id=admin_id, role=Role.ADMIN)
    admin = TenantSession(db, TenantContext(tenant_a.organization_id, admin_id))
    with pytest.raises(PermissionDenied):
        enqueue(admin, job_type=JobType.PURGE, payload={})


def test_a_purge_run_by_a_non_owner_worker_fails(db, tenant_a):
    """Le role est reverifie a l'execution, pas seulement a la mise en file."""
    enqueue(tenant_a.session, job_type=JobType.PURGE, payload={})
    analyst_id = ensure_user(db, "test|purge|analyst")
    add_member(tenant_a.session, user_id=analyst_id, role=Role.ANALYST)
    analyst = TenantSession(db, TenantContext(tenant_a.organization_id, analyst_id))
    outcome = Worker(sessions=[analyst], worker_id="w-analyst").run_once()
    assert outcome.job.status == JobStatus.FAILED.value
    assert outcome.error_code == "permission_denied"


# -- metriques ------------------------------------------------------------------------------------

def test_queue_statistics_follow_the_execution(tenant_a):
    registry = HandlerRegistry().register(JobType.ANALYSIS, lambda context: {"ok": True})
    for _ in range(3):
        enqueue(tenant_a.session, job_type=JobType.ANALYSIS, payload={})
    assert jobs.queue_statistics(tenant_a.session)["by_status"] == {"queued": 3}
    worker(tenant_a, registry=registry).run_until_empty()
    statistics = jobs.queue_statistics(tenant_a.session)
    assert statistics["by_status"] == {"succeeded": 3}
    assert statistics["jobs_total"] == 3
