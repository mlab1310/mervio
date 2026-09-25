"""Revision 0015 (Mission 004.4.5 E4): finalisation d'une purge d'objet brut.

Une revision d'une seule fonction: elle n'ajoute ni table, ni colonne, ni role, ni privilege
de table, ni vocabulaire d'audit. Ce fichier le prouve, et prouve que la descente ne laisse
rien derriere elle.
"""
from __future__ import annotations

import psycopg
import pytest

from mervio.persistence import migrate

PREVIOUS = "0014_customer_erasure"
CURRENT = "0015_raw_object_purge"
FUNCTION = "app_finalize_raw_object_purge"


@pytest.fixture
def database(pg):
    name = pg.create_empty_database("raw_object_purge")
    try:
        yield pg.url("migrator", name)
    finally:
        pg.drop_database(name)


def _definers(conn):
    return {r[0] for r in conn.execute(
        "SELECT proname FROM pg_proc WHERE prosecdef AND pronamespace = 'public'::regnamespace").fetchall()}


def _snapshot(url):
    """Ce que la revision ne doit PAS toucher: tables, colonnes, privileges, politiques."""
    with psycopg.connect(url) as conn:
        return (
            conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1").fetchall(),
            conn.execute("SELECT table_name, column_name FROM information_schema.columns "
                         "WHERE table_schema = 'public' ORDER BY 1, 2").fetchall(),
            conn.execute("SELECT grantee, table_name, privilege_type FROM information_schema.table_privileges "
                         "WHERE table_schema = 'public' ORDER BY 1, 2, 3").fetchall(),
            conn.execute("SELECT tablename, policyname FROM pg_policies ORDER BY 1, 2").fetchall(),
            conn.execute("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                         "WHERE connamespace = 'public'::regnamespace ORDER BY 1").fetchall(),
        )


def test_the_revision_adds_exactly_one_security_definer_function(database):
    migrate.upgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        before = _definers(conn)
    assert before == {"app_ensure_service_principal", "app_redact_customer"}

    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        after = _definers(conn)
    assert after == before | {FUNCTION}


def test_the_revision_changes_nothing_else_in_the_schema(database):
    """Aucune table, colonne, privilege de table, politique ni contrainte n'est touchee."""
    migrate.upgrade(database, PREVIOUS)
    before = _snapshot(database)
    migrate.upgrade(database, CURRENT)
    assert _snapshot(database) == before


def test_the_function_is_executable_by_the_worker_role_alone(database):
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        for role, expected in (("public", False), ("mervio_app", False), ("mervio_worker", True)):
            assert conn.execute(
                "SELECT has_function_privilege(%s, %s || '(uuid, uuid)', 'EXECUTE')",
                (role, FUNCTION)).fetchone()[0] is expected, role


def test_the_function_pins_its_search_path(database):
    """D-049: `pg_catalog` cherche en PREMIER, donc aucune relation ni operateur ombrable."""
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        config = conn.execute("SELECT proconfig FROM pg_proc WHERE proname = %s", (FUNCTION,)).fetchone()[0]
    assert config == ["search_path=pg_catalog, public, pg_temp"]


def test_the_function_reads_no_relation_by_an_unqualified_name(database):
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        source = conn.execute("SELECT prosrc FROM pg_proc WHERE proname = %s", (FUNCTION,)).fetchone()[0]
    for relation in ("users", "service_authorizations", "jobs", "memberships", "raw_objects"):
        assert f" public.{relation}" in source, relation
        assert f" FROM {relation}" not in source, relation


def test_the_revision_adds_no_role(database):
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname IN "
                            "('mervio_redactor', 'mervio_purger')").fetchone() == (0,)


def test_the_downgrade_leaves_no_residue(database):
    migrate.upgrade(database, CURRENT)
    migrate.downgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        assert FUNCTION not in _definers(conn)
        assert conn.execute("SELECT count(*) FROM pg_proc WHERE proname = %s", (FUNCTION,)).fetchone() == (0,)


def test_revision_0015_round_trips_exactly(database):
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database) as conn:
        first = conn.execute("SELECT prosrc, proconfig, proacl::text FROM pg_proc "
                             "WHERE proname = %s", (FUNCTION,)).fetchone()
    migrate.downgrade(database, PREVIOUS)
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT prosrc, proconfig, proacl::text FROM pg_proc "
                            "WHERE proname = %s", (FUNCTION,)).fetchone() == first
