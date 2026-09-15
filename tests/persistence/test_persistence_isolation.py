"""Isolation entre tenants (critere d'acceptation 004.1).

Organisation A et Organisation B ont chacune: boutique, connexion, instantane
(commandes, lignes, paiements, remboursements, produits, campagnes), execution
d'analyse et rapport.

Trois niveaux de preuve:
1. depots applicatifs avec le role applicatif reel (barriere applicative + RLS);
2. MEMES appels sur une connexion BYPASSRLS: la barriere applicative SEULE suffit;
3. SQL brut du role applicatif: RLS SEULE suffit, meme quand l'appelant se trompe
   ou triche (mauvais organization_id, identifiants devines, cles etrangeres croisees).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg
import pytest

from mervio.application.persisted_analysis import SnapshotImportRequest, analyze_snapshot, import_csv_snapshot
from mervio.persistence import analyses, provenance, snapshots
from mervio.persistence.database import Database
from mervio.persistence.errors import NotFound, TenantAccessDenied
from mervio.persistence.stores import (
    create_csv_connection, create_store, get_connection, get_store, list_stores, revoke_connection,
)
from mervio.persistence.tenancy import TenantContext, TenantSession

from .persistence_support import SAMPLE_FILES, make_tenant

TENANT_TABLES = ("organizations", "memberships", "stores", "connections", "data_snapshots", "snapshot_sources",
                 "products", "orders", "order_lines", "payments", "refunds", "campaigns", "ad_daily_performance",
                 "analysis_runs", "reports")


@dataclass
class World:
    tenant: object
    snapshot_id: uuid.UUID
    run_id: uuid.UUID
    report_id: uuid.UUID
    order_ref: str


def _populate(tenant) -> World:
    imported = import_csv_snapshot(tenant.session, store_id=tenant.store_id, connection_id=tenant.connection_id,
                                   request=SnapshotImportRequest(**SAMPLE_FILES))
    assert imported.status == "completed"
    outcome = analyze_snapshot(tenant.session, store_id=tenant.store_id, snapshot_id=imported.snapshot.id)
    assert outcome.status == "completed"
    order_ref = snapshots.load_dataset(tenant.session, tenant.store_id, imported.snapshot.id).orders[0].order_id
    # la trace existe bien pour son proprietaire: un NotFound croise ne peut pas venir d'un identifiant faux
    provenance.trace_record(tenant.session, tenant.store_id, imported.snapshot.id, entity="order", source="shopify",
                            source_record_id=order_ref)
    return World(tenant, imported.snapshot.id, outcome.run.id, outcome.report.id, order_ref)


@pytest.fixture
def worlds(db):
    return _populate(make_tenant(db, "A")), _populate(make_tenant(db, "B"))


def _cross_tenant_reads(reader, victim: World):
    """Chaque lecture de B sur une ressource de A, avec les identifiants EXACTS de A."""
    t = victim.tenant
    return {
        "store": lambda: get_store(reader, t.store_id),
        "connection": lambda: get_connection(reader, t.store_id, t.connection_id),
        "snapshot": lambda: snapshots.get_snapshot(reader, t.store_id, victim.snapshot_id),
        "snapshot_sources": lambda: snapshots.snapshot_sources(reader, t.store_id, victim.snapshot_id),
        "dataset": lambda: snapshots.load_dataset(reader, t.store_id, victim.snapshot_id),
        "snapshot_list": lambda: snapshots.list_snapshots(reader, t.store_id),
        "run": lambda: analyses.get_run(reader, t.store_id, victim.run_id),
        "run_list": lambda: analyses.list_runs(reader, t.store_id),
        "report": lambda: analyses.get_report(reader, t.store_id, victim.report_id),
        "report_for_run": lambda: analyses.get_report_for_run(reader, t.store_id, victim.run_id),
        "latest_report": lambda: analyses.latest_report(reader, t.store_id),
        "report_provenance": lambda: provenance.trace_report(reader, t.store_id, victim.report_id),
        "order_provenance": lambda: provenance.trace_record(reader, t.store_id, victim.snapshot_id, entity="order",
                                                            source="shopify", source_record_id=victim.order_ref),
    }


def _cross_tenant_writes(writer, victim: World):
    t = victim.tenant
    return {
        "connection_on_foreign_store": lambda: create_csv_connection(writer, store_id=t.store_id, label="intrusion"),
        "revoke_foreign_connection": lambda: revoke_connection(writer, t.store_id, t.connection_id),
        "import_into_foreign_store": lambda: import_csv_snapshot(
            writer, store_id=t.store_id, connection_id=t.connection_id,
            request=SnapshotImportRequest(**SAMPLE_FILES)),
        "analyze_foreign_snapshot": lambda: analyze_snapshot(writer, store_id=t.store_id,
                                                             snapshot_id=victim.snapshot_id),
    }


def _assert_all_not_found(operations):
    for name, operation in operations.items():
        with pytest.raises(NotFound):
            operation()
            pytest.fail(f"{name}: lecture ou ecriture croisee autorisee")


# -- 1. depots applicatifs ---------------------------------------------------------------------

def test_a_cannot_read_b_and_b_cannot_read_a(worlds):
    a, b = worlds
    _assert_all_not_found(_cross_tenant_reads(b.tenant.session, a))
    _assert_all_not_found(_cross_tenant_reads(a.tenant.session, b))


def test_a_cannot_write_into_b_and_b_cannot_write_into_a(worlds):
    a, b = worlds
    _assert_all_not_found(_cross_tenant_writes(b.tenant.session, a))
    _assert_all_not_found(_cross_tenant_writes(a.tenant.session, b))
    # rien n'a ete ecrit chez la victime
    assert len(snapshots.list_snapshots(a.tenant.session, a.tenant.store_id)) == 1
    assert len(analyses.list_runs(a.tenant.session, a.tenant.store_id)) == 1
    assert get_connection(a.tenant.session, a.tenant.store_id, a.tenant.connection_id).status == "active"


def test_lists_only_return_own_resources(worlds):
    a, b = worlds
    assert [s.id for s in list_stores(a.tenant.session)] == [a.tenant.store_id]
    assert [s.id for s in list_stores(b.tenant.session)] == [b.tenant.store_id]


def test_own_resource_under_a_wrong_store_id_is_not_found(db, worlds):
    a, _ = worlds
    second_store = create_store(a.tenant.session, name="Seconde boutique A")
    t = a.tenant
    for operation in (lambda: snapshots.get_snapshot(t.session, second_store.id, a.snapshot_id),
                      lambda: snapshots.load_dataset(t.session, second_store.id, a.snapshot_id),
                      lambda: analyses.get_run(t.session, second_store.id, a.run_id),
                      lambda: analyses.get_report(t.session, second_store.id, a.report_id),
                      lambda: provenance.trace_report(t.session, second_store.id, a.report_id),
                      lambda: get_connection(t.session, second_store.id, t.connection_id),
                      lambda: analyze_snapshot(t.session, store_id=second_store.id, snapshot_id=a.snapshot_id)):
        with pytest.raises(NotFound):
            operation()


def test_guessed_identifiers_are_not_found(worlds):
    a, _ = worlds
    t = a.tenant
    for guess in (uuid.uuid4(), uuid.UUID(int=0), uuid.UUID(int=1), "not-a-uuid"):
        for operation in (lambda: snapshots.get_snapshot(t.session, t.store_id, guess),
                          lambda: analyses.get_report(t.session, t.store_id, guess),
                          lambda: analyses.get_run(t.session, t.store_id, guess),
                          lambda: get_store(t.session, guess)):
            with pytest.raises(NotFound):
                operation()


def test_wrong_organization_id_in_the_context_is_refused(db, worlds):
    a, b = worlds
    # utilisateur de B pretendant agir dans l'organisation de A
    forged = TenantSession(db, TenantContext(a.tenant.organization_id, b.tenant.owner_id))
    for operation in _cross_tenant_reads(forged, a).values():
        with pytest.raises(TenantAccessDenied):
            operation()


# -- 2. barriere applicative seule (RLS contournee) ---------------------------------------------

def test_application_barrier_alone_blocks_cross_tenant_access(pg, worlds):
    a, b = worlds
    bypass = Database(pg.url("bypass"), _allow_rls_bypass_for_tests=True)
    try:
        conn = bypass.connection()
        assert conn.execute("SELECT count(DISTINCT organization_id) FROM reports").fetchone()[0] == 2  # RLS contournee
        reader = TenantSession(bypass, TenantContext(b.tenant.organization_id, b.tenant.owner_id))
        _assert_all_not_found(_cross_tenant_reads(reader, a))
        _assert_all_not_found(_cross_tenant_writes(reader, a))
        assert [s.id for s in list_stores(reader)] == [b.tenant.store_id]
    finally:
        bypass.close()


# -- 3. RLS seule (SQL brut du role applicatif) ---------------------------------------------------

def _in_context(conn, organization_id=None, user_id=None):
    conn.execute("SELECT set_config('app.organization_id', %s, true), set_config('app.user_id', %s, true)",
                 (str(organization_id) if organization_id else "", str(user_id) if user_id else ""))


def test_without_tenant_context_the_application_role_sees_nothing(app_conn, worlds):
    for table in TENANT_TABLES:
        assert app_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_rls_scopes_every_table_to_the_current_organization(app_conn, owner, worlds):
    a, b = worlds
    for world, other in ((a, b), (b, a)):
        with app_conn.transaction():
            _in_context(app_conn, world.tenant.organization_id)
            for table in TENANT_TABLES:
                column = "id" if table == "organizations" else "organization_id"
                orgs = {r[0] for r in app_conn.execute(f"SELECT DISTINCT {column} FROM {table}").fetchall()}
                assert orgs == {world.tenant.organization_id}, table
            # identifiants exacts de l'autre tenant: aucune ligne
            assert app_conn.execute("SELECT count(*) FROM reports WHERE id = %s",
                                    (other.report_id,)).fetchone()[0] == 0
            assert app_conn.execute("SELECT count(*) FROM orders WHERE snapshot_id = %s",
                                    (other.snapshot_id,)).fetchone()[0] == 0


def test_explicit_filter_on_the_other_organization_returns_nothing(app_conn, worlds):
    a, b = worlds
    with app_conn.transaction():
        _in_context(app_conn, b.tenant.organization_id)
        assert app_conn.execute("SELECT count(*) FROM data_snapshots WHERE organization_id = %s",
                                (a.tenant.organization_id,)).fetchone()[0] == 0
        assert app_conn.execute("SELECT payload_json FROM reports WHERE organization_id = %s AND id = %s",
                                (a.tenant.organization_id, a.report_id)).fetchall() == []


def test_rls_refuses_inserting_rows_for_another_organization(app_conn, worlds):
    a, b = worlds
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with app_conn.transaction():
            _in_context(app_conn, b.tenant.organization_id)
            app_conn.execute("INSERT INTO stores (id, organization_id, name) VALUES (%s, %s, 'intrusion')",
                             (uuid.uuid4(), a.tenant.organization_id))


def test_rls_updates_and_revocations_cannot_reach_another_organization(app_conn, worlds):
    a, b = worlds
    with app_conn.transaction():
        _in_context(app_conn, b.tenant.organization_id)
        assert app_conn.execute("UPDATE stores SET name = 'pirate' WHERE id = %s",
                                (a.tenant.store_id,)).rowcount == 0
        assert app_conn.execute("UPDATE connections SET status = 'revoked', revoked_at = now() WHERE id = %s",
                                (a.tenant.connection_id,)).rowcount == 0
        assert app_conn.execute("DELETE FROM memberships WHERE organization_id = %s",
                                (a.tenant.organization_id,)).rowcount == 0


def test_rls_refuses_moving_a_row_to_another_organization(owner, worlds):
    """Meme avec le droit UPDATE sur la colonne (proprietaire), WITH CHECK bloque le changement d'organisation."""
    a, b = worlds
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with owner.transaction():
            _in_context(owner, a.tenant.organization_id)
            owner.execute("UPDATE memberships SET organization_id = %s WHERE organization_id = %s",
                          (b.tenant.organization_id, a.tenant.organization_id))


def test_cross_tenant_foreign_keys_are_impossible_even_when_the_target_exists(app_conn, worlds):
    """Les controles de cle etrangere ignorent RLS: les cles composites (organization_id, ...) ferment la breche."""
    a, b = worlds
    attempts = {
        "connection_on_foreign_store": (
            "INSERT INTO connections (id, organization_id, store_id, kind, label) VALUES (%s, %s, %s, 'csv_upload', 'x')",
            lambda: (uuid.uuid4(), b.tenant.organization_id, a.tenant.store_id)),
        "snapshot_on_foreign_connection": (
            "INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, source, connector, "
            "connector_version, schema_version, normalization_version, status, ingestion_started_at, synthetic, "
            "not_for_production) VALUES (%s, %s, %s, %s, 'csv', 'x', 'x', 'x', 'x', 'ingesting', now(), false, false)",
            lambda: (uuid.uuid4(), b.tenant.organization_id, b.tenant.store_id, a.tenant.connection_id)),
        "run_on_foreign_snapshot": (
            "INSERT INTO analysis_runs (id, organization_id, store_id, snapshot_id, status, engine_version, grain, "
            "config, config_sha256, started_at) VALUES (%s, %s, %s, %s, 'running', '0.1.0', 'week', '{}', %s, now())",
            lambda: (uuid.uuid4(), b.tenant.organization_id, b.tenant.store_id, a.snapshot_id, "0" * 64)),
        "report_on_foreign_run": (
            "INSERT INTO reports (id, organization_id, store_id, analysis_run_id, snapshot_id, engine_version, "
            "generated_at, grain, period_label, period_start, period_end, currency, synthetic, payload_json, payload, "
            "payload_sha256, payload_bytes) VALUES (%s, %s, %s, %s, %s, '0.1.0', now(), 'week', 'x', '2026-01-01', "
            "'2026-01-07', 'EUR', false, '{}', '{}', encode(sha256(convert_to('{}', 'UTF8')), 'hex'), 2)",
            lambda: (uuid.uuid4(), b.tenant.organization_id, b.tenant.store_id, a.run_id, a.snapshot_id)),
    }
    for name, (statement, params) in attempts.items():
        with pytest.raises((psycopg.errors.ForeignKeyViolation, psycopg.errors.RestrictViolation)):
            with app_conn.transaction():
                _in_context(app_conn, b.tenant.organization_id)
                app_conn.execute(statement, params())
                pytest.fail(f"{name}: reference croisee acceptee")


def test_foreign_natural_keys_fail_as_missing_references_never_as_duplicates(app_conn, worlds):
    """Les identifiants exacts d'une ligne de A (instantane, rang) ne produisent pas un "doublon" revelateur.

    Unicite et cles primaires commencent par organization_id: B obtient la meme erreur que pour un instantane
    inexistant (cle etrangere), jamais une violation d'unicite qui confirmerait l'existence de la ligne de A.
    """
    a, b = worlds
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with app_conn.transaction():
            _in_context(app_conn, b.tenant.organization_id)
            app_conn.execute(
                "INSERT INTO orders (organization_id, store_id, snapshot_id, snapshot_source_id, position, source, "
                "source_record_id, order_ref, customer_ref, created_at, currency, subtotal, discount, shipping, tax, "
                "total, financial_status) VALUES (%s, %s, %s, %s, 0, 'shopify', 'x', 'x', 'x', now(), 'EUR', 1, 0, 0, "
                "0, 1, 'paid')",
                (b.tenant.organization_id, b.tenant.store_id, a.snapshot_id, uuid.uuid4()))


def test_tenant_context_is_transaction_local(app_conn, worlds):
    a, _ = worlds
    with app_conn.transaction():
        _in_context(app_conn, a.tenant.organization_id)
        assert app_conn.execute("SELECT count(*) FROM stores").fetchone()[0] == 1
    assert app_conn.execute("SELECT count(*) FROM stores").fetchone()[0] == 0
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        with app_conn.transaction():
            app_conn.execute("SELECT set_config('app.organization_id', %s, true)", ("' OR true --",))
            app_conn.execute("SELECT count(*) FROM stores")


def test_user_context_only_exposes_the_users_own_memberships(app_conn, worlds):
    a, b = worlds
    with app_conn.transaction():
        _in_context(app_conn, user_id=a.tenant.owner_id)
        rows = app_conn.execute("SELECT organization_id FROM memberships").fetchall()
        assert rows == [(a.tenant.organization_id,)]
        assert app_conn.execute("SELECT count(*) FROM stores").fetchone()[0] == 0
