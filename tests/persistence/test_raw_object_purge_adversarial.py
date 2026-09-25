"""Finalisation d'une purge d'objet brut: tout ce qui doit ECHOUER (004.4.5 E4, revision 0015).

`app_finalize_raw_object_purge` est la seconde fonction privilegiee du depot. Ses gardes sont
ecrites SEPAREMENT de celles de `app_redact_customer` (0014), deliberement: chaque fonction
privilegiee reste auditable seule. La protection contre la derive entre les deux est donc
BEHAVIORALE, et c'est ce fichier qui la porte -- il rejoue ici, un par un, exactement les
refus prouves pour E3.

L'invariant defendu:

    une ligne ne devient `purged` que par une tentative autorisee, et jamais sans etre
    passee par `purging`.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from mervio.persistence import erasure, jobs
from mervio.persistence.jobs import JobType
from mervio.persistence.service import ServiceSession, authorize_service, delegated_session, revoke_service
from mervio.persistence.tenancy import Role, add_member, ensure_user

from .persistence_support import make_tenant
from .test_customer_erasure import ALICE, _raw_object, _sealed_snapshot_with_orders


@pytest.fixture
def victim(db, owner, worker_db, principal):
    tenant = make_tenant(db, "purge-a")
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE])
    objects = {kind: _raw_object(owner, tenant, kind) for kind in ("shopify_orders", "shopify_products")}
    return tenant, objects


@pytest.fixture
def bystander(db, owner, principal):
    tenant = make_tenant(db, "purge-b")
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE])
    return tenant, {"shopify_orders": _raw_object(owner, tenant, "shopify_orders")}


def _claimed(worker_db, principal, tenant, *, worker_id="worker-1"):
    """Un travail d'effacement pris, et la session deleguee qui va avec."""
    job = jobs.enqueue_job(tenant.session, job_type=JobType.REDACT_CUSTOMER,
                           payload={"customer_ref": ALICE})
    claimed = jobs.claim_next_job(tenant.session, worker_id=worker_id, job_id=job.id)
    session = delegated_session(ServiceSession(worker_db, tenant.organization_id, principal), claimed)
    return session, claimed


def _marked(session, claimed):
    """Amene les objets porteurs d'identite en `purging` (E3), prealable de toute finalisation."""
    erasure.redact_customer(session, claimed)


def _state(owner, tenant, object_id) -> str:
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        return owner.execute("SELECT state FROM raw_objects WHERE id = %s", (object_id,)).fetchone()[0]


def _finalize_raw(conn, tenant, claimed, object_id, *, token=None, organization_id=None):
    """Appel direct, comme le ferait un worker: contexte + jeton, rien d'autre."""
    organization = organization_id if organization_id is not None else tenant.organization_id
    lease = token if token is not None else f"{claimed.attempts}:{claimed.locked_by}"
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true), "
                     "set_config('app.job_lease_token', %s, true)", (str(organization), lease))
        return conn.execute("SELECT app_finalize_raw_object_purge(%s, %s)",
                            (claimed.id, object_id)).fetchone()[0]


@pytest.fixture
def worker_sql(pg):
    with psycopg.connect(pg.url("worker"), autocommit=True) as conn:
        yield conn


@pytest.fixture
def worker2_sql(pg):
    with psycopg.connect(pg.url("worker2"), autocommit=True) as conn:
        yield conn


# -- 1. machine a etats ---------------------------------------------------------------------------

def test_an_available_object_cannot_jump_straight_to_purged(victim, worker_db, principal, worker_sql, owner):
    """Sauter `purging` finaliserait un objet que personne n'a rendu illisible."""
    tenant, objects = victim
    _, claimed = _claimed(worker_db, principal, tenant)
    with pytest.raises(psycopg.errors.RestrictViolation, match="only a raw object being purged"):
        _finalize_raw(worker_sql, tenant, claimed, objects["shopify_products"])
    assert _state(owner, tenant, objects["shopify_products"]) == "available"


def test_a_purged_object_is_never_resurrected_nor_rewritten(victim, worker_db, principal, worker_sql, owner):
    tenant, objects = victim
    session, claimed = _claimed(worker_db, principal, tenant)
    _marked(session, claimed)
    assert _finalize_raw(worker_sql, tenant, claimed, objects["shopify_orders"]) is True
    first = _purged_at(owner, tenant, objects["shopify_orders"])

    assert _finalize_raw(worker_sql, tenant, claimed, objects["shopify_orders"]) is False
    assert _purged_at(owner, tenant, objects["shopify_orders"]) == first, "horodatage inchange"
    assert _state(owner, tenant, objects["shopify_orders"]) == "purged"


def _purged_at(owner, tenant, object_id):
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        return owner.execute("SELECT purged_at FROM raw_objects WHERE id = %s", (object_id,)).fetchone()[0]


def test_the_finalization_keeps_every_non_sensitive_field(victim, worker_db, principal, worker_sql, owner):
    """D-056: la ligne est CONSERVEE. Seuls `state` et `purged_at` bougent."""
    tenant, objects = victim
    session, claimed = _claimed(worker_db, principal, tenant)
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        before = owner.execute(
            "SELECT sha256, byte_size, source_kind, object_key, origin, created_at, retain_until "
            "FROM raw_objects WHERE id = %s", (objects["shopify_orders"],)).fetchone()
    _marked(session, claimed)
    _finalize_raw(worker_sql, tenant, claimed, objects["shopify_orders"])
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        after = owner.execute(
            "SELECT sha256, byte_size, source_kind, object_key, origin, created_at, retain_until, "
            "purge_reason FROM raw_objects WHERE id = %s", (objects["shopify_orders"],)).fetchone()
    assert after[:7] == before
    assert after[7] == "customer_erasure", "le motif de purge est conserve, pas efface"


# -- 2. frontiere de locataire ----------------------------------------------------------------------

def test_an_object_of_another_organization_is_simply_not_found(victim, bystander, worker_db,
                                                                principal, worker_sql, owner):
    """Aucun oracle: l'objet d'autrui est introuvable, pas refuse pour une autre raison."""
    tenant, _ = victim
    other, other_objects = bystander
    session, claimed = _claimed(worker_db, principal, tenant)
    _marked(session, claimed)
    with pytest.raises(psycopg.errors.NoDataFound, match="no such raw object in this organization"):
        _finalize_raw(worker_sql, tenant, claimed, other_objects["shopify_orders"])
    assert _state(owner, other, other_objects["shopify_orders"]) == "available"


def test_a_job_of_another_organization_is_simply_not_found(victim, bystander, worker_db,
                                                            principal, worker_sql):
    tenant, objects = victim
    other, _ = bystander
    _, claimed = _claimed(worker_db, principal, tenant)
    with pytest.raises(psycopg.errors.NoDataFound, match="no such job in this organization"):
        _finalize_raw(worker_sql, tenant, claimed, objects["shopify_orders"],
                      organization_id=other.organization_id)


def test_without_an_organization_context_nothing_is_finalized(victim, worker_db, principal, worker_sql):
    tenant, objects = victim
    _, claimed = _claimed(worker_db, principal, tenant)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="context of one organization"):
        with worker_sql.transaction():
            worker_sql.execute("SELECT set_config('app.job_lease_token', %s, true)",
                               (f"{claimed.attempts}:{claimed.locked_by}",))
            worker_sql.execute("SELECT app_finalize_raw_object_purge(%s, %s)",
                               (claimed.id, objects["shopify_orders"]))


# -- 3. identite de service ---------------------------------------------------------------------------

def test_a_service_that_was_never_authorized_cannot_finalize(victim, worker_db, principal,
                                                              worker2_sql, principal2):
    tenant, objects = victim
    session, claimed = _claimed(worker_db, principal, tenant)
    _marked(session, claimed)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not authorized for this organization"):
        _finalize_raw(worker2_sql, tenant, claimed, objects["shopify_orders"])


def test_a_revoked_service_cannot_finalize_anymore(victim, worker_db, principal, worker_sql, owner):
    tenant, objects = victim
    session, claimed = _claimed(worker_db, principal, tenant)
    _marked(session, claimed)
    revoke_service(tenant.session, principal.id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not authorized for this organization"):
        _finalize_raw(worker_sql, tenant, claimed, objects["shopify_orders"])
    assert _state(owner, tenant, objects["shopify_orders"]) == "purging", "toujours pas detruit"


def test_the_application_role_cannot_execute_the_finalization(victim, worker_db, principal, app_conn):
    """Privilege minimal: `EXECUTE` retire a PUBLIC, accorde au seul role worker."""
    tenant, objects = victim
    _, claimed = _claimed(worker_db, principal, tenant)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _finalize_raw(app_conn, tenant, claimed, objects["shopify_orders"])


def test_only_the_worker_role_holds_execute(owner):
    for role, expected in (("public", False), ("mervio_app", False), ("mervio_worker", True)):
        assert owner.execute(
            "SELECT has_function_privilege(%s, 'app_finalize_raw_object_purge(uuid, uuid)', 'EXECUTE')",
            (role,)).fetchone()[0] is expected, role


def test_no_role_gains_a_general_update_on_raw_objects(owner):
    """La fonction est le SEUL chemin: aucun `UPDATE` de table n'a ete accorde en 0015."""
    granted = {(r[0], r[1]) for r in owner.execute(
        "SELECT grantee, privilege_type FROM information_schema.table_privileges "
        "WHERE table_name = 'raw_objects' AND grantee IN ('mervio_app', 'mervio_worker', 'PUBLIC')"
    ).fetchall()}
    assert granted == {("mervio_app", "SELECT"), ("mervio_app", "INSERT")}, granted


# -- 4. frontiere du travail ----------------------------------------------------------------------------

def test_a_job_of_another_type_cannot_finalize_a_purge(victim, worker_db, principal, worker_sql, owner):
    tenant, objects = victim
    session, claimed = _claimed(worker_db, principal, tenant)
    _marked(session, claimed)
    analysis = jobs.enqueue_job(tenant.session, job_type=JobType.ANALYSIS, payload={"k": "v"})
    other = jobs.claim_next_job(tenant.session, worker_id="worker-2", job_id=analysis.id)
    with pytest.raises(psycopg.errors.RestrictViolation, match="only runs for a customer erasure job"):
        _finalize_raw(worker_sql, tenant, other, objects["shopify_orders"])
    assert _state(owner, tenant, objects["shopify_orders"]) == "purging"


def test_a_job_that_does_not_exist_is_refused(victim, worker_db, principal, worker_sql):
    tenant, objects = victim
    _, claimed = _claimed(worker_db, principal, tenant)
    with pytest.raises(psycopg.errors.NoDataFound):
        with worker_sql.transaction():
            worker_sql.execute("SELECT set_config('app.organization_id', %s, true), "
                               "set_config('app.job_lease_token', %s, true)",
                               (str(tenant.organization_id), f"{claimed.attempts}:{claimed.locked_by}"))
            worker_sql.execute("SELECT app_finalize_raw_object_purge(%s, %s)",
                               (uuid.uuid4(), objects["shopify_orders"]))


def test_a_stale_or_forged_lease_token_cannot_finalize(victim, worker_db, principal, worker_sql, owner):
    """Jeton d'exclusion (0006): un ancien detenteur ne finalise plus rien."""
    tenant, objects = victim
    session, claimed = _claimed(worker_db, principal, tenant)
    _marked(session, claimed)
    for forged in ("", f"{claimed.attempts + 1}:{claimed.locked_by}",
                   f"{claimed.attempts}:someone-else", "0:worker-1"):
        with pytest.raises(psycopg.errors.RestrictViolation, match="only the lease holder"):
            _finalize_raw(worker_sql, tenant, claimed, objects["shopify_orders"], token=forged)
    assert _state(owner, tenant, objects["shopify_orders"]) == "purging"


def test_a_queued_job_that_was_never_claimed_cannot_finalize(victim, worker_db, principal, worker_sql):
    tenant, objects = victim
    session, claimed = _claimed(worker_db, principal, tenant)
    _marked(session, claimed)
    fresh = jobs.enqueue_job(tenant.session, job_type=JobType.REDACT_CUSTOMER,
                             payload={"customer_ref": ALICE})
    with pytest.raises(psycopg.errors.RestrictViolation, match="only runs for a claimed job"):
        with worker_sql.transaction():
            worker_sql.execute("SELECT set_config('app.organization_id', %s, true), "
                               "set_config('app.job_lease_token', %s, true)",
                               (str(tenant.organization_id), "0:worker-1"))
            worker_sql.execute("SELECT app_finalize_raw_object_purge(%s, %s)",
                               (fresh.id, objects["shopify_orders"]))


def test_a_requester_who_lost_the_rank_cannot_have_the_purge_finalized(db, victim, worker_db, principal,
                                                                        worker_sql, owner):
    """D-052: le rang est relu MAINTENANT, aussi pour la destruction des octets."""
    tenant, objects = victim
    admin = ensure_user(db, "test|purge|admin")
    add_member(tenant.session, user_id=admin, role=Role.ADMIN)
    from mervio.persistence.tenancy import TenantContext, TenantSession
    requester = TenantSession(db, TenantContext(tenant.organization_id, admin))
    job = jobs.enqueue_job(requester, job_type=JobType.REDACT_CUSTOMER, payload={"customer_ref": ALICE})
    claimed = jobs.claim_next_job(tenant.session, worker_id="worker-1", job_id=job.id)
    session = delegated_session(ServiceSession(worker_db, tenant.organization_id, principal), claimed)
    _marked(session, claimed)

    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        owner.execute("UPDATE memberships SET role = 'analyst' WHERE user_id = %s", (admin,))

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="no longer holds the rank"):
        _finalize_raw(worker_sql, tenant, claimed, objects["shopify_orders"])
    assert _state(owner, tenant, objects["shopify_orders"]) == "purging"


# -- 5. ombrage pg_temp et search_path ------------------------------------------------------------------

def test_forged_temp_tables_cannot_steer_the_finalization(victim, worker_db, principal, worker_sql, owner):
    """Toutes les relations sont qualifiees `public.` (D-049): `pg_temp` ne substitue rien."""
    if not worker_sql.execute("SELECT has_database_privilege(current_database(), 'TEMPORARY')").fetchone()[0]:
        pytest.skip("le role worker n'a pas TEMPORARY (config type Docker): ombrage impossible")
    tenant, objects = victim
    session, claimed = _claimed(worker_db, principal, tenant)
    _marked(session, claimed)
    worker_sql.execute("CREATE TEMP TABLE raw_objects (id uuid, organization_id uuid, state text)")
    worker_sql.execute("INSERT INTO pg_temp.raw_objects VALUES (%s, %s, 'purged')",
                       (objects["shopify_products"], tenant.organization_id))
    worker_sql.execute("CREATE TEMP TABLE memberships (organization_id uuid, user_id uuid, role text)")

    assert _finalize_raw(worker_sql, tenant, claimed, objects["shopify_orders"]) is True
    assert _state(owner, tenant, objects["shopify_orders"]) == "purged"
    assert _state(owner, tenant, objects["shopify_products"]) == "available", "la table forgee n'a rien change"


def test_a_hostile_search_path_does_not_change_the_outcome(victim, worker_db, principal, worker_sql, owner):
    tenant, objects = victim
    session, claimed = _claimed(worker_db, principal, tenant)
    _marked(session, claimed)
    with worker_sql.transaction():
        worker_sql.execute("SELECT set_config('app.organization_id', %s, true), "
                           "set_config('app.job_lease_token', %s, true), "
                           "set_config('search_path', 'pg_temp', true)",
                           (str(tenant.organization_id), f"{claimed.attempts}:{claimed.locked_by}"))
        done = worker_sql.execute("SELECT public.app_finalize_raw_object_purge(%s, %s)",
                                  (claimed.id, objects["shopify_orders"])).fetchone()[0]
    assert done is True
    assert _state(owner, tenant, objects["shopify_orders"]) == "purged"


def test_the_function_pins_its_search_path_and_is_owned_by_the_schema_owner(owner):
    config, secdef, is_owner = owner.execute(
        "SELECT proconfig, prosecdef, pg_get_userbyid(proowner) = current_user "
        "FROM pg_proc WHERE proname = 'app_finalize_raw_object_purge'").fetchone()
    assert secdef is True and is_owner is True
    assert config == ["search_path=pg_catalog, public, pg_temp"], config


# -- 6. concurrence ---------------------------------------------------------------------------------------

def test_two_workers_cannot_both_finalize_the_same_object(victim, worker_db, principal,
                                                           worker_sql, worker2_sql, owner):
    """Un seul `True`: la seconde tentative voit `purged` et rend `False`, sans rien reecrire.

    (Le second appel emprunte la connexion du premier service: c'est la CONCURRENCE sur la
    ligne qui est testee ici, pas l'autorisation -- couverte plus haut.)
    """
    tenant, objects = victim
    session, claimed = _claimed(worker_db, principal, tenant)
    _marked(session, claimed)
    outcomes = [_finalize_raw(worker_sql, tenant, claimed, objects["shopify_orders"]),
                _finalize_raw(worker_sql, tenant, claimed, objects["shopify_orders"])]
    assert outcomes == [True, False]
    assert _state(owner, tenant, objects["shopify_orders"]) == "purged"


def test_two_erasures_of_different_customers_keep_distinct_tombstones(db, owner, worker_db, principal):
    """Deux clients effacés ne fusionnent jamais, meme quand les objets sont deja partis."""
    from .test_customer_erasure import BOB
    tenant = make_tenant(db, "purge-two")
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE, BOB])
    _raw_object(owner, tenant, "shopify_orders")

    tombstones = []
    for reference, worker_id in ((ALICE, "worker-1"), (BOB, "worker-2")):
        job = jobs.enqueue_job(tenant.session, job_type=JobType.REDACT_CUSTOMER,
                               payload={"customer_ref": reference})
        claimed = jobs.claim_next_job(tenant.session, worker_id=worker_id, job_id=job.id)
        session = delegated_session(ServiceSession(worker_db, tenant.organization_id, principal), claimed)
        tombstones.append(erasure.redact_customer(session, claimed).redacted_ref)
    assert tombstones[0] != tombstones[1]
