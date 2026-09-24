"""Frontiere d'import sans chemin, de bout en bout (Mission 004.4.4, D-054, D-061).

La propriete que cette mission ferme (G-02 / SEC-01): un chemin fourni par l'appelant ne peut
plus atteindre le worker. Ce module en fait la preuve sur la vraie chaine -- file, RLS, magasin,
materialisation, parseurs inchanges -- et verifie ce qui l'entoure: integrite, nettoyage du
temporaire, et absence de chemin dans l'audit.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import pytest

from mervio.persistence import audit, jobs, raw_objects
from mervio.persistence.errors import PayloadRejected
from mervio.persistence.jobs import JobStatus, JobType
from mervio.storage import build_object_key
from mervio.workers import handlers as handlers_module
from mervio.workers.handlers import default_registry
from mervio.workers.worker import Worker, enqueue

from .persistence_support import (
    SAMPLE_FILES, TEST_MASTER_KEY, TEST_OBJECT_STORE, deposit_object, deposit_sources,
)


def worker(tenant) -> Worker:
    return Worker(sessions=[tenant.session],
                  registry=default_registry(identity_master=TEST_MASTER_KEY,
                                            object_store=TEST_OBJECT_STORE),
                  worker_id="boundary-test")


def enqueue_import(tenant, raw_objects_by_kind=None, **payload):
    return enqueue(tenant.session, job_type=JobType.IMPORT, store_id=tenant.store_id,
                   payload={"store_id": str(tenant.store_id),
                            "connection_id": str(tenant.connection_id),
                            "raw_objects": raw_objects_by_kind or deposit_sources(tenant), **payload})


@pytest.fixture
def watched_temporaries(monkeypatch):
    """Retient chaque repertoire temporaire cree par le gestionnaire d'import."""
    created = []
    original = tempfile.mkdtemp

    def record(*args, **kwargs):
        directory = original(*args, **kwargs)
        created.append(Path(directory))
        return directory

    monkeypatch.setattr(handlers_module.tempfile, "mkdtemp", record)
    return created


# -- le parcours nominal -----------------------------------------------------------------------

def test_an_import_runs_end_to_end_from_raw_object_ids(tenant_a):
    job = enqueue_import(tenant_a)
    assert "raw_objects" in job.payload and "sources" not in job.payload
    outcome = worker(tenant_a).run_once()
    assert outcome.succeeded, outcome.error_code
    assert outcome.job.result["status"] == "completed"
    assert outcome.job.result["row_count"] > 0


def test_the_four_source_kinds_all_travel_as_raw_object_ids(tenant_a):
    identifiers = deposit_sources(tenant_a, SAMPLE_FILES)
    assert set(identifiers) == set(SAMPLE_FILES)
    for value in identifiers.values():
        uuid.UUID(value)  # un identifiant, jamais un emplacement
    enqueue_import(tenant_a, identifiers)
    outcome = worker(tenant_a).run_once()
    assert outcome.succeeded, outcome.error_code


# -- aucun chemin ne peut entrer --------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"sources": {"shopify_orders": "/srv/orders.csv"}},
    {"raw_objects": {"shopify_orders": "/srv/orders.csv"}},
    {"meta": {"source": "/tmp/x.csv"}},
    {"note": "/etc/passwd"},
    {"path": "orders.csv"},
    {"candidates": ["ok", "../../etc/passwd"]},
])
def test_a_path_bearing_payload_never_reaches_the_queue(tenant_a, payload):
    with pytest.raises(PayloadRejected):
        enqueue(tenant_a.session, job_type=JobType.IMPORT, store_id=tenant_a.store_id,
                payload={"store_id": str(tenant_a.store_id), **payload})
    assert jobs.list_jobs(tenant_a.session) == []


def test_the_stored_payload_of_a_real_import_contains_no_path(tenant_a):
    job = enqueue_import(tenant_a)
    serialized = json.dumps({k: v for k, v in job.payload.items() if k != "synthetic_manifest"})
    assert "/" not in serialized, serialized


# -- isolation: la RLS, pas un controle applicatif ---------------------------------------------

def test_an_object_of_another_organization_is_not_found_by_the_worker(tenant_a, tenant_b):
    foreign = deposit_sources(tenant_b)
    enqueue_import(tenant_a, {"shopify_orders": foreign["shopify_orders"]})
    outcome = worker(tenant_a).run_once()
    assert outcome.job.status == JobStatus.FAILED.value
    assert outcome.error_code == "not_found"


def test_a_foreign_object_and_an_unknown_one_fail_identically(tenant_a, tenant_b):
    """Aucun oracle d'existence, meme vu du worker."""
    foreign = deposit_sources(tenant_b)["shopify_orders"]
    codes = []
    for identifier in (foreign, str(uuid.uuid4())):
        enqueue_import(tenant_a, {"shopify_orders": identifier})
        outcome = worker(tenant_a).run_once()
        codes.append((outcome.error_code, outcome.job.last_error))
    assert codes[0] == codes[1], codes


def test_an_object_of_another_store_is_refused(tenant_a):
    from mervio.persistence.stores import create_store
    other = create_store(tenant_a.session, name="Autre boutique")
    identifier = deposit_object(tenant_a, "shopify_orders", SAMPLE_FILES["shopify_orders"])
    enqueue(tenant_a.session, job_type=JobType.IMPORT, store_id=other.id,
            payload={"store_id": str(other.id), "connection_id": str(tenant_a.connection_id),
                     "raw_objects": {"shopify_orders": identifier}})
    outcome = worker(tenant_a).run_once()
    assert outcome.job.status == JobStatus.FAILED.value


# -- integrite (N1) ----------------------------------------------------------------------------

def test_a_checksum_mismatch_is_permanent_and_no_parser_is_called(tenant_a, monkeypatch):
    """N1 (D-061): les octets doivent etre ceux que LA BASE declare, sinon rien n'est analyse."""
    identifiers = deposit_sources(tenant_a, {"shopify_orders": SAMPLE_FILES["shopify_orders"]})
    recorded = raw_objects.get_object(tenant_a.session, uuid.UUID(identifiers["shopify_orders"]))
    TEST_OBJECT_STORE._objects[recorded.object_key] = b"octets-substitues\n"  # noqa: SLF001

    calls = []
    import mervio.application.persisted_analysis as persisted

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("le parseur ne doit pas etre appele apres un echec d'integrite")

    monkeypatch.setattr(persisted, "import_csv_snapshot", refuse)
    enqueue_import(tenant_a, identifiers)
    outcome = worker(tenant_a).run_once()
    assert outcome.error_code == "object_checksum_mismatch"
    assert outcome.job.status == JobStatus.FAILED.value
    assert outcome.job.attempts == 1  # definitif: les memes octets divergeront demain
    assert calls == []


def test_an_object_whose_bytes_are_missing_fails_permanently(tenant_a):
    from datetime import datetime, timezone
    orphan = raw_objects.record_object(
        tenant_a.session, store_id=tenant_a.store_id,
        object_key=build_object_key(tenant_a.organization_id, tenant_a.store_id),
        sha256="0" * 64, byte_size=0, source_kind="shopify_orders",
        retain_until=datetime(2099, 1, 1, tzinfo=timezone.utc))
    enqueue_import(tenant_a, {"shopify_orders": str(orphan.id)})
    outcome = worker(tenant_a).run_once()
    assert (outcome.error_code, outcome.job.attempts) == ("object_missing", 1)


# -- le temporaire du worker -------------------------------------------------------------------

def test_the_temporary_directory_is_removed_after_a_successful_import(tenant_a, watched_temporaries):
    enqueue_import(tenant_a)
    assert worker(tenant_a).run_once().succeeded
    assert watched_temporaries, "le gestionnaire doit materialiser dans un temporaire"
    assert all(not directory.exists() for directory in watched_temporaries)


def test_the_temporary_directory_is_removed_when_the_import_fails(tenant_a, watched_temporaries, monkeypatch):
    import mervio.application.persisted_analysis as persisted
    monkeypatch.setattr(persisted, "import_csv_snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("panne")))
    enqueue_import(tenant_a)
    outcome = worker(tenant_a).run_once()
    assert not outcome.succeeded
    assert watched_temporaries
    assert all(not directory.exists() for directory in watched_temporaries), "nettoyage en `finally`"


# -- hygiene de l'audit et des journaux --------------------------------------------------------

def test_no_path_and_no_filename_appear_in_the_audit_trail(tenant_a):
    enqueue_import(tenant_a)
    assert worker(tenant_a).run_once().succeeded
    events = audit.list_events(tenant_a.session, limit=200)
    serialized = json.dumps([{"action": e.action, "metadata": e.metadata} for e in events], default=str)
    assert "/" not in serialized, serialized
    for name in ("shopify_orders.csv", "stripe_transactions.csv", "google_ads.csv"):
        assert name not in serialized
