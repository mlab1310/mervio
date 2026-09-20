"""Objets bruts: RLS, contraintes et privileges (Mission 004.4.4, revision 0013, D-054, D-061).

La propriete centrale: c'est la LIGNE sous RLS qui porte la frontiere de tenant, pas la cle
d'objet ni le magasin. Un objet d'une autre organisation doit etre INDISCERNABLE d'un objet
qui n'a jamais existe.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from mervio.persistence import raw_objects
from mervio.persistence.errors import NotFound, PermissionDenied
from mervio.persistence.raw_objects import fetch_object, get_object, record_object
from mervio.persistence.service import authorize_service, revoke_service
from mervio.persistence.tenancy import Permission
from mervio.storage import build_object_key

from .persistence_support import make_tenant

SHA = "a" * 64
LATER = datetime(2026, 12, 31, tzinfo=timezone.utc)


GENERATE = object()  # sentinelle: "" est une cle A TESTER, pas une absence de cle


def deposit(tenant, *, kind="shopify_orders", key=GENERATE, sha256=SHA, size=42, hook=None):
    if key is GENERATE:
        key = build_object_key(tenant.organization_id, tenant.store_id)
    return record_object(
        tenant.session, store_id=tenant.store_id, object_key=key,
        sha256=sha256, byte_size=size, source_kind=kind, retain_until=LATER, hook=hook)


# -- cycle nominal -----------------------------------------------------------------------------

def test_a_deposited_object_is_available_and_readable_again(tenant_a):
    recorded = deposit(tenant_a)
    assert recorded.state == "available"
    assert recorded.origin == "csv_upload"
    assert recorded.available
    again = get_object(tenant_a.session, recorded.id)
    assert (again.id, again.sha256, again.byte_size) == (recorded.id, SHA, 42)


def test_the_public_view_carries_no_object_key_and_no_filename(tenant_a):
    public = deposit(tenant_a).public()
    assert "object_key" not in public
    assert not any("/" in str(value) for value in public.values()), public
    assert set(public) == {"id", "store_id", "source_kind", "origin", "sha256", "byte_size",
                           "state", "retain_until", "created_at"}


def test_objects_are_listed_per_store(tenant_a):
    first, second = deposit(tenant_a), deposit(tenant_a)
    listed = {o.id for o in raw_objects.list_objects(tenant_a.session, tenant_a.store_id)}
    assert listed == {first.id, second.id}


# -- isolation: la RLS, pas un `if` Python -----------------------------------------------------

def test_an_object_of_another_organization_is_not_found(tenant_a, tenant_b):
    foreign = deposit(tenant_b)
    with pytest.raises(NotFound):
        get_object(tenant_a.session, foreign.id)


def test_a_foreign_object_and_a_nonexistent_one_are_indistinguishable(tenant_a, tenant_b):
    """Aucun oracle d'existence: meme type, meme message, aucune donnee de l'autre tenant."""
    foreign = deposit(tenant_b)
    never = uuid.uuid4()
    with pytest.raises(NotFound) as cross:
        get_object(tenant_a.session, foreign.id)
    with pytest.raises(NotFound) as absent:
        get_object(tenant_a.session, never)
    assert type(cross.value) is type(absent.value)
    assert str(cross.value) == str(absent.value)
    assert str(foreign.id) not in str(cross.value)


def test_the_repository_never_filters_on_the_organization_itself(tenant_a):
    """La requete ne porte QUE l'identifiant: l'isolation vient de PostgreSQL (D-054)."""
    import inspect
    lines = inspect.getsource(fetch_object).splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith('"""'))
    end = next(i for i, line in enumerate(lines[start + 1:], start + 1) if '"""' in line)
    code = "\n".join(lines[:start] + lines[end + 1:])  # sans le docstring
    assert "WHERE id = %s" in code
    assert "organization_id" not in code


def test_a_viewer_cannot_deposit_an_object(db, tenant_a):
    from mervio.persistence.tenancy import Role, TenantContext, TenantSession, add_member, ensure_user
    viewer = ensure_user(db, "test|viewer|raw-objects")
    add_member(tenant_a.session, user_id=viewer, role=Role.VIEWER)
    session = TenantSession(db, TenantContext(tenant_a.organization_id, viewer))
    with pytest.raises(PermissionDenied):
        record_object(session, store_id=tenant_a.store_id,
                      object_key=build_object_key(tenant_a.organization_id, tenant_a.store_id),
                      sha256=SHA, byte_size=1, source_kind="stripe", retain_until=LATER)


# -- contraintes de la base --------------------------------------------------------------------

@pytest.mark.parametrize("bad_key", [
    "../../etc/passwd", "/etc/passwd", "org/x/store/y/raw/z", "",
    "org/" + "0" * 36 + "/store/" + "0" * 36 + "/raw/" + "0" * 35,
])
def test_the_database_refuses_a_key_outside_the_canonical_form(tenant_a, bad_key):
    with pytest.raises(psycopg.errors.CheckViolation):
        deposit(tenant_a, key=bad_key)


@pytest.mark.parametrize("bad_sha", ["", "z" * 64, "A" * 64, "a" * 63, "a" * 65])
def test_the_database_refuses_a_sha256_that_is_not_lowercase_hex64(tenant_a, bad_sha):
    with pytest.raises(psycopg.errors.CheckViolation):
        deposit(tenant_a, sha256=bad_sha)


def test_the_database_refuses_a_negative_size(tenant_a):
    with pytest.raises(psycopg.errors.CheckViolation):
        deposit(tenant_a, size=-1)


def test_an_unknown_source_kind_is_refused_before_the_database(tenant_a):
    with pytest.raises(ValueError):
        deposit(tenant_a, kind="quickbooks")


def test_the_same_key_cannot_be_recorded_twice_in_one_organization(tenant_a):
    key = build_object_key(tenant_a.organization_id, tenant_a.store_id)
    deposit(tenant_a, key=key)
    with pytest.raises(psycopg.errors.UniqueViolation):
        deposit(tenant_a, key=key)


def test_an_object_cannot_reference_a_store_of_another_organization(tenant_a, tenant_b):
    with pytest.raises((psycopg.errors.ForeignKeyViolation, psycopg.errors.InsufficientPrivilege, NotFound)):
        record_object(tenant_a.session, store_id=tenant_b.store_id,
                      object_key=build_object_key(tenant_a.organization_id, tenant_b.store_id),
                      sha256=SHA, byte_size=1, source_kind="stripe", retain_until=LATER)


def test_a_store_with_objects_cannot_be_deleted(tenant_a, owner):
    """`ON DELETE RESTRICT`: un objet retient sa boutique (ordre de purge, D-051, 004.4.6)."""
    deposit(tenant_a)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(tenant_a.organization_id),))
        owner.execute("DELETE FROM stores WHERE id = %s", (tenant_a.store_id,))


# -- privileges: aucune mutation possible en 004.4.4 -------------------------------------------

def test_the_application_role_can_only_select_and_insert(app_conn):
    for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
        granted = app_conn.execute(
            "SELECT has_table_privilege('mervio_app', 'raw_objects', %s)", (privilege,)).fetchone()[0]
        assert not granted, privilege
    for privilege in ("SELECT", "INSERT"):
        assert app_conn.execute(
            "SELECT has_table_privilege('mervio_app', 'raw_objects', %s)", (privilege,)).fetchone()[0]


def test_row_level_security_is_enabled_and_forced(owner):
    row = owner.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'raw_objects'").fetchone()
    assert row == (True, True)


def test_no_state_transition_is_possible_before_004_4_5(tenant_a, app_conn):
    """Aucun `UPDATE` accordé: `sha256` est fige et `available` ne bouge pas."""
    recorded = deposit(tenant_a)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("UPDATE raw_objects SET state = 'purged' WHERE id = %s", (recorded.id,))


# -- worker ------------------------------------------------------------------------------------

def _worker_sees(worker_db, organization_id, principal, user_id) -> int:
    from mervio.persistence.tenancy import TenantContext, TenantSession
    session = TenantSession(worker_db, TenantContext(organization_id, user_id))
    with session.transaction(Permission.READ) as conn:
        return conn.execute("SELECT count(*) FROM raw_objects").fetchone()[0]


def test_an_authorized_worker_reads_objects_under_a_human_delegate(tenant_a, worker_db, principal):
    deposit(tenant_a)
    authorize_service(tenant_a.session, principal.id)
    assert _worker_sees(worker_db, tenant_a.organization_id, principal, tenant_a.owner_id) == 1


def test_a_worker_without_authorization_cannot_even_open_the_organization(tenant_a, worker_db, principal):
    """Sans autorisation de service, la RLS masque jusqu'a l'appartenance du delegue."""
    deposit(tenant_a)
    with pytest.raises(NotFound):  # TenantAccessDenied herite de NotFound
        _worker_sees(worker_db, tenant_a.organization_id, principal, tenant_a.owner_id)


def test_a_revoked_worker_stops_seeing_objects(tenant_a, worker_db, principal):
    deposit(tenant_a)
    authorize_service(tenant_a.session, principal.id)
    assert _worker_sees(worker_db, tenant_a.organization_id, principal, tenant_a.owner_id) == 1
    revoke_service(tenant_a.session, principal.id)
    with pytest.raises(NotFound):
        _worker_sees(worker_db, tenant_a.organization_id, principal, tenant_a.owner_id)


def test_a_worker_can_never_insert_update_or_delete_an_object(tenant_a, worker_conn, principal):
    recorded = deposit(tenant_a)
    authorize_service(tenant_a.session, principal.id)
    context = (str(tenant_a.organization_id), str(tenant_a.owner_id))
    with worker_conn.transaction():
        worker_conn.execute("SELECT set_config('app.organization_id', %s, true), "
                            "set_config('app.user_id', %s, true)", context)
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.RaiseException)):
            worker_conn.execute(
                "INSERT INTO raw_objects (id, organization_id, store_id, object_key, sha256, byte_size, "
                "source_kind, origin, state, retain_until) VALUES (%s, %s, %s, %s, %s, 1, 'stripe', "
                "'csv_upload', 'available', %s)",
                (uuid.uuid4(), tenant_a.organization_id, tenant_a.store_id,
                 build_object_key(tenant_a.organization_id, tenant_a.store_id), SHA, LATER))
    for statement, params in (
        ("UPDATE raw_objects SET state = 'purged' WHERE id = %s", (recorded.id,)),
        ("DELETE FROM raw_objects WHERE id = %s", (recorded.id,)),
    ):
        with worker_conn.transaction():
            worker_conn.execute("SELECT set_config('app.organization_id', %s, true), "
                                "set_config('app.user_id', %s, true)", context)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                worker_conn.execute(statement, params)
