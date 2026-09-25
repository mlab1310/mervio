"""Effacement client: chemin privilegie, preuve et tombstone (Mission 004.4.5, revision 0014).

Ces tests prouvent le COMPORTEMENT EN BASE, sous les vrais roles, jamais une intention Python.

Ce que la revision doit tenir:
- `app_redact_customer` ne s'execute que pour UN travail reellement pris, du bon type, de la
  bonne organisation, par un service autorise, pour un demandeur qui a ENCORE le rang (D-052);
- l'objet de l'operation vient de `jobs.payload`, jamais d'un parametre;
- `orders.customer_ref` devient un tombstone ALEATOIRE, distinct par client (D-056);
- tout le reste des lignes canoniques demeure immuable;
- `SECURITY DEFINER` ne contourne PAS la RLS: la fonction reste confinee a une organisation;
- la trace d'audit existe et ne porte ni identite, ni valeur effacee.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import psycopg
import pytest

from psycopg.types.json import Jsonb

from mervio.persistence.audit import Action, ResourceType
from mervio.persistence.service import authorize_service

from .persistence_support import make_tenant

TOMBSTONE = re.compile(r"^redacted:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ALICE = "c1:" + "a" * 32
BOB = "c1:" + "b" * 32
GUEST = "g1:" + "c" * 32


# -- mise en place -----------------------------------------------------------------------------

def _sealed_snapshot_with_orders(owner, tenant, refs) -> uuid.UUID:
    """Un instantane SCELLE portant une commande par reference client donnee.

    Scelle: c'est l'etat reel au moment d'un effacement, et celui ou l'immuabilite mord.
    """
    snapshot_id, source_id = uuid.uuid4(), uuid.uuid4()
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        owner.execute(
            "INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, source, connector, "
            "connector_version, schema_version, normalization_version, status, ingestion_started_at, "
            "synthetic, not_for_production) VALUES (%s, %s, %s, %s, 'csv', 'test', 'test', 'test', 'test', "
            "'ingesting', now(), true, true)",
            (snapshot_id, tenant.organization_id, tenant.store_id, tenant.connection_id))
        owner.execute(
            "INSERT INTO snapshot_sources (id, organization_id, store_id, snapshot_id, source_kind, "
            "file_sha256, byte_size, rows_read, rows_accepted) "
            "VALUES (%s, %s, %s, %s, 'shopify_orders', %s, 1024, %s, %s)",
            (source_id, tenant.organization_id, tenant.store_id, snapshot_id, "0" * 64,
             len(refs), len(refs)))
        for position, ref in enumerate(refs):
            owner.execute(
                "INSERT INTO orders (organization_id, store_id, snapshot_id, snapshot_source_id, position, "
                "source, source_record_id, order_ref, customer_ref, created_at, currency, subtotal, discount, "
                "shipping, tax, total, financial_status) VALUES (%s, %s, %s, %s, %s, 'shopify_orders', %s, %s, "
                "%s, now(), 'EUR', 10, 0, 0, 0, 10, 'paid')",
                (tenant.organization_id, tenant.store_id, snapshot_id, source_id, position,
                 f"rec-{position}", f"ord-{position}", ref))
        # une ligne canonique SANS reference client: elle doit rester totalement figee
        owner.execute(
            "INSERT INTO payments (organization_id, store_id, snapshot_id, snapshot_source_id, position, "
            "source, source_record_id, payment_ref, created_at, amount, status, order_ref) "
            "VALUES (%s, %s, %s, %s, 0, 'stripe', 'pay-0', 'pay-0', now(), 10, 'succeeded', 'ord-0')",
            (tenant.organization_id, tenant.store_id, snapshot_id, source_id))
        # SCELLE = `completed` (revision 0002), avec tout ce que la contrainte de completude exige
        owner.execute(
            "UPDATE data_snapshots SET status = 'completed', ingested_at = now(), inputs_sha256 = %s, "
            "currency = 'EUR', row_count = %s, record_counts = '{}'::jsonb, quality = '{}'::jsonb "
            "WHERE id = %s", ("1" * 64, len(refs), snapshot_id))
    return snapshot_id


def _raw_object(owner, tenant, source_kind: str, state: str = "available") -> uuid.UUID:
    object_id = uuid.uuid4()
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        owner.execute(
            "INSERT INTO raw_objects (id, organization_id, store_id, object_key, sha256, byte_size, "
            "source_kind, origin, state, retain_until) VALUES (%s, %s, %s, %s, %s, 10, %s, 'csv_upload', %s, "
            "now() + interval '30 days')",
            (object_id, tenant.organization_id, tenant.store_id,
             f"org/{tenant.organization_id}/store/{tenant.store_id}/raw/{uuid.uuid4()}",
             "0" * 64, source_kind, state))
    return object_id


def enqueue_redaction(owner, tenant, ref: str = ALICE, *, requested_by=None, payload=None):
    """Met un travail `redact_customer` en file EN SQL BRUT, et rend son identifiant.

    Le type existe en base (revision 0014) mais PAS encore dans `JobType`: son gestionnaire est
    E4. Ecrire la ligne directement teste d'ailleurs mieux E3 -- la charge n'est alors PAS
    filtree par `validate_payload`, donc la base est confrontee a ce qu'un appelant pourrait
    reellement y glisser, e-mail compris.
    """
    job_id = uuid.uuid4()
    document = {"customer_ref": ref} if payload is None else payload
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        owner.execute(
            "INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, enqueued_by, "
            "available_at, payload) VALUES (%s, %s, 'redact_customer', 'queued', %s, %s, now(), %s)",
            (job_id, tenant.organization_id, uuid.uuid4(),
             requested_by if requested_by is not None else tenant.owner_id, Jsonb(document)))
    return job_id


@dataclass(frozen=True)
class Claimed:
    """Ce dont la fonction privilegiee a besoin d'un travail pris: son identifiant et son jeton."""

    id: uuid.UUID
    attempts: int
    locked_by: str


def _claim(owner, tenant, job_id, worker_id: str = "worker-1") -> Claimed:
    """Prend le travail EN SQL BRUT: `queued -> running`, une tentative consommee, un bail pose.

    `jobs.claim_next_job` filtre sur `list(JobType)`, qui ne contient pas encore
    `redact_customer` (son gestionnaire est E4): la prise passe donc par SQL. La transition
    reste validee par le declencheur `jobs_guard_transition` (revision 0006) -- c'est bien la
    machine a etats reelle qui est exercee, pas une simulation.
    """
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        row = owner.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_at = now(), "
            "locked_by = %s, lease_expires_at = now() + interval '300 seconds', "
            "started_at = coalesce(started_at, now()), updated_at = now() "
            "WHERE id = %s AND status = 'queued' RETURNING id, attempts, locked_by",
            (worker_id, job_id)).fetchone()
    assert row is not None, "le travail n'etait pas en file"
    return Claimed(*row)


def _requeue(owner, tenant, claimed: Claimed) -> None:
    """Remet un travail pris en file (echec transitoire): le jeton du detenteur est exige."""
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true), "
                      "set_config('app.job_lease_token', %s, true)",
                      (str(tenant.organization_id), f"{claimed.attempts}:{claimed.locked_by}"))
        owner.execute(
            "UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL, "
            "lease_expires_at = NULL, last_error_code = 'transient', available_at = now(), "
            "updated_at = now() WHERE id = %s", (claimed.id,))


def _redact(conn, tenant, claimed, *, token=None, organization_id=None):
    """Appelle la fonction privilegiee comme le ferait le worker: contexte + jeton, rien d'autre."""
    organization = organization_id if organization_id is not None else tenant.organization_id
    lease = token if token is not None else f"{claimed.attempts}:{claimed.locked_by}"
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true), "
                     "set_config('app.job_lease_token', %s, true)", (str(organization), lease))
        return conn.execute("SELECT * FROM app_redact_customer(%s)", (claimed.id,)).fetchone()


@pytest.fixture
def erasure(db, owner, worker_db, principal, pg):
    """Un locataire pret a effacer: instantane scelle, objets bruts, service autorise."""
    tenant = make_tenant(db, "erasure")
    authorize_service(tenant.session, principal.id)
    snapshot_id = _sealed_snapshot_with_orders(owner, tenant, [ALICE, ALICE, BOB, GUEST])
    objects = {kind: _raw_object(owner, tenant, kind)
               for kind in ("shopify_orders", "stripe", "shopify_products", "google_ads")}
    return tenant, snapshot_id, objects


@pytest.fixture
def worker_sql(pg):
    with psycopg.connect(pg.url("worker"), autocommit=True) as conn:
        yield conn


def _refs(owner, tenant):
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        return [r[0] for r in owner.execute(
            "SELECT customer_ref FROM orders ORDER BY position").fetchall()]


def _objects_state(owner, tenant):
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        return dict(owner.execute("SELECT source_kind, state || ':' || coalesce(purge_reason, '-') "
                                  "FROM raw_objects").fetchall())


# -- vocabulaire et permission -----------------------------------------------------------------

def test_the_python_vocabulary_matches_the_migration():
    assert Action.CUSTOMER_REDACTED.value == "customer.redacted"
    assert ResourceType.CUSTOMER_REDACTION.value == "customer_redaction"
    # le TYPE DE TRAVAIL, lui, n'entre pas encore dans `JobType` (voir le test suivant)


def test_the_job_type_exists_in_the_database_but_not_yet_in_the_python_queue():
    """E2 declare le type EN BASE; son gestionnaire est E4.

    Le depot exige que tout type declare dans `JobType` ait un gestionnaire
    (`tests/test_jobs_unit.py`): l'invariant reste donc vrai, et aucun worker ne peut prendre
    un effacement avant de savoir l'executer.
    """
    from mervio.persistence.jobs import JobType
    assert "redact_customer" not in {kind.value for kind in JobType}


# -- effacement nominal --------------------------------------------------------------------------

def test_an_erasure_replaces_only_that_customer_reference(erasure, owner, worker_sql):
    tenant, _, _ = erasure
    claimed = _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE))
    _redaction, tombstone, orders, _objects = _redact(worker_sql, tenant, claimed)

    assert TOMBSTONE.match(tombstone), tombstone
    assert orders == 2, "les deux commandes d'Alice, et elles seules"
    assert _refs(owner, tenant) == [tombstone, tombstone, BOB, GUEST]


def test_two_erased_customers_never_share_a_tombstone(erasure, owner, worker_sql):
    """D-056: un tombstone DISTINCT par client, sinon deux clients fusionnent dans les agregats."""
    tenant, _, _ = erasure
    first = _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE)))[1]
    second = _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, BOB), "worker-2"))[1]

    assert first != second
    assert _refs(owner, tenant) == [first, first, second, GUEST]


def test_the_tombstone_is_random_and_not_derived_from_the_identity(erasure, db, owner, worker_sql, principal):
    """Le jeton ne doit tenir AUCUNE information sur le client: deux organisations effacant la
    MEME reference obtiennent deux jetons differents.

    Un jeton derive de l'identite (un second HMAC, par exemple) serait identique des deux cotes
    et relierait les deux organisations. D-056 exige un `uuid4` aleatoire, justement pour cela.
    """
    tenant, _, _ = erasure
    other = make_tenant(db, "erasure-twin")
    authorize_service(other.session, principal.id)
    _sealed_snapshot_with_orders(owner, other, [ALICE])

    mine = _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE)))[1]
    theirs = _redact(worker_sql, other, _claim(owner, other, enqueue_redaction(owner, other, ALICE), "worker-2"))[1]
    assert mine != theirs, "un tombstone derive de l'identite serait identique des deux cotes"


def test_the_aggregate_count_of_distinct_customers_is_preserved(erasure, owner, worker_sql):
    """Effacer ne doit ni fusionner ni multiplier les clients: la cardinalite reste la meme."""
    tenant, _, _ = erasure
    before = len(set(_refs(owner, tenant)))
    _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE)))
    _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, BOB), "worker-2"))
    assert len(set(_refs(owner, tenant))) == before


def test_a_guest_order_reference_is_erasable_too(erasure, owner, worker_sql):
    """`g1:` n'est pas une reference client (D-062) mais reste une reference EFFACABLE."""
    tenant, _, _ = erasure
    _, tombstone, orders, _ = _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, GUEST)))
    assert orders == 1
    assert _refs(owner, tenant) == [ALICE, ALICE, BOB, tombstone]


# -- preuve ---------------------------------------------------------------------------------------

def test_the_redaction_is_recorded_once_with_no_personal_data(erasure, owner, worker_sql):
    tenant, _, _ = erasure
    claimed = _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE))
    redaction_id, tombstone, _, _ = _redact(worker_sql, tenant, claimed)

    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        rows = owner.execute("SELECT id, customer_ref, redacted_ref, origin, requested_by, job_id "
                             "FROM customer_redactions").fetchall()
    assert rows == [(redaction_id, ALICE, tombstone, "operator_request", tenant.owner_id, claimed.id)]


def test_the_proof_table_holds_no_column_that_could_carry_an_identity(owner):
    columns = {r[0] for r in owner.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'customer_redactions'").fetchall()}
    assert columns == {"id", "organization_id", "customer_ref", "redacted_ref", "origin",
                       "requested_by", "job_id", "created_at"}
    for forbidden in ("email", "name", "phone", "address", "identity", "customer_email"):
        assert not any(forbidden in column for column in columns), forbidden


def test_the_recorded_reference_can_only_be_a_hmac_never_an_email(erasure, owner):
    """La contrainte refuse en base ce qu'un appelant ne doit jamais pouvoir y ecrire."""
    tenant, _, _ = erasure
    job_id = enqueue_redaction(owner, tenant)
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        for bad in ("alice@example.com", "redacted:" + str(uuid.uuid4()), "c1:zzz", ""):
            with pytest.raises(psycopg.errors.CheckViolation):
                with owner.transaction(force_rollback=True):
                    owner.execute(
                        "INSERT INTO customer_redactions (id, organization_id, customer_ref, redacted_ref, "
                        "origin, requested_by, job_id) VALUES (%s, %s, %s, %s, 'operator_request', %s, %s)",
                        (uuid.uuid4(), tenant.organization_id, bad, f"redacted:{uuid.uuid4()}",
                         tenant.owner_id, job_id))


# -- objets bruts ---------------------------------------------------------------------------------

def test_only_identity_bearing_objects_become_unreadable(erasure, owner, worker_sql):
    """D-056: `shopify_orders` et `stripe` portent l'identite; les deux autres sont conserves."""
    tenant, _, _ = erasure
    _, _, _, marked = _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE)))

    assert marked == 2
    assert _objects_state(owner, tenant) == {
        "shopify_orders": "purging:customer_erasure",
        "stripe": "purging:customer_erasure",
        "shopify_products": "available:-",
        "google_ads": "available:-",
    }


def test_objects_are_marked_even_when_no_canonical_row_carried_the_reference(erasure, owner, worker_sql):
    """Portee volontairement large (D-056): l'absence de commande ne prouve pas l'absence de
    l'identite dans les octets bruts -- un fichier Stripe porte l'e-mail sans reference persistee.

    Consequence assumee: une reference etrangere a l'organisation rend quand meme ses objets
    bruts illisibles. C'est le choix conservateur ratifie, pas un effet de bord.
    """
    tenant, _, _ = erasure
    stranger = "c1:" + "9" * 32
    _, _, orders, marked = _redact(worker_sql, tenant,
                                   _claim(owner, tenant, enqueue_redaction(owner, tenant, stranger)))
    assert orders == 0, "aucune commande ne portait cette reference"
    assert marked == 2, "les objets porteurs d'identite sont marques malgre tout"
    assert _refs(owner, tenant) == [ALICE, ALICE, BOB, GUEST], "aucune ligne canonique touchee"


def test_the_object_row_and_its_non_sensitive_metadata_survive(erasure, owner, worker_sql):
    """D-056: la ligne est CONSERVEE (empreinte, taille, type, dates); seuls les octets partent."""
    tenant, _, objects = erasure
    _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE)))
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        row = owner.execute("SELECT sha256, byte_size, source_kind, created_at IS NOT NULL, purged_at "
                            "FROM raw_objects WHERE id = %s", (objects["shopify_orders"],)).fetchone()
    assert row == ("0" * 64, 10, "shopify_orders", True, None)


def test_a_purge_reason_cannot_be_attached_to_an_available_object(erasure, owner):
    """La contrainte lie le motif a l'etat: pas de motif sans purge, pas de purge sans motif."""
    tenant, _, objects = erasure
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        with pytest.raises(psycopg.errors.CheckViolation):
            with owner.transaction(force_rollback=True):
                owner.execute("UPDATE raw_objects SET purge_reason = 'customer_erasure' WHERE id = %s",
                              (objects["shopify_products"],))


# -- idempotence ------------------------------------------------------------------------------------

def test_running_the_same_job_twice_restores_nothing_and_creates_one_tombstone(erasure, owner, worker_sql):
    tenant, _, _ = erasure
    claimed = _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE))
    first = _redact(worker_sql, tenant, claimed)
    second = _redact(worker_sql, tenant, claimed)

    assert second[0] == first[0] and second[1] == first[1], "meme preuve, meme tombstone"
    assert second[2] == 0, "plus aucune commande a effacer au second passage"
    assert _refs(owner, tenant) == [first[1], first[1], BOB, GUEST]
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        assert owner.execute("SELECT count(*) FROM customer_redactions").fetchone() == (1,)


def test_a_second_job_for_the_same_customer_reuses_the_first_tombstone(erasure, owner, worker_sql):
    """Une demande rejouee par un operateur ne doit pas creer un second client tombstone."""
    tenant, _, _ = erasure
    first = _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE)))
    second = _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE), "worker-2"))
    assert second[1] == first[1]
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        assert owner.execute("SELECT count(*) FROM customer_redactions").fetchone() == (1,)


def test_an_already_purging_object_is_not_marked_again(erasure, owner, worker_sql):
    tenant, _, _ = erasure
    _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE)))
    _, _, _, marked = _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, BOB), "worker-2"))
    assert marked == 0, "les objets sont deja illisibles: rien a refaire"


def test_the_whole_erasure_is_one_transaction_and_rolls_back_with_its_caller(erasure, owner, pg):
    """La fonction n'ouvre AUCUNE transaction autonome: annuler l'appelant annule TOUT.

    Preuve necessaire parce que l'operation touche quatre tables (preuve, commandes, objets,
    audit): si l'une d'elles etait commise separement, un echec laisserait un client
    partiellement efface -- des octets illisibles sans tombstone, ou l'inverse.
    """
    tenant, _, _ = erasure
    claimed = _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE))
    with psycopg.connect(pg.url("worker")) as conn:  # PAS autocommit: on controle la transaction
        conn.execute("SELECT set_config('app.organization_id', %s, true), "
                     "set_config('app.job_lease_token', %s, true)",
                     (str(tenant.organization_id), f"{claimed.attempts}:{claimed.locked_by}"))
        row = conn.execute("SELECT * FROM app_redact_customer(%s)", (claimed.id,)).fetchone()
        assert row[2] == 2, "l'effacement a bien eu lieu DANS la transaction"
        conn.rollback()

    assert _refs(owner, tenant) == [ALICE, ALICE, BOB, GUEST], "aucune commande tombstonee"
    assert _objects_state(owner, tenant) == {
        "shopify_orders": "available:-", "stripe": "available:-",
        "shopify_products": "available:-", "google_ads": "available:-",
    }
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        assert owner.execute("SELECT count(*) FROM customer_redactions").fetchone() == (0,)
        assert owner.execute("SELECT count(*) FROM audit_events WHERE action = 'customer.redacted'"
                             ).fetchone() == (0,)


# -- audit --------------------------------------------------------------------------------------------

def test_the_erasure_writes_one_audit_event_without_any_identity(erasure, owner, worker_sql, principal):
    tenant, _, _ = erasure
    claimed = _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE))
    redaction_id, tombstone, _, _ = _redact(worker_sql, tenant, claimed)

    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        rows = owner.execute(
            "SELECT actor_type, actor_id, resource_type, resource_id, outcome, on_behalf_of, metadata::text "
            "FROM audit_events WHERE action = 'customer.redacted'").fetchall()
    assert len(rows) == 1
    actor_type, actor_id, resource_type, resource_id, outcome, on_behalf_of, metadata = rows[0]
    assert (actor_type, actor_id) == ("worker", principal.id)
    assert (resource_type, resource_id) == ("customer_redaction", redaction_id)
    assert (outcome, on_behalf_of) == ("succeeded", tenant.owner_id)
    # ni l'identite, ni la valeur effacee, ni meme le tombstone ne transitent par la trace
    for secret in (ALICE, tombstone, "@"):
        assert secret not in metadata, metadata


def test_no_audit_event_carries_a_customer_reference_anywhere(erasure, owner, worker_sql):
    tenant, _, _ = erasure
    _redact(worker_sql, tenant, _claim(owner, tenant, enqueue_redaction(owner, tenant, ALICE)))
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        blob = " ".join(str(r[0]) for r in owner.execute(
            "SELECT metadata::text FROM audit_events").fetchall())
    assert ALICE not in blob and "c1:" not in blob and "g1:" not in blob
