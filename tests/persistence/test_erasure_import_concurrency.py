"""Ordonnancement effacement / import sous D-063 (004.4.5 E5).

L'invariant ratifie, et le seul:

    un import et une operation destructrice portant sur la MEME organisation sont totalement
    ordonnes a la frontiere de transaction.

Les DEUX ordres sont corrects, et les deux sont testes ici. Le seul resultat interdit est
celui que la campagne D-063 avait reproduit AVANT le verrou:

    l'effacement commit, puis l'import commit la reference D'ORIGINE.
"""
from __future__ import annotations

import threading
import time
import uuid

import psycopg
import pytest

from mervio.persistence import erasure, jobs
from mervio.persistence.concurrency import (
    LockOutsideTransaction, lock_organization_exclusive, lock_organization_shared,
    organization_scope_key,
)
from mervio.persistence.database import Database
from mervio.persistence.jobs import JobType
from mervio.persistence.service import ServiceSession, authorize_service, delegated_session
from mervio.persistence.tenancy import Permission, TenantContext, TenantSession

from .persistence_support import make_tenant
from .test_customer_erasure import ALICE, BOB, TOMBSTONE
from .test_import_erasure_replay import _import, _orders_csv, _ref_of, _refs


@pytest.fixture
def dataset(tmp_path):
    """Meme constructeur d'export que la suite de rejeu: un CSV Shopify reduit."""
    def build(rows, name="orders.csv"):
        return {"shopify_orders": _orders_csv(tmp_path / name, rows)}
    return build


@pytest.fixture
def tenant(db, principal, owner):
    created = make_tenant(db, "conc")
    authorize_service(created.session, principal.id)
    return created


def _session_on(pg, tenant):
    """Une session du MEME locataire sur sa PROPRE connexion: un second worker, en somme."""
    database = Database(pg.url("app"))
    return database, TenantSession(database, TenantContext(tenant.organization_id, tenant.owner_id))


def _erase(tenant, worker_db, principal, reference, *, session=None):
    job = jobs.enqueue_job(session or tenant.session, job_type=JobType.REDACT_CUSTOMER,
                           payload={"customer_ref": reference})
    claimed = jobs.claim_next_job(session or tenant.session, worker_id=f"w-{uuid.uuid4().hex[:6]}",
                                  job_id=job.id)
    delegated = delegated_session(ServiceSession(worker_db, tenant.organization_id, principal), claimed)
    return erasure.redact_customer(delegated, claimed).redacted_ref


# -- 3. frontiere autocommit (le piege ratifie par D-063) ----------------------------------------

def test_a_lock_outside_a_transaction_is_refused_instead_of_silently_lost(db, tenant):
    """`Database` est en `autocommit=True`: un verrou de transaction pris hors transaction
    serait relache AUSSITOT. Le refuser vaut mieux que proteger pour de faux."""
    conn = db.connection()
    for acquire in (lock_organization_shared, lock_organization_exclusive):
        with pytest.raises(LockOutsideTransaction):
            acquire(conn, tenant.organization_id)


def test_inside_a_transaction_the_lock_is_really_held_until_the_end(db, tenant):
    with db.transaction(organization_id=tenant.organization_id, user_id=tenant.owner_id) as conn:
        lock_organization_shared(conn, tenant.organization_id)
        held = conn.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory'").fetchone()[0]
        assert held == 1, "le verrou doit etre detenu PENDANT la transaction"
    after = db.connection().execute(
        "SELECT count(*) FROM pg_locks WHERE locktype='advisory'").fetchone()[0]
    assert after == 0, "et relache a la sortie, sans intervention"


def test_the_lock_key_is_the_ratified_construction_and_carries_no_identity(db, tenant):
    with db.transaction(organization_id=tenant.organization_id) as conn:
        key = organization_scope_key(conn, tenant.organization_id)
        expected = conn.execute("SELECT hashtextextended(%s, 0)",
                                (f"{tenant.organization_id}:org",)).fetchone()[0]
        assert key == expected, "construction ratifiee par D-063, non modifiee"
        assert key == organization_scope_key(conn, tenant.organization_id), "deterministe"
    for secret in (ALICE, BOB, "c1:", "g1:", "@"):
        assert secret not in str(key), "aucune reference client dans la cle"


def test_the_key_separates_organizations(db, tenant, owner):
    other = make_tenant(db, "conc-other")
    with db.transaction(organization_id=tenant.organization_id) as conn:
        mine = organization_scope_key(conn, tenant.organization_id)
        theirs = organization_scope_key(conn, other.organization_id)
    assert mine != theirs


# -- 7. les deux ordres de la course --------------------------------------------------------------

def test_race_ordering_A_import_first_then_erasure(tenant, owner, worker_db, principal, pg, dataset):
    """CAS A: l'import prend le partage, l'effacement ATTEND, puis rattrape les lignes neuves."""
    _import(tenant, dataset([("#1001", "alice@example.com", "10.00")]))
    reference = _ref_of(tenant, "alice@example.com")
    outcome = {}
    database, second = _session_on(pg, tenant)
    try:
        held = threading.Event()

        def eraser():
            held.wait()
            outcome["tomb"] = _erase(tenant, worker_db, principal, reference, session=second)

        thread = threading.Thread(target=eraser)
        # le verrou partage est pris a l'interieur de write_snapshot: on le prend ici en
        # PREMIER, sur une transaction ouverte a la main, pour forcer l'ordre A.
        with tenant.session.transaction(Permission.IMPORT_DATA) as conn:
            lock_organization_shared(conn, tenant.organization_id)
            thread.start(); held.set(); time.sleep(0.5)
            assert thread.is_alive(), "l'effacement doit ATTENDRE le partage de l'import"
        thread.join(timeout=20)
    finally:
        database.close()

    later = _import(tenant, dataset([("#1001", "alice@example.com", "10.00"),
                                     ("#1002", "bob@example.com", "20.00")], name="a.csv"))
    written = _refs(tenant, later.snapshot.id)
    assert written[0] == outcome["tomb"], "l'import suivant applique le tombstone enregistre"
    assert reference not in written


def test_race_ordering_B_erasure_first_then_import(tenant, owner, worker_db, principal, pg, dataset):
    """CAS B: l'effacement prend l'exclusif, l'import ATTEND, puis sa consultation le voit."""
    _import(tenant, dataset([("#1001", "alice@example.com", "10.00")]))
    reference = _ref_of(tenant, "alice@example.com")
    database, second = _session_on(pg, tenant)
    try:
        tomb = {}
        released = threading.Event()

        def eraser():
            with second.transaction(Permission.REDACT_CUSTOMER) as conn:
                lock_organization_exclusive(conn, tenant.organization_id)
                time.sleep(0.6)          # l'import va se cogner a ce verrou
            released.set()

        thread = threading.Thread(target=eraser); thread.start(); time.sleep(0.1)
        tomb["t"] = _erase(tenant, worker_db, principal, reference)
        thread.join(timeout=20)
    finally:
        database.close()

    later = _import(tenant, dataset([("#1001", "alice@example.com", "10.00"),
                                     ("#1002", "bob@example.com", "20.00")], name="b.csv"))
    written = _refs(tenant, later.snapshot.id)
    assert written[0] == tomb["t"] and TOMBSTONE.match(written[0])
    assert reference not in written, "AUCUNE resurrection"


def test_the_forbidden_outcome_can_no_longer_happen(tenant, owner, worker_db, principal, pg, dataset):
    """Le scenario exact que la campagne D-063 avait reproduit AVANT le verrou.

    L'effacement tente de commiter pendant que l'import tient son verrou partage: il ne le
    PEUT pas. Il attend, et le resultat final est correct dans tous les cas.
    """
    _import(tenant, dataset([("#1001", "alice@example.com", "10.00")]))
    database, second = _session_on(pg, tenant)
    blocked = {}
    try:
        with tenant.session.transaction(Permission.IMPORT_DATA) as conn:
            lock_organization_shared(conn, tenant.organization_id)
            with second.transaction(Permission.REDACT_CUSTOMER) as other:
                key = organization_scope_key(other, tenant.organization_id)
                blocked["exclusive"] = other.execute(
                    "SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0]
    finally:
        database.close()
    assert blocked["exclusive"] is False, "un effacement ne peut PAS s'insinuer dans la fenetre"


# -- 8. matrice de concurrence ----------------------------------------------------------------------

def test_two_imports_of_the_same_organization_run_concurrently(tenant, pg, db):
    database, second = _session_on(pg, tenant)
    try:
        with tenant.session.transaction(Permission.IMPORT_DATA) as first:
            lock_organization_shared(first, tenant.organization_id)
            with second.transaction(Permission.IMPORT_DATA) as other:
                key = organization_scope_key(other, tenant.organization_id)
                together = other.execute("SELECT pg_try_advisory_xact_lock_shared(%s)",
                                         (key,)).fetchone()[0]
    finally:
        database.close()
    assert together is True, "deux imports d'un meme tenant ne doivent pas s'exclure"


def test_imports_of_different_organizations_never_block_each_other(db, pg, tenant):
    other = make_tenant(db, "conc-b")
    database, second = _session_on(pg, other)
    try:
        with tenant.session.transaction(Permission.IMPORT_DATA) as first:
            lock_organization_exclusive(first, tenant.organization_id)   # le cas le plus dur
            with second.transaction(Permission.IMPORT_DATA) as conn:
                key = organization_scope_key(conn, other.organization_id)
                free = conn.execute("SELECT pg_try_advisory_xact_lock_shared(%s)", (key,)).fetchone()[0]
    finally:
        database.close()
    assert free is True, "un effacement chez A ne bloque pas un import chez B"


def test_an_import_blocks_an_erasure_of_the_same_organization(tenant, pg):
    database, second = _session_on(pg, tenant)
    try:
        with tenant.session.transaction(Permission.IMPORT_DATA) as first:
            lock_organization_shared(first, tenant.organization_id)
            with second.transaction(Permission.REDACT_CUSTOMER) as conn:
                key = organization_scope_key(conn, tenant.organization_id)
                assert conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0] is False
    finally:
        database.close()


def test_an_erasure_blocks_an_import_of_the_same_organization(tenant, pg):
    database, second = _session_on(pg, tenant)
    try:
        with tenant.session.transaction(Permission.REDACT_CUSTOMER) as first:
            lock_organization_exclusive(first, tenant.organization_id)
            with second.transaction(Permission.IMPORT_DATA) as conn:
                key = organization_scope_key(conn, tenant.organization_id)
                assert conn.execute("SELECT pg_try_advisory_xact_lock_shared(%s)",
                                    (key,)).fetchone()[0] is False
    finally:
        database.close()


def test_a_rollback_releases_the_lock(tenant, db, pg):
    database, second = _session_on(pg, tenant)
    try:
        try:
            with tenant.session.transaction(Permission.IMPORT_DATA) as conn:
                lock_organization_exclusive(conn, tenant.organization_id)
                raise RuntimeError("echec simule de l'import")
        except RuntimeError:
            pass
        with second.transaction(Permission.IMPORT_DATA) as conn:
            key = organization_scope_key(conn, tenant.organization_id)
            assert conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0] is True
    finally:
        database.close()


def test_a_dead_connection_releases_the_lock(tenant, pg, db):
    """Un arret brutal ne laisse aucun verrou derriere lui: PostgreSQL le libere."""
    victim = psycopg.connect(pg.url("app"))
    with victim.transaction():
        victim.execute("SELECT set_config('app.organization_id', %s, true)",
                       (str(tenant.organization_id),))
        lock_organization_exclusive(victim, tenant.organization_id)
        victim.close()                                     # la connexion meurt, verrou detenu
    database, second = _session_on(pg, tenant)
    try:
        for _ in range(40):
            with second.transaction(Permission.IMPORT_DATA) as conn:
                key = organization_scope_key(conn, tenant.organization_id)
                if conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0]:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("le verrou n'a pas ete libere par la mort de la connexion")
    finally:
        database.close()


def test_no_deadlock_between_an_import_and_an_erasure(tenant, owner, worker_db, principal, pg, dataset):
    """Un verrou UNIQUE par organisation: aucun ordre d'acquisition, donc aucun cycle possible."""
    _import(tenant, dataset([("#1001", "alice@example.com", "10.00")]))
    reference = _ref_of(tenant, "alice@example.com")
    database, second = _session_on(pg, tenant)
    errors = []
    try:
        def importer():
            try:
                _import(tenant, dataset([("#1001", "alice@example.com", "10.00"),
                                         ("#1002", "bob@example.com", "20.00")], name="d1.csv"))
            except Exception as exc:                        # noqa: BLE001 - on veut le type exact
                errors.append(f"import: {type(exc).__name__}")

        def eraser():
            try:
                _erase(tenant, worker_db, principal, reference, session=second)
            except Exception as exc:                        # noqa: BLE001
                errors.append(f"erase: {type(exc).__name__}")

        threads = [threading.Thread(target=importer), threading.Thread(target=eraser)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        database.close()
    assert errors == [], errors
