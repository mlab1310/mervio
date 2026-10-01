"""Purge tenant sous CONCURRENCE REELLE: plusieurs connexions, de vraies courses (004.4.6).

Les autres suites appellent les operations dans un ordre choisi. Celle-ci les lance EN MEME
TEMPS, depuis des connexions PostgreSQL distinctes synchronisees par barriere, parce que
l'invariant defendu ici n'est pas atteignable autrement:

    deux operations destructrices d'une meme organisation sont totalement ordonnees, et
    aucun entrelacement ne produit un etat que le schema ne saurait interpreter.

CE QUE LA CONCURRENCE EXERCE ICI, ET CE QU'ELLE N'EXERCE PAS
Le verrou de D-063 est un verrou de TRANSACTION, et chaque operation privilegiee est SA
PROPRE transaction (`tenant_purge._call`). Le verrou est donc pris et relache a chaque etape:
il ordonne les etapes entre elles, il ne tient pas un parcours entier. Ce qui protege ENTRE
les etapes est le STATUT (`purging`), pas le verrou -- exactement ce que D-063 point 2 ecrit
pour l'effacement, et que les purges heritent. Ces tests mesurent donc les deux: l'ordre total
a l'interieur d'une etape, et la coherence de l'etat final malgre l'entrelacement des etapes.

CE QUE CES COURSES ONT FAIT APPARAITRE
Avant D-067, elles produisaient un DEAD-END reproductible: les deux purges detruisaient les
donnees et le sel, puis echouaient TOUTES DEUX a la pierre tombale -- chacune voyant survivre
le travail de l'autre, protege par D-066 I1 -- et l'organisation restait `purging` sans qu'une
nouvelle purge puisse etre mise en file. D-067 tranche desormais la course A L'INSERTION: le
perdant recoit `23505` sur `jobs_tenant_purge_active_uniq` et ne detruit RIEN.

L'ASSERTION QUI COMPTE est donc `organization.status <> 'purging'` a la fin. Sans elle, ces
courses passeraient alors meme que le dead-end reviendrait: l'etat detruit est identique, seul
le statut final le distingue. Elle est portee par `_assert_coherent`, pour les trois scenarios.
"""
from __future__ import annotations

import io
import threading
import uuid
from dataclasses import dataclass

import psycopg
import pytest

from mervio.admin import operations
from mervio.persistence import jobs
from mervio.persistence.concurrency import organization_scope_key
from mervio.persistence.database import Database
from mervio.persistence.jobs import JobType
from mervio.persistence.service import ServiceSession, authorize_service, delegated_session
from mervio.persistence.tenancy import Permission, TenantContext, TenantSession
from mervio.workers.handlers import (JobContext, make_purge_organization_handler,
                                     make_purge_store_handler)

from .persistence_support import make_tenant
from .test_customer_erasure import ALICE, BOB, _raw_object, _sealed_snapshot_with_orders
from .test_customer_erasure_handler import PAYLOAD_BYTES, Store, _NullLog, _key_of
from .test_tenant_purge import ORG_JOB, _ctx
from .test_tenant_purge_recovery import OWNER, stall

#: toute course doit se terminer: un `join` sans borne masquerait un interblocage en CI
JOIN_TIMEOUT = 30
#: repetitions des scenarios critiques. Une course qui passe une fois ne prouve rien.
REPEATS = 20


@dataclass
class Outcome:
    """Ce qu'un fil a obtenu: son resultat, ou l'exception exacte qui l'a arrete."""

    name: str
    result: object = None
    error: BaseException | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def sqlstate(self) -> str | None:
        return getattr(self.error, "sqlstate", None)


def _own_session(pg, tenant):
    """Une session du MEME locataire sur sa PROPRE connexion: un second worker, en somme."""
    database = Database(pg.url("app"))
    return database, TenantSession(database, TenantContext(tenant.organization_id, tenant.owner_id))


def _own_worker(pg):
    return Database(pg.url("worker"))


def _race(*targets):
    """Lance les fils SIMULTANEMENT, sur une barriere, et rend leurs issues.

    La barriere est le mecanisme de synchronisation -- pas un `sleep`: chaque fil attend que
    tous soient prets, puis ils partent ensemble. C'est ce qui rend la course reproductible.
    """
    barrier = threading.Barrier(len(targets))
    outcomes = [Outcome(name) for name, _ in targets]

    def wrap(index, function):
        def run():
            barrier.wait(timeout=JOIN_TIMEOUT)
            try:
                outcomes[index].result = function()
            except BaseException as exc:  # noqa: BLE001 - l'exception EST la mesure
                outcomes[index].error = exc
        return run

    threads = [threading.Thread(target=wrap(index, function), daemon=True)
               for index, (_, function) in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
        assert not thread.is_alive(), "un fil ne s'est pas termine: interblocage probable"
    return outcomes


# -- fixtures -------------------------------------------------------------------------------------

@pytest.fixture
def store():
    return Store()


def _tenant_with_bytes(db, owner, principal, store, name: str, *, stores: int = 1):
    tenant = make_tenant(db, name)
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE, BOB])
    for kind in ("shopify_orders", "stripe"):
        object_id = _raw_object(owner, tenant, kind)
        store.put(_key_of(owner, tenant, object_id), io.BytesIO(PAYLOAD_BYTES))
    return tenant


@pytest.fixture
def victim(db, owner, principal, store):
    return _tenant_with_bytes(db, owner, principal, store, "conc-victim")


@pytest.fixture
def other_org(db, owner, principal, store):
    return _tenant_with_bytes(db, owner, principal, store, "conc-other")


def _purge(session, worker_db, principal, store, *, organization: bool, store_id=...):
    """Met en file, prend, et EXECUTE le gestionnaire -- le chemin reel du worker."""
    kind = JobType.PURGE_ORGANIZATION if organization else JobType.PURGE_STORE
    payload_store = None if organization else (session.organization_id and store_id)
    job = jobs.enqueue_job(session, job_type=kind, payload={},
                           store_id=None if organization else payload_store)
    claimed = jobs.claim_next_job(session, worker_id=f"w-{uuid.uuid4().hex[:6]}", job_id=job.id)
    assert claimed is not None
    delegated = delegated_session(ServiceSession(worker_db, session.organization_id, principal),
                                  claimed)
    handler = (make_purge_organization_handler(store) if organization
               else make_purge_store_handler(store))
    return handler(JobContext(session=delegated, job=claimed,
                              correlation_id=str(claimed.correlation_id), log=_NullLog(),
                              actor_id=principal.id, on_behalf_of=claimed.enqueued_by))


def _state(owner, tenant):
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(tenant.organization_id),))
        org = owner.execute("SELECT name, status FROM organizations WHERE id = %s",
                            (tenant.organization_id,)).fetchone()
        stores = owner.execute("SELECT status FROM stores WHERE organization_id = %s "
                               "ORDER BY id", (tenant.organization_id,)).fetchall()
        counts = {table: owner.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("raw_objects", "data_snapshots", "orders", "connections",
                                "reports", "analysis_runs")}
        jobs_left = owner.execute("SELECT count(*) FROM jobs").fetchone()[0]
    return {"organization": org, "stores": [s[0] for s in stores], **counts, "jobs": jobs_left}


def _assert_coherent(state, *, organization_purged: bool):
    """L'etat final doit etre INTERPRETABLE, quel que soit l'entrelacement observe."""
    for table in ("raw_objects", "data_snapshots", "orders", "connections", "reports",
                  "analysis_runs"):
        assert state[table] == 0, f"{table} subsiste: destruction partielle"
    # LE DEAD-END, nomme. Une organisation figee en `purging` a ses donnees detruites et sa
    # purge morte: aucune des autres assertions ne l'en distingue, puisque la destruction a
    # bien eu lieu. C'est celle-ci, et elle seule, qui echouerait si D-067 disparaissait.
    assert state["organization"][1] != "purging", \
        "organisation figee en `purging`: purge morte apres destruction (dead-end C1/C2)"
    if organization_purged:
        assert state["organization"] == ("[purged]", "purged")
        assert set(state["stores"]) == {"purged"}
    assert all(status in ("active", "purging", "purged") for status in state["stores"])


def _assert_one_winner(outcomes, loser_count: int = 1):
    """D-067 arbitre A L'INSERTION: le perdant echoue AVANT de pouvoir detruire quoi que ce soit."""
    losers = [o for o in outcomes if o.failed]
    assert len(losers) == loser_count, \
        f"{len(losers)} perdant(s) attendu(s) {loser_count}: {[str(o.error) for o in outcomes]}"
    for loser in losers:
        assert loser.sqlstate == "23505", str(loser.error)
        assert "jobs_tenant_purge_active_uniq" in str(loser.error), str(loser.error)


# -- C1: deux purges d'organisation simultanees ---------------------------------------------------

@pytest.mark.parametrize("attempt", range(REPEATS))
def test_c1_two_concurrent_organization_purges(victim, pg, principal, store, owner, attempt):
    """C1: deux purges de la MEME organisation, lancees ensemble.

    Avant D-067, ce scenario echouait 20 fois sur 20: les deux detruisaient, puis aucune ne
    pouvait poser la pierre tombale. D-067 rend la course decidable a l'insertion -- une seule
    purge existe -- et l'organisation atteint donc `purged`, jamais `purging`.
    """
    db_a, session_a = _own_session(pg, victim)
    db_b, session_b = _own_session(pg, victim)
    worker_a, worker_b = _own_worker(pg), _own_worker(pg)
    try:
        outcomes = _race(
            ("A", lambda: _purge(session_a, worker_a, principal, store, organization=True)),
            ("B", lambda: _purge(session_b, worker_b, principal, store, organization=True)),
        )
    finally:
        for resource in (db_a, db_b, worker_a, worker_b):
            resource.close()

    for outcome in outcomes:
        assert not isinstance(outcome.error, psycopg.errors.DeadlockDetected), "interblocage"
        assert outcome.sqlstate != "55P03", "lock timeout inattendu"
    _assert_one_winner(outcomes)
    state = _state(owner, victim)
    assert state["organization"][1] == "purged", "la purge gagnante doit aller jusqu'au bout"
    _assert_coherent(state, organization_purged=True)


# -- C2: purge_store contre purge_organization ----------------------------------------------------

@pytest.mark.parametrize("attempt", range(REPEATS))
def test_c2_store_purge_against_organization_purge(victim, pg, principal, store, owner, attempt):
    """C2: les deux portees s'affrontent sur la meme organisation, donc sur le MEME verrou.

    Meme dead-end qu'en C1 avant D-067, et il passait inapercu: la purge de boutique reussissait,
    la purge d'organisation mourait a la pierre tombale, et l'etat detruit etait indistinguable
    d'un succes. Les deux types partagent desormais la MEME cle de reservation: un seul gagne.

    LES DEUX VAINQUEURS SONT LEGITIMES. Boutique gagnante: l'organisation reste `active` et
    vivante. Organisation gagnante: tout est detruit et `purged`. Ce qui est interdit, c'est
    l'entre-deux.
    """
    db_a, session_a = _own_session(pg, victim)
    db_b, session_b = _own_session(pg, victim)
    worker_a, worker_b = _own_worker(pg), _own_worker(pg)
    try:
        outcomes = _race(
            ("store", lambda: _purge(session_a, worker_a, principal, store, organization=False,
                                     store_id=victim.store_id)),
            ("organization", lambda: _purge(session_b, worker_b, principal, store,
                                            organization=True)),
        )
    finally:
        for resource in (db_a, db_b, worker_a, worker_b):
            resource.close()

    for outcome in outcomes:
        assert not isinstance(outcome.error, psycopg.errors.DeadlockDetected), outcome.name
        assert outcome.sqlstate != "55P03", outcome.name
    _assert_one_winner(outcomes)
    winner = next(o.name for o in outcomes if not o.failed)
    state = _state(owner, victim)
    # aucune transition illegale: le statut reste dans le domaine, et une boutique d'une
    # organisation purgee est necessairement purgee elle aussi (barriere du tombstone).
    assert state["organization"][1] == ("purged" if winner == "organization" else "active"), \
        f"gagnant={winner}, statut={state['organization'][1]}"
    _assert_coherent(state, organization_purged=winner == "organization")
    assert set(state["stores"]) == {"purged"}


# -- C3: deux purges de la meme boutique ----------------------------------------------------------

@pytest.mark.parametrize("attempt", range(REPEATS))
def test_c3_two_concurrent_purges_of_the_same_store(victim, pg, principal, store, owner, attempt):
    """C3: meme boutique, deux fois. D-067 refuse la seconde -- une purge redondante n'a
    aucune valeur, et la boutique disparait exactement une fois.

    Ce scenario ne produisait PAS de dead-end avant D-067: `app_tombstone_store` ne porte pas
    la barriere "jobs remain" de ses homologues d'organisation. C'est toute l'asymetrie que la
    revue a mise au jour, et ce test la verrouille.
    """
    db_a, session_a = _own_session(pg, victim)
    db_b, session_b = _own_session(pg, victim)
    worker_a, worker_b = _own_worker(pg), _own_worker(pg)
    try:
        outcomes = _race(
            ("A", lambda: _purge(session_a, worker_a, principal, store, organization=False,
                                 store_id=victim.store_id)),
            ("B", lambda: _purge(session_b, worker_b, principal, store, organization=False,
                                 store_id=victim.store_id)),
        )
    finally:
        for resource in (db_a, db_b, worker_a, worker_b):
            resource.close()

    for outcome in outcomes:
        assert not isinstance(outcome.error, psycopg.errors.DeadlockDetected), outcome.name
        assert outcome.sqlstate != "55P03", outcome.name
    _assert_one_winner(outcomes)
    state = _state(owner, victim)
    assert state["raw_objects"] == 0 and state["data_snapshots"] == 0
    assert set(state["stores"]) == {"purged"}
    assert state["organization"][1] == "active", "une purge de boutique ne clot pas l'organisation"



# -- C4: deux administrateurs reprennent la MEME purge figee (D-070, 004.4.7) ---------------------
#
# Deux `org reconcile-purges --execute` simultanes sur une organisation figee. Les deux passent
# le pre-controle -- aucune purge n'est en vol -- puis D-067 tranche A L'INSERTION. Ce qui est
# mesure ici n'est pas l'arbitrage (C1 le fait deja) mais ce que D-070 exige de lui:
#   - UN seul gagnant, qui ecrit `job.enqueued`;
#   - UN seul perdant, dont la trace de refus EXISTE alors meme que sa transaction de mise en
#     file a ete ANNULEE par la `23505`. C'est la propriete non evidente de D-070: la trace est
#     ecrite APRES le `except`, dans une transaction neuve -- sinon elle disparaitrait avec le
#     rollback, et la classe de refus la plus interessante serait silencieusement vide.

REFUSAL_ACTION = "organization.purge_recovery_refused"


@pytest.fixture
def stalled(db, owner, worker_conn, principal, store):
    """Une organisation figee en `purging`, sa purge DEFINITIVEMENT echouee et ANTIDATEE.

    Chemin reel de bout en bout: mise en file, prise, cloture par la fonction privilegiee de
    `0016`, puis echec antidate pour que la garde `A` (delai minimal) n'oppose pas `too_soon`.
    """
    tenant = _tenant_with_bytes(db, owner, principal, store, "conc-stalled")
    with owner.transaction():
        _ctx(owner, tenant)
        owner.execute("UPDATE users SET idp_subject = %s WHERE id = %s", (OWNER, tenant.owner_id))
    stall(owner, worker_conn, tenant, age="2 hours")
    return tenant


def _reconcile(pg, tenant):
    """Un administrateur qui reprend, sur SA PROPRE connexion -- deux fils, deux connexions."""
    database = operations.connect(pg.url("app"))
    return lambda: operations.reconcile_purges(
        database, actor=OWNER, organization=tenant.organization_id, execute=True)


def _audit(owner, tenant, action):
    with owner.transaction():
        _ctx(owner, tenant)
        return owner.execute(
            "SELECT count(*) FROM audit_events WHERE organization_id = %s AND action = %s",
            (tenant.organization_id, action)).fetchone()[0]


@pytest.mark.parametrize("attempt", range(REPEATS))
def test_c4_two_administrators_resume_the_same_stalled_purge(pg, owner, stalled, attempt):
    """Un gagnant, un perdant, et le refus du perdant EST trace."""
    outcomes = _race(("A", _reconcile(pg, stalled)), ("B", _reconcile(pg, stalled)))

    # Aucun fil ne leve: `reconcile-purges` est une commande d'INSPECTION, elle RAPPORTE.
    assert not any(o.failed for o in outcomes), [str(o.error) for o in outcomes]
    issues = [o.result["reconciled"][0]["outcome"] for o in outcomes]
    assert sorted(issues) == ["already_active", "recovered"], issues

    # UNE seule purge en file: D-067 a tranche a l'insertion, comme en C1.
    with owner.transaction():
        _ctx(owner, stalled)
        active = owner.execute(
            "SELECT count(*) FROM jobs WHERE organization_id = %s AND job_type = %s "
            "AND status IN ('queued', 'running')",
            (stalled.organization_id, ORG_JOB)).fetchone()[0]
    assert active == 1, "D-067 n'a pas tranche: deux purges actives"

    # D-070: le gagnant trace sa mise en file, le perdant trace son refus. Un chacun.
    assert _audit(owner, stalled, "job.enqueued") == 1
    assert _audit(owner, stalled, REFUSAL_ACTION) == 1, \
        "la trace du perdant a disparu avec le rollback de la 23505"


def test_c4_the_loser_reports_the_race_as_the_reason(pg, owner, stalled):
    """Le motif enregistre nomme la course, et non une cause inventee."""
    outcomes = _race(("A", _reconcile(pg, stalled)), ("B", _reconcile(pg, stalled)))
    loser = next(o for o in outcomes
                 if o.result["reconciled"][0]["outcome"] == "already_active")
    assert "course" in loser.result["reconciled"][0]["blocked_reason"]
    assert loser.result["reconciled"][0].get("audit_error") is None, "la trace a bien ete ecrite"
    with owner.transaction():
        _ctx(owner, stalled)
        metadata = owner.execute(
            "SELECT metadata FROM audit_events WHERE organization_id = %s AND action = %s",
            (stalled.organization_id, REFUSAL_ACTION)).fetchone()[0]
    assert metadata["outcome"] == "already_active"
    assert "course" in metadata["reason"]
