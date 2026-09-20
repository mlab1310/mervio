"""Migrations et schema (Mission 004.1): reproductibles, reversibles, sans float monetaire, RLS partout."""
from __future__ import annotations

import psycopg
import pytest

from mervio.persistence import migrate

TENANT_TABLES = ("organizations", "memberships", "stores", "connections", "data_snapshots", "snapshot_sources",
                 "products", "orders", "order_lines", "payments", "refunds", "campaigns", "ad_daily_performance",
                 "analysis_runs", "reports", "jobs", "audit_events", "service_authorizations",
                 "organization_identity_keys", "raw_objects")

MONEY_COLUMNS = {
    ("products", "unit_cogs"), ("orders", "subtotal"), ("orders", "discount"), ("orders", "shipping"),
    ("orders", "tax"), ("orders", "total"), ("order_lines", "unit_price"), ("payments", "amount"),
    ("payments", "fee"), ("payments", "net"), ("refunds", "amount"), ("ad_daily_performance", "spend"),
    ("ad_daily_performance", "conversion_value"),
}


@pytest.fixture
def empty_database(pg):
    name = pg.create_empty_database("migrations")
    try:
        yield name, pg.url("migrator", name)
    finally:
        pg.drop_database(name)


def _tables(url):
    with psycopg.connect(url) as conn:
        return {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()}


def test_revisions_are_linear_and_versioned():
    assert migrate.revisions() == ["0001_tenancy", "0002_snapshots", "0003_analysis_reports", "0004_jobs",
                                   "0005_audit", "0006_job_leases", "0007_service_identity", "0008_admin_audit",
                                   "0009_qualify_security_functions", "0010_qualify_residual_security",
                                   "0011_identity_pii_schema", "0012_dispatch_probe_plan",
                                   "0013_raw_objects"]
    assert migrate.head_revision() == "0013_raw_objects"


def test_empty_database_upgrades_to_head(empty_database):
    _, url = empty_database
    assert migrate.current_revision(url) is None
    migrate.upgrade(url)
    assert migrate.current_revision(url) == migrate.head_revision()
    assert set(TENANT_TABLES) | {"users", "alembic_version"} == _tables(url)


def test_each_revision_upgrades_and_downgrades_step_by_step(empty_database):
    _, url = empty_database
    expected = {
        "0001_tenancy": {"organizations", "users", "memberships", "stores", "connections"},
        "0002_snapshots": {"data_snapshots", "snapshot_sources", "products", "orders", "order_lines", "payments",
                           "refunds", "campaigns", "ad_daily_performance"},
        "0003_analysis_reports": {"analysis_runs", "reports"},
        "0004_jobs": {"jobs"},
        "0005_audit": {"audit_events"},
        # 004.3: garde de transition remplacee (bail et jeton d'exclusion), aucune table
        "0006_job_leases": set(),
        # 004.3: identite de service et autorisations explicites par organisation
        "0007_service_identity": {"service_authorizations"},
        # 004.3.7: vocabulaire d'audit de l'administration, aucune table
        "0008_admin_audit": set(),
        # 004.3.10: qualification des fonctions de securite (anti-ombrage pg_temp), aucune table
        "0009_qualify_security_functions": set(),
        # 004.4.1: gardes d'instantanes qualifiees (risque residuel D-049), aucune table
        "0010_qualify_residual_security": set(),
        # 004.4.2: sel d'identite d'organisation; e-mails retires des lignes canoniques
        "0011_identity_pii_schema": {"organization_identity_keys"},
        # sonde du dispatcher: expression de politique et index de disponibilite, aucune table
        "0012_dispatch_probe_plan": set(),
        # 004.4.4: objets bruts par tenant et vocabulaire d'audit associe
        "0013_raw_objects": {"raw_objects"},
    }
    present = {"alembic_version"}
    for revision in migrate.revisions():
        migrate.upgrade(url, revision)
        present |= expected[revision]
        assert migrate.current_revision(url) == revision
        assert _tables(url) == present
    for revision in reversed(migrate.revisions()):
        migrate.downgrade(url, "-1")
        present -= expected[revision]
        assert _tables(url) == present
    assert migrate.current_revision(url) is None
    # reproductible: remonter apres une descente complete redonne le meme schema
    migrate.upgrade(url)
    assert migrate.current_revision(url) == migrate.head_revision()


def test_schema_is_identical_whatever_the_migration_path(pg, empty_database):
    """Base vide -> head d'un coup == base de session (montee revision par revision en conftest ou non)."""
    _, url = empty_database
    migrate.upgrade(url, "0001_tenancy")
    migrate.upgrade(url, "0002_snapshots")
    migrate.upgrade(url, "0003_analysis_reports")
    migrate.upgrade(url, "0004_jobs")
    migrate.upgrade(url)

    def columns(target):
        with psycopg.connect(target) as conn:
            return conn.execute(
                "SELECT table_name, column_name, data_type, numeric_precision, numeric_scale, is_nullable "
                "FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1, 2").fetchall()
    assert columns(url) == columns(pg.url("migrator"))


def test_every_tenant_table_has_forced_rls_and_an_organization_boundary(owner):
    rows = dict(owner.execute(
        "SELECT c.relname, (c.relrowsecurity AND c.relforcerowsecurity) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r'").fetchall())
    for table in TENANT_TABLES:
        assert rows[table] is True, table
        column = owner.execute(
            "SELECT is_nullable FROM information_schema.columns WHERE table_name = %s AND column_name = 'organization_id'",
            (table,)).fetchone()
        if table == "organizations":
            continue
        assert column == ("NO",), table
    # seule exception documentee: l'identite plateforme, sans donnee de tenant
    assert rows["users"] is False
    policies = {r[0] for r in owner.execute("SELECT tablename FROM pg_policies").fetchall()}
    assert set(TENANT_TABLES) <= policies


def test_store_owned_tables_carry_store_id(owner):
    # jobs et audit_events couvrent aussi des operations d'organisation: store_id y est facultatif;
    # service_authorizations (004.3) autorise un service pour toute l'organisation, comme memberships;
    # organization_identity_keys (004.4.2): une cle d'identite par organisation, pas par boutique
    for table in set(TENANT_TABLES) - {"organizations", "memberships", "stores", "jobs", "audit_events",
                                       "service_authorizations", "organization_identity_keys"}:
        assert owner.execute(
            "SELECT is_nullable FROM information_schema.columns WHERE table_name = %s AND column_name = 'store_id'",
            (table,)).fetchone() == ("NO",), table


def test_money_is_numeric_19_4_and_no_float_column_holds_money(owner):
    rows = owner.execute(
        "SELECT table_name, column_name, data_type, numeric_precision, numeric_scale FROM information_schema.columns "
        "WHERE table_schema = 'public'").fetchall()
    for table, column, data_type, precision, scale in rows:
        if (table, column) in MONEY_COLUMNS:
            assert (data_type, precision, scale) == ("numeric", 19, 4), (table, column)
        if data_type in ("real", "double precision"):
            # le seul flottant: un compte de conversions attribue par la regie, pas un montant
            assert (table, column) == ("ad_daily_performance", "conversions")
    found = {(t, c) for t, c, *_ in rows}
    assert MONEY_COLUMNS <= found


def test_timestamps_are_timestamptz(owner):
    rows = owner.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND data_type = 'timestamp without time zone'").fetchall()
    assert rows == []


def test_application_role_is_not_privileged(owner):
    superuser, bypass, login = owner.execute(
        "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = 'mervio_app'").fetchone()
    assert (superuser, bypass, login) == (False, False, False)


def test_application_role_cannot_delete_or_rewrite_history(owner):
    def allowed(table, privilege):
        return owner.execute("SELECT has_table_privilege('mervio_app', %s, %s)", (table, privilege)).fetchone()[0]

    for table in ("data_snapshots", "snapshot_sources", "products", "orders", "order_lines", "payments", "refunds",
                  "campaigns", "ad_daily_performance", "analysis_runs", "reports", "organizations", "stores",
                  "connections", "users"):
        assert not allowed(table, "DELETE"), table
        assert not allowed(table, "TRUNCATE"), table
    for table in ("snapshot_sources", "products", "orders", "order_lines", "payments", "refunds", "campaigns",
                  "ad_daily_performance", "reports", "users", "audit_events"):
        assert not allowed(table, "UPDATE"), table
    # 004.2: jobs et audit_events sont les seules tables ou la purge supprime; ni l'une ni
    # l'autre ne se vident en bloc, et la politique RESTRICTIVE borne ce que DELETE atteint
    for table in ("jobs", "audit_events"):
        assert allowed(table, "DELETE"), table
        assert not allowed(table, "TRUNCATE"), table
    assert allowed("memberships", "DELETE")
    # colonnes d'identite jamais modifiables par l'application
    for table, column in (("stores", "organization_id"), ("connections", "store_id"),
                          ("data_snapshots", "organization_id"), ("analysis_runs", "snapshot_id")):
        assert not owner.execute("SELECT has_column_privilege('mervio_app', %s, %s, 'UPDATE')",
                                 (table, column)).fetchone()[0]


def test_tenant_indexes_lead_with_the_organization(owner):
    indexes = dict(owner.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'").fetchall())
    for name in ("data_snapshots_store_recent_idx", "analysis_runs_store_recent_idx", "reports_store_recent_idx",
                 "data_snapshots_inputs_uniq", "analysis_runs_snapshot_idx", "reports_snapshot_idx"):
        assert "(organization_id, store_id" in indexes[name], name


def test_only_the_primary_key_starts_with_organization_and_snapshot(owner):
    """Regression (import quadratique): aucun index secondaire ne concurrence la cle primaire des lignes canoniques."""
    rows = owner.execute(
        "SELECT t.relname, i.relname, x.indisprimary, "
        "       array(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY k(attnum, n) "
        "             JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum ORDER BY k.n) "
        "FROM pg_index x JOIN pg_class t ON t.oid = x.indrelid JOIN pg_class i ON i.oid = x.indexrelid "
        "WHERE t.relname IN ('products', 'orders', 'order_lines', 'payments', 'refunds', 'campaigns', "
        "'ad_daily_performance')").fetchall()
    for table, index, primary, columns in rows:
        assert columns[0] == "organization_id", (table, index)
        if not primary:
            assert columns[:2] != ["organization_id", "snapshot_id"], (table, index)
        else:
            assert columns[:3] == ["organization_id", "snapshot_id", "order_position" if table == "order_lines"
                                   else "position"], (table, index)


def test_migrations_refuse_to_run_without_an_explicit_url(monkeypatch):
    monkeypatch.delenv("MERVIO_MIGRATION_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        migrate.upgrade()


# -- Mission 004.2: file de travaux et audit -------------------------------------------------

def test_the_new_tenant_indexes_lead_with_the_organization(owner):
    """Lecon 004.1: un index de tenant commence par organization_id, sinon le plan derape."""
    indexes = dict(owner.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "AND tablename IN ('jobs', 'audit_events')").fetchall())
    for name, definition in indexes.items():
        if name.endswith("_pkey") or "_tenant_key" in name:
            continue  # cle primaire technique et cle composite de tenant
        assert "(organization_id" in definition, name
    for name in ("jobs_ready_idx", "jobs_lease_idx", "jobs_terminal_idx", "jobs_recent_idx",
                 "jobs_idempotency_uniq", "audit_events_recent_idx", "audit_events_resource_idx",
                 "audit_events_correlation_idx"):
        assert name in indexes, name


def test_the_ready_queue_index_is_partial_and_ordered_by_priority(owner):
    definition = owner.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'jobs_ready_idx'").fetchone()[0]
    assert "WHERE (status = 'queued'::text)" in definition
    assert "priority DESC" in definition


def test_the_idempotency_key_is_prefixed_by_the_organization(owner):
    """Sinon une violation d'unicite revelerait l'existence du travail d'un autre tenant."""
    definition = owner.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'jobs_idempotency_uniq'").fetchone()[0]
    assert "(organization_id, job_type, idempotency_key)" in definition
    assert "UNIQUE" in definition


def test_deletion_is_restricted_by_a_restrictive_policy(owner):
    """Politique RESTRICTIVE: elle s'ajoute par ET, elle ne peut pas etre contournee par une autre."""
    policies = {row[0]: row for row in owner.execute(
        "SELECT p.polname, p.polpermissive, p.polcmd, c.relname FROM pg_policy p "
        "JOIN pg_class c ON c.oid = p.polrelid WHERE c.relname IN ('jobs', 'audit_events')").fetchall()}
    assert policies["jobs_purge_terminal_only"][1] is False
    assert policies["jobs_purge_terminal_only"][2] == "d"
    assert policies["audit_events_retention_floor"][1] is False
    assert policies["audit_events_retention_floor"][2] == "d"
    assert policies["jobs_tenant_isolation"][1] is True
    assert policies["audit_events_tenant_isolation"][1] is True


def test_the_job_state_machine_lives_in_the_database(owner):
    triggers = {row[0] for row in owner.execute(
        "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE c.relname IN ('jobs', 'audit_events') AND NOT t.tgisinternal").fetchall()}
    assert {"jobs_guard_transition", "jobs_guard_insert", "audit_events_forbid_update"} <= triggers


def test_the_job_table_keeps_its_invariants_as_check_constraints(owner):
    constraints = {row[0] for row in owner.execute(
        "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname = 'jobs' AND c.contype = 'c'").fetchall()}
    for name in ("jobs_lock_consistent", "jobs_finished_consistent", "jobs_attempts_bounded",
                 "jobs_result_only_when_succeeded", "jobs_payload_is_object"):
        assert name in constraints, name


def test_the_new_tables_reference_their_store_with_a_composite_key(owner):
    """Cle etrangere composite: une reference vers la boutique d'un autre tenant est impossible."""
    for table in ("jobs", "audit_events"):
        definition = owner.execute(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = %s AND c.conname = %s", (table, f"{table}_store_fkey")).fetchone()[0]
        assert definition.startswith("FOREIGN KEY (organization_id, store_id)"), table
        assert "REFERENCES stores(organization_id, id)" in definition, table
