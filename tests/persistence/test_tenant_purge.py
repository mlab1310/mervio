"""Fondation de la purge tenant: ce qui doit REUSSIR, et surtout ce qui doit ECHOUER (004.4.6).

Revision `0016`. Les invariants defendus ici sont ceux que D-051, D-065 et D-066 ont ratifies,
et que rien dans le schema ne garantissait avant cette revision:

    - la garde partagee `app_purge_guard` n'accorde AUCUN pouvoir a qui l'appelle directement,
      et aucun role applicatif ne peut la remplacer, l'alterer ni s'en octroyer l'execution;
    - la cloture, l'annulation des travaux en file et l'interdiction d'ecriture tiennent dans
      UNE transaction, et un `ROLLBACK` ne laisse aucun etat partiel;
    - D-066 n'amende QUE le plancher temporel: `queued` et `running` restent indestructibles,
      y compris le travail de purge lui-meme;
    - la suppression passe par `data_snapshots` et JAMAIS par une table canonique (C1), en un
      seul `DELETE` par portee (C2);
    - le sel d'identite survit a une purge de boutique, meme la derniere (D-065 Q4);
    - `customer_redactions` survit a une purge de boutique (D-065 Q5).
"""
from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from mervio.persistence.service import authorize_service

from .persistence_support import make_tenant
from .test_customer_erasure import ALICE, Claimed, _raw_object, _sealed_snapshot_with_orders

ORG_JOB = "purge_organization"
STORE_JOB = "purge_store"
#: Un travail QUELCONQUE du meme locataire. Ces tests ont besoin d'UN travail, pas d'une
#: SECONDE purge tenant -- que D-067 refuse desormais a l'insertion (`23505`).
OTHER_JOB = "redact_customer"

#: les sept operations privilegiees de `0016`, avec leur signature
PURGE_FUNCTIONS = (
    ("app_close_organization", "uuid"),
    ("app_close_store", "uuid"),
    ("app_destroy_identity_key", "uuid"),
    ("app_purge_finalize_raw_object", "uuid, uuid"),
    ("app_purge_tenant_data", "uuid"),
    ("app_tombstone_organization_stores", "uuid"),
    ("app_tombstone_organization", "uuid"),
    ("app_tombstone_store", "uuid"),
)


# -- outillage ---------------------------------------------------------------------------------

def _ctx(conn, tenant, organization_id=None):
    conn.execute("SELECT set_config('app.organization_id', %s, true)",
                 (str(organization_id if organization_id is not None else tenant.organization_id),))


def _enqueue(owner, tenant, job_type: str, *, store_id=..., requested_by=None):
    """Met un travail de purge en file EN SQL BRUT.

    `JobType` ne connait pas encore ces types -- leur gestionnaire appartient a la phase worker,
    et `0016` les declare en base seulement, exactement comme `0014` l'a fait pour
    `redact_customer`. La mise en file passe donc par SQL, mais la machine a etats reelle
    (`jobs_guard_insert`, `jobs_guard_transition`) est bien celle qui est exercee.
    """
    job_id = uuid.uuid4()
    store = tenant.store_id if store_id is ... else store_id
    with owner.transaction():
        _ctx(owner, tenant)
        owner.execute(
            "INSERT INTO jobs (id, organization_id, store_id, job_type, status, correlation_id, "
            "enqueued_by, available_at, payload) VALUES (%s, %s, %s, %s, 'queued', %s, %s, now(), %s)",
            (job_id, tenant.organization_id, store, job_type, uuid.uuid4(),
             requested_by if requested_by is not None else tenant.owner_id, Jsonb({})))
    return job_id


def _claim(owner, tenant, job_id, worker_id: str = "worker-1") -> Claimed:
    with owner.transaction():
        _ctx(owner, tenant)
        row = owner.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_at = now(), "
            "locked_by = %s, lease_expires_at = now() + interval '300 seconds', "
            "started_at = coalesce(started_at, now()), updated_at = now() "
            "WHERE id = %s AND status = 'queued' RETURNING id, attempts, locked_by",
            (worker_id, job_id)).fetchone()
    assert row is not None, "le travail n'etait pas en file"
    return Claimed(*row)


def _call(conn, tenant, claimed, sql: str, params=(), *, token=None, organization_id=None):
    """Appel d'une operation privilegiee comme le ferait un worker: contexte + jeton, rien d'autre."""
    lease = token if token is not None else f"{claimed.attempts}:{claimed.locked_by}"
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true), "
                     "set_config('app.job_lease_token', %s, true)",
                     (str(organization_id if organization_id is not None else tenant.organization_id), lease))
        return conn.execute(sql, params).fetchone()


def _status(owner, tenant, table: str, ident=None):
    with owner.transaction():
        _ctx(owner, tenant)
        if table == "organizations":
            row = owner.execute("SELECT status, purged_at FROM organizations WHERE id = %s",
                                (tenant.organization_id,)).fetchone()
        else:
            row = owner.execute("SELECT status, purged_at FROM stores WHERE id = %s",
                                (ident or tenant.store_id,)).fetchone()
    return row


@pytest.fixture
def worker_sql(pg):
    with psycopg.connect(pg.url("worker"), autocommit=True) as conn:
        yield conn


@pytest.fixture
def app_sql(pg):
    with psycopg.connect(pg.url("app"), autocommit=True) as conn:
        yield conn


@pytest.fixture
def victim(db, owner, principal):
    tenant = make_tenant(db, "purge-victim")
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE])
    objects = {kind: _raw_object(owner, tenant, kind) for kind in ("shopify_orders", "shopify_products")}
    return tenant, objects


@pytest.fixture
def bystander(db, owner, principal):
    """Une organisation VOISINE, qu'aucune purge ne doit toucher."""
    tenant = make_tenant(db, "purge-bystander")
    authorize_service(tenant.session, principal.id)
    _sealed_snapshot_with_orders(owner, tenant, [ALICE])
    return tenant, {"shopify_orders": _raw_object(owner, tenant, "shopify_orders")}


# -- 1. la garde partagee: inalterable et inatteignable ------------------------------------------

def test_purge_guard_is_immutable(pg):
    """Aucun role applicatif ne peut remplacer, alterer, supprimer ni s'approprier la garde.

    Deux barrieres INDEPENDANTES, et le test les exerce toutes les deux: l'absence de `CREATE`
    sur le schema bloque la creation et le remplacement; la non-propriete bloque le reste. La
    derniere ligne compte autant que les autres -- un role qui pourrait s'octroyer `EXECUTE`
    contournerait le `REVOKE ALL ... FROM PUBLIC` qui rend la garde inatteignable.
    """
    mutations = (
        ("CREATE OR REPLACE FUNCTION public.app_purge_guard(p_job_id uuid, p_job_type text) "
         "RETURNS public.jobs LANGUAGE sql AS $$ SELECT NULL::public.jobs $$", "schema public"),
        ("DROP FUNCTION public.app_purge_guard(uuid, text)", "must be owner"),
        ("ALTER FUNCTION public.app_purge_guard(uuid, text) SET search_path = pg_temp, public",
         "must be owner"),
        ("ALTER FUNCTION public.app_purge_guard(uuid, text) SECURITY DEFINER", "must be owner"),
        ("ALTER FUNCTION public.app_purge_guard(uuid, text) RENAME TO app_purge_guard_old",
         "must be owner"),
    )
    for kind in ("app", "worker"):
        with psycopg.connect(pg.url(kind), autocommit=True) as conn:
            for statement, fragment in mutations:
                with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
                    conn.execute(statement)
                assert fragment in str(raised.value), (kind, statement)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"GRANT EXECUTE ON FUNCTION public.app_purge_guard(uuid, text) "
                             f"TO {pg.role(kind)}")


def test_the_application_roles_cannot_create_anything_in_the_public_schema(pg, owner):
    """La premiere barriere de l'inalterabilite, affirmee pour elle-meme."""
    for kind in ("app", "worker"):
        assert owner.execute("SELECT has_schema_privilege(%s, 'public', 'USAGE')",
                             (pg.role(kind),)).fetchone()[0] is True
        assert owner.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')",
                             (pg.role(kind),)).fetchone()[0] is False


def test_purge_guard_is_unreachable(pg, victim, owner, worker_sql, app_sql):
    """Appelee directement, la garde est refusee; appelee par un definisseur, elle fonctionne.

    C'est ce qui rend l'arbitrage soutenable: la garde partagee n'ajoute AUCUNE surface
    privilegiee. Le proprietaire conserve son `EXECUTE` implicite, donc les sept definisseurs
    l'appellent; personne d'autre ne le peut.
    """
    tenant, _ = victim
    job = _enqueue(owner, tenant, ORG_JOB, store_id=None)
    claimed = _claim(owner, tenant, job)
    for conn in (app_sql, worker_sql):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _call(conn, tenant, claimed, "SELECT app_purge_guard(%s, %s)", (claimed.id, ORG_JOB))
    # le meme travail, par le chemin prevu: accepte
    assert _call(worker_sql, tenant, claimed,
                 "SELECT * FROM app_close_organization(%s)", (claimed.id,)) is not None


def _grantees(owner, name: str, schema_owner: str) -> set:
    """Beneficiaires d'`EXECUTE`, le PROPRIETAIRE exclu -- son droit est implicite, pas un grant."""
    acl = owner.execute(
        "SELECT coalesce(p.proacl, '{}'::aclitem[]) FROM pg_proc p "
        "WHERE p.pronamespace = 'public'::regnamespace AND p.proname = %s", (name,)).fetchone()[0]
    found = set()
    for item in acl:
        grantee = str(item).split("=", 1)[0]
        if grantee and grantee != schema_owner:
            found.add(grantee)
    return found


def test_only_the_worker_role_may_execute_the_purge_functions(pg, owner):
    """`REVOKE ALL FROM PUBLIC` puis `GRANT EXECUTE` au seul `mervio_worker` (D-052).

    Un beneficiaire vide -- PUBLIC -- ferait de chaque operation privilegiee une porte
    ouverte; un beneficiaire de trop suffirait a rompre D-052. On enumere donc, plutot que
    de chercher une sous-chaine.
    """
    schema_owner = pg.role("migrator")
    for name, _args in PURGE_FUNCTIONS:
        assert _grantees(owner, name, schema_owner) == {"mervio_worker"}, name
    assert _grantees(owner, "app_purge_guard", schema_owner) == set(), \
        "la garde ne doit recevoir AUCUN grant: elle n'est atteignable que par un definisseur"


def test_the_guard_is_not_security_definer_and_pins_its_search_path(owner):
    """Elle n'ajoute aucune surface privilegiee, et ne depend pas de l'heritage du `search_path`."""
    row = owner.execute(
        "SELECT p.prosecdef, array_to_string(p.proconfig, ',') FROM pg_proc p "
        "WHERE p.pronamespace = 'public'::regnamespace AND p.proname = 'app_purge_guard'").fetchone()
    assert row[0] is False
    assert row[1] == "search_path=pg_catalog, public, pg_temp"


def test_the_erasure_functions_are_untouched(owner):
    """D-065 Q2: `0016` ne rouvre ni `app_redact_customer` ni `app_finalize_raw_object_purge`."""
    definers = [r[0] for r in owner.execute(
        "SELECT p.proname FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
        "AND p.prosecdef ORDER BY 1").fetchall()]
    assert definers == sorted([
        "app_ensure_service_principal", "app_finalize_raw_object_purge", "app_redact_customer",
        *[name for name, _ in PURGE_FUNCTIONS]])
    for name in ("app_redact_customer", "app_finalize_raw_object_purge"):
        body = owner.execute("SELECT p.prosrc FROM pg_proc p WHERE p.proname = %s", (name,)).fetchone()[0]
        assert "app_purge_guard" not in body, f"{name} ne doit pas appeler la garde de purge"
    body = owner.execute(
        "SELECT p.prosrc FROM pg_proc p WHERE p.proname = 'app_finalize_raw_object_purge'").fetchone()[0]
    assert "redact_customer" in body, "son refus de type doit rester intact"


# -- 2. cloture, annulation, atomicite -----------------------------------------------------------

def test_closing_an_organization_cancels_queued_jobs_and_marks_objects(victim, owner, worker_sql):
    """D-065 Q7: la cloture, l'annulation et le marquage tiennent dans UNE transaction."""
    tenant, objects = victim
    idle = _enqueue(owner, tenant, OTHER_JOB, store_id=None)        # restera `queued`
    job = _enqueue(owner, tenant, ORG_JOB, store_id=None)
    claimed = _claim(owner, tenant, job)

    cancelled, marked = _call(worker_sql, tenant, claimed,
                              "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    assert cancelled == 1 and marked == len(objects)
    assert _status(owner, tenant, "organizations") == ("purging", None)
    with owner.transaction():
        _ctx(owner, tenant)
        assert owner.execute("SELECT status FROM jobs WHERE id = %s", (idle,)).fetchone()[0] == "cancelled"
        # le travail de purge est `running`: il n'est jamais sa propre victime
        assert owner.execute("SELECT status FROM jobs WHERE id = %s", (claimed.id,)).fetchone()[0] == "running"
        states = {r[0] for r in owner.execute("SELECT DISTINCT state FROM raw_objects").fetchall()}
        assert states == {"purging"}


def test_a_running_job_is_never_cancelled_by_the_closure(victim, owner, worker_sql):
    """D-065 Q7: un `redact_customer` deja pris n'est pas annule -- D-063 le serialise."""
    tenant, _ = victim
    other = _claim(owner, tenant, _enqueue(owner, tenant, OTHER_JOB, store_id=None), worker_id="worker-9")
    job = _enqueue(owner, tenant, ORG_JOB, store_id=None)
    claimed = _claim(owner, tenant, job)
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    with owner.transaction():
        _ctx(owner, tenant)
        assert owner.execute("SELECT status FROM jobs WHERE id = %s", (other.id,)).fetchone()[0] == "running"


def test_a_rolled_back_closure_leaves_nothing_behind(victim, owner, pg):
    """BEGIN -> cloture -> erreur -> ROLLBACK: aucun etat partiel, aucun travail annule."""
    tenant, _ = victim
    idle = _enqueue(owner, tenant, OTHER_JOB, store_id=None)
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    with psycopg.connect(pg.url("worker")) as conn:          # PAS autocommit: on controle la transaction
        conn.execute("SELECT set_config('app.organization_id', %s, false), "
                     "set_config('app.job_lease_token', %s, false)",
                     (str(tenant.organization_id), f"{claimed.attempts}:{claimed.locked_by}"))
        conn.execute("SELECT * FROM app_close_organization(%s)", (claimed.id,))
        conn.rollback()
    assert _status(owner, tenant, "organizations") == ("active", None)
    with owner.transaction():
        _ctx(owner, tenant)
        assert owner.execute("SELECT status FROM jobs WHERE id = %s", (idle,)).fetchone()[0] == "queued"
        states = {r[0] for r in owner.execute("SELECT DISTINCT state FROM raw_objects").fetchall()}
        assert states == {"available"}


def test_a_closed_organization_accepts_no_new_business_record(victim, owner, worker_sql):
    """Le declencheur de cloture: plus aucune ecriture metier, ni aucun travail -- SAUF UN.

    D-068 ouvre une exception, et une seule: la REPRISE de la purge d'organisation elle-meme.
    Sans elle, une purge definitivement echouee apres la cloture figeait l'organisation pour
    toujours, alors que D-066 qualifie `purging` d'etat "signalant une purge a reprendre".
    L'exception est bornee au type `purge_organization`, a la table `jobs`, et au statut
    `purging`; D-067 garantit par ailleurs qu'il n'y en aura jamais deux actives.
    """
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    with pytest.raises(psycopg.errors.RestrictViolation, match="closed and accepts no new record"):
        with owner.transaction():
            _ctx(owner, tenant)
            owner.execute("INSERT INTO stores (id, organization_id, name) VALUES (%s, %s, 'tardive')",
                          (uuid.uuid4(), tenant.organization_id))
    # tous les AUTRES types de travaux restent refuses: l'intention de D-065 Q7 est intacte
    for kind in (STORE_JOB, OTHER_JOB, "import", "analysis", "purge"):
        store = tenant.store_id if kind in (STORE_JOB, "import", "analysis") else None
        with pytest.raises(psycopg.errors.RestrictViolation, match="closed and accepts no new record"):
            _enqueue(owner, tenant, kind, store_id=store)
    # la purge en cours interdit la reprise: c'est D-067, pas le declencheur
    with pytest.raises(psycopg.errors.UniqueViolation, match="jobs_tenant_purge_active_uniq"):
        _enqueue(owner, tenant, ORG_JOB, store_id=None)


def test_the_closure_still_lets_the_purge_audit_itself(victim, owner, worker_sql):
    """Le declencheur ne doit PAS bloquer ce que la purge doit ecrire: son propre audit."""
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    with owner.transaction():
        _ctx(owner, tenant)
        rows = owner.execute(
            "SELECT action, resource_type, resource_id, metadata FROM audit_events "
            "WHERE action = 'organization.purge_started'").fetchall()
    assert len(rows) == 1
    action, resource_type, resource_id, metadata = rows[0]
    assert (action, resource_type, resource_id) == ("organization.purge_started", "organization",
                                                    tenant.organization_id)
    assert set(metadata) == {"job_id", "jobs_cancelled", "raw_objects_marked"}


# -- 3. D-066: l'exception porte sur le SEUL plancher temporel ------------------------------------

def _terminal_job(owner, tenant, *, age: str):
    """Un travail TERMINAL dont on choisit l'anciennete.

    Il est cree `queued` -- `jobs_guard_insert` n'en accepte pas d'autre -- puis annule par la
    transition LEGALE `queued -> cancelled`, qui est la seule occasion de poser `finished_at`:
    une fois termine, le travail est IMMUABLE et plus rien ne peut l'antidater. C'est
    exactement le piege que D-066 a documente, et le reproduire ici evite de le contourner.
    """
    job_id = uuid.uuid4()
    with owner.transaction():
        _ctx(owner, tenant)
        owner.execute(
            "INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, enqueued_by, "
            "available_at, payload) VALUES (%s, %s, 'analysis', 'queued', %s, %s, now(), %s)",
            (job_id, tenant.organization_id, uuid.uuid4(), tenant.owner_id, Jsonb({})))
        owner.execute(
            "UPDATE jobs SET status = 'cancelled', updated_at = now(), "
            f"finished_at = now() - interval '{age}' WHERE id = %s", (job_id,))
    return job_id


def _try_delete(owner, tenant, job_id) -> int:
    with owner.transaction():
        _ctx(owner, tenant)
        return owner.execute("DELETE FROM jobs WHERE id = %s", (job_id,)).rowcount


def _set_status(owner, tenant, status: str):
    """Pose le statut SANS passer par la purge: on teste la POLITIQUE, pas le chemin nominal.

    Relation QUALIFIEE `public.`: un test de cette suite plante volontairement une table
    `pg_temp.organizations`, et une session psycopg survit a la transaction qui l'a creee.
    """
    purged_at = "now()" if status == "purged" else "NULL"
    with owner.transaction():
        _ctx(owner, tenant)
        owner.execute(f"UPDATE public.organizations SET status = %s, purged_at = {purged_at} "
                      "WHERE id = %s", (status, tenant.organization_id))


def test_d066_active_organization_keeps_the_retention_floor(victim, owner):
    """1 et 2: `active` conserve la politique NORMALE, entiere."""
    tenant, _ = victim
    recent = _terminal_job(owner, tenant, age="1 minute")
    old = _terminal_job(owner, tenant, age="2 hours")
    assert _try_delete(owner, tenant, recent) == 0, "le plancher doit tenir pour une organisation active"
    assert _try_delete(owner, tenant, old) == 1


def test_d066_purging_lifts_the_floor_for_this_organization_only(victim, bystander, owner):
    """3 et 5: la fenetre s'ouvre pour l'organisation close, et pour elle seule."""
    tenant, _ = victim
    neighbour, _ = bystander
    mine = _terminal_job(owner, tenant, age="1 minute")
    theirs = _terminal_job(owner, neighbour, age="1 minute")
    _set_status(owner, tenant, "purging")
    assert _try_delete(owner, tenant, mine) == 1
    assert _try_delete(owner, neighbour, theirs) == 0, "une organisation voisine ne profite pas de la fenetre"


def test_d066_a_purged_organization_closes_the_window_again(victim, owner):
    """4: apres `purged`, l'exception est refermee -- d'ou l'ordre impose par D-066."""
    tenant, _ = victim
    recent = _terminal_job(owner, tenant, age="1 minute")
    _set_status(owner, tenant, "purged")
    assert _try_delete(owner, tenant, recent) == 0


def test_d066_never_makes_queued_or_running_jobs_deletable(victim, owner):
    """13 et 14: I1 n'est PAS amendee. C'est ce qui protege le travail de purge lui-meme."""
    tenant, _ = victim
    queued = _enqueue(owner, tenant, OTHER_JOB, store_id=None)
    running = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _set_status(owner, tenant, "purging")
    assert _try_delete(owner, tenant, queued) == 0, "un travail en file reste indestructible"
    assert _try_delete(owner, tenant, running.id) == 0, "un travail en cours reste indestructible"


def test_d066_requires_finished_at_even_during_the_window(victim, owner):
    """I2 n'est pas amendee: sans `finished_at`, un travail terminal reste indestructible.

    Mesure: l'etat ne se fabrique PAS. `jobs_guard_insert` n'accepte qu'un travail `queued`,
    et `jobs_finished_consistent` refuse ensuite tout etat terminal sans `finished_at`. I2 est
    donc portee par le SCHEMA, en amont de la politique -- une garantie plus forte que si elle
    dependait de la seule clause de suppression, et qui tient meme pendant la fenetre D-066.
    """
    tenant, _ = victim
    job_id = _enqueue(owner, tenant, ORG_JOB, store_id=None)
    _set_status(owner, tenant, "purging")
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_finished_consistent"):
        with owner.transaction():
            _ctx(owner, tenant)
            owner.execute("UPDATE jobs SET status = 'cancelled', updated_at = now() WHERE id = %s",
                          (job_id,))


def test_d066_cannot_be_opened_by_the_application_role(victim, app_sql, owner):
    """6: `mervio_app` ne peut pas ecrire `organizations.status`, donc pas ouvrir la fenetre."""
    tenant, _ = victim
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with app_sql.transaction():
            _ctx(app_sql, tenant)
            app_sql.execute("UPDATE organizations SET status = 'purging' WHERE id = %s",
                            (tenant.organization_id,))


def test_d066_ignores_a_forged_session_setting(victim, owner):
    """7: aucun GUC n'intervient dans la condition -- ils sont forgeables, pas autorisants."""
    tenant, _ = victim
    recent = _terminal_job(owner, tenant, age="1 minute")
    with owner.transaction():
        _ctx(owner, tenant)
        owner.execute("SELECT set_config('app.organization_status', 'purging', true), "
                      "set_config('app.purging', 'true', true)")
        assert owner.execute("DELETE FROM jobs WHERE id = %s", (recent,)).rowcount == 0


def test_d066_survives_pg_temp_shadowing_of_organizations(victim, owner):
    """12: une expression de politique resout ses relations au `CREATE POLICY` et stocke des OID.

    Une table `pg_temp.organizations` qui se declarerait `purging` n'a donc aucun effet -- et
    le temoin le prouve: seule la VRAIE table ouvre la fenetre.
    """
    tenant, _ = victim
    recent = _terminal_job(owner, tenant, age="1 minute")
    with owner.transaction():
        _ctx(owner, tenant)
        owner.execute("CREATE TEMP TABLE organizations (id uuid, status text)")
        owner.execute("INSERT INTO pg_temp.organizations VALUES (%s, 'purging')",
                      (tenant.organization_id,))
        owner.execute("SET LOCAL search_path = pg_temp, public")
        assert owner.execute("DELETE FROM public.jobs WHERE id = %s", (recent,)).rowcount == 0
    owner.execute("DROP TABLE pg_temp.organizations")     # une table temporaire survit a sa transaction
    _set_status(owner, tenant, "purging")            # temoin: la vraie table, elle, ouvre bien
    assert _try_delete(owner, tenant, recent) == 1


# -- 4. C1 / C2: la suppression passe par `data_snapshots`, en un seul DELETE ---------------------

def test_c1_a_sealed_canonical_row_is_never_deleted_directly(victim, owner):
    """C1: le refus qui IMPOSE de passer par `data_snapshots`. Non-regression.

    Si ce refus disparaissait, une purge pourrait se mettre a supprimer les canoniques
    directement -- et l'ordre de D-051 cesserait d'etre contraint par le schema.
    """
    tenant, _ = victim
    for table in ("orders", "snapshot_sources"):
        with pytest.raises(psycopg.errors.RestrictViolation, match="sealed data snapshot are immutable"):
            with owner.transaction():
                _ctx(owner, tenant)
                owner.execute(f"DELETE FROM {table} WHERE organization_id = %s",
                              (tenant.organization_id,))


def test_c1_the_cascade_from_data_snapshots_is_accepted(victim, owner):
    """C1, l'autre moitie: le CASCADE passe, lui, et emporte sources et canoniques."""
    tenant, _ = victim
    with owner.transaction():
        _ctx(owner, tenant)
        assert owner.execute("SELECT count(*) FROM orders").fetchone()[0] > 0
        owner.execute("DELETE FROM data_snapshots WHERE organization_id = %s",
                      (tenant.organization_id,))
        assert owner.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
        assert owner.execute("SELECT count(*) FROM snapshot_sources").fetchone()[0] == 0


def test_c2_a_superseded_snapshot_cannot_be_deleted_alone(victim, owner):
    """C2: la FK AUTO-REFERENTE `supersedes` interdit le lotissement, et impose un DELETE unique."""
    tenant, _ = victim
    with owner.transaction():
        _ctx(owner, tenant)
        first = owner.execute("SELECT id FROM data_snapshots WHERE organization_id = %s",
                              (tenant.organization_id,)).fetchone()[0]
        second = uuid.uuid4()
        owner.execute(
            "INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, source, "
            "connector, connector_version, schema_version, normalization_version, status, "
            "ingestion_started_at, synthetic, not_for_production, supersedes_snapshot_id) "
            "SELECT %s, organization_id, store_id, connection_id, source, connector, "
            "connector_version, schema_version, normalization_version, 'ingesting', now(), "
            "synthetic, not_for_production, %s FROM data_snapshots WHERE id = %s",
            (second, first, first))
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="supersedes"):
        with owner.transaction():
            _ctx(owner, tenant)
            owner.execute("DELETE FROM data_snapshots WHERE id = %s", (first,))
    with owner.transaction():          # UN SEUL DELETE couvrant toute la portee: accepte
        _ctx(owner, tenant)
        owner.execute("DELETE FROM data_snapshots WHERE organization_id = %s",
                      (tenant.organization_id,))
        assert owner.execute("SELECT count(*) FROM data_snapshots").fetchone()[0] == 0


# -- 5. finaliseur d'objet brut (D-065 Q2, Q3) ---------------------------------------------------

def test_the_purge_finalizer_only_removes_a_row_already_being_purged(victim, owner, worker_sql):
    """Q3: `available` ne saute pas la case `purging` -- ce serait perdre la trace a detruire."""
    tenant, objects = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    with pytest.raises(psycopg.errors.RestrictViolation, match="only a raw object being purged"):
        _call(worker_sql, tenant, claimed, "SELECT app_purge_finalize_raw_object(%s, %s)",
              (claimed.id, objects["shopify_orders"]))


def test_the_purge_finalizer_removes_the_row_and_is_idempotent(victim, owner, worker_sql):
    """Q3: apres `purging`, la ligne PART -- et un rejeu ne leve pas, il rend `false`."""
    tenant, objects = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    target = objects["shopify_orders"]
    assert _call(worker_sql, tenant, claimed, "SELECT app_purge_finalize_raw_object(%s, %s)",
                 (claimed.id, target))[0] is True
    assert _call(worker_sql, tenant, claimed, "SELECT app_purge_finalize_raw_object(%s, %s)",
                 (claimed.id, target))[0] is False
    with owner.transaction():
        _ctx(owner, tenant)
        assert owner.execute("SELECT count(*) FROM raw_objects WHERE id = %s", (target,)).fetchone()[0] == 0


def test_the_purge_finalizer_refuses_an_erasure_job(victim, owner, worker_sql):
    """Q2: les deux voies restent separees -- celle-ci ne sert pas `redact_customer`."""
    tenant, _ = victim
    from mervio.persistence import jobs as jobs_module
    from mervio.persistence.jobs import JobType
    job = jobs_module.enqueue_job(tenant.session, job_type=JobType.REDACT_CUSTOMER,
                                  payload={"customer_ref": ALICE})
    claimed = _claim(owner, tenant, job.id)
    with pytest.raises(psycopg.errors.RestrictViolation, match="only runs for a tenant purge job"):
        _call(worker_sql, tenant, claimed, "SELECT app_purge_finalize_raw_object(%s, %s)",
              (claimed.id, uuid.uuid4()))


# -- 6. sel d'identite et preuves d'effacement (D-065 Q4, Q5) ------------------------------------

def _seed_identity_key(owner, tenant):
    with owner.transaction():
        _ctx(owner, tenant)
        owner.execute(
            "INSERT INTO organization_identity_keys (organization_id, scheme, salt, master_key_id) "
            "VALUES (%s, 'hmac-sha256-v1', %s, %s)",
            (tenant.organization_id, b"\x01" * 32, "0" * 16))


def test_a_store_purge_never_destroys_the_identity_salt(victim, owner, worker_sql):
    """Q4: meme derniere boutique, le sel survit -- l'organisation, elle, reste vivante."""
    tenant, _ = victim
    _seed_identity_key(owner, tenant)
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, STORE_JOB))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_store(%s)", (claimed.id,))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_purge_tenant_data(%s)", (claimed.id,))
    with owner.transaction():
        _ctx(owner, tenant)
        salt, destroyed = owner.execute(
            "SELECT salt IS NOT NULL, destroyed_at FROM organization_identity_keys "
            "WHERE organization_id = %s", (tenant.organization_id,)).fetchone()
    assert salt is True and destroyed is None
    assert _status(owner, tenant, "organizations")[0] == "active", "l'organisation reste vivante"


def test_destroying_the_salt_requires_an_organization_purge(victim, owner, worker_sql):
    """Q4: la destruction du sel n'est PAS accessible a un travail de purge de boutique."""
    tenant, _ = victim
    _seed_identity_key(owner, tenant)
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, STORE_JOB))
    with pytest.raises(psycopg.errors.RestrictViolation, match="does not run for this job type"):
        _call(worker_sql, tenant, claimed, "SELECT app_destroy_identity_key(%s)", (claimed.id,))


def test_an_organization_purge_destroys_the_salt_once_and_only_once(victim, owner, worker_sql):
    """D-053: le sel se DETRUIT, jamais ne se supprime; le rejeu est sans effet et sans erreur.

    D-069: la cloture PRECEDE desormais la destruction du sel, comme elle precedait deja celle
    des donnees. La sequence ci-dessous n'est donc plus un raccourci de test: c'est l'ordre.
    """
    tenant, _ = victim
    _seed_identity_key(owner, tenant)
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    assert _call(worker_sql, tenant, claimed, "SELECT app_destroy_identity_key(%s)",
                 (claimed.id,))[0] is True
    assert _call(worker_sql, tenant, claimed, "SELECT app_destroy_identity_key(%s)",
                 (claimed.id,))[0] is False
    with owner.transaction():
        _ctx(owner, tenant)
        row = owner.execute(
            "SELECT salt, destroyed_at IS NOT NULL, master_key_id FROM organization_identity_keys "
            "WHERE organization_id = %s", (tenant.organization_id,)).fetchone()
    assert row[0] is None and row[1] is True and row[2] == "0" * 16, "la LIGNE survit, videe"


def test_a_store_purge_keeps_the_customer_redaction_proofs(victim, owner, worker_sql):
    """Q5: `customer_redactions` est organization-scoped -- une boutique ne l'emporte pas."""
    tenant, _ = victim
    proof_job = _enqueue(owner, tenant, OTHER_JOB, store_id=None)
    with owner.transaction():
        _ctx(owner, tenant)
        owner.execute(
            "INSERT INTO customer_redactions (id, organization_id, customer_ref, redacted_ref, "
            "origin, requested_by, job_id) VALUES (%s, %s, %s, %s, 'operator_request', %s, %s)",
            (uuid.uuid4(), tenant.organization_id, ALICE, f"redacted:{uuid.uuid4()}",
             tenant.owner_id, proof_job))
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, STORE_JOB))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_store(%s)", (claimed.id,))
    _, _, _, _, redactions, _ = _call(worker_sql, tenant, claimed,
                                      "SELECT * FROM app_purge_tenant_data(%s)", (claimed.id,))
    assert redactions == 0
    with owner.transaction():
        _ctx(owner, tenant)
        assert owner.execute("SELECT count(*) FROM customer_redactions").fetchone()[0] == 1


# -- 7. isolation et rang ------------------------------------------------------------------------

def test_a_purge_never_reaches_a_neighbouring_organization(victim, bystander, owner, worker_sql):
    """L'isolation locative ne depend d'aucune condition Python: elle est portee par la RLS."""
    tenant, _ = victim
    neighbour, neighbour_objects = bystander
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_purge_tenant_data(%s)", (claimed.id,))
    with owner.transaction():
        _ctx(owner, neighbour)
        assert owner.execute("SELECT count(*) FROM data_snapshots").fetchone()[0] == 1
        assert owner.execute("SELECT state FROM raw_objects WHERE id = %s",
                             (neighbour_objects["shopify_orders"],)).fetchone()[0] == "available"
        assert owner.execute("SELECT status FROM organizations WHERE id = %s",
                             (neighbour.organization_id,)).fetchone()[0] == "active"


def test_a_job_of_another_organization_is_invisible(victim, bystander, owner, worker_sql):
    """La RLS, pas une condition relue: le travail du voisin n'existe simplement pas ici."""
    tenant, _ = victim
    neighbour, _ = bystander
    theirs = _claim(owner, neighbour, _enqueue(owner, neighbour, ORG_JOB, store_id=None))
    with pytest.raises(psycopg.errors.NoDataFound, match="no such job in this organization"):
        _call(worker_sql, tenant, theirs, "SELECT * FROM app_close_organization(%s)", (theirs.id,),
              organization_id=tenant.organization_id)


def test_a_requester_below_owner_rank_cannot_purge(victim, db, owner, worker_sql):
    """D-065 Q6: `owner` est exige, et RELU a l'execution -- `admin` ne suffit pas."""
    from mervio.persistence.tenancy import Role, add_member, ensure_user
    tenant, _ = victim
    admin_id = ensure_user(db, "test|purge|admin")
    add_member(tenant.session, user_id=admin_id, role=Role.ADMIN)
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None,
                                             requested_by=admin_id))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="rank required to purge"):
        _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))


def test_a_stale_lease_token_cannot_purge(victim, owner, worker_sql):
    """Le jeton d'exclusion de `0006`: un ancien detenteur ne purge plus rien."""
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    with pytest.raises(psycopg.errors.RestrictViolation, match="only the lease holder"):
        _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,),
              token="0:someone-else")


def test_an_unauthorized_service_cannot_purge(victim, db, owner, pg):
    """Un service non autorise pour l'organisation est refuse, meme avec un travail valide."""
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    with psycopg.connect(pg.url("worker2"), autocommit=True) as other:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _call(other, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))


# -- 8. parcours complet et ordre impose ---------------------------------------------------------

def _finalize_all(worker_sql, tenant, claimed, owner):
    with owner.transaction():
        _ctx(owner, tenant)
        ids = [r[0] for r in owner.execute("SELECT id FROM raw_objects").fetchall()]
    for object_id in ids:
        _call(worker_sql, tenant, claimed, "SELECT app_purge_finalize_raw_object(%s, %s)",
              (claimed.id, object_id))


def test_the_tombstone_refuses_to_close_before_the_bytes_are_gone(victim, owner, worker_sql):
    """D-065 Q3: poser la pierre tombale avant la destruction affirmerait un fait inexistant."""
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    with pytest.raises(psycopg.errors.RestrictViolation, match="raw objects remain"):
        _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization(%s)", (claimed.id,))


def test_the_tombstone_refuses_to_close_before_the_jobs_are_gone(victim, owner, worker_sql):
    """D-066: apres `purged` l'exception se referme, donc le nettoyage doit la PRECEDER."""
    tenant, _ = victim
    # seme AVANT la cloture: apres, le declencheur refuse tout nouveau travail (D-065 Q7)
    _terminal_job(owner, tenant, age="1 minute")
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    _finalize_all(worker_sql, tenant, claimed, owner)
    with pytest.raises(psycopg.errors.RestrictViolation, match="jobs remain"):
        _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization(%s)", (claimed.id,))


def test_the_whole_organization_purge_runs_in_the_ratified_order(victim, owner, worker_sql):
    """Le parcours complet de D-051, tel que `0016` le rend possible.

    Ce qui DISPARAIT: rapports, executions, instantanes (et par CASCADE sources et lignes
    canoniques), connexions, preuves d'effacement, travaux, lignes `raw_objects`.
    Ce qui SURVIT: la ligne `organizations`, videe et tombstonee; l'audit; les `memberships`;
    la ligne de sel, DETRUITE mais presente; et le travail de purge lui-meme.
    """
    tenant, _ = victim
    _seed_identity_key(owner, tenant)
    _terminal_job(owner, tenant, age="1 minute")          # un travail que le plancher protegerait
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))

    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    _call(worker_sql, tenant, claimed, "SELECT app_destroy_identity_key(%s)", (claimed.id,))
    _finalize_all(worker_sql, tenant, claimed, owner)
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_purge_tenant_data(%s)", (claimed.id,))
    # UNIQUEMENT le chemin privilegie: aucune ecriture brute de statut. La premiere version de
    # ce test posait la pierre tombale des boutiques a la main, en SQL brut sous le
    # proprietaire -- ce qui masquait que rien, dans 0016, ne savait le faire pour une purge
    # d'organisation. C'est ce contournement qui a laisse passer le defaut.
    assert _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization_stores(%s)",
                 (claimed.id,))[0] == 1
    assert _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization(%s)",
                 (claimed.id,))[0] is True

    with owner.transaction():
        _ctx(owner, tenant)
        for table in ("reports", "analysis_runs", "data_snapshots", "snapshot_sources", "orders",
                      "connections", "customer_redactions", "raw_objects"):
            assert owner.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
        assert owner.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1, "seule la purge subsiste"
        name, status, purged_at = owner.execute(
            "SELECT name, status, purged_at FROM organizations WHERE id = %s",
            (tenant.organization_id,)).fetchone()
        assert (name, status) == ("[purged]", "purged") and purged_at is not None
        assert owner.execute("SELECT count(*) FROM memberships").fetchone()[0] >= 1
        store_name, store_currency, store_status, store_purged = owner.execute(
            "SELECT name, currency, status, purged_at FROM stores WHERE id = %s",
            (tenant.store_id,)).fetchone()
        assert (store_name, store_currency, store_status) == ("[purged]", None, "purged")
        assert store_purged is not None
        assert owner.execute("SELECT count(*) FROM organization_identity_keys").fetchone()[0] == 1
        actions = [r[0] for r in owner.execute(
            "SELECT action FROM audit_events WHERE action LIKE 'organization.purge%' "
            "OR action = 'organization.purged' ORDER BY created_at").fetchall()]
    assert actions == ["organization.purge_started", "organization.purged"]


def test_the_purge_audit_carries_no_identity(victim, owner, worker_sql):
    """Q1: metadonnees limitees a `job_id` et a des compteurs. Aucun nom, aucune devise."""
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, STORE_JOB))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_store(%s)", (claimed.id,))
    with owner.transaction():
        _ctx(owner, tenant)
        action, resource_type, resource_id, metadata = owner.execute(
            "SELECT action, resource_type, resource_id, metadata FROM audit_events "
            "WHERE action = 'store.purge_started'").fetchone()
    assert (action, resource_type, resource_id) == ("store.purge_started", "store", tenant.store_id)
    assert set(metadata) == {"job_id", "jobs_cancelled", "raw_objects_marked"}
    serialized = str(metadata)
    for forbidden in ("Boutique", "Organisation", "EUR", ALICE):
        assert forbidden not in serialized


# -- 9. pierre tombale des boutiques lors d'une purge d'ORGANISATION -------------------------------

def _ready_to_tombstone(worker_sql, tenant, owner, claimed):
    """Amene l'organisation au point ou les boutiques peuvent etre tombstonees.

    Cloture, destruction des octets, retrait des lignes, puis purge des donnees: la barriere de
    `app_tombstone_organization_stores` exige exactement cet etat.
    """
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    _finalize_all(worker_sql, tenant, claimed, owner)
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_purge_tenant_data(%s)", (claimed.id,))


def test_the_organization_tombstone_refuses_while_a_store_is_not_purged(victim, owner, worker_sql):
    """A: la TROISIEME garde, qu'aucun test ne couvrait -- et que le SQL brut contournait."""
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _ready_to_tombstone(worker_sql, tenant, owner, claimed)
    with pytest.raises(psycopg.errors.RestrictViolation, match="every store must be tombstoned"):
        _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization(%s)", (claimed.id,))


def test_the_organization_store_tombstone_refuses_a_store_purge_job(victim, owner, worker_sql):
    """B: la separation des voies est portee par la GARDE, pas par une convention."""
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, STORE_JOB))
    with pytest.raises(psycopg.errors.RestrictViolation, match="does not run for this job type"):
        _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization_stores(%s)",
              (claimed.id,))


def test_the_store_tombstone_refuses_an_organization_purge_job(victim, owner, worker_sql):
    """C: reciproque de B. `app_tombstone_store` n'a PAS ete elargie (D-065 Q2)."""
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    with pytest.raises(psycopg.errors.RestrictViolation, match="does not run for this job type"):
        _call(worker_sql, tenant, claimed, "SELECT app_tombstone_store(%s)", (claimed.id,))


def test_an_active_store_is_tombstoned_directly_by_an_organization_purge(victim, owner, worker_sql):
    """D: `active -> purged` en une transition.

    La cloture de l'ORGANISATION est deja la barriere d'ecriture qui protege l'ensemble:
    exiger un passage prealable par `purging` n'ajouterait aucune garantie, seulement une
    transition artificielle a la charge de `app_close_organization`.
    """
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    assert _status(owner, tenant, "stores")[0] == "active"
    _ready_to_tombstone(worker_sql, tenant, owner, claimed)
    assert _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization_stores(%s)",
                 (claimed.id,))[0] == 1
    with owner.transaction():
        _ctx(owner, tenant)
        name, currency, status, purged_at = owner.execute(
            "SELECT name, currency, status, purged_at FROM stores WHERE id = %s",
            (tenant.store_id,)).fetchone()
    assert (name, currency, status) == ("[purged]", None, "purged") and purged_at is not None


def test_a_purging_store_is_also_tombstoned(victim, owner, worker_sql):
    """E: `purging -> purged` fonctionne aussi -- une boutique deja close n'est pas laissee."""
    tenant, _ = victim
    store_job = _claim(owner, tenant, _enqueue(owner, tenant, STORE_JOB))
    _call(worker_sql, tenant, store_job, "SELECT * FROM app_close_store(%s)", (store_job.id,))
    assert _status(owner, tenant, "stores")[0] == "purging"
    # La purge de boutique se termine: un travail `running` survivrait a la purge des donnees
    # (I1 le protege) et bloquerait la barriere. Ici la boutique reste `purging`, ce qui est
    # exactement l'etat que ce test veut exercer.
    with owner.transaction():
        _ctx(owner, tenant)
        owner.execute("SELECT set_config('app.job_lease_token', %s, true)",
                      (f"{store_job.attempts}:{store_job.locked_by}",))
        # `jobs_lock_consistent`: seul un travail `running` porte un bail. Le relacher fait
        # partie de la terminaison, ce n'est pas un contournement du test.
        owner.execute("UPDATE jobs SET status = 'succeeded', finished_at = now(), "
                      "updated_at = now(), locked_at = NULL, locked_by = NULL, "
                      "lease_expires_at = NULL WHERE id = %s", (store_job.id,))
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _ready_to_tombstone(worker_sql, tenant, owner, claimed)
    assert _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization_stores(%s)",
                 (claimed.id,))[0] == 1
    assert _status(owner, tenant, "stores")[0] == "purged"


def test_tombstoning_the_stores_twice_changes_nothing(victim, owner, worker_sql):
    """F: idempotent. Le rejeu ne trouve plus rien a faire, et ne leve pas."""
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _ready_to_tombstone(worker_sql, tenant, owner, claimed)
    first = _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization_stores(%s)",
                  (claimed.id,))[0]
    with owner.transaction():
        _ctx(owner, tenant)
        stamped = owner.execute("SELECT purged_at FROM stores WHERE id = %s",
                                (tenant.store_id,)).fetchone()[0]
    second = _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization_stores(%s)",
                   (claimed.id,))[0]
    assert (first, second) == (1, 0)
    with owner.transaction():
        _ctx(owner, tenant)
        assert owner.execute("SELECT purged_at FROM stores WHERE id = %s",
                             (tenant.store_id,)).fetchone()[0] == stamped, \
            "`coalesce` doit preserver l'horodatage du premier passage"


def test_tombstoning_stores_never_reaches_another_organization(victim, bystander, owner, worker_sql):
    """G: l'isolation vient de la RLS, pas d'une condition relue."""
    tenant, _ = victim
    neighbour, _ = bystander
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _ready_to_tombstone(worker_sql, tenant, owner, claimed)
    _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization_stores(%s)", (claimed.id,))
    with owner.transaction():
        _ctx(owner, neighbour)
        name, status = owner.execute("SELECT name, status FROM stores WHERE id = %s",
                                     (neighbour.store_id,)).fetchone()
    assert status == "active" and name != "[purged]"


def test_the_application_roles_cannot_write_a_store_status(victim, pg):
    """H et I: aucun `GRANT UPDATE` sur `stores.status`, pour ni l'un ni l'autre des roles."""
    tenant, _ = victim
    for kind in ("app", "worker"):
        with psycopg.connect(pg.url(kind), autocommit=True) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    _ctx(conn, tenant)
                    conn.execute("UPDATE stores SET status = 'purged' WHERE id = %s",
                                 (tenant.store_id,))


def test_a_rolled_back_store_tombstone_leaves_every_store_untouched(victim, owner, pg, worker_sql):
    """J: tout ou rien. Un `UPDATE` unique, jamais une boucle: pas d'etat partiel."""
    tenant, _ = victim
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _ready_to_tombstone(worker_sql, tenant, owner, claimed)
    with psycopg.connect(pg.url("worker")) as conn:          # PAS autocommit
        conn.execute("SELECT set_config('app.organization_id', %s, false), "
                     "set_config('app.job_lease_token', %s, false)",
                     (str(tenant.organization_id), f"{claimed.attempts}:{claimed.locked_by}"))
        assert conn.execute("SELECT app_tombstone_organization_stores(%s)",
                            (claimed.id,)).fetchone()[0] == 1
        conn.rollback()
    with owner.transaction():
        _ctx(owner, tenant)
        name, status, purged_at = owner.execute(
            "SELECT name, status, purged_at FROM stores WHERE id = %s",
            (tenant.store_id,)).fetchone()
    assert status == "active" and name != "[purged]" and purged_at is None


# -- 10. colonnes de cycle de vie: ni UPDATE, ni INSERT par le role applicatif --------------------

def test_the_application_role_cannot_insert_a_lifecycle_status(victim, pg):
    """A et B: `mervio_app` ne peut pas NAITRE dans un etat de cycle de vie qu'il choisit.

    Un `GRANT INSERT` de TABLE (`0001`) couvre automatiquement les colonnes ajoutees ensuite:
    sans la restriction de `0016`, `mervio_app` aurait pu creer une organisation deja
    `purging`. Et un `REVOKE INSERT (status)` ne suffit PAS contre un grant de table -- mesure
    a l'appui, il est sans effet. Seul le retrait du privilege de table, suivi d'un
    re-octroi colonne par colonne, ferme la porte.
    """
    tenant, _ = victim
    with psycopg.connect(pg.url("app"), autocommit=True) as conn:
        conn.execute("SELECT set_config('app.organization_id', %s, false), "
                     "set_config('app.user_id', %s, false)",
                     (str(tenant.organization_id), str(tenant.owner_id)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO organizations (id, name, status) VALUES (%s, 'x', 'purging')",
                         (uuid.uuid4(),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO stores (id, organization_id, name, status, purged_at) "
                         "VALUES (%s, %s, 'x', 'purged', now())",
                         (uuid.uuid4(), tenant.organization_id))


def test_the_lifecycle_columns_are_writable_by_no_application_privilege(pg, owner):
    """Ni `INSERT` ni `UPDATE`, pour aucun des deux roles, sur les quatre colonnes."""
    for table in ("organizations", "stores"):
        for column in ("status", "purged_at"):
            for kind in ("app", "worker"):
                for privilege in ("INSERT", "UPDATE"):
                    granted = owner.execute(
                        "SELECT has_column_privilege(%s, %s, %s, %s)",
                        (pg.role(kind), f"public.{table}", column, privilege)).fetchone()[0]
                    assert granted is False, (table, column, kind, privilege)


def test_creating_an_organization_and_a_store_still_works(db, owner):
    """C: le chemin NORMAL de creation reste intact, et prend les valeurs par defaut.

    Le re-octroi colonne par colonne doit couvrir exactement ce dont le produit a besoin: ce
    test echouerait si une colonne inserable avait ete oubliee.
    """
    tenant = make_tenant(db, "lifecycle")
    with owner.transaction():
        _ctx(owner, tenant)
        assert owner.execute("SELECT status, purged_at FROM organizations WHERE id = %s",
                             (tenant.organization_id,)).fetchone() == ("active", None)
        assert owner.execute("SELECT status, purged_at FROM stores WHERE id = %s",
                             (tenant.store_id,)).fetchone() == ("active", None)


def test_no_other_grant_was_withdrawn(pg, owner):
    """D: seules `status` et `purged_at` perdent l'`INSERT`; tout le reste est inchange."""
    expected = {
        "organizations": ("id", "name", "created_at"),
        "stores": ("id", "organization_id", "name", "currency", "created_at",
                   "timezone", "timezone_source"),
    }
    for table, columns in expected.items():
        for column in columns:
            assert owner.execute("SELECT has_column_privilege(%s, %s, %s, 'INSERT')",
                                 (pg.role("app"), f"public.{table}", column)).fetchone()[0] is True, \
                (table, column)
        assert owner.execute("SELECT has_table_privilege(%s, %s, 'SELECT')",
                             (pg.role("app"), f"public.{table}")).fetchone()[0] is True, table
    # les `UPDATE` de colonnes metier de `0001` ne sont pas touches
    for table, column in (("organizations", "name"), ("stores", "name"), ("stores", "currency")):
        assert owner.execute("SELECT has_column_privilege(%s, %s, %s, 'UPDATE')",
                             (pg.role("app"), f"public.{table}", column)).fetchone()[0] is True, \
            (table, column)


# -- 9. D-067: au plus UNE purge tenant active par organisation -----------------------------------

def test_two_organization_purges_cannot_coexist(victim, owner):
    """D-067. Sans elle, les deux detruisaient, puis echouaient toutes deux a la pierre tombale."""
    _enqueue(owner, tenant := victim[0], ORG_JOB, store_id=None)
    with pytest.raises(psycopg.errors.UniqueViolation, match="jobs_tenant_purge_active_uniq"):
        _enqueue(owner, tenant, ORG_JOB, store_id=None)


def test_a_store_purge_and_an_organization_purge_exclude_each_other(victim, owner):
    """C2, dans LES DEUX ORDRES: les deux types partagent la meme cle de reservation."""
    tenant, _ = victim
    _enqueue(owner, tenant, STORE_JOB)
    with pytest.raises(psycopg.errors.UniqueViolation, match="jobs_tenant_purge_active_uniq"):
        _enqueue(owner, tenant, ORG_JOB, store_id=None)


def test_an_organization_purge_excludes_a_later_store_purge(victim, owner):
    tenant, _ = victim
    _enqueue(owner, tenant, ORG_JOB, store_id=None)
    with pytest.raises(psycopg.errors.UniqueViolation, match="jobs_tenant_purge_active_uniq"):
        _enqueue(owner, tenant, STORE_JOB)


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_a_terminal_tenant_purge_frees_the_reservation(victim, owner, terminal):
    """Le predicat porte sur le STATUT: un travail termine ne bloque plus rien."""
    tenant, _ = victim
    job_id = _enqueue(owner, tenant, ORG_JOB, store_id=None)
    if terminal == "cancelled":
        with owner.transaction():
            _ctx(owner, tenant)
            owner.execute("UPDATE jobs SET status = 'cancelled', finished_at = now() WHERE id = %s",
                          (job_id,))
    else:
        claimed = _claim(owner, tenant, job_id)
        with owner.transaction():
            _ctx(owner, tenant)
            owner.execute("SELECT set_config('app.job_lease_token', %s, true)",
                          (f"{claimed.attempts}:{claimed.locked_by}",))
            owner.execute("UPDATE jobs SET status = %s, finished_at = now(), locked_at = NULL, "
                          "locked_by = NULL, lease_expires_at = NULL WHERE id = %s", (terminal, job_id))
    assert _enqueue(owner, tenant, ORG_JOB, store_id=None) is not None


def test_the_reservation_is_not_global(victim, bystander, owner):
    """La cle est le SEUL `organization_id`: une organisation n'en contraint jamais une autre."""
    _enqueue(owner, victim[0], ORG_JOB, store_id=None)
    assert _enqueue(owner, bystander[0], ORG_JOB, store_id=None) is not None


def test_non_tenant_purge_jobs_are_never_constrained(victim, owner):
    """`redact_customer`, `import`, `analysis` et la purge de RETENTION restent libres."""
    tenant, _ = victim
    for kind in (OTHER_JOB, OTHER_JOB, "import", "import", "analysis", "purge"):
        store = tenant.store_id if kind in ("import", "analysis") else None
        assert _enqueue(owner, tenant, kind, store_id=store) is not None
    assert _enqueue(owner, tenant, ORG_JOB, store_id=None) is not None


def test_even_a_superuser_cannot_bypass_the_reservation(victim, owner, pg):
    """L'unicite est imposee par le MOTEUR DE STOCKAGE: aucune politique, aucun role ne la leve."""
    tenant, _ = victim
    _enqueue(owner, tenant, ORG_JOB, store_id=None)
    with psycopg.connect(pg.admin_conninfo, dbname=pg.database, autocommit=True) as god:
        assert god.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles "
                           "WHERE rolname = current_user").fetchone()[0] is True
        with pytest.raises(psycopg.errors.UniqueViolation, match="jobs_tenant_purge_active_uniq"):
            god.execute(
                "INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, "
                "available_at, payload) VALUES (%s, %s, %s, 'queued', %s, now(), '{}')",
                (uuid.uuid4(), tenant.organization_id, ORG_JOB, uuid.uuid4()))


# -- 10. D-068: rien n'est detruit tant qu'un travail est en vol ----------------------------------

#: `purge_store` est volontairement ABSENT: D-067 l'empeche desormais de coexister avec une
#: purge d'organisation, et c'est teste plus haut. Les quatre autres types, eux, coexistent
#: legitimement -- D-065 Q7 l'exige pour `redact_customer`, D-063 pour l'import -- et ce sont
#: donc eux qui exercent la barriere de drain.
@pytest.mark.parametrize("intruder", ["redact_customer", "import", "analysis", "purge"])
def test_nothing_is_destroyed_while_another_job_is_in_flight(victim, owner, worker_sql, intruder):
    """D-068. Le blocage ne venait PAS de `purge_store`: les cinq types le produisaient.

    D-066 I1 GARANTIT qu'un travail `running` survit a la purge; la barriere des pierres
    tombales INTERDISAIT qu'il survive. La purge detruisait donc tout, puis echouait
    definitivement. Le refus est desormais transitoire, et il arrive AVANT la destruction.
    """
    tenant, _ = victim
    _seed_identity_key(owner, tenant)
    store = tenant.store_id if intruder in ("import", "analysis") else None
    _claim(owner, tenant, _enqueue(owner, tenant, intruder, store_id=store), worker_id="intrus")
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))

    for operation in ("SELECT app_destroy_identity_key(%s)", "SELECT * FROM app_purge_tenant_data(%s)"):
        with pytest.raises(psycopg.errors.ObjectInUse, match="still in flight"):
            _call(worker_sql, tenant, claimed, operation, (claimed.id,))
    with owner.transaction():
        _ctx(owner, tenant)
        assert owner.execute("SELECT salt IS NOT NULL FROM organization_identity_keys "
                             "WHERE organization_id = %s", (tenant.organization_id,)).fetchone()[0], \
            "le sel doit etre INTACT"
        assert owner.execute("SELECT count(*) FROM data_snapshots").fetchone()[0] == 1, \
            "les donnees doivent etre INTACTES"


def test_the_in_flight_refusal_is_classified_retryable():
    """`55006` est DEJA transitoire dans `retry.py`: aucune ligne de Python ne change."""
    from mervio.persistence.retry import RETRYABLE_SQLSTATES

    assert "55006" in RETRYABLE_SQLSTATES


def test_a_forgotten_terminal_job_stays_a_permanent_refusal(victim, owner, worker_sql):
    """La scission ne relache rien: un travail TERMINAL oublie reste une faute d'ORDRE."""
    tenant, _ = victim
    _terminal_job(owner, tenant, age="1 minute")
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    _finalize_all(worker_sql, tenant, claimed, owner)
    with pytest.raises(psycopg.errors.RestrictViolation, match="jobs remain"):
        _call(worker_sql, tenant, claimed, "SELECT app_tombstone_organization(%s)", (claimed.id,))


# -- 11. D-069: le sel ne se detruit que dans une organisation close ------------------------------

def test_the_salt_is_not_destroyed_before_the_closure(victim, owner, worker_sql):
    """D-069. Cette fonction detruisait le sel d'une organisation restee `active`."""
    tenant, _ = victim
    _seed_identity_key(owner, tenant)
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    assert _status(owner, tenant, "organizations")[0] == "active"
    with pytest.raises(psycopg.errors.RestrictViolation, match="must be closed"):
        _call(worker_sql, tenant, claimed, "SELECT app_destroy_identity_key(%s)", (claimed.id,))
    with owner.transaction():
        _ctx(owner, tenant)
        assert owner.execute("SELECT salt IS NOT NULL FROM organization_identity_keys "
                             "WHERE organization_id = %s", (tenant.organization_id,)).fetchone()[0]


def test_the_salt_is_not_destroyed_after_the_tombstone(victim, owner, worker_sql):
    """`purged` referme la fenetre: la meme precondition refuse dans l'autre sens."""
    tenant, _ = victim
    _seed_identity_key(owner, tenant)
    claimed = _claim(owner, tenant, _enqueue(owner, tenant, ORG_JOB, store_id=None))
    _call(worker_sql, tenant, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    _set_status(owner, tenant, "purged")
    with pytest.raises(psycopg.errors.RestrictViolation, match="must be closed"):
        _call(worker_sql, tenant, claimed, "SELECT app_destroy_identity_key(%s)", (claimed.id,))
