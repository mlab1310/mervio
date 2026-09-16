"""Provenance: rapport -> execution -> instantane -> fichiers source -> connexion -> enregistrement d'origine."""
from __future__ import annotations

import hashlib

import pytest

from mervio.application.persisted_analysis import SnapshotImportRequest, analyze_snapshot, import_csv_snapshot
from mervio.config import ENGINE_VERSION, AnalyticsConfig
from mervio.persistence import provenance, snapshots
from mervio.persistence.codec import config_sha256
from mervio.persistence.errors import NotFound, PermissionDenied
from mervio.persistence.tenancy import Role, TenantContext, TenantSession, add_member, ensure_user

from .persistence_support import SAMPLE_FILES


@pytest.fixture
def traced(db, tenant_a):
    imported = import_csv_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                                   request=SnapshotImportRequest(**SAMPLE_FILES))
    outcome = analyze_snapshot(tenant_a.session, store_id=tenant_a.store_id, snapshot_id=imported.snapshot.id)
    return tenant_a, imported.snapshot, outcome


def _file_sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_report_traces_back_to_the_exact_source_files(traced):
    tenant, snapshot, outcome = traced
    chain = provenance.trace_report(tenant.session, tenant.store_id, outcome.report.id)
    assert (chain.organization_id, chain.store_id) == (tenant.organization_id, tenant.store_id)
    assert chain.report_id == outcome.report.id and chain.payload_sha256 == outcome.report.payload_sha256
    assert chain.analysis_run_id == outcome.run.id
    assert chain.engine_version == ENGINE_VERSION
    assert chain.config_sha256 == config_sha256(AnalyticsConfig())
    assert chain.snapshot_id == snapshot.id and chain.inputs_sha256 == snapshot.inputs_sha256
    assert chain.connection_id == tenant.connection_id and chain.connection_kind == "csv_upload"
    assert chain.connector == snapshots.CSV_CONNECTOR and chain.normalization_version == snapshots.NORMALIZATION_VERSION
    assert {s.source_kind: s.file_sha256 for s in chain.sources} == {
        kind: _file_sha(path) for kind, path in SAMPLE_FILES.items()}
    orders = next(s for s in chain.sources if s.source_kind == "shopify_orders")
    assert orders.rows_read >= orders.rows_accepted > 0
    assert chain.synthetic is False


@pytest.mark.parametrize("entity, source, record_id, kind", [
    ("order", "shopify", None, "shopify_orders"),
    ("payment", "stripe", None, "stripe"),
    ("product", "shopify", None, "shopify_products"),
    ("campaign", "google_ads", None, "google_ads"),
])
def test_canonical_record_traces_to_its_source_file(traced, entity, source, record_id, kind):
    tenant, snapshot, _ = traced
    dataset = snapshots.load_dataset(tenant.session, tenant.store_id, snapshot.id)
    record_id = record_id or {"order": lambda: dataset.orders[0].order_id,
                              "payment": lambda: dataset.payments[0].payment_id,
                              "product": lambda: next(iter(dataset.products)),
                              "campaign": lambda: next(iter(dataset.campaigns))}[entity]()
    trace = provenance.trace_record(tenant.session, tenant.store_id, snapshot.id, entity=entity, source=source,
                                    source_record_id=record_id)
    assert (trace.organization_id, trace.store_id, trace.snapshot_id) == (
        tenant.organization_id, tenant.store_id, snapshot.id)
    assert trace.source == source and trace.source_record_id == record_id
    assert trace.source_kind == kind and trace.file_sha256 == _file_sha(SAMPLE_FILES[kind])
    assert trace.connection_id == tenant.connection_id


def test_internal_identity_source_identity_and_business_reference_are_distinct_columns(owner, traced):
    tenant, snapshot, _ = traced
    columns = {r[0] for r in owner.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'orders'").fetchall()}
    assert {"snapshot_id", "position", "source", "source_record_id", "order_ref", "snapshot_source_id"} <= columns
    assert "id" not in columns  # identite interne = (organization_id, snapshot_id, position), jamais l'identifiant Shopify


def test_unknown_record_or_entity(traced):
    tenant, snapshot, _ = traced
    with pytest.raises(NotFound):
        provenance.trace_record(tenant.session, tenant.store_id, snapshot.id, entity="order", source="shopify",
                                source_record_id="#inexistante")
    with pytest.raises(ValueError):
        provenance.trace_record(tenant.session, tenant.store_id, snapshot.id, entity="users; --", source="x",
                                source_record_id="x")


def test_provenance_requires_the_analyst_role(db, traced):
    tenant, _, outcome = traced
    viewer = ensure_user(db, "test|viewer")
    add_member(tenant.session, user_id=viewer, role=Role.VIEWER)
    session = TenantSession(db, TenantContext(tenant.organization_id, viewer))
    with pytest.raises(PermissionDenied):
        provenance.trace_report(session, tenant.store_id, outcome.report.id)
