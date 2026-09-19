"""Dispatcher multi-tenant sur liste explicite d'organisations autorisees (Mission 004.3).

Pas de processus worker ici (004.3.5): la boucle des tests prend, termine et recommence,
avec les vrais depots et le vrai role worker.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from mervio.persistence import jobs
from mervio.persistence.database import Database
from mervio.persistence.dispatch import Dispatcher, expired_lease_organizations, ready_organizations
from mervio.persistence.errors import ServiceIdentityError
from mervio.persistence.jobs import JobStatus
from mervio.persistence.service import authorize_service, revoke_service

from .persistence_support import make_tenant
from .service_support import enqueue


def later(**delta):
    return datetime.now(timezone.utc) + timedelta(**delta)


@pytest.fixture
def tenants(db):
    return [make_tenant(db, name) for name in ("C1", "C2", "C3")]


def authorize(principal, *tenants):
    for tenant in tenants:
        authorize_service(tenant.session, principal.id)


def sorted_ids(*tenants):
    return sorted(tenant.organization_id for tenant in tenants)


def drain(dispatcher, *, worker_id="dispatch-test", limit=500):
    """Boucle minimale: un travail par organisation par tour, jusqu'a epuisement."""
    order = []
    while len(order) < limit:
        organizations = dispatcher.next_organizations()
        if not organizations:
            return order
        for organization_id in organizations:
            session = dispatcher.session(organization_id)
            job = jobs.claim_next_job(session, worker_id=worker_id, lease_seconds=60)
            if job is None:
                continue
            assert job.organization_id == organization_id
            jobs.mark_succeeded(session, job)
            order.append((organization_id, job.id))
    raise AssertionError("la file ne se vide pas")


def test_only_authorized_organizations_with_ready_work_are_returned(worker_db, principal, tenants, tenant_b):
    c1, c2, c3 = tenants
    authorize(principal, c1, c2, c3)
    enqueue(c1)
    enqueue(c2, available_at=later(hours=1))            # pas encore disponible
    enqueue(tenant_b)                                     # organisation non autorisee
    done = enqueue(c3)
    jobs.mark_succeeded(c3.session, jobs.claim_next_job(c3.session, worker_id="w"))  # terminee
    assert done.id
    assert ready_organizations(worker_db, principal) == [c1.organization_id]
    assert ready_organizations(worker_db, principal, now=later(hours=2)) == sorted_ids(c1, c2)


def test_an_organization_authorized_after_start_is_served_without_restart(db, worker_db, principal, tenants):
    c1, *_ = tenants
    authorize(principal, c1)
    enqueue(c1)
    dispatcher = Dispatcher(worker_db, principal)
    assert dispatcher.next_organizations() == [c1.organization_id]
    newcomer = make_tenant(db, "NEW")
    enqueue(newcomer)
    assert newcomer.organization_id not in ready_organizations(worker_db, principal)
    authorize(principal, newcomer)
    served = drain(dispatcher)
    assert {organization for organization, _ in served} == {c1.organization_id, newcomer.organization_id}


def test_a_revoked_organization_disappears_immediately(worker_db, principal, tenants):
    c1, c2, _ = tenants
    authorize(principal, c1, c2)
    enqueue(c1)
    enqueue(c2)
    revoke_service(c1.session, principal.id)
    assert ready_organizations(worker_db, principal) == [c2.organization_id]
    served = drain(Dispatcher(worker_db, principal))
    assert [organization for organization, _ in served] == [c2.organization_id]
    assert jobs.list_jobs(c1.session)[0].status == JobStatus.QUEUED.value


def test_each_service_sees_only_its_own_organizations(worker_db, worker2_db, principal, principal2, tenants):
    c1, c2, _ = tenants
    authorize(principal, c1)
    authorize(principal2, c2)
    enqueue(c1)
    enqueue(c2)
    assert ready_organizations(worker_db, principal) == [c1.organization_id]
    assert ready_organizations(worker2_db, principal2) == [c2.organization_id]


def test_a_connection_that_is_not_the_principal_is_refused(db, worker_db, principal, principal2):
    with pytest.raises(ServiceIdentityError):
        ready_organizations(worker_db, principal2)
    with pytest.raises(ServiceIdentityError):
        ready_organizations(db, principal)
    with pytest.raises(ServiceIdentityError):
        Dispatcher(db, principal).next_organizations()


def test_the_dispatcher_only_returns_organization_identifiers(worker_db, principal, tenants):
    authorize(principal, *tenants)
    for tenant in tenants:
        enqueue(tenant)
    found = ready_organizations(worker_db, principal)
    assert found == sorted_ids(*tenants)
    assert all(isinstance(organization, uuid.UUID) for organization in found)


def test_organizations_are_served_in_turn(worker_db, principal, tenants):
    authorize(principal, *tenants)
    for tenant in tenants:
        for _ in range(3):
            enqueue(tenant)
    dispatcher = Dispatcher(worker_db, principal, batch=1)
    turns = [dispatcher.next_organizations() for _ in range(7)]
    expected = sorted_ids(*tenants)
    assert turns == [[expected[i % 3]] for i in range(7)]


def test_a_batch_wraps_around_without_serving_twice(worker_db, principal, tenants):
    authorize(principal, *tenants)
    for tenant in tenants:
        enqueue(tenant)
    first, second, third = sorted_ids(*tenants)
    dispatcher = Dispatcher(worker_db, principal, batch=2)
    assert dispatcher.next_organizations() == [first, second]
    assert dispatcher.next_organizations() == [third, first]
    assert dispatcher.next_organizations() == [second, third]


def test_a_busy_organization_never_starves_the_others(db, worker_db, principal, tenant_a, owner):
    small = [make_tenant(db, f"S{i}") for i in range(5)]
    authorize(principal, tenant_a, *small)
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant_a.organization_id),))
        owner.execute("INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, payload, "
                      "available_at, enqueued_by) SELECT gen_random_uuid(), %s, 'analysis', 'queued', "
                      "gen_random_uuid(), '{}'::jsonb, now(), %s FROM generate_series(1, 200)",
                      (tenant_a.organization_id, tenant_a.owner_id))
    for tenant in small:
        enqueue(tenant)
    served = drain(Dispatcher(worker_db, principal), limit=300)
    first_pass = [organization for organization, _ in served[:6]]
    assert set(first_pass) == {tenant_a.organization_id} | {t.organization_id for t in small}
    assert len(served) == 205


def test_unauthorized_work_is_never_executed(worker_db, principal, tenants, tenant_b):
    c1, c2, c3 = tenants
    authorize(principal, c1, c2)
    expected = {enqueue(t).id for t in (c1, c1, c2)}
    foreign = {enqueue(t).id for t in (c3, tenant_b)}
    served = drain(Dispatcher(worker_db, principal))
    assert {job_id for _, job_id in served} == expected
    for tenant in (c3, tenant_b):
        assert {j.status for j in jobs.list_jobs(tenant.session)} == {JobStatus.QUEUED.value}
    assert foreign


def test_expired_leases_are_found_only_where_authorized(worker_db, principal, tenants, tenant_b):
    c1, c2, _ = tenants
    authorize(principal, c1, c2)
    for tenant in (c1, tenant_b):
        enqueue(tenant)
        jobs.claim_next_job(tenant.session, worker_id="dead", lease_seconds=30)
    enqueue(c2)
    jobs.claim_next_job(c2.session, worker_id="alive", lease_seconds=3600)
    assert expired_lease_organizations(worker_db, principal) == []
    assert expired_lease_organizations(worker_db, principal, now=later(minutes=5)) == [c1.organization_id]
    dispatcher = Dispatcher(worker_db, principal)
    [organization] = dispatcher.expired_organizations(now=later(minutes=5))
    recovered = jobs.recover_stale_jobs(dispatcher.session(organization), now=later(minutes=5))
    assert [job.organization_id for job in recovered] == [c1.organization_id]


@pytest.mark.parametrize("limit", [0, -1, 1001, "5"])
def test_the_batch_size_is_bounded(worker_db, principal, limit):
    with pytest.raises(ValueError):
        ready_organizations(worker_db, principal, limit=limit)
    with pytest.raises(ValueError):
        Dispatcher(worker_db, principal, batch=limit)


def test_two_dispatchers_never_execute_a_job_twice(pg, worker_db, principal, tenants):
    authorize(principal, *tenants)
    expected = {enqueue(tenant).id for tenant in tenants for _ in range(20)}
    results, errors = [], []

    def run(name):
        database = Database(pg.url("worker"))
        try:
            results.extend(drain(Dispatcher(database, principal), worker_id=name))
        except BaseException as exc:  # noqa: BLE001 - remonte dans le fil principal
            errors.append(exc)
        finally:
            database.close()

    threads = [threading.Thread(target=run, args=(f"dispatcher-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    executed = [job_id for _, job_id in results]
    assert sorted(executed) == sorted(expected)
    assert len(executed) == len(set(executed)) == 60


def _fill(owner, tenant, jobs_count, status="queued"):
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        owner.execute("INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, payload, "
                      "available_at) SELECT gen_random_uuid(), %s, 'analysis', 'queued', gen_random_uuid(), "
                      "'{}'::jsonb, now() FROM generate_series(1, %s)", (tenant.organization_id, jobs_count))


def _probe(worker_db, statement, params):
    with worker_db.transaction() as conn:
        plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, params).fetchone()[0][0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    job_scans = [node for node in nodes(plan) if node.get("Relation Name") == "jobs"]
    return job_scans, plan["Shared Hit Blocks"] + plan["Shared Read Blocks"]


def test_the_dispatch_probe_cost_does_not_grow_with_queue_depth(db, worker_db, principal, tenant_a, owner):
    """Files profondes (3 000 puis 6 000 travaux par organisation): la sonde suit un index ordonne
    (aucun bitmap, aucun tri de `jobs`) et s'arrete au premier travail disponible; les blocs lus ne
    croissent pas avec la file.

    Avant la revision 0012, cette assertion portait sur le nom `jobs_ready_idx` et echouait
    aleatoirement (~2,7 %): sous RLS, le planificateur basculait vers un bitmap sur `jobs_tenant_key`
    suivi d'un tri. La propriete verifiee est desormais le comportement (voir
    test_dispatch_probe_plan.py, y compris la stabilite sur des ordres physiques aleatoires)."""
    from mervio.persistence.dispatch import _READY_SQL

    from .test_dispatch_probe_plan import MAX_PROBE_BLOCKS, _index_driven_on, _jobs_blocks, _plan
    others = [make_tenant(db, f"P{i}") for i in range(10)]
    authorize(principal, tenant_a, *others)
    measures = []
    for _ in range(2):
        for tenant in (tenant_a, *others):
            _fill(owner, tenant, 3000)
        owner.execute("ANALYZE jobs")
        plan = _plan(worker_db, _READY_SQL, (principal.id, None, None, None, 50))
        _index_driven_on(plan)
        blocks = _jobs_blocks(plan)
        assert blocks <= MAX_PROBE_BLOCKS, blocks
        measures.append(blocks)
    shallow, deep = measures
    assert deep <= shallow + 20, measures
    assert deep < 150, measures


def test_the_expired_lease_probe_uses_the_lease_index(db, worker_db, principal, tenant_a, owner):
    others = [make_tenant(db, f"L{i}") for i in range(10)]
    authorize(principal, tenant_a, *others)
    for tenant in (tenant_a, *others):
        _fill(owner, tenant, 500)
        for _ in range(3):
            jobs.claim_next_job(tenant.session, worker_id="dead", lease_seconds=30)
    owner.execute("ANALYZE jobs")
    from mervio.persistence.dispatch import _EXPIRED_SQL
    job_scans, _ = _probe(worker_db, _EXPIRED_SQL, (principal.id, None, None, later(minutes=5), 50))
    assert job_scans and all(node["Node Type"] != "Seq Scan" for node in job_scans), job_scans
