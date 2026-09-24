"""Worker dispatche de bout en bout, sous le vrai role worker (Mission 004.3)."""
from __future__ import annotations

import io
import json
import logging
import time
import uuid
from datetime import date

import pytest

from mervio.application.persisted_analysis import analyze_snapshot
from mervio.observability.logging import configure
from mervio.persistence import audit, jobs, snapshots
from mervio.persistence.jobs import JobType
from mervio.persistence.service import authorize_service, revoke_service
from mervio.persistence.tenancy import Role, TenantContext, TenantSession, add_member, ensure_user, remove_member
from mervio.workers.handlers import HandlerRegistry, analysis_handler
from mervio.workers.worker import enqueue

from .persistence_support import SAMPLE_FILES, TEST_OBJECT_STORE, deposit_sources
from .service_support import raw
from .worker_support import DispatchedWorker, actions, enqueue_scripted, scripted_registry


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
    configure(format="json", level=logging.DEBUG, stream=buffer, environment="test")
    yield buffer
    root.handlers, root.propagate, root.level = state


def member(db, tenant, role, label):
    user_id = ensure_user(db, f"test|dispatch|{label}")
    add_member(tenant.session, user_id=user_id, role=role)
    return user_id, TenantSession(db, TenantContext(tenant.organization_id, user_id))


def test_import_then_analysis_run_as_the_service_on_behalf_of_the_analyst(db, tenant_a, principal, workers):
    authorize_service(tenant_a.session, principal.id)
    analyst_id, analyst = member(db, tenant_a, Role.ANALYST, "analyst")
    worker = workers("e2e", registry=scripted_registry(with_defaults=True), lease_seconds=30, renew_seconds=5)
    importing = enqueue(analyst, job_type=JobType.IMPORT, store_id=tenant_a.store_id, payload={
        "store_id": str(tenant_a.store_id), "connection_id": str(tenant_a.connection_id),
        "raw_objects": deposit_sources(tenant_a)})
    imported = worker.worker.run_once()
    assert imported.succeeded and imported.result["status"] == "completed"
    snapshot = snapshots.get_snapshot(tenant_a.session, tenant_a.store_id, uuid.UUID(imported.result["snapshot_id"]))
    assert snapshot.created_by == analyst_id

    trail = actions(tenant_a, importing.id)
    assert [a for a, *_ in trail] == ["job.enqueued", "job.claimed", "import.started", "job.succeeded"]
    worker_events = [t for t in trail if t[1] == "worker"]
    assert {(actor, behalf) for _, _, actor, behalf in worker_events} == {(principal.id, analyst_id)}
    assert trail[0][1:] == ("user", analyst_id, None)
    import_events = audit.list_events(tenant_a.session, correlation_id=importing.correlation_id,
                                      action=["import.succeeded"])
    assert [(e.actor_type, e.actor_id, e.on_behalf_of) for e in import_events] == [("worker", principal.id,
                                                                                   analyst_id)]

    # l'analyse: le moteur inchange, execute pour le compte de l'analyste
    real = HandlerRegistry().register(JobType.ANALYSIS, analysis_handler)
    analysis_worker = workers("e2e-analysis", registry=real, lease_seconds=30, renew_seconds=5)
    jobs.enqueue_job(analyst, job_type=JobType.ANALYSIS, store_id=tenant_a.store_id, payload={
        "store_id": str(tenant_a.store_id), "snapshot_id": str(snapshot.id), "as_of_date": "2026-09-14"})
    analysed = analysis_worker.worker.run_once()
    assert analysed.succeeded, analysed
    direct = analyze_snapshot(analyst, store_id=tenant_a.store_id, snapshot_id=snapshot.id,
                              today=date(2026, 9, 14))
    assert direct.status == "reused" and str(direct.report.id) == analysed.result["report_id"]
    assert direct.report.payload_sha256 == analysed.result["payload_sha256"]


def test_only_authorized_organizations_are_processed(tenant_a, tenant_b, principal, workers):
    authorize_service(tenant_a.session, principal.id)
    mine = {enqueue_scripted(tenant_a).id for _ in range(3)}
    foreign = {enqueue_scripted(tenant_b).id for _ in range(3)}
    outcomes = workers("isolated").worker.run_until_empty()
    assert {o.job.id for o in outcomes} == mine
    untouched = jobs.list_jobs(tenant_b.session)
    assert {j.id for j in untouched} == foreign
    assert {(j.status, j.attempts) for j in untouched} == {("queued", 0)}


def test_a_service_nobody_authorized_processes_nothing(pg, tenant_a, principal, principal2, workers):
    authorize_service(tenant_a.session, principal.id)
    enqueue_scripted(tenant_a)
    other = DispatchedWorker(pg, principal2, worker_id="unauthorized", kind="worker2")
    try:
        assert other.worker.run_until_empty() == []
        assert other.worker.recover_stale() == []
    finally:
        other.close()
    assert jobs.list_jobs(tenant_a.session)[0].status == "queued"


def test_a_revoked_organization_stops_being_served(tenant_a, principal, workers):
    authorize_service(tenant_a.session, principal.id)
    first, second = enqueue_scripted(tenant_a), enqueue_scripted(tenant_a)
    worker = workers("revoked")
    assert worker.worker.run_once().job.id == first.id
    revoke_service(tenant_a.session, principal.id)
    assert worker.worker.run_once() is None
    assert jobs.get_job(tenant_a.session, second.id).status == "queued"


@pytest.mark.parametrize("change, code", [("removed", "tenant_access_denied"), ("downgraded", "permission_denied")])
def test_a_requester_who_lost_the_right_is_not_executed_for(db, app_conn, tenant_a, principal, workers, change,
                                                            code):
    authorize_service(tenant_a.session, principal.id)
    analyst_id, analyst = member(db, tenant_a, Role.ANALYST, f"lost-{change}")
    job = enqueue_scripted(tenant_a, session=analyst, mode="quick")
    if change == "removed":
        remove_member(tenant_a.session, user_id=analyst_id)
    else:
        with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn:
            conn.execute("UPDATE memberships SET role = 'viewer' WHERE user_id = %s", (analyst_id,))
    outcome = workers(f"denied-{change}").worker.run_once()
    assert (outcome.outcome, outcome.error_code) == ("failed", code)
    stored = jobs.get_job(tenant_a.session, job.id)
    assert (stored.status, stored.last_error_code, stored.result) == ("failed", code, None)


def test_the_current_role_of_the_requester_applies_to_a_purge(db, app_conn, tenant_a, principal, workers):
    authorize_service(tenant_a.session, principal.id)
    owner2_id, owner2 = member(db, tenant_a, Role.OWNER, "purger")
    registry = scripted_registry(with_defaults=True)
    first = jobs.enqueue_job(owner2, job_type=JobType.PURGE, payload={})
    second = jobs.enqueue_job(owner2, job_type=JobType.PURGE, payload={})
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn:
        conn.execute("UPDATE memberships SET role = 'admin' WHERE user_id = %s", (owner2_id,))
    worker = workers("purge-role", registry=registry)
    outcome = worker.worker.run_once()
    assert (outcome.job.id, outcome.outcome, outcome.error_code) == (first.id, "failed", "permission_denied")
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn:
        conn.execute("UPDATE memberships SET role = 'owner' WHERE user_id = %s", (owner2_id,))
    outcome = worker.worker.run_once()
    assert (outcome.job.id, outcome.outcome) == (second.id, "succeeded")
    purge_events = audit.list_events(tenant_a.session, correlation_id=second.correlation_id,
                                     action=["purge.started", "purge.completed"])
    assert {(e.actor_id, e.on_behalf_of) for e in purge_events} == {(principal.id, owner2_id)}


def test_the_service_recovers_a_dead_worker_job_and_audits_itself(tenant_a, principal, workers):
    authorize_service(tenant_a.session, principal.id)
    job = enqueue_scripted(tenant_a)
    jobs.claim_next_job(tenant_a.session, worker_id="dead", lease_seconds=1)
    time.sleep(1.2)
    worker = workers("rescuer")
    [recovered] = worker.worker.recover_stale()
    assert recovered.id == job.id
    [event] = audit.list_events(tenant_a.session, resource_id=job.id, action=["job.recovered"])
    assert (event.actor_type, event.actor_id, event.on_behalf_of) == ("system", principal.id, None)
    outcome = worker.worker.run_once()
    assert outcome.succeeded and outcome.job.attempts == 2


def test_an_execution_produces_correlated_and_clean_logs(pg, tenant_a, principal, workers, json_logs):
    authorize_service(tenant_a.session, principal.id)
    job = enqueue_scripted(tenant_a, mode="checkpointed", seconds=1.2)
    workers("logged", lease_seconds=2, renew_seconds=0.3).worker.run_once()
    lines = [json.loads(line) for line in json_logs.getvalue().splitlines() if line.strip()]
    job_lines = [line for line in lines if line.get("job_id") == str(job.id)]
    names = [line["event"] for line in job_lines]
    assert names[0] == "job.claimed" and names[-1] == "job.succeeded"
    assert names.count("job.lease_renewed") >= 2
    assert {line["correlation_id"] for line in job_lines} == {str(job.correlation_id)}
    assert {line["worker_id"] for line in job_lines} == {"logged"}
    assert all(line["organization_id"] == str(tenant_a.organization_id) for line in job_lines)
    raw_logs = json_logs.getvalue()
    for leak in (pg.passwords["worker"], "postgresql://", str(tenant_a.owner_id) + "@"):
        assert leak not in raw_logs
