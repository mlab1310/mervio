"""Revision 0013 (Mission 004.4.4): table `raw_objects` et vocabulaire d'audit associe.

Deux proprietes distinctes:
- la table apparait a la montee et disparait a la descente, sans residu (politiques, privileges);
- les listes `CHECK` d'`audit_events` s'allongent, exactement comme en 0008: la descente restaure
  les listes precedentes en `NOT VALID`, donc elle CONSERVE les traces deja ecrites et refuse
  seulement les nouvelles.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from mervio.persistence import migrate
from mervio.persistence.audit import Action, ResourceType

PREVIOUS = "0012_dispatch_probe_plan"
CURRENT = "0013_raw_objects"
OBJECT_ACTIONS = ("object.uploaded",)
OBJECT_RESOURCES = ("raw_object",)


@pytest.fixture
def database(pg):
    name = pg.create_empty_database("raw_objects")
    try:
        yield pg.url("migrator", name)
    finally:
        pg.drop_database(name)


def _organization(conn) -> uuid.UUID:
    organization_id = uuid.uuid4()
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
        conn.execute("INSERT INTO organizations (id, name) VALUES (%s, 'Migration')", (organization_id,))
    return organization_id


def _trace(conn, organization_id, action, resource_type) -> None:
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
        conn.execute(
            "INSERT INTO audit_events (id, organization_id, actor_type, action, resource_type, resource_id, "
            "correlation_id, outcome) VALUES (%s, %s, 'system', %s, %s, %s, %s, 'succeeded')",
            (uuid.uuid4(), organization_id, action, resource_type, uuid.uuid4(), uuid.uuid4()))


def _count(conn, organization_id) -> int:
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
        return conn.execute("SELECT count(*) FROM audit_events").fetchone()[0]


def _relations(conn, name: str):
    return conn.execute("SELECT count(*) FROM pg_tables WHERE tablename = %s", (name,)).fetchone()[0]


# -- vocabulaire ------------------------------------------------------------------------------

def test_the_python_vocabulary_matches_the_migration():
    assert {a.value for a in Action} >= set(OBJECT_ACTIONS)
    assert {r.value for r in ResourceType} >= set(OBJECT_RESOURCES)


def test_upgrade_accepts_object_events_and_downgrade_keeps_them_but_refuses_new_ones(database):
    migrate.upgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        organization_id = _organization(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            _trace(conn, organization_id, "object.uploaded", "raw_object")

    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        _trace(conn, organization_id, "object.uploaded", "raw_object")
        assert _count(conn, organization_id) == 1

    migrate.downgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        assert _count(conn, organization_id) == 1, "le journal en ajout seul ne perd rien"
        with pytest.raises(psycopg.errors.CheckViolation):
            _trace(conn, organization_id, "object.uploaded", "raw_object")

    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        _trace(conn, organization_id, "object.uploaded", "raw_object")
        assert _count(conn, organization_id) == 2


# -- table -------------------------------------------------------------------------------------

def test_the_table_appears_at_upgrade_and_disappears_at_downgrade(database):
    migrate.upgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        assert _relations(conn, "raw_objects") == 0
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        assert _relations(conn, "raw_objects") == 1
    migrate.downgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        assert _relations(conn, "raw_objects") == 0
        assert not conn.execute(
            "SELECT count(*) FROM pg_policies WHERE tablename = 'raw_objects'").fetchone()[0]


def test_the_revision_round_trips_to_an_identical_schema(database):
    """Montee, descente, remontee: meme schema qu'au premier passage (porte `ci_migrations`)."""
    def snapshot():
        with psycopg.connect(database, autocommit=True) as conn:
            columns = conn.execute(
                "SELECT table_name, column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema = 'public' "
                "ORDER BY table_name, column_name").fetchall()
            checks = conn.execute(
                "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
                "FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
                "ORDER BY 1, 2").fetchall()
            policies = conn.execute(
                "SELECT tablename, policyname, permissive, roles::text, cmd, qual, with_check "
                "FROM pg_policies WHERE schemaname = 'public' ORDER BY tablename, policyname").fetchall()
        return columns, checks, policies

    migrate.upgrade(database, CURRENT)
    before = snapshot()
    migrate.downgrade(database, PREVIOUS)
    migrate.upgrade(database, CURRENT)
    assert snapshot() == before


def test_the_table_carries_no_update_or_delete_grant(database):
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not conn.execute(
                "SELECT has_table_privilege('mervio_app', 'raw_objects', %s)", (privilege,)).fetchone()[0]


def test_the_revision_adds_no_role_and_no_security_definer_function(database):
    """D-052 reserve les fonctions privilegiees a 004.4.5/004.4.6: 0013 n'en cree aucune."""
    migrate.upgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        roles_before = {r[0] for r in conn.execute("SELECT rolname FROM pg_roles").fetchall()}
        definers_before = {r[0] for r in conn.execute(
            "SELECT proname FROM pg_proc WHERE prosecdef AND pronamespace = 'public'::regnamespace").fetchall()}
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        roles_after = {r[0] for r in conn.execute("SELECT rolname FROM pg_roles").fetchall()}
        definers_after = {r[0] for r in conn.execute(
            "SELECT proname FROM pg_proc WHERE prosecdef AND pronamespace = 'public'::regnamespace").fetchall()}
    assert roles_after == roles_before
    assert definers_after == definers_before


def test_the_state_domain_is_complete_but_only_available_is_produced(database):
    """D-061: le domaine des quatre etats est fige ICI, 004.4.4 ne produit que `available`."""
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'raw_objects_state_known'"
        ).fetchone()[0]
    for state in ("pending", "available", "purging", "purged"):
        assert f"'{state}'" in definition, state
