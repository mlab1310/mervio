"""Revision 0011_identity_pii_schema sur une base neuve (Mission 004.4.2, D-057).

- refus si un instantane NON synthetique existe (aucune conversion silencieuse d'identite);
- la RLS forcee de `data_snapshots` est retablie apres la montee ET apres un refus;
- les anciens instantanes synthetiques (reference = e-mail synthetique) restent lisibles: la garde
  `orders_customer_ref_keyed` est NOT VALID;
- descente refusee si une cle d'identite existe; sinon colonnes e-mail recreees VIDES;
- aller-retour 0010 -> 0011 -> 0010 -> 0011: definitions identiques.
"""
from __future__ import annotations

import json
import secrets
import uuid

import psycopg
import pytest

from mervio.domain.quality import DataQualityReport
from mervio.persistence import migrate, snapshots
from mervio.persistence.codec import quality_to_document
from mervio.persistence.database import Database
from mervio.persistence.tenancy import TenantContext, TenantSession

BEFORE = "0010_qualify_residual_security"
AFTER = "0011_identity_pii_schema"
LEGACY_REF = "legacy.customer@example.invalid"


@pytest.fixture
def fresh(pg):
    name = pg.create_empty_database("m0011")
    try:
        yield pg, name, pg.url("migrator", name)
    finally:
        pg.drop_database(name)


def _forced(url, table="data_snapshots"):
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass",
                            (f"public.{table}",)).fetchone()


def _seed(url, *, synthetic: bool):
    """Organisation, proprietaire, boutique, connexion et un instantane scelle d'une commande (schema 0010)."""
    ids = {k: uuid.uuid4() for k in ("org", "owner", "store", "connection", "snapshot", "source")}
    with psycopg.connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(ids["org"]),))
        conn.execute("INSERT INTO users (id, idp_subject) VALUES (%s, %s)", (ids["owner"], f"test|m11|{ids['owner']}"))
        conn.execute("INSERT INTO organizations (id, name) VALUES (%s, 'm11')", (ids["org"],))
        conn.execute("INSERT INTO memberships (id, organization_id, user_id, role) VALUES (%s, %s, %s, 'owner')",
                     (uuid.uuid4(), ids["org"], ids["owner"]))
        conn.execute("INSERT INTO stores (id, organization_id, name) VALUES (%s, %s, 'm11')", (ids["store"], ids["org"]))
        conn.execute("INSERT INTO connections (id, organization_id, store_id, kind, label) "
                     "VALUES (%s, %s, %s, 'csv_upload', 'm11')", (ids["connection"], ids["org"], ids["store"]))
        conn.execute(
            "INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, source, connector, "
            "connector_version, schema_version, normalization_version, status, ingestion_started_at, synthetic, "
            "not_for_production) VALUES (%s, %s, %s, %s, 'csv', 'mervio.ingestion.csv', '0.1.0', 's', "
            "'mervio-ingestion/0.1.0', 'ingesting', now(), %s, %s)",
            (ids["snapshot"], ids["org"], ids["store"], ids["connection"], synthetic, synthetic))
        conn.execute("INSERT INTO snapshot_sources (id, organization_id, store_id, snapshot_id, source_kind, "
                     "file_sha256, byte_size) VALUES (%s, %s, %s, %s, 'shopify_orders', %s, 1)",
                     (ids["source"], ids["org"], ids["store"], ids["snapshot"], "0" * 64))
        conn.execute(
            "INSERT INTO orders (organization_id, store_id, snapshot_id, snapshot_source_id, position, source, "
            "source_record_id, order_ref, customer_ref, customer_email, created_at, currency, subtotal, discount, "
            "shipping, tax, total, financial_status) VALUES (%s, %s, %s, %s, 0, 'shopify', '#1', '#1', %s, %s, now(), "
            "'EUR', 10, 0, 0, 0, 10, 'paid')",
            (ids["org"], ids["store"], ids["snapshot"], ids["source"], LEGACY_REF, LEGACY_REF))
        counts = {"products": 0, "orders": 1, "order_lines": 0, "payments": 0, "refunds": 0, "campaigns": 0,
                  "ad_daily_performance": 0}
        conn.execute("UPDATE data_snapshots SET status = 'completed', ingested_at = now(), inputs_sha256 = %s, "
                     "currency = 'EUR', row_count = 1, record_counts = %s, quality = %s WHERE id = %s",
                     ("1" * 64, json.dumps(counts), json.dumps(quality_to_document(DataQualityReport())),
                      ids["snapshot"]))
    return ids


def test_0011_refuses_a_database_with_non_synthetic_snapshots_and_keeps_rls_forced(fresh):
    _, _, url = fresh
    migrate.upgrade(url, BEFORE)
    _seed(url, synthetic=False)
    with pytest.raises(Exception) as refused:
        migrate.upgrade(url, AFTER)
    message = str(refused.value)
    assert "non-synthetic data snapshots exist" in message
    assert LEGACY_REF not in message  # aucune donnee dans le message
    assert migrate.current_revision(url) == BEFORE  # rien n'est applique
    assert _forced(url) == (True, True)  # le NO FORCE du controle a ete annule avec la transaction
    with psycopg.connect(url) as conn:  # l'e-mail n'a pas ete converti en silence: rien n'a change
        assert conn.execute("SELECT count(*) FROM information_schema.columns "
                            "WHERE table_name = 'orders' AND column_name = 'customer_email'").fetchone() == (1,)


def test_0011_keeps_legacy_synthetic_snapshots_readable_and_rls_forced(fresh):
    pg, name, url = fresh
    migrate.upgrade(url, BEFORE)
    ids = _seed(url, synthetic=True)
    migrate.upgrade(url, AFTER)
    assert _forced(url) == (True, True) and _forced(url, "organization_identity_keys") == (True, True)
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                            "AND column_name = 'customer_email'").fetchone() == (0,)
    database = Database(pg.url("app", name))
    try:
        session = TenantSession(database, TenantContext(ids["org"], ids["owner"]))
        record = snapshots.get_snapshot(session, ids["store"], ids["snapshot"])
        assert record.normalization_version == "mervio-ingestion/0.1.0"  # identifiable comme ancien (D-057)
        dataset = snapshots.load_dataset(session, ids["store"], ids["snapshot"])
    finally:
        database.close()
    assert [o.customer_id for o in dataset.orders] == [LEGACY_REF]  # NOT VALID: jamais revalidee ni reecrite


def test_0011_downgrade_is_refused_once_an_identity_key_exists(fresh):
    _, _, url = fresh
    migrate.upgrade(url, AFTER)
    ids = _seed_after(url)
    with psycopg.connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(ids["org"]),))
        conn.execute("INSERT INTO organization_identity_keys (organization_id, scheme, salt, master_key_id) "
                     "VALUES (%s, 'hmac-sha256-v1', %s, %s)", (ids["org"], secrets.token_bytes(32), "0" * 16))
    with pytest.raises(Exception, match="organization identity keys exist"):
        migrate.downgrade(url, BEFORE)
    assert migrate.current_revision(url) == AFTER
    assert _forced(url, "organization_identity_keys") == (True, True)


def test_0011_downgrade_recreates_empty_email_columns_without_restoring_anything(fresh):
    _, _, url = fresh
    migrate.upgrade(url, AFTER)
    ids = _seed_after(url)
    migrate.downgrade(url, BEFORE)
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT table_name, is_nullable FROM information_schema.columns WHERE table_schema = "
                            "'public' AND column_name = 'customer_email' ORDER BY 1").fetchall() == [
            ("orders", "YES"), ("payments", "YES")]
        conn.execute("SELECT set_config('app.organization_id', %s, false)", (str(ids["org"]),))
        assert conn.execute("SELECT count(*), count(customer_email) FROM orders").fetchone() == (1, 0)
    assert _forced(url) == (True, True)


def _seed_after(url):
    """Une boutique a la tete 0011 avec un instantane d'une commande (reference a cle, aucun e-mail)."""
    ids = {k: uuid.uuid4() for k in ("org", "store", "connection", "snapshot", "source")}
    with psycopg.connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(ids["org"]),))
        conn.execute("INSERT INTO organizations (id, name) VALUES (%s, 'm11')", (ids["org"],))
        conn.execute("INSERT INTO stores (id, organization_id, name) VALUES (%s, %s, 'm11')", (ids["store"], ids["org"]))
        conn.execute("INSERT INTO connections (id, organization_id, store_id, kind, label) "
                     "VALUES (%s, %s, %s, 'csv_upload', 'm11')", (ids["connection"], ids["org"], ids["store"]))
        conn.execute(
            "INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, source, connector, "
            "connector_version, schema_version, normalization_version, status, ingestion_started_at, synthetic, "
            "not_for_production) VALUES (%s, %s, %s, %s, 'csv', 'c', 'v', 's', 'mervio-normalization/2', 'ingesting', "
            "now(), true, true)", (ids["snapshot"], ids["org"], ids["store"], ids["connection"]))
        conn.execute("INSERT INTO snapshot_sources (id, organization_id, store_id, snapshot_id, source_kind, "
                     "file_sha256, byte_size) VALUES (%s, %s, %s, %s, 'shopify_orders', %s, 1)",
                     (ids["source"], ids["org"], ids["store"], ids["snapshot"], "0" * 64))
        conn.execute(
            "INSERT INTO orders (organization_id, store_id, snapshot_id, snapshot_source_id, position, source, "
            "source_record_id, order_ref, customer_ref, created_at, currency, subtotal, discount, shipping, tax, total, "
            "financial_status) VALUES (%s, %s, %s, %s, 0, 'shopify', '#1', '#1', %s, now(), 'EUR', 10, 0, 0, 0, 10, "
            "'paid')", (ids["org"], ids["store"], ids["snapshot"], ids["source"], "g1:" + "a" * 32))
    return ids


def _definitions(url):
    with psycopg.connect(url) as conn:
        return conn.execute("""
            SELECT 'f', p.proname, pg_get_functiondef(p.oid), coalesce(p.proacl::text, '') FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'
            UNION ALL SELECT 'p', polname, coalesce(pg_get_expr(polqual, polrelid), '') || '|'
                || coalesce(pg_get_expr(polwithcheck, polrelid), '') || '|' || polpermissive::text, '' FROM pg_policy
            UNION ALL SELECT 't', tgname, pg_get_triggerdef(t.oid), '' FROM pg_trigger t WHERE NOT tgisinternal
            UNION ALL SELECT 'c', conname, pg_get_constraintdef(c.oid), '' FROM pg_constraint c
                JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public'
            UNION ALL SELECT 'r', c.relname, c.relrowsecurity::text || c.relforcerowsecurity::text,
                coalesce(c.relacl::text, '') FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY 1, 2""").fetchall()


def test_0010_0011_roundtrip_restores_identical_definitions(fresh):
    _, _, url = fresh
    migrate.upgrade(url, BEFORE)
    before = _definitions(url)
    migrate.upgrade(url, AFTER)
    after = _definitions(url)
    migrate.downgrade(url, BEFORE)
    assert _definitions(url) == before
    migrate.upgrade(url, AFTER)
    assert _definitions(url) == after
    assert before != after
    # aucune fonction SECURITY DEFINER ajoutee par 0011
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT array_agg(proname) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                            "WHERE n.nspname = 'public' AND p.prosecdef").fetchone() == (["app_ensure_service_principal"],)
