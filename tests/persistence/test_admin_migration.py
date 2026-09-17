"""Revision 0008 (Mission 004.3.7): vocabulaire d'audit de l'administration.

La revision n'ajoute que des valeurs aux contraintes CHECK de `audit_events`. Sa descente ne
supprime AUCUNE trace deja ecrite (journal en ajout seul) mais refuse les nouvelles; sa remontee
les accepte de nouveau et revalide toutes les lignes.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from mervio.persistence import migrate
from mervio.persistence.audit import Action, ResourceType

ADMIN_ACTIONS = ("organization.created", "member.added", "store.created", "connection.created",
                 "service.authorized", "service.revoked")
ADMIN_RESOURCES = ("membership", "store", "connection", "service_authorization")


@pytest.fixture
def database(pg):
    name = pg.create_empty_database("admin_audit")
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


def test_the_python_vocabulary_matches_the_migration():
    assert {a.value for a in Action} >= set(ADMIN_ACTIONS)
    assert {r.value for r in ResourceType} >= set(ADMIN_RESOURCES)


def test_upgrade_accepts_admin_events_and_downgrade_keeps_them_but_refuses_new_ones(database):
    migrate.upgrade(database, "0007_service_identity")
    with psycopg.connect(database, autocommit=True) as conn:
        organization_id = _organization(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            _trace(conn, organization_id, "store.created", "store")

    migrate.upgrade(database, "0008_admin_audit")
    with psycopg.connect(database, autocommit=True) as conn:
        for action, resource in zip(ADMIN_ACTIONS, ADMIN_RESOURCES * 2):
            _trace(conn, organization_id, action, resource)
        _trace(conn, organization_id, "job.enqueued", "job")  # vocabulaire de 0005 intact
        with pytest.raises(psycopg.errors.CheckViolation):
            _trace(conn, organization_id, "organization.deleted", "organization")
        with pytest.raises(psycopg.errors.CheckViolation):
            _trace(conn, organization_id, "store.created", "user")
        assert _count(conn, organization_id) == 7

    migrate.downgrade(database, "0007_service_identity")
    with psycopg.connect(database, autocommit=True) as conn:
        assert _count(conn, organization_id) == 7  # aucune trace effacee
        with pytest.raises(psycopg.errors.CheckViolation):
            _trace(conn, organization_id, "member.added", "membership")
        _trace(conn, organization_id, "job.enqueued", "job")
        validated = dict(conn.execute(
            "SELECT conname, convalidated FROM pg_constraint WHERE conrelid = 'audit_events'::regclass "
            "AND conname IN ('audit_events_action_check', 'audit_events_resource_type_check')").fetchall())
        assert validated == {"audit_events_action_check": False, "audit_events_resource_type_check": False}

    migrate.upgrade(database)
    assert migrate.current_revision(database) == "0008_admin_audit"
    with psycopg.connect(database, autocommit=True) as conn:
        _trace(conn, organization_id, "service.revoked", "service_authorization")
        assert _count(conn, organization_id) == 9
        validated = dict(conn.execute(
            "SELECT conname, convalidated FROM pg_constraint WHERE conrelid = 'audit_events'::regclass "
            "AND conname IN ('audit_events_action_check', 'audit_events_resource_type_check')").fetchall())
        assert validated == {"audit_events_action_check": True, "audit_events_resource_type_check": True}


def test_the_revision_changes_no_table_column_privilege_or_policy(database):
    def catalog(conn):
        return (
            conn.execute("SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                         "WHERE table_schema = 'public' ORDER BY 1, 2").fetchall(),
            conn.execute("SELECT tablename, policyname, cmd, roles::text, qual, with_check FROM pg_policies "
                         "ORDER BY 1, 2").fetchall(),
            conn.execute("SELECT table_name, grantee, privilege_type FROM information_schema.role_table_grants "
                         "WHERE table_schema = 'public' ORDER BY 1, 2, 3").fetchall(),
            conn.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1").fetchall(),
        )

    migrate.upgrade(database, "0007_service_identity")
    with psycopg.connect(database) as conn:
        before = catalog(conn)
    migrate.upgrade(database)
    with psycopg.connect(database) as conn:
        assert catalog(conn) == before


def _fingerprint(url):
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "ci_migrations.py"
    spec = importlib.util.spec_from_file_location("mervio_ci_migrations_fp_admin", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.PostgresTarget(url).fingerprint()


def _without_not_valid(fingerprint):
    return tuple(tuple(tuple(str(value).replace(" NOT VALID", "") if isinstance(value, str) else value
                             for value in row) for row in rows) for rows in fingerprint)


def test_0008_downgrades_to_the_0007_schema_and_upgrades_to_the_exact_head(pg, database):
    reference = pg.create_empty_database("admin_ref")
    try:
        reference_url = pg.url("migrator", reference)
        migrate.upgrade(reference_url, "0007_service_identity")
        migrate.upgrade(database)
        head = _fingerprint(database)
        assert head == _fingerprint(pg.url("migrator"))
        migrate.downgrade(database, "0007_service_identity")
        downgraded = _fingerprint(database)
        # seule difference avec un 0007 d'origine: les deux listes restent NON VALIDEES (traces conservees)
        assert downgraded != _fingerprint(reference_url)
        assert _without_not_valid(downgraded) == _without_not_valid(_fingerprint(reference_url))
        migrate.upgrade(database)
        assert _fingerprint(database) == head
    finally:
        pg.drop_database(reference)
