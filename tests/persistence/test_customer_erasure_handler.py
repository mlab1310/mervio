"""Gestionnaire d'effacement client: destruction reelle des octets (004.4.5 E4, D-056).

E3 rendait les objets ILLISIBLES (`available -> purging`). E4 les rend VIDES, puis le dit
(`purging -> purged`). Ces tests portent sur l'ordre, l'idempotence et la reprise -- les trois
proprietes qui remplacent l'atomicite qu'on ne peut pas avoir entre PostgreSQL et un magasin
d'objets externe.

L'invariant central, verifie sous plusieurs angles:

    une ligne `purged` implique des octets detruits; jamais l'inverse.
"""
from __future__ import annotations

import io
import uuid

import pytest

from mervio.persistence import erasure, jobs
from mervio.persistence.jobs import JobType
from mervio.persistence.service import ServiceSession, authorize_service, delegated_session
from mervio.storage import MemoryObjectStore, ObjectStoreError
from mervio.workers.errors import PermanentJobError, RetryableJobError
from mervio.workers.handlers import JobContext, make_redact_customer_handler

from .persistence_support import make_tenant
from .test_customer_erasure import ALICE, BOB, GUEST, TOMBSTONE, _raw_object, _refs, _sealed_snapshot_with_orders

PAYLOAD_BYTES = b"order_id,email\nA-1,alice@example.com\n"


# -- mise en place ------------------------------------------------------------------------------

class Store(MemoryObjectStore):
    """Magasin memoire instrumente: on observe l'ORDRE des appels, et on peut les faire echouer."""

    def __init__(self):
        super().__init__()
        self.deleted = []
        self.fail_on = set()

    def delete(self, key: str) -> None:
        if key in self.fail_on:
            raise ObjectStoreError("magasin indisponible")
        self.deleted.append(key)
        super().delete(key)


@pytest.fixture
def store():
    return Store()


@pytest.fixture
def erasable(db, owner, worker_db, principal, store):
    """Un locataire pret a effacer, avec des OCTETS reellement deposes sous chaque cle."""
    tenant = make_tenant(db, "e4")
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE, ALICE, BOB, GUEST])
    objects = {}
    for kind in ("shopify_orders", "stripe", "shopify_products", "google_ads"):
        object_id = _raw_object(owner, tenant, kind)
        objects[kind] = object_id
        store.put(_key_of(owner, tenant, object_id), io.BytesIO(PAYLOAD_BYTES))
    return tenant, objects


def _key_of(owner, tenant, object_id) -> str:
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        return owner.execute("SELECT object_key FROM raw_objects WHERE id = %s", (object_id,)).fetchone()[0]


def _states(owner, tenant):
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        return dict(owner.execute(
            "SELECT source_kind, state || ':' || coalesce(purged_at::text, '-') "
            "FROM raw_objects").fetchall())


def _enqueue(tenant, reference=ALICE, *, session=None):
    return jobs.enqueue_job(session or tenant.session, job_type=JobType.REDACT_CUSTOMER,
                            payload={"customer_ref": reference})


def _run(worker_db, principal, tenant, store, job, *, log=None):
    """Execute le gestionnaire comme le worker le ferait: session DELEGUEE au demandeur."""
    claimed = jobs.claim_next_job(tenant.session, worker_id="worker-1", job_id=job.id)
    assert claimed is not None, "le travail n'a pas ete pris"
    session = delegated_session(ServiceSession(worker_db, tenant.organization_id, principal), claimed)
    handler = make_redact_customer_handler(store)
    return handler(JobContext(session=session, job=claimed, correlation_id=str(claimed.correlation_id),
                              log=log or _NullLog(), actor_id=principal.id,
                              on_behalf_of=claimed.enqueued_by))


class _NullLog:
    """Journal muet qui RETIENT ce qu'on lui a donne: sert aussi de preuve d'absence de PII."""

    def __init__(self):
        self.events = []

    def info(self, name, **fields):
        self.events.append((name, fields))

    warning = error = debug = info

    def bind(self, **fields):
        return self


# -- chemin nominal -------------------------------------------------------------------------------

def test_the_bytes_are_destroyed_and_only_then_the_rows_are_marked_purged(erasable, owner, worker_db,
                                                                          principal, store):
    tenant, objects = erasable
    result = _run(worker_db, principal, tenant, store, _enqueue(tenant))

    assert result["orders_tombstoned"] == 2
    assert result["raw_objects_purged"] == 2 and result["raw_objects_already_purged"] == 0
    # les octets des objets porteurs d'identite ont disparu du magasin
    for kind in ("shopify_orders", "stripe"):
        with pytest.raises(Exception):
            with store.open(_key_of(owner, tenant, objects[kind])):
                pass
    # et les lignes le DISENT, avec leur horodatage
    states = _states(owner, tenant)
    assert states["shopify_orders"].startswith("purged:") and states["stripe"].startswith("purged:")


def test_objects_without_customer_identity_keep_their_bytes(erasable, owner, worker_db, principal, store):
    """D-056: `shopify_products` et `google_ads` ne portent aucune identite client."""
    tenant, objects = erasable
    _run(worker_db, principal, tenant, store, _enqueue(tenant))

    for kind in ("shopify_products", "google_ads"):
        with store.open(_key_of(owner, tenant, objects[kind])) as handle:
            assert handle.read() == PAYLOAD_BYTES
    states = _states(owner, tenant)
    assert states["shopify_products"] == "available:-" and states["google_ads"] == "available:-"


def test_the_canonical_rows_are_tombstoned_and_the_proof_is_recorded(erasable, owner, worker_db,
                                                                      principal, store):
    tenant, _ = erasable
    result = _run(worker_db, principal, tenant, store, _enqueue(tenant))

    references = _refs(owner, tenant)
    assert references[0] == references[1] and TOMBSTONE.match(references[0])
    assert references[2:] == [BOB, GUEST]
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        rows = owner.execute("SELECT customer_ref, redacted_ref FROM customer_redactions").fetchall()
        audited = owner.execute("SELECT count(*) FROM audit_events WHERE action = 'customer.redacted'"
                                ).fetchone()[0]
    assert rows == [(ALICE, references[0])] and audited == 1
    assert result["redaction_id"]


def test_an_unknown_customer_reference_still_destroys_the_identity_bearing_bytes(erasable, owner, worker_db,
                                                                                  principal, store):
    """D-056, porte volontairement large: l'absence de commande ne prouve pas l'absence
    d'identite dans les octets (un fichier Stripe porte l'e-mail sans reference persistee)."""
    tenant, _ = erasable
    stranger = "c1:" + "7" * 32
    result = _run(worker_db, principal, tenant, store, _enqueue(tenant, stranger))

    assert result["orders_tombstoned"] == 0
    assert result["raw_objects_purged"] == 2
    assert _refs(owner, tenant) == [ALICE, ALICE, BOB, GUEST], "aucune ligne canonique touchee"
    assert len(store.deleted) == 2


# -- idempotence et reprise -------------------------------------------------------------------------

def test_running_the_same_erasure_twice_destroys_nothing_twice_and_restores_nothing(erasable, owner,
                                                                                     worker_db, principal,
                                                                                     store):
    tenant, _ = erasable
    first = _run(worker_db, principal, tenant, store, _enqueue(tenant))
    second = _run(worker_db, principal, tenant, store, _enqueue(tenant))

    assert first["raw_objects_purged"] == 2
    # au second passage il ne reste RIEN en `purging`: aucun objet a redetruire
    assert second["raw_objects_purged"] == 0 and second["raw_objects_already_purged"] == 0
    assert first["redaction_id"] == second["redaction_id"], "une seule preuve, un seul tombstone"
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        assert owner.execute("SELECT count(*) FROM customer_redactions").fetchone() == (1,)


def test_a_retry_after_the_bytes_vanished_still_finalizes_the_row(erasable, owner, worker_db,
                                                                   principal, store):
    """CAS 2 du contrat: octets detruits, arret avant le commit. Le rejeu doit FINIR le travail.

    On simule l'interruption en supprimant les octets hors du gestionnaire, en laissant les
    lignes en `purging`: c'est exactement l'etat que laisse un arret dans la fenetre.
    """
    tenant, objects = erasable
    erasure.redact_customer(*_claimed_session(worker_db, principal, tenant, _enqueue(tenant)))
    for kind in ("shopify_orders", "stripe"):
        store.delete(_key_of(owner, tenant, objects[kind]))  # les octets sont deja partis
    assert all(state.startswith("purging") for kind, state in _states(owner, tenant).items()
               if kind in ("shopify_orders", "stripe"))

    result = _run(worker_db, principal, tenant, store, _enqueue(tenant))
    assert result["raw_objects_purged"] == 2, "la reprise finalise, sans echouer sur l'absence"
    states = _states(owner, tenant)
    assert states["shopify_orders"].startswith("purged:") and states["stripe"].startswith("purged:")


def _claimed_session(worker_db, principal, tenant, job):
    claimed = jobs.claim_next_job(tenant.session, worker_id="worker-1", job_id=job.id)
    return delegated_session(ServiceSession(worker_db, tenant.organization_id, principal), claimed), claimed


def test_an_object_already_purged_is_reported_as_such_and_not_rewritten(erasable, owner, worker_db,
                                                                         principal, store):
    """Le gestionnaire distingue "je viens de le detruire" de "c'etait deja fait"."""
    tenant, objects = erasable
    session, claimed = _claimed_session(worker_db, principal, tenant, _enqueue(tenant))
    erasure.redact_customer(session, claimed)
    first = erasure.finalize_object_purge(session, claimed, objects["shopify_orders"])
    second = erasure.finalize_object_purge(session, claimed, objects["shopify_orders"])
    assert first is True and second is False


# -- echec de destruction ---------------------------------------------------------------------------

def test_a_failing_delete_leaves_the_object_purging_and_never_purged(erasable, owner, worker_db,
                                                                      principal, store):
    """CAS 1: la destruction echoue. Rien ne doit AFFIRMER une destruction qui n'a pas eu lieu."""
    tenant, objects = erasable
    doomed = _key_of(owner, tenant, objects["shopify_orders"])
    store.fail_on.add(doomed)

    with pytest.raises(RetryableJobError) as failure:
        _run(worker_db, principal, tenant, store, _enqueue(tenant))
    assert failure.value.code == "object_delete_failed"

    states = _states(owner, tenant)
    assert states["shopify_orders"] == "purging:-", "illisible, mais PAS declare detruit"
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        assert owner.execute("SELECT count(*) FROM raw_objects WHERE state = 'purged'").fetchone() == (0,)


def test_the_erasure_completes_once_the_store_recovers(erasable, owner, worker_db, principal, store):
    """Le travail reste REPRENABLE: la panne passee, un rejeu termine l'effacement."""
    tenant, objects = erasable
    doomed = _key_of(owner, tenant, objects["shopify_orders"])
    store.fail_on.add(doomed)
    with pytest.raises(RetryableJobError):
        _run(worker_db, principal, tenant, store, _enqueue(tenant))

    store.fail_on.clear()
    result = _run(worker_db, principal, tenant, store, _enqueue(tenant))
    assert result["raw_objects_purged"] == 2
    assert all(state.startswith("purged:") for kind, state in _states(owner, tenant).items()
               if kind in ("shopify_orders", "stripe"))


def test_a_failing_delete_is_retryable_not_permanent(erasable, worker_db, principal, store, owner):
    """Une panne de magasin est TRANSITOIRE: elle ne doit pas condamner l'effacement."""
    tenant, objects = erasable
    store.fail_on.add(_key_of(owner, tenant, objects["stripe"]))
    with pytest.raises(RetryableJobError) as failure:
        _run(worker_db, principal, tenant, store, _enqueue(tenant))
    assert not isinstance(failure.value, PermanentJobError)


# -- charge utile -----------------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {}, {"customer_ref": None}, {"customer_ref": ""}, {"customer_ref": "alice@example.com"},
    {"customer_ref": "c1:" + "z" * 32}, {"customer_ref": "redacted:" + str(uuid.uuid4())},
    {"customer_ref": 42},
])
def test_a_payload_without_a_well_formed_reference_is_refused_before_anything_is_touched(
        erasable, owner, worker_db, principal, store, payload):
    tenant, _ = erasable
    job = jobs.enqueue_job(tenant.session, job_type=JobType.REDACT_CUSTOMER, payload=payload)
    with pytest.raises(PermanentJobError):
        _run(worker_db, principal, tenant, store, job)
    assert store.deleted == [], "aucun octet detruit sur une charge refusee"
    assert _refs(owner, tenant) == [ALICE, ALICE, BOB, GUEST]


def test_the_handler_never_derives_a_reference_from_an_identity(erasable, worker_db, principal, store):
    """D-062: la resolution identite -> reference est worker-local, hors file, et n'est pas ici.

    Un e-mail dans la charge n'est pas traduit: il est REFUSE.
    """
    tenant, _ = erasable
    job = jobs.enqueue_job(tenant.session, job_type=JobType.REDACT_CUSTOMER,
                           payload={"customer_ref": "alice@example.com"})
    with pytest.raises(PermanentJobError):
        _run(worker_db, principal, tenant, store, job)


def test_the_erasure_handler_is_never_given_the_identity_master_key(erasable, worker_db, principal, store):
    """D-062, tenu par la STRUCTURE et pas seulement par la discipline.

    Le gestionnaire d'import recoit la cle maitre (il derive les references a l'ingestion);
    celui d'effacement ne la recoit PAS, et ne peut donc pas resoudre une identite meme s'il
    le voulait. La resolution reste worker-local, hors file, et appartient a E6.
    """
    import inspect

    from mervio.workers.handlers import make_import_handler, make_redact_customer_handler

    assert list(inspect.signature(make_redact_customer_handler).parameters) == ["object_store"]
    assert "identity_master" in inspect.signature(make_import_handler).parameters
    closure = make_redact_customer_handler(store).__closure__ or ()
    captured = [cell.cell_contents for cell in closure]
    assert captured == [store], "le gestionnaire ne capture QUE le magasin d'objets"


def test_the_handler_refuses_to_run_without_an_object_store(erasable, worker_db, principal):
    tenant, _ = erasable
    job = _enqueue(tenant)
    claimed = jobs.claim_next_job(tenant.session, worker_id="worker-1", job_id=job.id)
    session = delegated_session(ServiceSession(worker_db, tenant.organization_id, principal), claimed)
    with pytest.raises(PermanentJobError) as failure:
        make_redact_customer_handler(None)(JobContext(
            session=session, job=claimed, correlation_id=str(claimed.correlation_id), log=_NullLog()))
    assert failure.value.code == "object_store_unavailable"


# -- bout en bout, par le vrai worker ---------------------------------------------------------------

def test_the_real_worker_claims_executes_and_succeeds_an_erasure(erasable, owner, pg, principal, store):
    """Preuve que le type, le registre, la prise et le gestionnaire sont bien relies.

    C'est ce chemin -- et non l'appel direct du gestionnaire -- qui echouait tant que
    `redact_customer` n'existait que dans la base (E2/E3).
    """
    from mervio.workers.handlers import default_registry

    from .worker_support import DispatchedWorker

    tenant, objects = erasable
    job = _enqueue(tenant)
    worker = DispatchedWorker(pg, principal, worker_id="e2e-1",
                              registry=default_registry(object_store=store))
    try:
        outcomes = worker.worker.run_until_empty(max_jobs=5)
    finally:
        worker.close()

    assert [o.job.id for o in outcomes] == [job.id]
    stored = jobs.get_job(tenant.session, job.id)
    assert stored.status == "succeeded", stored.last_error_code
    assert stored.result["raw_objects_purged"] == 2
    assert _states(owner, tenant)["shopify_orders"].startswith("purged:")
    assert len(store.deleted) == 2


# -- absence de PII -----------------------------------------------------------------------------------

def test_no_customer_reference_and_no_identity_ever_reaches_the_log(erasable, worker_db, principal, store):
    tenant, _ = erasable
    log = _NullLog()
    _run(worker_db, principal, tenant, store, _enqueue(tenant), log=log)

    assert log.events, "le gestionnaire journalise bien quelque chose"
    blob = repr(log.events)
    for secret in (ALICE, "c1:", "g1:", "@", "redacted:"):
        assert secret not in blob, blob


def test_the_job_result_carries_counts_and_a_proof_id_but_no_reference(erasable, worker_db, principal, store):
    tenant, _ = erasable
    result = _run(worker_db, principal, tenant, store, _enqueue(tenant))
    blob = repr(result)
    for secret in (ALICE, "c1:", "@"):
        assert secret not in blob, blob
    assert set(result) == {"redaction_id", "orders_tombstoned", "raw_objects_purged",
                           "raw_objects_already_purged"}
