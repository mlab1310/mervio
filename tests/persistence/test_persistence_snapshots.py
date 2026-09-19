"""Instantanes: import CSV -> PostgreSQL -> Dataset identique, immuable, idempotent."""
from __future__ import annotations

import shutil
import uuid
from dataclasses import replace
from datetime import datetime

import psycopg
import pytest

from mervio.analytics.pipeline import SourcePaths, load_dataset
from mervio.application.persisted_analysis import SnapshotImportRequest, import_csv_snapshot
from mervio.domain.models import Dataset, Order, OrderItem
from mervio.domain.quality import DataQualityReport
from mervio.persistence import snapshots
from mervio.persistence.codec import quality_from_document, quality_to_document
from mervio.persistence.errors import MoneyPrecisionError, NotFound, SnapshotIntegrityError
from mervio.persistence.snapshots import SourceFile, write_snapshot
from mervio.persistence.stores import create_store, revoke_connection

from .persistence_support import SAMPLE_FILES, TEST_MASTER_KEY, engine_files, make_tenant, org_identity


def _import(tenant, files, **extra):
    return import_csv_snapshot(tenant.session, store_id=tenant.store_id, connection_id=tenant.connection_id,
                               request=SnapshotImportRequest(**files, **extra), master_key=TEST_MASTER_KEY)


def test_sample_import_restores_a_dataset_equal_to_the_csv_ingestion(db, tenant_a):
    result = _import(tenant_a, SAMPLE_FILES)
    assert result.status == "completed"
    record = result.snapshot
    assert record.status == "completed" and record.ingested_at is not None
    expected = load_dataset(SourcePaths(**SAMPLE_FILES), org_identity(tenant_a))  # D-058: meme cle
    restored = snapshots.load_dataset(tenant_a.session, tenant_a.store_id, record.id)
    assert restored == expected
    # ordre des listes et des dictionnaires: condition des sommes flottantes identiques
    assert [o.order_id for o in restored.orders] == [o.order_id for o in expected.orders]
    assert list(restored.products) == list(expected.products)
    assert list(restored.quality.fields) == list(expected.quality.fields)
    assert list(restored.quality.row_counts) == list(expected.quality.row_counts)
    assert record.row_count == sum(record.record_counts.values())
    assert record.record_counts["orders"] == len(expected.orders)
    assert record.currency == expected.currency


def test_snapshot_metadata_records_the_import(db, tenant_a):
    record = _import(tenant_a, SAMPLE_FILES).snapshot
    assert (record.organization_id, record.store_id, record.connection_id) == (
        tenant_a.organization_id, tenant_a.store_id, tenant_a.connection_id)
    assert record.created_by == tenant_a.owner_id
    assert record.source == "csv" and record.connector == snapshots.CSV_CONNECTOR
    assert record.normalization_version == snapshots.NORMALIZATION_VERSION
    assert record.schema_version == snapshots.CSV_SCHEMA_VERSION
    assert len(record.inputs_sha256) == 64
    assert record.source_period_start <= record.source_period_end
    sources = snapshots.snapshot_sources(tenant_a.session, tenant_a.store_id, record.id)
    assert {s["source_kind"] for s in sources} == set(SAMPLE_FILES)
    assert all(len(s["file_sha256"]) == 64 and s["byte_size"] > 0 for s in sources)
    assert record.synthetic is False and record.not_for_production is False


def test_reimporting_identical_files_reuses_the_sealed_snapshot(db, tenant_a):
    first = _import(tenant_a, SAMPLE_FILES)
    second = _import(tenant_a, SAMPLE_FILES)
    assert second.status == "reused" and second.snapshot.id == first.snapshot.id
    assert len(snapshots.list_snapshots(tenant_a.session, tenant_a.store_id)) == 1


def test_a_changed_file_produces_a_new_snapshot_and_never_overwrites_the_old_one(db, tenant_a, tmp_path):
    first = _import(tenant_a, SAMPLE_FILES).snapshot
    changed = tmp_path / "shopify_orders.csv"
    shutil.copy(SAMPLE_FILES["shopify_orders"], changed)
    with changed.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    second = _import(tenant_a, {**SAMPLE_FILES, "shopify_orders": str(changed)}).snapshot
    assert second.id != first.id and second.inputs_sha256 != first.inputs_sha256
    assert snapshots.get_snapshot(tenant_a.session, tenant_a.store_id, first.id) == first


def test_synthetic_flag_changes_the_import_identity(db, tenant_a):
    real = _import(tenant_a, SAMPLE_FILES).snapshot
    synthetic = _import(tenant_a, SAMPLE_FILES, synthetic=True).snapshot
    assert synthetic.id != real.id and synthetic.synthetic and synthetic.not_for_production


def test_invalid_files_are_rejected_and_the_refusal_is_traced(db, tenant_a, tmp_path):
    broken = tmp_path / "orders.csv"
    broken.write_text("foo,bar\n1,2\n", encoding="utf-8")
    result = _import(tenant_a, {"shopify_orders": str(broken)})
    assert result.status == "rejected" and result.error_code == "validation_failed"
    assert result.snapshot.status == "failed" and result.snapshot.failure_code == "validation_failed"
    assert result.snapshot.row_count is None
    with pytest.raises(SnapshotIntegrityError):
        snapshots.load_dataset(tenant_a.session, tenant_a.store_id, result.snapshot.id)
    sources = snapshots.snapshot_sources(tenant_a.session, tenant_a.store_id, result.snapshot.id)
    assert [s["source_kind"] for s in sources] == ["shopify_orders"]


def test_revoked_connection_cannot_import(db, tenant_a):
    revoke_connection(tenant_a.session, tenant_a.store_id, tenant_a.connection_id)
    result = _import(tenant_a, SAMPLE_FILES)
    assert result.status == "rejected" and result.error_code == "connection_revoked"


def test_currency_different_from_the_store_currency_is_rejected(db):
    tenant = make_tenant(db, "USD", currency="USD")
    result = _import(tenant, SAMPLE_FILES)
    assert result.status == "rejected" and result.error_code == "currency_mismatch_with_store"


def test_matching_store_currency_is_accepted(db):
    currency = load_dataset(SourcePaths(**SAMPLE_FILES)).currency
    tenant = make_tenant(db, "EURO", currency=currency)
    assert _import(tenant, SAMPLE_FILES).status == "completed"


# -- immuabilite -------------------------------------------------------------------------------

@pytest.fixture
def sealed(db, tenant_a):
    return _import(tenant_a, SAMPLE_FILES).snapshot


def _with_context(conn, organization_id):
    conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))


def test_sealed_snapshot_cannot_be_updated_even_by_the_table_owner(owner, sealed):
    with pytest.raises(psycopg.errors.RestrictViolation):
        with owner.transaction():
            _with_context(owner, sealed.organization_id)
            owner.execute("UPDATE data_snapshots SET currency = 'XXX' WHERE id = %s", (sealed.id,))


def test_application_role_cannot_update_a_sealed_snapshot(app_conn, sealed):
    with pytest.raises(psycopg.errors.RestrictViolation):
        with app_conn.transaction():
            _with_context(app_conn, sealed.organization_id)
            app_conn.execute("UPDATE data_snapshots SET row_count = 0 WHERE id = %s", (sealed.id,))


def test_records_cannot_be_added_to_a_sealed_snapshot(owner, sealed):
    # hors contexte tenant, le proprietaire lui-meme ne voit rien (RLS FORCEE)
    assert owner.execute("SELECT id FROM snapshot_sources WHERE snapshot_id = %s", (sealed.id,)).fetchall() == []
    with pytest.raises(psycopg.errors.RestrictViolation):
        with owner.transaction():
            _with_context(owner, sealed.organization_id)
            source_id = owner.execute("SELECT id FROM snapshot_sources WHERE snapshot_id = %s AND source_kind = "
                                      "'stripe'", (sealed.id,)).fetchone()[0]
            owner.execute(
                "INSERT INTO refunds (organization_id, store_id, snapshot_id, snapshot_source_id, position, source, "
                "source_record_id, refund_ref, created_at, amount) VALUES (%s, %s, %s, %s, 999999, 'stripe', 'x', 'x', "
                "now(), 1)", (sealed.organization_id, sealed.store_id, sealed.id, source_id))


@pytest.mark.parametrize("statement", [
    "UPDATE orders SET subtotal = 0 WHERE snapshot_id = %s",
    "UPDATE order_lines SET quantity = 0 WHERE snapshot_id = %s",
    "DELETE FROM orders WHERE snapshot_id = %s",
    "DELETE FROM order_lines WHERE snapshot_id = %s",
    "DELETE FROM snapshot_sources WHERE snapshot_id = %s",
])
def test_records_of_a_sealed_snapshot_cannot_be_modified_or_deleted(owner, sealed, statement):
    with pytest.raises(psycopg.errors.RestrictViolation):
        with owner.transaction():
            _with_context(owner, sealed.organization_id)
            owner.execute(statement, (sealed.id,))


def test_import_is_all_or_nothing(db, tenant_a):
    identity = org_identity(tenant_a)  # 004.4.2 (F-08): cle d'organisation, seul le defaut vise est en cause
    dataset = load_dataset(SourcePaths(**SAMPLE_FILES), identity)
    last = dataset.orders[-1]
    dataset.orders[-1] = replace(last, total=12.345678)  # non representable: refuse en fin d'ecriture
    sources = [SourceFile(kind, "0" * 64, 1) for kind in SAMPLE_FILES]
    with pytest.raises(MoneyPrecisionError):
        write_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                       dataset=dataset, sources=sources, inputs_sha256="1" * 64, synthetic=False, identity=identity)
    assert snapshots.list_snapshots(tenant_a.session, tenant_a.store_id) == []


@pytest.mark.parametrize("corrupt", ["line_of_other_order", "product_key", "integer_money", "aware_datetime",
                                     "missing_source_file"])
def test_inconsistent_datasets_are_refused(db, tenant_a, corrupt):
    at = datetime(2026, 9, 1, 12)
    # 004.4.2: reference client calculee avec la cle de l'ORGANISATION (F-08), pour que seul le defaut
    # vise soit en cause
    identity = org_identity(tenant_a)
    order = Order("#1", identity.ref_email("c@example.invalid"), at, "EUR", 10.0, 0.0, 0.0,
                  0.0, 10.0, "paid",
                  items=[OrderItem("#1", "A", "A", 1, 10.0)])
    dataset = Dataset(orders=[order], quality=DataQualityReport(), currency="EUR", identity_key_id=identity.key_id)
    kinds = ["shopify_orders"]
    if corrupt == "line_of_other_order":
        order.items[0] = OrderItem("#2", "A", "A", 1, 10.0)
    elif corrupt == "product_key":
        from mervio.domain.models import Product
        dataset.products = {"B": Product("A", "A", "A", 1.0)}
        kinds.append("shopify_products")
    elif corrupt == "integer_money":
        order.subtotal = 10
    elif corrupt == "aware_datetime":
        from datetime import timezone
        order.created_at = at.replace(tzinfo=timezone.utc)
    elif corrupt == "missing_source_file":
        kinds = ["stripe"]
    with pytest.raises((SnapshotIntegrityError, MoneyPrecisionError)):
        write_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                       dataset=dataset, sources=[SourceFile(k, "0" * 64, 1) for k in kinds],
                       inputs_sha256="2" * 64, synthetic=False, identity=identity)
    assert snapshots.list_snapshots(tenant_a.session, tenant_a.store_id) == []


def test_quality_document_roundtrip_is_lossless():
    expected = load_dataset(SourcePaths(**SAMPLE_FILES)).quality
    assert quality_from_document(quality_to_document(expected)) == expected


def test_unknown_snapshot_and_store_are_not_found(db, tenant_a, sealed):
    with pytest.raises(NotFound):
        snapshots.get_snapshot(tenant_a.session, tenant_a.store_id, uuid.uuid4())
    other_store = create_store(tenant_a.session, name="Autre boutique")
    with pytest.raises(NotFound):
        snapshots.get_snapshot(tenant_a.session, other_store.id, sealed.id)
    with pytest.raises(NotFound):
        snapshots.load_dataset(tenant_a.session, other_store.id, sealed.id)


def test_synthetic_generator_dataset_roundtrips(db, tenant_a, synthetic_set):
    files = engine_files(synthetic_set.directory)
    result = _import(tenant_a, files, synthetic_manifest=synthetic_set.manifest)
    assert result.status == "completed" and result.snapshot.synthetic and result.snapshot.not_for_production
    assert result.snapshot.synthetic_manifest["not_for_production"] is True
    restored = snapshots.load_dataset(tenant_a.session, tenant_a.store_id, result.snapshot.id)
    assert restored == load_dataset(SourcePaths(**files), org_identity(tenant_a))
