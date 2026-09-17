"""Identite de service, autorisations explicites et delegation (Mission 004.3, revision 0007).

Chaque propriete de securite est prouvee au niveau PostgreSQL: en SQL brut sous le role
worker, avec un contexte forge, et meme quand la verification Python est contournee.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from mervio.application.persisted_analysis import SnapshotImportRequest, import_csv_snapshot
from mervio.persistence import audit, jobs, migrate, service
from mervio.persistence.audit import Action, ActorType, Outcome, ResourceType
from mervio.persistence.database import Database
from mervio.persistence.errors import (
    NotFound, PermissionDenied, ServiceIdentityError, TenantAccessDenied, UnsafeDatabaseConfiguration,
)
from mervio.persistence.jobs import JobStatus, JobType
from mervio.persistence.service import ServiceSession, authorize_service, delegated_session, revoke_service
from mervio.persistence.tenancy import (
    Permission, Role, TenantContext, TenantSession, add_member, ensure_user, remove_member,
)

from .persistence_support import SAMPLE_FILES
from .service_support import PAST, count, enqueue, raw

WORKER_ID = "service-worker-1"


def member(db, tenant, role, label):
    user_id = ensure_user(db, f"test|{label}|{role.value}")
    add_member(tenant.session, user_id=user_id, role=role)
    return user_id, TenantSession(db, TenantContext(tenant.organization_id, user_id))


def set_role(app_conn, tenant, user_id, role):
    with raw(app_conn, tenant.organization_id, tenant.owner_id) as conn:
        assert conn.execute("UPDATE memberships SET role = %s WHERE organization_id = %s AND user_id = %s",
                            (role, tenant.organization_id, user_id)).rowcount == 1


def import_job(tenant, session=None):
    return jobs.enqueue_job(session or tenant.session, job_type=JobType.IMPORT, store_id=tenant.store_id,
                            payload={"store_id": str(tenant.store_id), "connection_id": str(tenant.connection_id)})


@pytest.fixture
def authorized_a(tenant_a, principal):
    return authorize_service(tenant_a.session, principal.id)


@pytest.fixture
def temporary_role(pg):
    """Role de connexion jetable, supprime apres le test."""
    created = []

    def make(options: str):
        name = f"mervio_test_tmp{len(created)}_{pg.suffix}"
        password = secrets.token_hex(16)
        with pg.admin() as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} " + options).format(
                sql.Identifier(name), sql.Literal(password)))
        created.append(name)
        return make_conninfo(host=pg.host, port=pg.port, dbname=pg.database, user=name, password=password), name

    yield make
    with pg.admin() as conn:
        for name in created:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


# =============================================================================================
# Identite
# =============================================================================================

def test_the_principal_is_derived_from_the_connected_role(pg, principal, worker_db):
    assert principal.subject == f"service:{pg.role('worker')}"
    assert principal.name == pg.role("worker")
    assert service.service_principal(worker_db) == principal  # idempotent
    with worker_db.transaction() as conn:
        assert conn.execute("SELECT kind FROM users WHERE id = %s", (principal.id,)).fetchone() == ("service",)


def test_two_service_roles_are_two_principals(principal, principal2):
    assert principal.id != principal2.id and principal.subject != principal2.subject


def test_an_application_role_is_not_a_service(db, owner):
    with pytest.raises(ServiceIdentityError):
        service.service_principal(db)
    assert owner.execute("SELECT count(*) FROM users WHERE kind = 'service'").fetchone()[0] == 0


def test_a_member_without_inheritance_is_not_a_service(temporary_role):
    url, _ = temporary_role("NOSUPERUSER NOBYPASSRLS NOINHERIT IN ROLE mervio_app, mervio_worker")
    with Database(url) as database, pytest.raises(ServiceIdentityError):
        service.service_principal(database)


def test_a_worker_role_with_bypassrls_is_refused_before_anything(temporary_role):
    url, _ = temporary_role("NOSUPERUSER BYPASSRLS IN ROLE mervio_app, mervio_worker")
    with Database(url) as database, pytest.raises(UnsafeDatabaseConfiguration):
        service.service_principal(database)


def test_worker_roles_are_neither_superuser_nor_bypassrls(pg, owner):
    rows = dict((name, (superuser, bypass, login)) for name, superuser, bypass, login in owner.execute(
        "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = ANY(%s)",
        (["mervio_worker", pg.role("worker"), pg.role("worker2")],)).fetchall())
    assert rows["mervio_worker"] == (False, False, False)
    assert rows[pg.role("worker")] == (False, False, True)
    assert rows[pg.role("worker2")] == (False, False, True)


def test_the_application_cannot_create_a_service_principal(app_conn):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("INSERT INTO users (id, idp_subject, kind) VALUES (%s, 'service:forged', 'service')",
                         (uuid.uuid4(),))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("SELECT app_ensure_service_principal()")


def test_a_human_subject_cannot_take_the_service_prefix(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        ensure_user(db, "service:mervio_worker_login")


def test_the_service_identity_cannot_be_forged_by_a_setting(app_conn, worker_conn, principal, principal2):
    with raw(app_conn, user_id=principal.id) as conn:
        assert conn.execute("SELECT app_current_service_id()").fetchone()[0] is None
    with raw(worker_conn, user_id=principal2.id) as conn:
        assert conn.execute("SELECT app_current_service_id()").fetchone()[0] == principal.id


def test_a_service_principal_is_never_a_member(app_conn, tenant_a, principal):
    with pytest.raises(psycopg.errors.RestrictViolation):
        add_member(tenant_a.session, user_id=principal.id, role=Role.OWNER)
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn, \
            pytest.raises(psycopg.errors.RestrictViolation):
        conn.execute("INSERT INTO memberships (id, organization_id, user_id, role) VALUES (%s, %s, %s, 'owner')",
                     (uuid.uuid4(), tenant_a.organization_id, principal.id))


def test_a_service_is_found_by_its_name(db, principal):
    assert service.find_service(db, principal.name) == principal.id
    with pytest.raises(NotFound):
        service.find_service(db, "absent")


# =============================================================================================
# Autorisations explicites
# =============================================================================================

def test_an_owner_authorizes_a_service_idempotently(tenant_a, principal):
    first = authorize_service(tenant_a.session, principal.id)
    second = authorize_service(tenant_a.session, principal.id)
    assert first == second and first.active
    assert (first.organization_id, first.service_user_id, first.granted_by) == (
        tenant_a.organization_id, principal.id, tenant_a.owner_id)


@pytest.mark.parametrize("role", [Role.ADMIN, Role.ANALYST, Role.VIEWER])
def test_only_an_owner_manages_services(db, tenant_a, principal, role):
    _, session = member(db, tenant_a, role, "manage")
    with pytest.raises(PermissionDenied):
        authorize_service(session, principal.id)
    with pytest.raises(PermissionDenied):
        revoke_service(session, principal.id)


def test_only_a_service_principal_can_be_authorized(app_conn, tenant_a):
    with pytest.raises(NotFound):
        authorize_service(tenant_a.session, tenant_a.owner_id)
    with pytest.raises(NotFound):
        authorize_service(tenant_a.session, uuid.uuid4())
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn, \
            pytest.raises(psycopg.errors.RestrictViolation):
        conn.execute("INSERT INTO service_authorizations (id, organization_id, service_user_id, granted_by) "
                     "VALUES (%s, %s, %s, %s)",
                     (uuid.uuid4(), tenant_a.organization_id, tenant_a.owner_id, tenant_a.owner_id))


def test_only_a_human_grants_an_authorization(app_conn, tenant_a, principal, principal2):
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn, \
            pytest.raises(psycopg.errors.RestrictViolation):
        conn.execute("INSERT INTO service_authorizations (id, organization_id, service_user_id, granted_by) "
                     "VALUES (%s, %s, %s, %s)", (uuid.uuid4(), tenant_a.organization_id, principal.id, principal2.id))


def test_a_revocation_is_traced_and_history_is_kept(tenant_a, principal):
    granted = authorize_service(tenant_a.session, principal.id)
    revoked = revoke_service(tenant_a.session, principal.id)
    assert revoked.id == granted.id and not revoked.active and revoked.revoked_by == tenant_a.owner_id
    with pytest.raises(NotFound):
        revoke_service(tenant_a.session, principal.id)
    again = authorize_service(tenant_a.session, principal.id)
    assert again.id != granted.id and again.active
    history = service.list_service_authorizations(tenant_a.session, include_revoked=True)
    assert [a.id for a in history] == [granted.id, again.id]
    assert [a.id for a in service.list_service_authorizations(tenant_a.session)] == [again.id]


def test_an_authorization_only_changes_by_being_revoked(app_conn, tenant_a, principal):
    granted = authorize_service(tenant_a.session, principal.id)
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE service_authorizations SET granted_by = %s WHERE id = %s", (tenant_a.owner_id, granted.id))
    revoke_service(tenant_a.session, principal.id)
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn, \
            pytest.raises(psycopg.errors.RestrictViolation):
        conn.execute("UPDATE service_authorizations SET revoked_at = NULL, revoked_by = NULL WHERE id = %s",
                     (granted.id,))
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM service_authorizations WHERE id = %s", (granted.id,))


def test_authorizations_are_tenant_scoped(tenant_a, tenant_b, principal):
    authorize_service(tenant_a.session, principal.id)
    assert service.list_service_authorizations(tenant_b.session) == []


def test_the_explicit_list_follows_grants_and_revocations(worker_db, tenant_a, tenant_b, principal):
    assert service.authorized_organizations(worker_db, principal) == []
    authorize_service(tenant_a.session, principal.id)
    assert service.authorized_organizations(worker_db, principal) == [tenant_a.organization_id]
    authorize_service(tenant_b.session, principal.id)
    assert service.authorized_organizations(worker_db, principal) == sorted(
        [tenant_a.organization_id, tenant_b.organization_id])
    revoke_service(tenant_a.session, principal.id)
    assert service.authorized_organizations(worker_db, principal) == [tenant_b.organization_id]


def test_a_service_never_sees_another_service_list(worker_db, worker2_db, tenant_a, principal, principal2):
    authorize_service(tenant_a.session, principal.id)
    assert service.authorized_organizations(worker2_db, principal2) == []
    with pytest.raises(ServiceIdentityError):
        service.authorized_organizations(worker_db, principal2)


def test_a_service_cannot_authorize_itself(worker_conn, tenant_a, principal, authorized_a):
    with raw(worker_conn, tenant_a.organization_id) as conn, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("INSERT INTO service_authorizations (id, organization_id, service_user_id, granted_by) "
                     "VALUES (%s, %s, %s, %s)", (uuid.uuid4(), tenant_a.organization_id, principal.id,
                                                  tenant_a.owner_id))
    with raw(worker_conn, tenant_a.organization_id) as conn, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE service_authorizations SET revoked_at = now(), revoked_by = %s", (tenant_a.owner_id,))
    assert service.list_service_authorizations(tenant_a.session)[0].active


# =============================================================================================
# ServiceSession
# =============================================================================================

def test_an_authorized_service_runs_the_job_lifecycle(worker_db, tenant_a, principal, authorized_a):
    job = enqueue(tenant_a)
    session = ServiceSession(worker_db, tenant_a.organization_id, principal)
    claimed = jobs.claim_next_job(session, worker_id=WORKER_ID, lease_seconds=60)
    assert claimed.id == job.id and claimed.enqueued_by == tenant_a.owner_id
    renewed = jobs.renew_lease(session, claimed, lease_seconds=120)
    done = jobs.mark_succeeded(session, renewed, result={"ok": True})
    assert done.status == JobStatus.SUCCEEDED.value
    assert jobs.get_job(session, job.id).status == JobStatus.SUCCEEDED.value


def test_an_unauthorized_organization_is_out_of_reach(worker_db, tenant_a, tenant_b, principal, authorized_a):
    job_b = enqueue(tenant_b)
    session_b = ServiceSession(worker_db, tenant_b.organization_id, principal)
    with pytest.raises(TenantAccessDenied):
        jobs.claim_next_job(session_b, worker_id=WORKER_ID)
    with pytest.raises(TenantAccessDenied):
        jobs.get_job(session_b, job_b.id)
    assert jobs.get_job(tenant_b.session, job_b.id).status == JobStatus.QUEUED.value


def test_a_forged_organization_is_indistinguishable_from_an_unauthorized_one(worker_db, tenant_b, principal):
    for organization_id in (tenant_b.organization_id, uuid.uuid4()):
        session = ServiceSession(worker_db, organization_id, principal)
        with pytest.raises(TenantAccessDenied) as error:
            jobs.list_jobs(session)
        assert str(error.value) == "organization introuvable"


def test_a_job_of_another_tenant_is_not_found_like_a_missing_one(worker_db, tenant_a, tenant_b, principal,
                                                                 authorized_a):
    authorize_service(tenant_b.session, principal.id)
    job_b = enqueue(tenant_b)
    session_a = ServiceSession(worker_db, tenant_a.organization_id, principal)
    errors = []
    for job_id in (job_b.id, uuid.uuid4()):
        with pytest.raises(NotFound) as error:
            jobs.get_job(session_a, job_id)
        errors.append((type(error.value), str(error.value)))
        assert jobs.claim_next_job(session_a, worker_id=WORKER_ID, job_id=job_id) is None
    assert errors[0] == errors[1]


def test_a_session_bound_to_another_principal_is_refused(worker_db, tenant_a, principal2, authorized_a):
    with pytest.raises(ServiceIdentityError):
        jobs.list_jobs(ServiceSession(worker_db, tenant_a.organization_id, principal2))


@pytest.mark.parametrize("permission", sorted(set(Permission) - service.SERVICE_PERMISSIONS, key=lambda p: p.value))
def test_a_service_session_has_no_business_permission(worker_db, tenant_a, principal, authorized_a, permission):
    session = ServiceSession(worker_db, tenant_a.organization_id, principal)
    with pytest.raises(PermissionDenied):
        with session.transaction(permission):
            pass


def test_a_service_never_enqueues_and_has_no_role(worker_db, tenant_a, principal, authorized_a):
    session = ServiceSession(worker_db, tenant_a.organization_id, principal)
    with pytest.raises(PermissionDenied):
        enqueue(tenant_a, session=session)
    with pytest.raises(PermissionDenied):
        session.role()


def test_a_revocation_stops_the_service_at_its_next_statement(worker_db, tenant_a, principal, authorized_a):
    enqueue(tenant_a)
    session = ServiceSession(worker_db, tenant_a.organization_id, principal)
    claimed = jobs.claim_next_job(session, worker_id=WORKER_ID, lease_seconds=60)
    revoke_service(tenant_a.session, principal.id)
    with pytest.raises(TenantAccessDenied):
        jobs.mark_succeeded(session, claimed)
    with pytest.raises(TenantAccessDenied):
        jobs.renew_lease(session, claimed)
    assert jobs.get_job(tenant_a.session, claimed.id).status == JobStatus.RUNNING.value


# =============================================================================================
# SQL brut sous le role worker
# =============================================================================================

TENANT_READS = ("jobs", "audit_events", "memberships", "stores", "connections", "organizations",
                "data_snapshots", "orders", "reports", "analysis_runs", "service_authorizations")


def test_a_forged_context_reads_nothing(worker_conn, tenant_a, tenant_b, principal, authorized_a):
    enqueue(tenant_b)
    for table in TENANT_READS:
        assert count(worker_conn, table, tenant_b.organization_id, tenant_b.owner_id) == 0, table
        assert count(worker_conn, table, uuid.uuid4(), tenant_b.owner_id) == 0, table


def test_a_forged_context_writes_nothing(worker_conn, tenant_a, tenant_b, principal, authorized_a):
    job_b = enqueue(tenant_b)
    with raw(worker_conn, tenant_b.organization_id, tenant_b.owner_id) as conn:
        assert conn.execute("UPDATE jobs SET priority = 5 WHERE id = %s", (job_b.id,)).rowcount == 0
        assert conn.execute("DELETE FROM jobs WHERE id = %s", (job_b.id,)).rowcount == 0
    with raw(worker_conn, tenant_b.organization_id, tenant_b.owner_id) as conn, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        audit.record(conn, organization_id=tenant_b.organization_id, action=Action.JOB_CLAIMED,
                     resource_type=ResourceType.JOB, resource_id=job_b.id, correlation_id=job_b.correlation_id,
                     actor_type=ActorType.WORKER, actor_id=principal.id)
    assert jobs.get_job(tenant_b.session, job_b.id).priority == 0


def test_without_a_delegate_the_service_reads_no_business_data(worker_conn, tenant_a, principal, authorized_a):
    import_csv_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                        request=SnapshotImportRequest(**SAMPLE_FILES))
    enqueue(tenant_a)
    assert count(worker_conn, "jobs", tenant_a.organization_id) == 1
    assert count(worker_conn, "stores", tenant_a.organization_id) == 1
    for table in ("data_snapshots", "snapshot_sources", "orders", "order_lines", "products", "payments"):
        assert count(worker_conn, table, tenant_a.organization_id) == 0, table
        assert count(worker_conn, table, tenant_a.organization_id, tenant_a.owner_id) > 0, table


def test_without_a_context_the_service_sees_only_its_open_work(worker_conn, tenant_a, tenant_b, principal,
                                                               principal2, authorized_a):
    authorize_service(tenant_b.session, principal2.id)
    first, second, third = enqueue(tenant_a), enqueue(tenant_a), enqueue(tenant_a)
    done = jobs.claim_next_job(tenant_a.session, worker_id="w", lease_seconds=60)
    jobs.mark_succeeded(tenant_a.session, done)
    running = jobs.claim_next_job(tenant_a.session, worker_id="w", lease_seconds=60)
    assert (done.id, running.id) == (first.id, second.id)
    enqueue(tenant_b)
    with raw(worker_conn) as conn:
        visible = {row[0] for row in conn.execute("SELECT id FROM jobs").fetchall()}
        grants = conn.execute("SELECT service_user_id FROM service_authorizations").fetchall()
        assert conn.execute("SELECT count(*) FROM memberships").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM stores").fetchone()[0] == 0
    # en cours et en file de A; ni le travail termine de A, ni rien de B
    assert visible == {second.id, third.id}
    assert grants == [(principal.id,)]


def test_in_context_a_service_sees_only_its_own_authorization(worker_conn, tenant_a, principal, principal2,
                                                              authorized_a):
    authorize_service(tenant_a.session, principal2.id)
    with raw(worker_conn, tenant_a.organization_id) as conn:
        assert conn.execute("SELECT service_user_id FROM service_authorizations").fetchall() == [(principal.id,)]


@pytest.mark.parametrize("statement", [
    "INSERT INTO memberships (id, organization_id, user_id, role) VALUES (gen_random_uuid(), {org}, {owner}, 'viewer')",
    "UPDATE memberships SET role = 'viewer'",
    "DELETE FROM memberships",
    "UPDATE stores SET name = 'x'",
    "UPDATE connections SET label = 'x'",
    "UPDATE organizations SET name = 'x'",
    "INSERT INTO stores (id, organization_id, name) VALUES (gen_random_uuid(), {org}, 'x')",
    "INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, payload, available_at) "
    "VALUES (gen_random_uuid(), {org}, 'analysis', 'queued', gen_random_uuid(), '{{}}'::jsonb, now())",
])
def test_a_service_never_writes_tenancy_nor_creates_jobs(worker_conn, tenant_a, principal, authorized_a, statement):
    text = statement.format(org=f"'{tenant_a.organization_id}'", owner=f"'{tenant_a.owner_id}'")
    with raw(worker_conn, tenant_a.organization_id, tenant_a.owner_id) as conn:
        try:
            changed = conn.execute(text).rowcount
        except psycopg.errors.InsufficientPrivilege:
            changed = None
    assert changed in (None, 0), text
    assert count(worker_conn, "memberships", tenant_a.organization_id) == 1


def _finished_in_the_past(tenant):
    enqueue(tenant, available_at=PAST)
    job = jobs.claim_next_job(tenant.session, worker_id="w", now=PAST)
    return jobs.mark_succeeded(tenant.session, job, now=PAST + timedelta(seconds=1))


def test_only_an_owner_delegate_lets_the_service_purge(db, worker_conn, tenant_a, principal, authorized_a):
    job = _finished_in_the_past(tenant_a)
    admin_id, _ = member(db, tenant_a, Role.ADMIN, "purge")
    for delegate in (None, admin_id, principal.id):
        with raw(worker_conn, tenant_a.organization_id, delegate) as conn:
            assert conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,)).rowcount == 0
    with raw(worker_conn, tenant_a.organization_id, tenant_a.owner_id) as conn:
        assert conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,)).rowcount == 1


def test_a_revocation_hides_everything_immediately(worker_conn, tenant_a, principal, authorized_a):
    enqueue(tenant_a)
    assert count(worker_conn, "jobs", tenant_a.organization_id) == 1
    revoke_service(tenant_a.session, principal.id)
    assert count(worker_conn, "jobs", tenant_a.organization_id) == 0
    assert count(worker_conn, "jobs") == 0


# =============================================================================================
# Delegation: autorisation relue a l'execution
# =============================================================================================

def test_the_delegated_session_is_the_requester(worker_db, tenant_a, principal, authorized_a):
    job = import_job(tenant_a)
    session = delegated_session(ServiceSession(worker_db, tenant_a.organization_id, principal), job)
    assert (session.organization_id, session.user_id) == (tenant_a.organization_id, tenant_a.owner_id)
    assert session.database is worker_db


def test_a_requester_removed_after_enqueue_cannot_be_executed_for(db, worker_db, tenant_a, principal, authorized_a):
    analyst_id, analyst = member(db, tenant_a, Role.ANALYST, "removed")
    job = import_job(tenant_a, session=analyst)
    remove_member(tenant_a.session, user_id=analyst_id)
    with pytest.raises(TenantAccessDenied):
        delegated_session(ServiceSession(worker_db, tenant_a.organization_id, principal), job)


def test_a_requester_downgraded_after_enqueue_cannot_be_executed_for(db, app_conn, worker_db, tenant_a, principal,
                                                                     authorized_a):
    analyst_id, analyst = member(db, tenant_a, Role.ANALYST, "downgraded")
    job = import_job(tenant_a, session=analyst)
    set_role(app_conn, tenant_a, analyst_id, "viewer")
    with pytest.raises(PermissionDenied):
        delegated_session(ServiceSession(worker_db, tenant_a.organization_id, principal), job)


def test_the_current_role_applies_in_both_directions(db, app_conn, worker_db, tenant_a, principal, authorized_a):
    owner2_id, owner2 = member(db, tenant_a, Role.OWNER, "purger")
    job = jobs.enqueue_job(owner2, job_type=JobType.PURGE, payload={})
    service_session = ServiceSession(worker_db, tenant_a.organization_id, principal)
    set_role(app_conn, tenant_a, owner2_id, "admin")
    with pytest.raises(PermissionDenied):
        delegated_session(service_session, job)
    set_role(app_conn, tenant_a, owner2_id, "owner")
    assert delegated_session(service_session, job).user_id == owner2_id


def test_the_delegated_session_rechecks_at_every_transaction(db, app_conn, worker_db, tenant_a, principal,
                                                             authorized_a):
    analyst_id, analyst = member(db, tenant_a, Role.ANALYST, "later")
    job = import_job(tenant_a, session=analyst)
    session = delegated_session(ServiceSession(worker_db, tenant_a.organization_id, principal), job)
    set_role(app_conn, tenant_a, analyst_id, "viewer")
    with pytest.raises(PermissionDenied):
        with session.transaction(Permission.IMPORT_DATA):
            pass


def test_a_job_without_requester_is_never_executed(owner, worker_db, tenant_a, principal, authorized_a):
    with raw(owner, tenant_a.organization_id) as conn:
        conn.execute("INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, payload, available_at) "
                     "VALUES (gen_random_uuid(), %s, 'analysis', 'queued', gen_random_uuid(), '{}'::jsonb, now())",
                     (tenant_a.organization_id,))
    session = ServiceSession(worker_db, tenant_a.organization_id, principal)
    job = jobs.claim_next_job(session, worker_id=WORKER_ID)
    assert job.enqueued_by is None
    with pytest.raises(PermissionDenied):
        delegated_session(session, job)


def test_a_job_of_another_organization_is_refused_by_the_delegation(worker_db, tenant_a, tenant_b, principal,
                                                                    authorized_a):
    job_b = import_job(tenant_b)
    with pytest.raises(NotFound):
        delegated_session(ServiceSession(worker_db, tenant_a.organization_id, principal), job_b)


class UncheckedSession(TenantSession):
    """Session dont la verification Python est volontairement contournee."""

    def transaction(self, permission):
        return self.database.transaction(organization_id=self.organization_id, user_id=self.user_id)


@pytest.mark.parametrize("delegate", ["removed", "viewer", "service", "nobody"])
def test_the_database_refuses_business_writes_without_a_valid_delegate(db, worker_db, tenant_a, principal,
                                                                       authorized_a, delegate):
    """Meme si le code Python oublie la verification, la base refuse l'import."""
    if delegate == "removed":
        user_id, _ = member(db, tenant_a, Role.ANALYST, "gone")
        remove_member(tenant_a.session, user_id=user_id)
    elif delegate == "viewer":
        user_id, _ = member(db, tenant_a, Role.VIEWER, "reader")
    elif delegate == "service":
        user_id = principal.id
    else:
        user_id = uuid.uuid4()
    session = UncheckedSession(worker_db, TenantContext(tenant_a.organization_id, user_id))
    with pytest.raises((psycopg.errors.InsufficientPrivilege, NotFound)):
        import_csv_snapshot(session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                            request=SnapshotImportRequest(**SAMPLE_FILES))
    assert count(worker_db.connection(), "data_snapshots", tenant_a.organization_id, tenant_a.owner_id) == 0


def test_the_service_imports_on_behalf_of_an_analyst(db, worker_db, tenant_a, principal, authorized_a):
    analyst_id, analyst = member(db, tenant_a, Role.ANALYST, "importer")
    job = import_job(tenant_a, session=analyst)
    session = delegated_session(ServiceSession(worker_db, tenant_a.organization_id, principal), job)
    result = import_csv_snapshot(session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                                 request=SnapshotImportRequest(**SAMPLE_FILES))
    assert result.status == "completed"
    assert result.snapshot.created_by == analyst_id


# =============================================================================================
# Audit: acteur technique et acteur metier
# =============================================================================================

def worker_event(conn, tenant, job, **fields):
    values = dict(organization_id=tenant.organization_id, action=Action.JOB_CLAIMED, resource_type=ResourceType.JOB,
                  resource_id=job.id, correlation_id=job.correlation_id, outcome=Outcome.STARTED,
                  actor_type=ActorType.WORKER)
    values.update(fields)
    return audit.record(conn, **values)


def test_the_service_audits_as_itself_on_behalf_of_the_requester(db, worker_conn, tenant_a, principal, authorized_a):
    analyst_id, analyst = member(db, tenant_a, Role.ANALYST, "requester")
    job = enqueue(tenant_a, session=analyst)
    with raw(worker_conn, tenant_a.organization_id) as conn:
        event = worker_event(conn, tenant_a, job, actor_id=principal.id, on_behalf_of=analyst_id)
    assert (event.actor_type, event.actor_id, event.on_behalf_of) == ("worker", principal.id, analyst_id)
    [read] = audit.list_events(tenant_a.session, resource_id=job.id, action=[Action.JOB_CLAIMED])
    assert (read.actor_type, read.actor_id, read.on_behalf_of) == ("worker", principal.id, analyst_id)
    assert job.enqueued_by == analyst_id


def test_the_delegate_context_may_be_named_as_the_business_actor(worker_conn, tenant_a, principal, authorized_a):
    job = enqueue(tenant_a)
    with raw(worker_conn, tenant_a.organization_id, tenant_a.owner_id) as conn:
        event = audit.record(conn, organization_id=tenant_a.organization_id, action=Action.IMPORT_STARTED,
                             resource_type=ResourceType.SNAPSHOT, resource_id=uuid.uuid4(),
                             correlation_id=job.correlation_id, actor_type=ActorType.WORKER,
                             actor_id=principal.id, on_behalf_of=tenant_a.owner_id)
    assert event.on_behalf_of == tenant_a.owner_id


@pytest.mark.parametrize("case", ["user_actor", "human_actor", "other_service", "no_actor", "forged_behalf",
                                  "system_as_human"])
def test_the_service_cannot_forge_audit_actors(db, worker_conn, tenant_a, principal, principal2, authorized_a, case):
    other_id, _ = member(db, tenant_a, Role.ADMIN, "bystander")
    job = enqueue(tenant_a)
    fields = {
        "user_actor": dict(actor_type=ActorType.USER, actor_id=tenant_a.owner_id),
        "human_actor": dict(actor_id=tenant_a.owner_id),
        "other_service": dict(actor_id=principal2.id),
        "no_actor": dict(actor_id=None),
        "forged_behalf": dict(actor_id=principal.id, on_behalf_of=other_id),
        "system_as_human": dict(actor_type=ActorType.SYSTEM, actor_id=tenant_a.owner_id),
    }[case]
    with raw(worker_conn, tenant_a.organization_id) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        worker_event(conn, tenant_a, job, **fields)


def test_nobody_acts_on_behalf_of_a_service(app_conn, tenant_a, principal):
    job = enqueue(tenant_a)
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn, \
            pytest.raises(psycopg.errors.RestrictViolation):
        worker_event(conn, tenant_a, job, actor_id=tenant_a.owner_id, on_behalf_of=principal.id)


def test_a_user_event_never_names_a_service(app_conn, tenant_a, principal):
    job = enqueue(tenant_a)
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn, \
            pytest.raises(psycopg.errors.RestrictViolation):
        worker_event(conn, tenant_a, job, actor_type=ActorType.USER, actor_id=principal.id)


def test_a_job_is_always_requested_by_a_human(owner, tenant_a, principal):
    with raw(owner, tenant_a.organization_id) as conn, pytest.raises(psycopg.errors.RestrictViolation):
        conn.execute("INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, payload, "
                     "available_at, enqueued_by) VALUES (gen_random_uuid(), %s, 'analysis', 'queued', "
                     "gen_random_uuid(), '{}'::jsonb, now(), %s)", (tenant_a.organization_id, principal.id))


def test_the_audit_record_without_business_actor_is_unchanged(tenant_a):
    event = audit.record_event(tenant_a.session, action=Action.JOB_RECOVERED, resource_type=ResourceType.JOB,
                               resource_id=uuid.uuid4(), correlation_id=uuid.uuid4())
    assert event.on_behalf_of is None


# =============================================================================================
# Plans et migration
# =============================================================================================

def test_the_claim_keeps_its_index_under_the_worker_role(worker_db, tenant_a, principal, authorized_a, owner):
    with raw(owner, tenant_a.organization_id) as conn:
        conn.execute("INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, payload, available_at) "
                     "SELECT gen_random_uuid(), %s, 'analysis', 'queued', gen_random_uuid(), '{}'::jsonb, now() "
                     "FROM generate_series(1, 3000)", (tenant_a.organization_id,))
        conn.execute("ANALYZE jobs")
    plan = jobs.explain_claim(ServiceSession(worker_db, tenant_a.organization_id, principal))
    job_scans = [node for node in _nodes(plan) if node.get("Relation Name") == "jobs"]
    assert job_scans and all(node["Node Type"] != "Seq Scan" for node in job_scans), job_scans
    assert "jobs_ready_idx" in {node.get("Index Name") for node in job_scans}
    assert "Sort" not in {node["Node Type"] for node in _nodes(plan)}


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def _fingerprint(url):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "ci_migrations.py"
    import sys
    spec = importlib.util.spec_from_file_location("mervio_ci_migrations_fp", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.PostgresTarget(url).fingerprint()


def test_0007_downgrades_to_the_exact_0006_schema_and_upgrades_again(pg, owner):
    reference = pg.create_empty_database("svc_ref")
    subject = pg.create_empty_database("svc_down")
    try:
        reference_url, url = pg.url("migrator", reference), pg.url("migrator", subject)
        migrate.upgrade(reference_url, "0006_job_leases")
        migrate.upgrade(url, "0007_service_identity")
        head = _fingerprint(url)
        migrate.downgrade(url, "0006_job_leases")
        assert migrate.current_revision(url) == "0006_job_leases"
        assert _fingerprint(url) == _fingerprint(reference_url)
        migrate.upgrade(url, "0007_service_identity")
        assert migrate.current_revision(url) == "0007_service_identity"
        assert _fingerprint(url) == head
        # 004.3.7: 0007 n'est plus la tete; remontee complete, puis schema identique a la base de session
        migrate.upgrade(url)
        assert _fingerprint(url) == _fingerprint(pg.url("migrator"))
        # le role de groupe est global au cluster: jamais supprime par une descente
        assert owner.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'mervio_worker'").fetchone()[0] == 1
    finally:
        pg.drop_database(reference)
        pg.drop_database(subject)


def test_a_role_removed_from_the_worker_group_loses_its_service_identity(pg, temporary_role, tenant_a):
    url, name = temporary_role("NOSUPERUSER NOBYPASSRLS IN ROLE mervio_app, mervio_worker")
    with Database(url) as database:
        principal = service.service_principal(database)
        authorize_service(tenant_a.session, principal.id)
        assert service.authorized_organizations(database, principal) == [tenant_a.organization_id]
        with pg.admin() as conn:
            conn.execute(sql.SQL("REVOKE mervio_worker FROM {}").format(sql.Identifier(name)))
        database.close()
        with database.transaction() as conn:
            assert conn.execute("SELECT app_current_service_id()").fetchone()[0] is None
        with pytest.raises(ServiceIdentityError):
            service.service_principal(database)
        with pytest.raises(ServiceIdentityError):
            service.authorized_organizations(database, principal)
        with pytest.raises(ServiceIdentityError):
            jobs.list_jobs(ServiceSession(database, tenant_a.organization_id, principal))
