"""Politique de suppression: jamais de cascade silencieuse sur l'historique analytique.

- organisation, boutique, connexion: RESTRICT tant qu'un enfant existe (purge explicite et ordonnee, 004.2);
- instantane: RESTRICT s'il est reference par une analyse; sinon sa suppression (purge) emporte en CASCADE
  ses fichiers source et lignes canoniques, qui n'ont pas de sens sans lui;
- execution et rapport: RESTRICT; le role applicatif n'a aucun droit DELETE sur l'historique.
"""
from __future__ import annotations

import psycopg
import pytest

from mervio.application.persisted_analysis import SnapshotImportRequest, analyze_snapshot, import_csv_snapshot

from .persistence_support import SAMPLE_FILES


def _context(conn, organization_id):
    conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))


@pytest.fixture
def history(db, tenant_a):
    imported = import_csv_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                                   request=SnapshotImportRequest(**SAMPLE_FILES))
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=imported.snapshot.id)
    return tenant_a, imported.snapshot, outcome


@pytest.mark.parametrize("statement, target", [
    ("DELETE FROM organizations WHERE id = %s", "organization"),
    ("DELETE FROM stores WHERE id = %s", "store"),
    ("DELETE FROM connections WHERE id = %s", "connection"),
    ("DELETE FROM data_snapshots WHERE id = %s", "snapshot"),
    ("DELETE FROM analysis_runs WHERE id = %s", "run"),
])
def test_parents_of_history_are_restricted_even_for_the_owner(owner, history, statement, target):
    tenant, snapshot, outcome = history
    ids = {"organization": tenant.organization_id, "store": tenant.store_id, "connection": tenant.connection_id,
           "snapshot": snapshot.id, "run": outcome.run.id}
    with pytest.raises(psycopg.errors.IntegrityError):  # RESTRICT: ForeignKeyViolation ou RestrictViolation
        with owner.transaction():
            _context(owner, tenant.organization_id)
            owner.execute(statement, (ids[target],))


def test_purging_a_report_then_its_run_then_its_snapshot_cascades_only_to_snapshot_content(owner, history):
    tenant, snapshot, outcome = history
    with owner.transaction():
        _context(owner, tenant.organization_id)
        assert owner.execute("DELETE FROM reports WHERE id = %s", (outcome.report.id,)).rowcount == 1
        assert owner.execute("DELETE FROM analysis_runs WHERE id = %s", (outcome.run.id,)).rowcount == 1
        assert owner.execute("DELETE FROM data_snapshots WHERE id = %s", (snapshot.id,)).rowcount == 1
        for table in ("snapshot_sources", "orders", "order_lines", "payments", "refunds", "products", "campaigns",
                      "ad_daily_performance"):
            assert owner.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
        # la boutique, la connexion et l'organisation restent
        assert owner.execute("SELECT count(*) FROM connections WHERE id = %s",
                             (tenant.connection_id,)).fetchone()[0] == 1


@pytest.mark.parametrize("table", ["data_snapshots", "orders", "order_lines", "analysis_runs", "reports", "stores",
                                   "connections", "organizations"])
def test_application_role_has_no_delete_privilege_on_history(app_conn, history, table):
    tenant, _, _ = history
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with app_conn.transaction():
            _context(app_conn, tenant.organization_id)
            app_conn.execute(f"DELETE FROM {table}")


def test_application_role_cannot_truncate(app_conn, history):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("TRUNCATE reports")


def test_user_with_memberships_cannot_be_deleted(owner, history):
    tenant, _, _ = history
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        owner.execute("DELETE FROM users WHERE id = %s", (tenant.owner_id,))
