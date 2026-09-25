"""Effacement client: tout ce qui doit ECHOUER (Mission 004.4.5, revision 0014, D-049/D-052).

Chaque test ci-dessous echouerait sur une conception ou le `customer_ref` serait un parametre,
ou la RLS serait contournee par `SECURITY DEFINER`, ou le privilege serait accorde plus large
que le strict necessaire.

Le fil conducteur: le modele est

    travail  ->  payload.customer_ref  ->  operation privilegiee

et JAMAIS `fonction(customer_ref) -> UPDATE`.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from mervio.persistence import jobs
from mervio.persistence.jobs import JobType
from mervio.persistence.service import authorize_service, revoke_service
from mervio.persistence.tenancy import Role, add_member, ensure_user

from .persistence_support import make_tenant
from .test_customer_erasure import (
    ALICE, BOB, TOMBSTONE, _claim, _raw_object, _redact, _refs, _requeue,
    _sealed_snapshot_with_orders, enqueue_redaction,
)


def _require_temp(conn) -> None:
    allowed = conn.execute("SELECT has_database_privilege(current_database(), 'TEMPORARY')").fetchone()[0]
    if not allowed:
        pytest.skip("le role n'a pas TEMPORARY (config type Docker): ombrage impossible")


@pytest.fixture
def victim(db, owner, worker_db, principal):
    """Organisation A: le locataire vise, avec un service autorise et des commandes scellees."""
    tenant = make_tenant(db, "adv-a")
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE, BOB])
    _raw_object(owner, tenant, "shopify_orders")
    return tenant


@pytest.fixture
def bystander(db, owner, principal):
    """Organisation B: un TIERS, qui partage la meme reference client par coincidence."""
    tenant = make_tenant(db, "adv-b")
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE])
    _raw_object(owner, tenant, "shopify_orders")
    return tenant


@pytest.fixture
def worker_sql(pg):
    with psycopg.connect(pg.url("worker"), autocommit=True) as conn:
        yield conn


@pytest.fixture
def worker2_sql(pg):
    """Un SECOND service, avec son propre principal: authentifie, mais pas autorise ici."""
    with psycopg.connect(pg.url("worker2"), autocommit=True) as conn:
        yield conn


# -- 1. frontiere de locataire -----------------------------------------------------------------

def test_a_job_of_another_organization_is_simply_not_found(victim, bystander, worker_sql, owner):
    """La RLS ne fuit AUCUN oracle d'existence: le travail d'autrui est introuvable, pas refuse."""
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE))
    with pytest.raises(psycopg.errors.NoDataFound, match="no such job in this organization"):
        _redact(worker_sql, victim, claimed, organization_id=bystander.organization_id)


def test_an_erasure_never_reaches_the_same_reference_in_another_organization(victim, bystander,
                                                                             owner, worker_sql):
    """Le meme `customer_ref` peut exister ailleurs: il ne doit PAS etre efface avec.

    (Deux organisations ne produisent normalement pas le meme HMAC -- les sels different, D-053 --
    mais la garantie d'isolation ne doit pas reposer sur cette improbabilite.)
    """
    _redact(worker_sql, victim, _claim(owner, victim, enqueue_redaction(owner, victim, ALICE)))
    assert _refs(owner, bystander) == [ALICE], "l'organisation voisine est intacte"


def test_the_privileged_function_does_not_bypass_row_level_security(victim, bystander, owner, worker_sql):
    """`SECURITY DEFINER` donne le DROIT d'ecrire, pas celui de sortir de l'organisation.

    Toutes les tables touchees sont en RLS FORCEE: le proprietaire y est soumis comme les autres.
    """
    _redact(worker_sql, victim, _claim(owner, victim, enqueue_redaction(owner, victim, ALICE)))
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(bystander.organization_id),))
        assert owner.execute("SELECT count(*) FROM customer_redactions").fetchone() == (0,)
        assert owner.execute("SELECT count(*) FROM raw_objects WHERE state <> 'available'").fetchone() == (0,)


def test_an_erasure_without_an_organization_context_is_refused(victim, worker_sql, owner):
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="context of one organization"):
        with worker_sql.transaction():
            worker_sql.execute("SELECT set_config('app.job_lease_token', %s, true)",
                               (f"{claimed.attempts}:{claimed.locked_by}",))
            worker_sql.execute("SELECT * FROM app_redact_customer(%s)", (claimed.id,))


# -- 2. identite et autorisation de service ------------------------------------------------------

def test_a_service_that_was_never_authorized_cannot_erase(victim, worker2_sql, principal2, owner):
    """Worker2 est un principal legitime, mais l'organisation ne l'a pas autorise."""
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not authorized for this organization"):
        _redact(worker2_sql, victim, claimed)


def test_a_revoked_service_cannot_erase_anymore(victim, worker_sql, principal, owner):
    """La revocation vaut IMMEDIATEMENT, pas a la prochaine mise en file."""
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE))
    revoke_service(victim.session, principal.id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not authorized for this organization"):
        _redact(worker_sql, victim, claimed)
    assert _refs(owner, victim) == [ALICE, BOB], "rien n'a ete efface"


def test_the_application_role_cannot_execute_the_privileged_function(victim, app_conn, owner):
    """Privilege minimal: `EXECUTE` est retire a PUBLIC et accorde au SEUL role worker."""
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with app_conn.transaction():
            app_conn.execute("SELECT set_config('app.organization_id', %s, true), "
                             "set_config('app.job_lease_token', %s, true)",
                             (str(victim.organization_id), f"{claimed.attempts}:{claimed.locked_by}"))
            app_conn.execute("SELECT * FROM app_redact_customer(%s)", (claimed.id,))


def test_the_function_is_executable_by_the_worker_role_and_nobody_else(owner):
    """`REVOKE ALL ... FROM PUBLIC` puis `GRANT EXECUTE` au seul worker (D-052)."""
    assert owner.execute("SELECT has_function_privilege('public', 'app_redact_customer(uuid)', "
                         "'EXECUTE')").fetchone()[0] is False
    assert owner.execute("SELECT has_function_privilege('mervio_worker', 'app_redact_customer(uuid)', "
                         "'EXECUTE')").fetchone()[0] is True
    assert owner.execute("SELECT has_function_privilege('mervio_app', 'app_redact_customer(uuid)', "
                         "'EXECUTE')").fetchone()[0] is False


# -- 3. frontiere du travail ---------------------------------------------------------------------

def test_a_job_that_does_not_exist_is_refused(victim, worker_sql, owner):
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE))
    ghost = claimed.__class__(**{**claimed.__dict__, "id": uuid.uuid4()}) if False else claimed
    with pytest.raises(psycopg.errors.NoDataFound):
        with worker_sql.transaction():
            worker_sql.execute("SELECT set_config('app.organization_id', %s, true), "
                               "set_config('app.job_lease_token', %s, true)",
                               (str(victim.organization_id), f"{ghost.attempts}:{ghost.locked_by}"))
            worker_sql.execute("SELECT * FROM app_redact_customer(%s)", (uuid.uuid4(),))


def test_a_job_of_another_type_cannot_erase_a_customer(victim, worker_sql, owner):
    """Un travail d'analyse, meme pris et meme portant un `customer_ref`, n'efface rien."""
    job = jobs.enqueue_job(victim.session, job_type=JobType.ANALYSIS,
                           payload={"customer_ref": ALICE})
    claimed = _claim(owner, victim, job.id)
    with pytest.raises(psycopg.errors.RestrictViolation, match="only runs for a customer erasure job"):
        _redact(worker_sql, victim, claimed)


def test_a_queued_job_that_was_never_claimed_cannot_erase(victim, worker_sql, owner):
    """Sans prise, pas d'effacement: l'operateur ne court-circuite pas la file."""
    job_id = enqueue_redaction(owner, victim, ALICE)
    with pytest.raises(psycopg.errors.RestrictViolation, match="only runs for a claimed job"):
        with worker_sql.transaction():
            worker_sql.execute("SELECT set_config('app.organization_id', %s, true), "
                               "set_config('app.job_lease_token', %s, true)",
                               (str(victim.organization_id), "0:worker-1"))
            worker_sql.execute("SELECT * FROM app_redact_customer(%s)", (job_id,))
    assert _refs(owner, victim) == [ALICE, BOB]


def test_an_erasure_without_the_lease_token_is_refused(victim, worker_sql, owner):
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE))
    with pytest.raises(psycopg.errors.RestrictViolation, match="only the lease holder"):
        _redact(worker_sql, victim, claimed, token="")


def test_a_stale_lease_token_cannot_erase_after_the_job_was_reclaimed(victim, worker_sql, owner):
    """Jeton d'exclusion (0006): un ancien detenteur ne doit plus rien pouvoir effacer."""
    job = enqueue_redaction(owner, victim, ALICE)
    first = _claim(owner, victim, job)
    stale = f"{first.attempts}:{first.locked_by}"
    _requeue(owner, victim, first)
    second = _claim(owner, victim, job, "worker-2")
    assert second.attempts != first.attempts

    with pytest.raises(psycopg.errors.RestrictViolation, match="only the lease holder"):
        _redact(worker_sql, victim, second, token=stale)
    assert _refs(owner, victim) == [ALICE, BOB]


def test_a_forged_lease_token_is_refused(victim, worker_sql, owner):
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE))
    for forged in (f"{claimed.attempts}:someone-else", f"{claimed.attempts + 1}:{claimed.locked_by}",
                   "0:worker-1", "'; --"):
        with pytest.raises(psycopg.errors.RestrictViolation, match="only the lease holder"):
            _redact(worker_sql, victim, claimed, token=forged)


# -- 4. rang du demandeur, relu a l'execution ------------------------------------------------------

def test_a_requester_who_lost_the_rank_cannot_have_the_erasure_executed(db, victim, owner, worker_sql):
    """D-052: le rang est relu MAINTENANT. Retrograder l'humain arrete le travail deja en file."""
    admin = ensure_user(db, "test|adv|admin")
    add_member(victim.session, user_id=admin, role=Role.ADMIN)
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE, requested_by=admin))

    with owner.transaction():  # retrogradation apres la mise en file
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(victim.organization_id),))
        owner.execute("UPDATE memberships SET role = 'analyst' WHERE user_id = %s", (admin,))

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="no longer holds the rank"):
        _redact(worker_sql, victim, claimed)
    assert _refs(owner, victim) == [ALICE, BOB]


def test_a_requester_removed_from_the_organization_cannot_have_the_erasure_executed(db, victim, owner,
                                                                                    worker_sql):
    admin = ensure_user(db, "test|adv|gone")
    add_member(victim.session, user_id=admin, role=Role.ADMIN)
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE, requested_by=admin))

    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(victim.organization_id),))
        owner.execute("DELETE FROM memberships WHERE user_id = %s", (admin,))

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="no longer holds the rank"):
        _redact(worker_sql, victim, claimed)


# -- 5. charge utile forgee -------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {},
    {"customer_ref": None},
    {"customer_ref": "alice@example.com"},
    {"customer_ref": "redacted:" + "0" * 8 + "-0000-4000-8000-" + "0" * 12},
    {"customer_ref": "c1:" + "z" * 32},
    {"customer_ref": "c1:" + "a" * 31},
    {"customer_ref": "c1:" + "a" * 32 + " OR 1=1"},
    {"customer_ref": ""},
    {"customer": ALICE},
])
def test_a_payload_that_carries_no_erasable_reference_erases_nothing(victim, worker_sql, owner, payload):
    """L'e-mail est le cas important: meme glisse dans la charge, la base le REFUSE."""
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, payload=payload))
    with pytest.raises(psycopg.errors.RestrictViolation, match="no erasable customer reference"):
        _redact(worker_sql, victim, claimed)
    assert _refs(owner, victim) == [ALICE, BOB]


def test_an_already_tombstoned_reference_cannot_be_erased_again(victim, worker_sql, owner):
    """Effacer un tombstone reecrirait un jeton par-dessus un autre, et perdrait la preuve."""
    tombstone = _redact(worker_sql, victim, _claim(owner, victim, enqueue_redaction(owner, victim, ALICE)))[1]
    job_id = enqueue_redaction(owner, victim, tombstone)
    with pytest.raises(psycopg.errors.RestrictViolation, match="no erasable customer reference"):
        _redact(worker_sql, victim, _claim(owner, victim, job_id, "worker-2"))
    assert _refs(owner, victim) == [tombstone, BOB]


# -- 6. immuabilite des lignes canoniques ------------------------------------------------------------

def test_the_application_role_cannot_tombstone_an_order_itself(victim, app_conn, owner):
    """Sans le chemin privilegie, personne ne touche une ligne canonique: ni droit, ni declencheur."""
    with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.RestrictViolation)):
        with app_conn.transaction():
            app_conn.execute("SELECT set_config('app.organization_id', %s, true)",
                             (str(victim.organization_id),))
            app_conn.execute("UPDATE orders SET customer_ref = %s WHERE customer_ref = %s",
                             (f"redacted:{uuid.uuid4()}", ALICE))
    assert _refs(owner, victim) == [ALICE, BOB]


def test_the_worker_role_cannot_tombstone_an_order_itself(victim, worker_sql, owner, principal):
    """Le worker EXECUTE la fonction; il n'ecrit pas la ligne lui-meme."""
    with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.RestrictViolation)):
        with worker_sql.transaction():
            worker_sql.execute("SELECT set_config('app.organization_id', %s, true), "
                               "set_config('app.user_id', %s, true)",
                               (str(victim.organization_id), str(victim.owner_id)))
            worker_sql.execute("UPDATE orders SET customer_ref = %s WHERE customer_ref = %s",
                               (f"redacted:{uuid.uuid4()}", ALICE))
    assert _refs(owner, victim) == [ALICE, BOB]


@pytest.mark.parametrize("value", [
    "c1:" + "f" * 32,                       # une AUTRE identite: fusionnerait deux clients
    "alice@example.com",                    # une identite en clair
    "redacted:not-a-uuid",                  # un jeton hors forme
    "",
])
def test_even_the_table_owner_cannot_set_an_arbitrary_customer_reference(victim, owner, value):
    """Le relachement de D-052 vise le TOMBSTONE, pas la colonne: tout autre valeur est refusee."""
    with pytest.raises((psycopg.errors.RestrictViolation, psycopg.errors.CheckViolation)):
        with owner.transaction(force_rollback=True):
            owner.execute("SELECT set_config('app.organization_id', %s, true)",
                          (str(victim.organization_id),))
            owner.execute("UPDATE orders SET customer_ref = %s WHERE customer_ref = %s", (value, ALICE))


def test_the_owner_cannot_change_another_column_along_with_the_tombstone(victim, owner):
    """La comparaison jsonb impose que RIEN d'autre ne bouge dans la ligne."""
    with pytest.raises(psycopg.errors.RestrictViolation, match="canonical records are immutable"):
        with owner.transaction(force_rollback=True):
            owner.execute("SELECT set_config('app.organization_id', %s, true)",
                          (str(victim.organization_id),))
            owner.execute("UPDATE orders SET customer_ref = %s, total = total + 1 WHERE customer_ref = %s",
                          (f"redacted:{uuid.uuid4()}", ALICE))


def test_another_canonical_table_stays_completely_frozen(victim, owner):
    """Le relachement porte sur `orders` SEULE: `payments` reste totalement figee, meme pour
    le proprietaire, et meme sur une colonne anodine."""
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(victim.organization_id),))
        assert owner.execute("SELECT count(*) FROM payments").fetchone() == (1,), "il faut une ligne"
        for column, value in (("status", "'refunded'"), ("order_ref", "'x'"), ("amount", "0")):
            with pytest.raises(psycopg.errors.RestrictViolation, match="canonical records are immutable"):
                with owner.transaction(force_rollback=True):
                    owner.execute(f"UPDATE payments SET {column} = {value}")


def test_the_relaxation_is_keyed_to_the_orders_table_by_name(owner):
    """La condition du declencheur nomme explicitement `orders`: aucune autre table n'y entre."""
    source = owner.execute(
        "SELECT prosrc FROM pg_proc WHERE proname = 'canonical_rows_forbid_update'").fetchone()[0]
    assert "TG_TABLE_NAME = 'orders'" in source
    assert "customer_ref" in source and source.count("TG_TABLE_NAME = '") == 1


def test_an_order_of_a_sealed_snapshot_cannot_be_deleted_by_the_erasure_path(victim, owner):
    """L'effacement REMPLACE une reference; il ne supprime jamais une ligne canonique (D-056)."""
    with pytest.raises(psycopg.errors.RestrictViolation):
        with owner.transaction(force_rollback=True):
            owner.execute("SELECT set_config('app.organization_id', %s, true)",
                          (str(victim.organization_id),))
            owner.execute("DELETE FROM orders WHERE customer_ref = %s", (ALICE,))


# -- 7. ombrage pg_temp et search_path ----------------------------------------------------------------

def test_a_hostile_search_path_does_not_change_the_outcome(victim, worker_sql, owner):
    """La fonction epingle son `search_path`: le reglage de session n'a aucun effet."""
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE))
    with worker_sql.transaction():
        worker_sql.execute("SELECT set_config('app.organization_id', %s, true), "
                           "set_config('app.job_lease_token', %s, true), "
                           "set_config('search_path', 'pg_temp', true)",
                           (str(victim.organization_id), f"{claimed.attempts}:{claimed.locked_by}"))
        tombstone = worker_sql.execute("SELECT * FROM public.app_redact_customer(%s)",
                                       (claimed.id,)).fetchone()[1]
    assert TOMBSTONE.match(tombstone)
    assert _refs(owner, victim) == [tombstone, BOB]


def test_forged_temp_tables_cannot_steer_the_privileged_function(victim, worker_sql, owner, principal):
    """Toutes les relations sont qualifiees `public.` (D-049): `pg_temp` ne peut rien substituer.

    On forge d'un coup TOUTES les tables que la fonction lit -- autorisation, appartenance,
    travail, preuve -- avec des valeurs qui, si elles etaient lues, donneraient un effacement
    d'une autre organisation, ou sur une autre reference.
    """
    _require_temp(worker_sql)
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE))
    worker_sql.execute("CREATE TEMP TABLE service_authorizations (organization_id uuid, "
                       "service_user_id uuid, revoked_at timestamptz)")
    worker_sql.execute("CREATE TEMP TABLE memberships (organization_id uuid, user_id uuid, role text)")
    worker_sql.execute("CREATE TEMP TABLE users (id uuid, idp_subject text, kind text)")
    worker_sql.execute("CREATE TEMP TABLE customer_redactions (id uuid, organization_id uuid, "
                       "customer_ref text, redacted_ref text)")
    worker_sql.execute("INSERT INTO pg_temp.customer_redactions VALUES (%s, %s, %s, %s)",
                       (uuid.uuid4(), victim.organization_id, ALICE, "redacted:forged"))

    _redaction, tombstone, orders, _objects = _redact(worker_sql, victim, claimed)
    assert tombstone != "redacted:forged", "la preuve forgee n'a pas ete lue"
    assert TOMBSTONE.match(tombstone) and orders == 1
    assert _refs(owner, victim) == [tombstone, BOB]


def test_a_forged_temp_membership_cannot_grant_the_missing_rank(db, victim, worker_sql, owner):
    """Le vecteur le plus direct: se donner le rang `admin` dans une table temporaire."""
    _require_temp(worker_sql)
    analyst = ensure_user(db, "test|adv|forged")
    add_member(victim.session, user_id=analyst, role=Role.ADMIN)
    claimed = _claim(owner, victim, enqueue_redaction(owner, victim, ALICE, requested_by=analyst))
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(victim.organization_id),))
        owner.execute("UPDATE memberships SET role = 'viewer' WHERE user_id = %s", (analyst,))

    worker_sql.execute("CREATE TEMP TABLE memberships (organization_id uuid, user_id uuid, role text)")
    worker_sql.execute("INSERT INTO pg_temp.memberships VALUES (%s, %s, 'owner')",
                       (victim.organization_id, analyst))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="no longer holds the rank"):
        _redact(worker_sql, victim, claimed)
    assert _refs(owner, victim) == [ALICE, BOB]


def test_the_privileged_function_pins_its_search_path_and_is_owned_by_the_schema_owner(owner):
    """D-049 et D-052: `search_path` epingle, proprietaire du schema, aucun role dedie ajoute."""
    config, secdef, is_owner = owner.execute(
        "SELECT proconfig, prosecdef, pg_get_userbyid(proowner) = current_user "
        "FROM pg_proc WHERE proname = 'app_redact_customer'").fetchone()
    assert secdef is True and is_owner is True
    assert config == ["search_path=pg_catalog, public, pg_temp"], config
    assert owner.execute("SELECT count(*) FROM pg_roles WHERE rolname IN "
                         "('mervio_redactor', 'mervio_purger')").fetchone() == (0,)


def test_the_immutability_trigger_reads_no_relation_by_an_unqualified_name(owner):
    source = owner.execute(
        "SELECT prosrc FROM pg_proc WHERE proname = 'canonical_rows_forbid_update'").fetchone()[0]
    assert "pg_catalog.pg_class" in source and "pg_catalog.pg_get_userbyid" in source
    assert " FROM pg_class" not in source, source


# -- 8. objets bruts -------------------------------------------------------------------------------

def test_the_raw_objects_of_another_organization_are_never_marked(victim, bystander, owner, worker_sql):
    _redact(worker_sql, victim, _claim(owner, victim, enqueue_redaction(owner, victim, ALICE)))
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(bystander.organization_id),))
        assert owner.execute("SELECT state, purge_reason FROM raw_objects").fetchall() == [("available", None)]


def test_no_role_but_the_privileged_path_can_move_a_raw_object_out_of_available(victim, app_conn,
                                                                                worker_sql, owner):
    """0013 n'accorde ni UPDATE ni DELETE: l'etat ne bouge que par le chemin privilegie."""
    for conn, context in ((app_conn, {}), (worker_sql, {"user_id": victim.owner_id})):
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.RestrictViolation)):
            with conn.transaction():
                conn.execute("SELECT set_config('app.organization_id', %s, true), "
                             "set_config('app.user_id', %s, true)",
                             (str(victim.organization_id), str(context.get("user_id", ""))))
                conn.execute("UPDATE raw_objects SET state = 'purged', purged_at = now()")
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(victim.organization_id),))
        assert owner.execute("SELECT state FROM raw_objects").fetchall() == [("available",)]
