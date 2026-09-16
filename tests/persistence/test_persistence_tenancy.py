"""Contexte tenant, roles et barriere d'autorisation applicative."""
from __future__ import annotations

import uuid

import psycopg
import pytest

from mervio.persistence.database import Database
from mervio.persistence.errors import NotFound, PermissionDenied, TenantAccessDenied, UnsafeDatabaseConfiguration
from mervio.persistence.stores import create_csv_connection, create_store, get_store, list_stores, revoke_connection
from mervio.persistence.tenancy import (
    MINIMUM_ROLE, Permission, Role, TenantContext, TenantSession, add_member, create_organization, ensure_user,
    organization_name, organizations_of, remove_member, role_allows,
)

from .persistence_support import make_tenant


def test_role_matrix_matches_the_architecture():
    assert role_allows(Role.VIEWER, Permission.READ)
    assert not role_allows(Role.VIEWER, Permission.IMPORT_DATA)
    assert not role_allows(Role.VIEWER, Permission.READ_PROVENANCE)
    assert role_allows(Role.ANALYST, Permission.RUN_ANALYSIS) and role_allows(Role.ANALYST, Permission.READ_PROVENANCE)
    assert not role_allows(Role.ANALYST, Permission.MANAGE_CONNECTIONS)
    assert role_allows(Role.ADMIN, Permission.MANAGE_CONNECTIONS) and not role_allows(Role.ADMIN, Permission.MANAGE_MEMBERS)
    assert all(role_allows(Role.OWNER, p) for p in Permission)
    assert set(MINIMUM_ROLE) == set(Permission)


def test_context_requires_uuids():
    with pytest.raises(TypeError):
        TenantContext("00000000-0000-0000-0000-000000000000", uuid.uuid4())


def test_application_refuses_a_superuser_connection(pg):
    with pytest.raises(UnsafeDatabaseConfiguration):
        Database(pg.admin_conninfo).connection()


def test_application_refuses_a_bypassrls_connection(pg):
    with pytest.raises(UnsafeDatabaseConfiguration):
        Database(pg.url("bypass")).connection()


def test_application_refuses_a_missing_url(monkeypatch):
    monkeypatch.delenv("MERVIO_DATABASE_URL", raising=False)
    with pytest.raises(UnsafeDatabaseConfiguration):
        Database.from_env()


def test_owner_can_work_in_their_organization(db, tenant_a):
    assert organization_name(tenant_a.session) == "Organisation A"
    assert tenant_a.session.role() is Role.OWNER
    assert [s.id for s in list_stores(tenant_a.session)] == [tenant_a.store_id]


def test_a_context_for_an_organization_the_user_does_not_belong_to_is_refused(db, tenant_a, tenant_b):
    forged = TenantSession(db, TenantContext(tenant_a.organization_id, tenant_b.owner_id))
    with pytest.raises(TenantAccessDenied):
        get_store(forged, tenant_a.store_id)
    with pytest.raises(TenantAccessDenied):
        create_store(forged, name="intrusion")
    # indiscernable d'une organisation inexistante
    ghost = TenantSession(db, TenantContext(uuid.uuid4(), tenant_a.owner_id))
    with pytest.raises(TenantAccessDenied):
        list_stores(ghost)
    assert issubclass(TenantAccessDenied, NotFound)


def test_unknown_user_is_refused(db, tenant_a):
    with pytest.raises(TenantAccessDenied):
        list_stores(TenantSession(db, TenantContext(tenant_a.organization_id, uuid.uuid4())))


@pytest.mark.parametrize("role, can_import, can_manage_connections, can_manage_members", [
    (Role.VIEWER, False, False, False),
    (Role.ANALYST, True, False, False),
    (Role.ADMIN, True, True, False),
])
def test_roles_are_enforced_on_every_operation(db, tenant_a, role, can_import, can_manage_connections,
                                              can_manage_members):
    member = ensure_user(db, f"test|member|{role.value}")
    add_member(tenant_a.session, user_id=member, role=role)
    session = TenantSession(db, TenantContext(tenant_a.organization_id, member))
    assert get_store(session, tenant_a.store_id).id == tenant_a.store_id  # tout membre lit

    def attempt(fn):
        try:
            fn()
            return True
        except PermissionDenied:
            return False

    def enter(permission):
        with session.transaction(permission):
            pass

    assert attempt(lambda: enter(Permission.IMPORT_DATA)) is can_import
    assert attempt(lambda: create_csv_connection(session, store_id=tenant_a.store_id, label="x")) is can_manage_connections
    assert attempt(lambda: add_member(session, user_id=ensure_user(db, "test|x"), role=Role.VIEWER)) is can_manage_members


def test_role_is_read_from_the_database_on_each_transaction(db, tenant_a):
    member = ensure_user(db, "test|demoted")
    add_member(tenant_a.session, user_id=member, role=Role.ADMIN)
    session = TenantSession(db, TenantContext(tenant_a.organization_id, member))
    create_csv_connection(session, store_id=tenant_a.store_id, label="avant")
    remove_member(tenant_a.session, user_id=member)
    with pytest.raises(TenantAccessDenied):
        create_csv_connection(session, store_id=tenant_a.store_id, label="apres")


def test_owner_membership_cannot_be_removed_through_the_application(db, tenant_a):
    with pytest.raises(NotFound):
        remove_member(tenant_a.session, user_id=tenant_a.owner_id)


def test_ensure_user_is_idempotent(db):
    assert ensure_user(db, "test|same") == ensure_user(db, "test|same")


def test_user_lists_only_their_own_organizations(db, tenant_a, tenant_b):
    assert organizations_of(db, tenant_a.owner_id) == [tenant_a.organization_id]
    add_member(tenant_b.session, user_id=tenant_a.owner_id, role=Role.VIEWER)
    assert set(organizations_of(db, tenant_a.owner_id)) == {tenant_a.organization_id, tenant_b.organization_id}
    assert organizations_of(db, uuid.uuid4()) == []


def test_tenant_context_does_not_survive_the_transaction(db, tenant_a):
    get_store(tenant_a.session, tenant_a.store_id)
    conn = db.connection()
    assert conn.execute("SELECT current_setting('app.organization_id', true)").fetchone()[0] in (None, "")
    assert conn.execute("SELECT count(*) FROM stores").fetchone()[0] == 0


def test_malformed_identifiers_are_not_found_and_never_reach_sql(db, tenant_a):
    for bad in ("1 OR 1=1", "'; DROP TABLE stores; --", "", None, 42):
        with pytest.raises(NotFound):
            get_store(tenant_a.session, bad)


def test_invalid_store_currency_is_refused(db, tenant_a):
    with pytest.raises(ValueError):
        create_store(tenant_a.session, name="x", currency="eur")


def test_revoked_connection_stays_in_history(db, tenant_a):
    revoked = revoke_connection(tenant_a.session, tenant_a.store_id, tenant_a.connection_id)
    assert revoked.status == "revoked" and revoked.revoked_at is not None
    with pytest.raises(NotFound):
        revoke_connection(tenant_a.session, tenant_a.store_id, tenant_a.connection_id)


def test_organization_creation_is_atomic(db):
    owner = ensure_user(db, "test|atomic")
    with pytest.raises(psycopg.errors.CheckViolation):
        create_organization(db, owner_user_id=owner, name="")
    assert organizations_of(db, owner) == []


def test_second_tenant_fixture_is_independent(db):
    a, b = make_tenant(db, "X"), make_tenant(db, "Y")
    assert a.organization_id != b.organization_id and a.store_id != b.store_id
