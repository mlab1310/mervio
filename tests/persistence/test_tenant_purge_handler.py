"""Gestionnaires de purge tenant: le chemin REEL du worker (004.4.6, D-051, D-065, D-066).

Les tests de `test_tenant_purge.py` appellent les operations privilegiees une par une, en SQL.
Ceux-ci executent les GESTIONNAIRES, comme le worker le fait: travail mis en file par
`enqueue_job`, pris par `claim_next_job`, session DELEGUEE au demandeur, jeton d'exclusion
reel. C'est la seule facon de prouver que l'ORDRE tient -- l'ordre etant precisement ce
qu'une suite d'appels manuels ne prouve pas.

L'invariant central, verifie sous plusieurs angles:

    une boutique ou une organisation `purged` implique des octets detruits et des donnees
    supprimees; jamais l'inverse.
"""
from __future__ import annotations

import io
import uuid

import pytest

from mervio.persistence import jobs, tenant_purge
from mervio.persistence.jobs import JobType
from mervio.persistence.service import ServiceSession, authorize_service, delegated_session
from mervio.storage import MemoryObjectStore, ObjectStoreError
from mervio.workers.errors import RetryableJobError
from mervio.workers.handlers import (JobContext, make_purge_organization_handler,
                                     make_purge_store_handler)

from .persistence_support import make_tenant
from .test_customer_erasure import ALICE, BOB, _raw_object, _sealed_snapshot_with_orders
from .test_customer_erasure_handler import PAYLOAD_BYTES, Store, _NullLog, _key_of


@pytest.fixture
def store():
    return Store()


@pytest.fixture
def purgeable(db, owner, principal, store):
    """Un locataire complet, avec des OCTETS reellement deposes sous chaque cle."""
    tenant = make_tenant(db, "p3")
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE, BOB])
    objects = {}
    for kind in ("shopify_orders", "stripe", "shopify_products", "google_ads"):
        object_id = _raw_object(owner, tenant, kind)
        objects[kind] = object_id
        store.put(_key_of(owner, tenant, object_id), io.BytesIO(PAYLOAD_BYTES))
    return tenant, objects


@pytest.fixture
def neighbour(db, owner, principal, store):
    """Une organisation VOISINE, avec ses propres octets. Aucune purge ne doit l'atteindre."""
    tenant = make_tenant(db, "p3-voisin")
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE])
    object_id = _raw_object(owner, tenant, "shopify_orders")
    key = _key_of(owner, tenant, object_id)
    store.put(key, io.BytesIO(PAYLOAD_BYTES))
    return tenant, key


def _seed_identity_key(owner, tenant):
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(tenant.organization_id),))
        owner.execute(
            "INSERT INTO organization_identity_keys (organization_id, scheme, salt, master_key_id) "
            "VALUES (%s, 'hmac-sha256-v1', %s, %s)",
            (tenant.organization_id, b"\x02" * 32, "1" * 16))


def _run(worker_db, principal, tenant, store, job, *, organization: bool, log=None):
    """Execute le gestionnaire EXACTEMENT comme le worker: session deleguee, bail reel."""
    claimed = jobs.claim_next_job(tenant.session, worker_id="worker-1", job_id=job.id)
    assert claimed is not None, "le travail n'a pas ete pris"
    session = delegated_session(ServiceSession(worker_db, tenant.organization_id, principal), claimed)
    handler = (make_purge_organization_handler(store) if organization
               else make_purge_store_handler(store))
    return handler(JobContext(session=session, job=claimed, correlation_id=str(claimed.correlation_id),
                              log=log or _NullLog(), actor_id=principal.id,
                              on_behalf_of=claimed.enqueued_by))


def _enqueue_store(tenant):
    return jobs.enqueue_job(tenant.session, job_type=JobType.PURGE_STORE,
                            payload={}, store_id=tenant.store_id)


def _enqueue_organization(tenant):
    return jobs.enqueue_job(tenant.session, job_type=JobType.PURGE_ORGANIZATION, payload={})


def _rows(owner, tenant, table: str) -> int:
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(tenant.organization_id),))
        return owner.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _store_state(owner, tenant):
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(tenant.organization_id),))
        return owner.execute("SELECT name, currency, status FROM stores WHERE id = %s",
                             (tenant.store_id,)).fetchone()


def _organization_state(owner, tenant):
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(tenant.organization_id),))
        return owner.execute("SELECT name, status FROM organizations WHERE id = %s",
                             (tenant.organization_id,)).fetchone()


# -- purge de boutique ---------------------------------------------------------------------------

def test_the_store_purge_destroys_the_bytes_and_tombstones_the_store(purgeable, worker_db,
                                                                     principal, store, owner):
    """Le parcours complet d'une boutique, par le chemin du worker et lui seul."""
    tenant, objects = purgeable
    keys = {_key_of(owner, tenant, object_id) for object_id in objects.values()}
    assert keys <= set(store._objects), "les octets doivent exister AVANT"

    result = _run(worker_db, principal, tenant, store, _enqueue_store(tenant), organization=False)

    assert set(store.deleted) == keys, "tous les octets de la boutique ont ete detruits"
    assert not (keys & set(store._objects)), "aucun octet ne subsiste"
    assert _rows(owner, tenant, "raw_objects") == 0
    for table in ("reports", "analysis_runs", "data_snapshots", "snapshot_sources", "orders",
                  "connections"):
        assert _rows(owner, tenant, table) == 0, table
    assert _store_state(owner, tenant) == ("[purged]", None, "purged")
    # L'ORGANISATION reste vivante: c'est une purge de boutique, pas de locataire.
    assert _organization_state(owner, tenant)[1] == "active"
    assert result["raw_objects_destroyed"] == len(keys)


def test_the_store_purge_keeps_the_salt_and_the_redaction_proofs(purgeable, worker_db, principal,
                                                                 store, owner):
    """D-065 Q4 et Q5, par le chemin reel: ni le sel ni les preuves ne bougent."""
    tenant, _ = purgeable
    _seed_identity_key(owner, tenant)
    proof_job = jobs.enqueue_job(tenant.session, job_type=JobType.PURGE_ORGANIZATION, payload={})
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(tenant.organization_id),))
        owner.execute(
            "INSERT INTO customer_redactions (id, organization_id, customer_ref, redacted_ref, "
            "origin, requested_by, job_id) VALUES (%s, %s, %s, %s, 'operator_request', %s, %s)",
            (uuid.uuid4(), tenant.organization_id, ALICE, f"redacted:{uuid.uuid4()}",
             tenant.owner_id, proof_job.id))

    result = _run(worker_db, principal, tenant, store, _enqueue_store(tenant), organization=False)

    assert result["customer_redactions"] == 0
    assert _rows(owner, tenant, "customer_redactions") == 1
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(tenant.organization_id),))
        salt = owner.execute("SELECT salt IS NOT NULL FROM organization_identity_keys "
                             "WHERE organization_id = %s", (tenant.organization_id,)).fetchone()[0]
    assert salt is True, "une purge de boutique ne detruit JAMAIS le sel, meme la derniere"


def test_a_failing_object_store_leaves_the_store_purgeable_again(purgeable, worker_db, principal,
                                                                 store, owner):
    """Les octets d'abord: si le magasin echoue, RIEN n'est affirme detruit."""
    tenant, objects = purgeable
    store.fail_on = {_key_of(owner, tenant, objects["stripe"])}
    with pytest.raises(RetryableJobError) as raised:
        _run(worker_db, principal, tenant, store, _enqueue_store(tenant), organization=False)
    assert raised.value.code == "object_delete_failed"
    assert _store_state(owner, tenant)[2] == "purging", "la boutique reste close, pas tombstonee"
    assert _rows(owner, tenant, "data_snapshots") > 0, "aucune donnee n'a ete detruite"


# -- purge d'organisation -------------------------------------------------------------------------

def test_the_organization_purge_runs_end_to_end_through_the_worker(purgeable, worker_db, principal,
                                                                   store, owner):
    """Le parcours de D-051, execute par le gestionnaire -- sans aucune ecriture brute.

    C'est ce test qui aurait du reveler, en phase 2, qu'aucune fonction ne savait tombstoner
    les boutiques d'une organisation. Il passe desormais par `app_tombstone_organization_stores`.
    """
    tenant, objects = purgeable
    _seed_identity_key(owner, tenant)
    keys = {_key_of(owner, tenant, object_id) for object_id in objects.values()}

    result = _run(worker_db, principal, tenant, store, _enqueue_organization(tenant),
                  organization=True)

    assert set(store.deleted) == keys and not (keys & set(store._objects))
    for table in ("reports", "analysis_runs", "data_snapshots", "snapshot_sources", "orders",
                  "connections", "customer_redactions", "raw_objects"):
        assert _rows(owner, tenant, table) == 0, table
    assert _rows(owner, tenant, "jobs") == 1, "seul le travail de purge subsiste"
    assert _store_state(owner, tenant) == ("[purged]", None, "purged")
    assert _organization_state(owner, tenant) == ("[purged]", "purged")
    assert result["identity_salt_destroyed"] is True and result["stores_tombstoned"] == 1
    # D-051: ce qui SURVIT
    assert _rows(owner, tenant, "memberships") >= 1
    assert _rows(owner, tenant, "audit_events") > 0
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(tenant.organization_id),))
        salt, destroyed = owner.execute(
            "SELECT salt, destroyed_at IS NOT NULL FROM organization_identity_keys "
            "WHERE organization_id = %s", (tenant.organization_id,)).fetchone()
    assert salt is None and destroyed is True, "la LIGNE de sel survit, videe"


def test_the_organization_purge_never_touches_a_neighbour(purgeable, neighbour, worker_db,
                                                          principal, store, owner):
    """L'isolation vient de la RLS: rien du voisin ne doit bouger, ni en base ni au magasin."""
    tenant, _ = purgeable
    other, other_key = neighbour
    _run(worker_db, principal, tenant, store, _enqueue_organization(tenant), organization=True)
    assert other_key in store._objects, "les octets du voisin sont intacts"
    assert _rows(owner, other, "data_snapshots") == 1
    assert _rows(owner, other, "raw_objects") == 1
    assert _organization_state(owner, other)[1] == "active"
    assert _store_state(owner, other)[2] == "active"


def test_replaying_an_organization_purge_resurrects_nothing(purgeable, worker_db, principal,
                                                            store, owner):
    """Rejeu apres succes: aucun octet ne revient, aucune donnee ne ressuscite.

    Le second travail ne peut meme pas etre mis en file -- le declencheur de cloture refuse
    tout nouveau travail dans une organisation qui n'est plus `active`. C'est la bonne reponse:
    une organisation purgee n'a plus rien a purger.
    """
    import psycopg
    tenant, _ = purgeable
    _run(worker_db, principal, tenant, store, _enqueue_organization(tenant), organization=True)
    remaining = dict(store._objects)
    with pytest.raises(psycopg.errors.RestrictViolation, match="closed and accepts no new record"):
        _enqueue_organization(tenant)
    assert store._objects == remaining
    assert _organization_state(owner, tenant) == ("[purged]", "purged")
