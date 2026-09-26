"""Rejeu des effacements a l'import (Mission 004.4.5 E5, D-056).

    « Un import ulterieur applique les effacements enregistres : un effacement n'est pas
      annule par le prochain export du marchand. » -- D-056

Le rejeu est ENTIEREMENT pilote par `customer_redactions`: il n'efface personne, il n'invente
aucun jeton, il ne devine rien. Il reutilise le tombstone DEJA enregistre -- c'est ce qui
empeche un meme client d'apparaitre sous deux identites dans l'historique.

Le point d'insertion est le plus etroit possible: `write_snapshot`, DANS la transaction qui
rend les lignes durables, entre la derivation de la reference et son ecriture. Jamais
"ecrire puis corriger" -- une ligne canonique est immuable, il n'existe pas de second tour.
"""
from __future__ import annotations

import csv

import psycopg
import pytest

from mervio.application.persisted_analysis import SnapshotImportRequest, import_csv_snapshot
from mervio.persistence import erasure, jobs, snapshots
from mervio.persistence.jobs import JobType
from mervio.persistence.service import ServiceSession, authorize_service, delegated_session

from .persistence_support import TEST_MASTER_KEY, make_tenant, org_identity

ALICE_EMAIL = "alice@example.com"
BOB_EMAIL = "bob@example.com"


# -- jeu de donnees minimal ---------------------------------------------------------------------

def _orders_csv(path, rows):
    """Un export Shopify reduit a ce que le rejeu met en jeu: qui, quelle commande, combien."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Name", "Email", "Created at", "Currency", "Subtotal", "Discount Amount",
                         "Shipping", "Taxes", "Total", "Financial Status", "Lineitem quantity",
                         "Lineitem name", "Lineitem price", "Lineitem sku"])
        for order_ref, email, total in rows:
            writer.writerow([order_ref, email, "2026-01-15 10:00:00 +0000", "EUR", total, "0", "0", "0",
                             total, "paid", "1", "Article", total, "SKU-1"])
    return str(path)


@pytest.fixture
def dataset(tmp_path):
    def build(rows, name="orders.csv"):
        return {"shopify_orders": _orders_csv(tmp_path / name, rows)}
    return build


def _import(tenant, files, *, expect="completed"):
    """Importe ET exige qu'un instantane NEUF ait ete ecrit.

    Sans cette exigence un test peut passer pour la mauvaise raison: des octets deja importes
    rendent `reused` (`inputs_sha256`, `persisted_analysis`) et AUCUNE ligne n'est reecrite --
    le rejeu ne serait alors jamais exerce. Chaque reimport d'un test porte donc un contenu
    different, comme un vrai export posterieur qui contient aussi de nouvelles commandes.
    """
    result = import_csv_snapshot(tenant.session, store_id=tenant.store_id,
                                 connection_id=tenant.connection_id,
                                 request=SnapshotImportRequest(**files), master_key=TEST_MASTER_KEY)
    assert result.status == expect, f"{result.status} (attendu {expect})"
    return result


def _refs(tenant, snapshot_id):
    """References client REELLEMENT persistees, dans l'ordre des commandes."""
    restored = snapshots.load_dataset(tenant.session, tenant.store_id, snapshot_id)
    return [o.customer_id for o in restored.orders]


def _ref_of(tenant, email: str) -> str:
    """La reference que le mecanisme d'identite EXISTANT produit -- on ne la recalcule jamais."""
    return org_identity(tenant).ref_customer(email, "unused-order")


def _erase(tenant, worker_db, principal, reference):
    """Un effacement complet, par le chemin privilegie ratifie (E3/E4)."""
    job = jobs.enqueue_job(tenant.session, job_type=JobType.REDACT_CUSTOMER,
                           payload={"customer_ref": reference})
    claimed = jobs.claim_next_job(tenant.session, worker_id="eraser", job_id=job.id)
    session = delegated_session(ServiceSession(worker_db, tenant.organization_id, principal), claimed)
    return erasure.redact_customer(session, claimed).redacted_ref


@pytest.fixture
def tenant(db, principal):
    created = make_tenant(db, "replay")
    authorize_service(created.session, principal.id)
    return created


# -- A. client ordinaire --------------------------------------------------------------------------

def test_without_any_recorded_erasure_the_import_is_unchanged(tenant, dataset):
    """Compatibilite obligatoire: table d'effacements vide == comportement d'avant E5."""
    result = _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00"), ("#1002", BOB_EMAIL, "20.00")]))
    assert result.status == "completed"
    assert _refs(tenant, result.snapshot.id) == [_ref_of(tenant, ALICE_EMAIL), _ref_of(tenant, BOB_EMAIL)]


def test_a_customer_who_was_never_erased_keeps_a_normal_reference(tenant, dataset, worker_db, principal):
    """Le rejeu est pilote par un effacement ENREGISTRE: il ne deduit jamais qu'il faut effacer."""
    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))

    second = _import(tenant, dataset([("#2001", BOB_EMAIL, "30.00")], name="bob.csv"))
    assert _refs(tenant, second.snapshot.id) == [_ref_of(tenant, BOB_EMAIL)]


# -- B/F. client deja efface ------------------------------------------------------------------------

def test_a_historical_export_never_resurrects_an_erased_customer(tenant, dataset, worker_db, principal):
    """Le coeur de D-056: le marchand reimporte son vieil export, le client reste efface."""
    first = _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    assert _refs(tenant, first.snapshot.id) == [_ref_of(tenant, ALICE_EMAIL)]
    tombstone = _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))

    # un export POSTERIEUR: la vieille commande d'Alice y est toujours, plus une nouvelle.
    # Octets differents => instantane neuf => c'est bien le rejeu qui est exerce.
    again = _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00"), ("#1002", BOB_EMAIL, "20.00")],
                                    name="again.csv"))
    assert again.snapshot.id != first.snapshot.id
    assert _refs(tenant, again.snapshot.id) == [tombstone, _ref_of(tenant, BOB_EMAIL)]
    assert _ref_of(tenant, ALICE_EMAIL) not in _refs(tenant, again.snapshot.id)


def test_new_data_for_an_erased_customer_also_lands_on_the_recorded_tombstone(tenant, dataset,
                                                                               worker_db, principal):
    """Une commande POSTERIEURE a l'effacement, du meme client, ne le fait pas revivre non plus."""
    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    tombstone = _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))

    later = _import(tenant, dataset([("#9001", ALICE_EMAIL, "99.00")], name="later.csv"))
    assert _refs(tenant, later.snapshot.id) == [tombstone]


# -- C/I. stabilite du jeton --------------------------------------------------------------------------

def test_repeated_imports_always_reuse_the_same_recorded_tombstone(tenant, dataset, worker_db, principal):
    """Un nouveau jeton a chaque import scinderait le client en plusieurs dans l'historique."""
    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    tombstone = _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))

    seen, snapshot_ids = [], set()
    for attempt in range(3):
        # `attempt + 1` commandes en plus: chaque export differe du precedent ET du premier
        rows = [("#1001", ALICE_EMAIL, "10.00")] + [(f"#20{i}", BOB_EMAIL, "5.00")
                                                    for i in range(attempt + 1)]
        result = _import(tenant, dataset(rows, name=f"r{attempt}.csv"))
        snapshot_ids.add(result.snapshot.id)
        seen.append(_refs(tenant, result.snapshot.id)[0])
    assert len(snapshot_ids) == 3, "trois instantanes reels, aucun reuse"
    assert seen == [tombstone, tombstone, tombstone]


def test_the_replay_creates_no_redaction_and_no_second_tombstone(tenant, dataset, worker_db, principal, owner):
    """Le rejeu APPLIQUE un effacement; il n'en enregistre jamais un."""
    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))
    for attempt in range(3):
        rows = [("#1001", ALICE_EMAIL, "10.00")] + [(f"#30{i}", BOB_EMAIL, "5.00") for i in range(attempt + 1)]
        _import(tenant, dataset(rows, name=f"n{attempt}.csv"))

    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        rows = owner.execute("SELECT count(*), count(DISTINCT redacted_ref) FROM customer_redactions"
                             ).fetchone()
    assert rows == (1, 1), "une seule preuve, un seul jeton"


# -- D. deux clients ------------------------------------------------------------------------------------

def test_erasing_one_customer_leaves_the_other_untouched(tenant, dataset, worker_db, principal):
    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00"), ("#1002", BOB_EMAIL, "20.00")]))
    tombstone = _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))

    again = _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00"), ("#1002", BOB_EMAIL, "20.00"),
                                     ("#1003", BOB_EMAIL, "30.00")], name="both.csv"))
    assert _refs(tenant, again.snapshot.id) == [tombstone, _ref_of(tenant, BOB_EMAIL),
                                                _ref_of(tenant, BOB_EMAIL)]


# -- E. deux organisations ---------------------------------------------------------------------------------

def test_an_erasure_in_one_organization_never_reaches_another(db, dataset, worker_db, principal):
    """La RLS porte l'isolation: l'effacement de A est INVISIBLE pour B, donc inapplicable.

    Les deux organisations ont des sels differents (D-053), donc des references differentes
    pour le meme e-mail; la garantie ne doit pourtant pas reposer sur cette improbabilite.
    """
    first = make_tenant(db, "replay-a")
    second = make_tenant(db, "replay-b")
    authorize_service(first.session, principal.id)
    authorize_service(second.session, principal.id)

    _import(first, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    _erase(first, worker_db, principal, _ref_of(first, ALICE_EMAIL))

    result = _import(second, dataset([("#1001", ALICE_EMAIL, "10.00")], name="b.csv"))
    references = _refs(second, result.snapshot.id)
    assert references == [_ref_of(second, ALICE_EMAIL)]
    assert not references[0].startswith("redacted:"), "aucun tombstone d'une autre organisation"


def test_the_lookup_sees_nothing_of_another_organization(db, worker_db, principal, dataset):
    """Preuve directe sur la requete elle-meme, sans passer par l'import."""
    first = make_tenant(db, "look-a")
    second = make_tenant(db, "look-b")
    authorize_service(first.session, principal.id)
    _import(first, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    reference = _ref_of(first, ALICE_EMAIL)
    _erase(first, worker_db, principal, reference)

    from mervio.persistence.tenancy import Permission
    with first.session.transaction(Permission.IMPORT_DATA) as conn:
        assert erasure.recorded_redactions(conn, [reference]), "visible chez lui"
    with second.session.transaction(Permission.IMPORT_DATA) as conn:
        assert erasure.recorded_redactions(conn, [reference]) == {}, "invisible chez l'autre"


# -- H. table vide / references absentes ---------------------------------------------------------------------

def test_the_lookup_is_empty_and_cheap_when_nothing_was_ever_erased(tenant):
    from mervio.persistence.tenancy import Permission
    with tenant.session.transaction(Permission.IMPORT_DATA) as conn:
        assert erasure.recorded_redactions(conn, []) == {}
        assert erasure.recorded_redactions(conn, [None, ""]) == {}
        assert erasure.recorded_redactions(conn, ["c1:" + "0" * 32]) == {}


# -- 11. invariant KPI ---------------------------------------------------------------------------------------

def test_the_replay_changes_the_identity_and_nothing_else(tenant, dataset, worker_db, principal):
    """Les faits economiques sont intacts: memes commandes, memes montants, memes dates.

    Seule la reference du client change -- c'est tout ce que D-056 autorise le rejeu a faire.
    """
    files = dataset([("#1001", ALICE_EMAIL, "10.00"), ("#1002", BOB_EMAIL, "20.00")])
    before = snapshots.load_dataset(tenant.session, tenant.store_id, _import(tenant, files).snapshot.id)
    _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))
    later = _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00"), ("#1002", BOB_EMAIL, "20.00"),
                                     ("#1003", BOB_EMAIL, "7.00")], name="kpi.csv"))
    after = snapshots.load_dataset(tenant.session, tenant.store_id, later.snapshot.id)
    after.orders[:] = after.orders[:len(before.orders)]  # la commande ajoutee n'entre pas dans la comparaison

    assert len(after.orders) == len(before.orders)
    for old, new in zip(before.orders, after.orders):
        assert (new.order_id, new.total, new.subtotal, new.currency, new.created_at,
                new.financial_status) == (old.order_id, old.total, old.subtotal, old.currency,
                                          old.created_at, old.financial_status)
    assert sum(o.total for o in after.orders) == sum(o.total for o in before.orders)
    # la cardinalite des clients est preservee: un efface reste UN client, pas zero ni deux
    assert len({o.customer_id for o in after.orders}) == len({o.customer_id for o in before.orders})


# -- 13. audit ------------------------------------------------------------------------------------------------

def test_the_replay_emits_no_erasure_event(tenant, dataset, worker_db, principal, owner):
    """`customer.redacted` decrit l'EFFACEMENT, pas son application. Un import n'en ecrit pas."""
    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))

    def events():
        with owner.transaction():
            owner.execute("SELECT set_config('app.organization_id', %s, true)",
                          (str(tenant.organization_id),))
            return owner.execute("SELECT count(*) FROM audit_events WHERE action = 'customer.redacted'"
                                 ).fetchone()[0]

    before = events()
    for attempt in range(2):
        rows = [("#1001", ALICE_EMAIL, "10.00")] + [(f"#40{i}", BOB_EMAIL, "5.00") for i in range(attempt + 1)]
        _import(tenant, dataset(rows, name=f"a{attempt}.csv"))
    assert events() == before, "le rejeu n'ecrit aucun evenement d'effacement"


def test_no_import_audit_metadata_carries_a_customer_reference(tenant, dataset, worker_db, principal, owner):
    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))
    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00"), ("#1002", BOB_EMAIL, "20.00")],
                            name="audit.csv"))

    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        blob = " ".join(str(r[0]) for r in owner.execute("SELECT metadata::text FROM audit_events").fetchall())
    for secret in (ALICE_EMAIL, "c1:", "redacted:", "@"):
        assert secret not in blob, blob


# -- J. transaction -------------------------------------------------------------------------------------------

def test_a_failed_import_leaves_the_recorded_erasure_untouched(tenant, dataset, worker_db, principal, owner):
    """Le rejeu vit DANS la transaction de l'import: il s'annule avec lui, sans toucher la preuve."""
    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    tombstone = _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))

    broken = dataset([("#1001", ALICE_EMAIL, "10.00")], name="broken.csv")
    with open(broken["shopify_orders"], "w", encoding="utf-8") as handle:
        handle.write("Name,Nothing\n#1001,x\n")  # colonnes obligatoires absentes: refus
    _import(tenant, broken, expect="rejected")

    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        rows = owner.execute("SELECT customer_ref, redacted_ref FROM customer_redactions").fetchall()
    assert rows == [(_ref_of(tenant, ALICE_EMAIL), tombstone)], "la preuve est intacte"


# -- 17. course effacement / import -------------------------------------------------------------------------

def test_an_erasure_committed_before_the_lookup_is_applied(tenant, dataset, worker_db, principal):
    """Ordre SUR: l'effacement est commis AVANT la consultation, donc l'import le voit.

    READ COMMITTED: chaque instruction lit un instantane frais, donc la consultation faite
    apres le commit de l'effacement le voit necessairement.
    """
    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    tombstone = _erase(tenant, worker_db, principal, _ref_of(tenant, ALICE_EMAIL))
    result = _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00"), ("#1002", BOB_EMAIL, "20.00")],
                                     name="after.csv"))
    assert _refs(tenant, result.snapshot.id) == [tombstone, _ref_of(tenant, BOB_EMAIL)]


def test_no_erasure_can_commit_between_the_lookup_and_the_commit(tenant, dataset, worker_db,
                                                                  principal, pg, monkeypatch):
    """L'entrelacement que la campagne D-063 avait reproduit est desormais IMPOSSIBLE.

    Avant D-063, ce test constatait la resurrection: la consultation rendait vide, l'effacement
    commitait, et l'import ecrivait la reference d'origine. L'import tient maintenant le verrou
    PARTAGE de son organisation depuis l'ouverture de `write_snapshot`: un effacement ne peut
    plus prendre l'exclusif dans cette fenetre. On le verifie DE L'INTERIEUR de la fenetre,
    au moment exact ou la resurrection se produisait.
    """
    from mervio.persistence import snapshots as snapshots_module
    from mervio.persistence.concurrency import organization_scope_key

    _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00")]))
    reference = _ref_of(tenant, ALICE_EMAIL)
    observed = {}
    real_lookup = erasure.recorded_redactions

    eraser = psycopg.connect(pg.url("app"))

    def lookup_then_probe(conn, references):
        mapping = real_lookup(conn, references)
        with eraser.transaction():
            eraser.execute("SELECT set_config('app.organization_id', %s, true)",
                           (str(tenant.organization_id),))
            key = organization_scope_key(eraser, tenant.organization_id)
            # exactement ce qu'un effacement concurrent tenterait, a l'instant critique
            observed["exclusive"] = eraser.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0]
        return mapping

    monkeypatch.setattr(snapshots_module.erasure, "recorded_redactions", lookup_then_probe)
    result = _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00"), ("#1002", BOB_EMAIL, "20.00")],
                                     name="race.csv"))
    monkeypatch.undo()
    eraser.close()

    assert observed["exclusive"] is False, "un effacement a pu s'insinuer dans la fenetre"
    # l'import a donc ecrit la reference d'origine EN TOUTE SURETE: aucun effacement n'existe
    assert _refs(tenant, result.snapshot.id)[0] == reference

    # et un effacement commite APRES est applique au prochain import, comme D-056 l'exige
    tombstone = _erase(tenant, worker_db, principal, reference)
    later = _import(tenant, dataset([("#1001", ALICE_EMAIL, "10.00"), ("#1002", BOB_EMAIL, "20.00"),
                                     ("#1003", BOB_EMAIL, "5.00")], name="after-race.csv"))
    assert _refs(tenant, later.snapshot.id)[0] == tombstone
