"""Sonde du dispatcher et prise: contrat semantique et plans robustes sous RLS (revision 0012, D-060).

Constat corrige (preexistant depuis 0007): sous le role worker, la politique permissive
`jobs_service_dispatch` faisait estimer ~3 travaux prets par organisation pour ~3 000 reels. Une fois
la table analysee, la sonde READY basculait vers un `Bitmap Heap Scan` (sur `jobs_tenant_key` ou
`jobs_ready_idx`) suivi d'un tri de toute la file de l'organisation (11 x 1 000: 363 blocs au lieu
de ~66; 11 x 3 000 dont moitie differes: 945 au lieu de ~290), et la sonde des baux expires lisait
2 596 blocs au lieu de 63. `now()` n'etant pas leakproof, l'echeance du bail ne devenait jamais une
condition d'index sous RLS.

Ce module prouve, sans dependre du NOM d'un index pour le contrat metier:
- semantique: la sonde est un test d'existence par organisation; tour de role par identifiant;
  priorite appliquee par la prise seulement; disponibilite (`available_at`), travaux epuises;
- securite: l'expression 0012 de la politique rend visibles exactement les memes lignes que celle
  de 0007, dans tous les contextes; aucune organisation non autorisee ou revoquee n'est servie;
- performance des sondes: parcours d'index sans bitmap ni tri de `jobs`; blocs bornes (<= 83 pour
  11 organisations, maximum mesure du plan nominal) quelle que soit la profondeur; aucun plan
  degrade sur des ordres physiques aleatoires (MERVIO_DISPATCH_PLAN_DRAWS tirages, 20 par defaut);
- PRISE (`_CLAIM_SQL`, ordre et verrous inchanges): jamais de tri, sur une base FRAICHE jamais analysee (etat d'une
  table juste chargee, avant l'autovacuum), puis analysee, puis apres 400 prises. C'est la condition
  exacte qui a fait ecarter l'index `(organization_id, available_at) WHERE status = 'queued'`: la
  prise le choisissait avec un tri de toute la file (~50 ms par prise a 100 000 travaux).

D1 (revision 0012): `jobs_ready_idx` porte `available_at` en DERNIERE cle (meme prefixe, donc meme
ordre de prise) et l'horloge est evaluee une fois: les travaux DIFFERES (reprises avec delai, qui
gardent leur `created_at`, donc restent en tete de l'ordre) sont sautes DANS l'index, sans lecture
de table (90 000 differes en tete: ~28 ms -> ~1,8 ms par prise), et la prise ne trie plus la file
sans statistiques (un million de travaux jamais analyses: ~0,6 s -> <1 ms). Les tests de la fin du
module prouvent que le travail CHOISI ne change pas (ordre metier, borne d'horloge, verrous).
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from mervio.persistence import jobs
from mervio.persistence.dispatch import (
    _EXPIRED_SQL, _READY_SQL, Dispatcher, expired_lease_organizations, ready_organizations,
)
from mervio.persistence.jobs import JobType
from mervio.persistence.service import authorize_service, revoke_service

from .persistence_support import make_tenant
from .service_support import raw

#: blocs lus SUR `jobs` par une sonde, 11 organisations: reference 83 (maximum du plan nominal complet,
#: 74 a 83). Mesure sur `jobs` seul: 60 (199 tirages sur 200) ou 69; plan degrade: 354 a 2 596. Le
#: plan complet ajoute la lecture des 11 autorisations, par index (74) ou par parcours et tri (85):
#: variation legitime, hors du contrat, donc exclue de la mesure.
MAX_PROBE_BLOCKS = 83
DRAWS = int(os.environ.get("MERVIO_DISPATCH_PLAN_DRAWS", "20"))
#: expressions de la politique `jobs_service_dispatch`: 0007 (origine) et 0012 (actuelle)
ORIGINAL_USING = "app_current_organization_id() IS NULL AND status IN ('queued', 'running')"
PLANNABLE_USING = "(SELECT public.app_current_organization_id() IS NULL) AND status IN ('queued', 'running')"


def _authorize(principal, *tenants):
    for tenant in tenants:
        authorize_service(tenant.session, principal.id)


def _enqueue(tenant, *, priority=0, available_at=None, max_attempts=3):
    return jobs.enqueue_job(tenant.session, job_type=JobType.ANALYSIS, payload={"k": "v"}, priority=priority,
                            available_at=available_at, max_attempts=max_attempts)


# =============================================================================================
# Plans
# =============================================================================================

def _walk(node):
    yield node
    for child in node.get("Plans", []):
        yield from _walk(child)


def _plan(worker_db, statement, params):
    with worker_db.transaction() as conn:
        return conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, params).fetchone()[0][0]["Plan"]


def _blocks(plan):
    return plan["Shared Hit Blocks"] + plan["Shared Read Blocks"]


def _jobs_blocks(plan):
    """Blocs lus par les noeuds qui lisent `jobs` (la relation dont le contrat de la sonde parle)."""
    return sum(n.get("Shared Hit Blocks", 0) + n.get("Shared Read Blocks", 0)
               for n in _walk(plan) if n.get("Relation Name") == "jobs")


def _index_driven_on(plan, time_column=None):
    """La lecture de `jobs` est un parcours d'index ordonne, sans bitmap, sans `Seq Scan`, sans tri.

    Propriete de comportement (pas de nom d'index): la sous-requete s'arrete au premier travail
    trouve au lieu de lire puis trier toute la file de l'organisation. Avec `time_column`, la
    CONDITION d'index porte aussi sur l'horloge (sonde des baux expires).
    """
    shape = _shape(plan)
    scans = [n for n in _walk(plan) if n.get("Relation Name") == "jobs"]
    assert scans, shape
    for node in scans:
        assert node["Node Type"] in ("Index Scan", "Index Only Scan"), shape
        assert time_column is None or time_column in (node.get("Index Cond") or ""), shape
    # tri qui CONSOMME une lecture de `jobs` (signature du plan degrade: bitmap puis tri). Le tri
    # eventuel des autorisations (`sa.organization_id`, quelques lignes) n'est pas concerne.
    assert not [n for n in _walk(plan) if n["Node Type"] == "Sort" and any(
        c.get("Relation Name") == "jobs" for c in n.get("Plans", []))], shape


def _shape(plan):
    """Plan compact pour les messages d'echec: type, relation, index, condition, lignes, blocs."""
    return "\n".join(f"{n['Node Type']} rel={n.get('Relation Name')} idx={n.get('Index Name')} "
                     f"cond={n.get('Index Cond')} sort={n.get('Sort Key')} est={n.get('Plan Rows')} "
                     f"rows={n.get('Actual Rows')}x{n.get('Actual Loops')} hit={n.get('Shared Hit Blocks')}"
                     for n in _walk(plan))


def _ready_probe(worker_db, principal):
    return _plan(worker_db, _READY_SQL, (principal.id, None, None, None, 50))


def _insert(owner, tenant, count, *, delay="0", status="queued", lease="1 hour"):
    """Travaux en masse (proprietaire, sous RLS forcee avec contexte), en file ou en cours
    (bail a `now() + lease`: negatif pour un bail deja expire)."""
    with raw(owner, tenant.organization_id) as conn:
        conn.execute("INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, payload, available_at) "
                     "SELECT gen_random_uuid(), %s, 'analysis', 'queued', gen_random_uuid(), '{}'::jsonb, "
                     "now() + %s::interval FROM generate_series(1, %s)", (tenant.organization_id, delay, count))
        if status == "running":  # meme transition que la prise
            conn.execute("UPDATE jobs SET status = 'running', attempts = attempts + 1, "
                         "locked_at = LEAST(now(), now() + %s::interval) - interval '1 minute', locked_by = 'bulk', "
                         "lease_expires_at = now() + %s::interval, "
                         "started_at = LEAST(now(), now() + %s::interval) - interval '1 minute' "
                         "WHERE organization_id = %s AND status = 'queued'", (lease, lease, lease, tenant.organization_id))


@pytest.fixture
def eleven(db, principal, tenant_a):
    """11 organisations autorisees (celle du tenant + 10), comme le constat d'origine."""
    tenants = [tenant_a] + [make_tenant(db, f"D{i}") for i in range(10)]
    _authorize(principal, *tenants)
    return tenants


@pytest.mark.parametrize("per_organization", [100, 1000, 3000, 6000])
def test_the_ready_probe_is_index_driven_and_bounded(db, worker_db, principal, eleven, owner, per_organization):
    """Avant 0012, 100 et 1 000 travaux par organisation (table analysee) donnaient un bitmap puis un
    tri de la file (1 000: 363 blocs, ~3 ms); 3 000 et 6 000 basculaient aleatoirement."""
    for tenant in eleven:
        _insert(owner, tenant, per_organization)
    owner.execute("ANALYZE jobs")
    plan = _ready_probe(worker_db, principal)
    _index_driven_on(plan)
    assert _jobs_blocks(plan) <= MAX_PROBE_BLOCKS, _blocks(plan)
    assert sorted(ready_organizations(worker_db, principal)) == sorted(t.organization_id for t in eleven)


def test_delayed_jobs_keep_the_ready_probe_index_driven(db, worker_db, principal, eleven, owner):
    """11 organisations, 1 500 travaux differes en TETE de l'ordre (les reprises gardent leur
    `created_at`) puis 1 500 prets: la sonde saute les differes DANS l'index (D1, revision 0012), sans
    lecture de table chacun, et ne retombe jamais sur le plan degrade (bitmap puis tri)."""
    for tenant in eleven:
        _insert(owner, tenant, 1500, delay="1 hour")
        _insert(owner, tenant, 1500)
    owner.execute("ANALYZE jobs")
    plan = _ready_probe(worker_db, principal)
    _index_driven_on(plan, "available_at")
    assert _jobs_blocks(plan) <= _delayed_budget(len(eleven) * 1500), _shape(plan)
    assert sorted(ready_organizations(worker_db, principal)) == sorted(t.organization_id for t in eleven)


def test_an_organization_with_only_delayed_work_is_not_ready(db, worker_db, principal, eleven, owner):
    for tenant in eleven:
        _insert(owner, tenant, 200, delay="1 hour")
    _insert(owner, eleven[3], 1)
    owner.execute("ANALYZE jobs")
    assert ready_organizations(worker_db, principal) == [eleven[3].organization_id]


@pytest.mark.parametrize("expired", [0, 1])
def test_the_expired_lease_probe_is_index_driven_and_bounded(db, worker_db, principal, eleven, owner, expired):
    """10 000 baux non expires par organisation (+ `expired` bail expire): la sonde ne les parcourt pas.

    Horloge de la base, comme en production (`Worker._now()` vaut None): `now=None`. Une date
    explicite serait simplifiee en constante par le planificateur et masquerait le defaut corrige.
    """
    for tenant in eleven:
        _insert(owner, tenant, 10000, status="running")
        _insert(owner, tenant, expired, status="running", lease="-1 minute")
    owner.execute("ANALYZE jobs")
    plan = _plan(worker_db, _EXPIRED_SQL, (principal.id, None, None, None, 50))
    _index_driven_on(plan, "lease_expires_at")
    assert _jobs_blocks(plan) <= MAX_PROBE_BLOCKS, _shape(plan)
    found = expired_lease_organizations(worker_db, principal)
    assert sorted(found) == (sorted(t.organization_id for t in eleven) if expired else [])


def test_the_ready_probe_plan_is_stable_whatever_the_physical_order(db, worker_db, principal, eleven, owner):
    """La cause d'origine: l'ordre physique d'insertion (correlation) faisait basculer le plan.

    Chaque tirage reinsere 3 000 travaux par organisation dans un ordre d'organisations aleatoire,
    puis ANALYZE. Aucun tirage ne doit produire un plan degrade ni depasser la borne de blocs.
    """
    rng = random.Random(20260919)
    degraded = []
    for draw in range(DRAWS):
        with raw(owner) as conn:
            # 004.4.5: `customer_redactions` reference `jobs`; les deux se vident ensemble
            conn.execute("TRUNCATE customer_redactions, jobs")
        for tenant in rng.sample(eleven, len(eleven)):
            _insert(owner, tenant, 3000)
        owner.execute("ANALYZE jobs")
        plan = _ready_probe(worker_db, principal)
        try:
            _index_driven_on(plan)
            assert _jobs_blocks(plan) <= MAX_PROBE_BLOCKS, _shape(plan)
        except AssertionError as exc:
            degraded.append((draw, str(exc)))
    assert degraded == [], degraded


# =============================================================================================
# Semantique: test d'existence, tour de role, priorite dans la prise
# =============================================================================================

def test_availability_delay_and_exhaustion(db, worker_db, principal, owner):
    """Pret <=> au moins un travail en file, disponible, a qui il reste une tentative."""
    ready, delayed, exhausted, done = (make_tenant(db, name) for name in ("RDY", "DLY", "EXH", "DON"))
    _authorize(principal, ready, delayed, exhausted, done)
    _enqueue(ready)
    _enqueue(delayed, available_at=datetime.now(timezone.utc) + timedelta(hours=1))
    job = _enqueue(exhausted, max_attempts=1)
    claimed = jobs.claim_next_job(exhausted.session, worker_id="w")
    assert claimed.id == job.id
    assert jobs.mark_failed(exhausted.session, claimed, error_code="boom", retryable=True).status == "failed"
    job = _enqueue(done)
    jobs.mark_succeeded(done.session, jobs.claim_next_job(done.session, worker_id="w"))
    assert ready_organizations(worker_db, principal) == [ready.organization_id]
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert sorted(ready_organizations(worker_db, principal, now=later)) == sorted(
        [ready.organization_id, delayed.organization_id])


def test_organizations_come_in_identifier_order_whatever_the_priorities(db, worker_db, principal):
    """La sonde ne classe pas les organisations par priorite de travail (tour de role, D-050)."""
    tenants = [make_tenant(db, f"PR{i}") for i in range(4)]
    _authorize(principal, *tenants)
    for index, tenant in enumerate(tenants):
        _enqueue(tenant, priority=[-100, 100, 0, 50][index])
    assert ready_organizations(worker_db, principal) == sorted(t.organization_id for t in tenants)


def test_a_full_drain_follows_the_contract(db, worker_db, principal):
    """Sequence de vidage complete == simulation independante du contrat:
    tour de role par identifiant d'organisation (lots, curseur), puis dans l'organisation
    `priority DESC, created_at ASC, id ASC` parmi les travaux disponibles. Les travaux differes et
    les organisations non autorisees ou revoquees ne sont jamais servis."""
    rng = random.Random(7)
    served = [make_tenant(db, f"DR{i}") for i in range(6)]
    unauthorized, revoked = make_tenant(db, "DRU"), make_tenant(db, "DRR")
    _authorize(principal, *served, revoked)
    expected_jobs = {}
    for tenant in served + [unauthorized, revoked]:
        for _ in range(rng.randint(0, 6)):
            delay = rng.choice([None, None, None, timedelta(hours=1)])
            job = _enqueue(tenant, priority=rng.choice([-100, 0, 0, 5, 100]),
                           available_at=datetime.now(timezone.utc) + delay if delay else None)
            if delay is None:
                expected_jobs.setdefault(tenant.organization_id, []).append(job)
        if tenant in (unauthorized, revoked):  # toujours du travail pret, jamais servi
            _enqueue(tenant, priority=100)
    revoke_service(revoked.session, principal.id)

    queues = {t.organization_id: sorted(expected_jobs.get(t.organization_id, []),
                                        key=lambda j: (-j.priority, j.created_at, j.id)) for t in served}
    expected, cursor, batch = [], None, 3
    while True:
        eligible = sorted(o for o, q in queues.items() if q)
        found = [o for o in eligible if cursor is None or o > cursor][:batch]
        if len(found) < batch and cursor is not None:
            found += [o for o in eligible if o not in found][: batch - len(found)]
        cursor = found[-1] if found else None
        if not found:
            break
        expected += [(o, queues[o].pop(0).id) for o in found]

    dispatcher, actual = Dispatcher(worker_db, principal, batch=batch), []
    while True:
        organizations = dispatcher.next_organizations()
        if not organizations:
            break
        for organization_id in organizations:
            session = dispatcher.session(organization_id)
            job = jobs.claim_next_job(session, worker_id="drain", lease_seconds=60)
            if job is not None:
                actual.append((organization_id, job.id))
                jobs.mark_succeeded(session, job)
    assert actual == expected
    assert not {o for o, _ in actual} & {unauthorized.organization_id, revoked.organization_id}


# =============================================================================================
# Securite: l'expression 0012 == l'expression 0007, dans tous les contextes
# =============================================================================================

def _set_policy(owner, using):
    owner.execute(f"ALTER POLICY jobs_service_dispatch ON public.jobs USING ({using})")


def _visible(conn, organization_id=None, user_id=None):
    with raw(conn, organization_id, user_id) as c:
        return sorted(r[0] for r in c.execute("SELECT id FROM jobs").fetchall())


def test_the_policy_rewrite_is_equivalent_in_every_context(db, pg, owner, principal, tenant_a, tenant_b):
    """Memes lignes visibles avec l'expression de 0007 et celle de 0012: role worker sans contexte
    (vue du dispatcher), avec le contexte d'une organisation autorisee (delegue proprietaire), d'une
    organisation jamais autorisee, d'une autorisation revoquee; role applicatif."""
    revoked = make_tenant(db, "REV")
    _authorize(principal, tenant_a, revoked)
    for tenant in (tenant_a, tenant_b, revoked):
        _enqueue(tenant)
        _enqueue(tenant, available_at=datetime.now(timezone.utc) + timedelta(hours=1))
        done = _enqueue(tenant, priority=100)
        claimed = jobs.claim_next_job(tenant.session, worker_id="eq")
        assert claimed.id == done.id
        jobs.mark_succeeded(tenant.session, claimed)
        jobs.claim_next_job(tenant.session, worker_id="eq")  # un travail en cours; le differe reste en file
    revoke_service(revoked.session, principal.id)

    contexts = [("worker", None, None), ("worker", tenant_a.organization_id, tenant_a.owner_id),
                ("worker", tenant_b.organization_id, tenant_b.owner_id),
                ("worker", revoked.organization_id, revoked.owner_id),
                ("app", None, None), ("app", tenant_a.organization_id, tenant_a.owner_id)]
    with psycopg.connect(pg.url("worker"), autocommit=True) as worker, \
            psycopg.connect(pg.url("app"), autocommit=True) as app:
        connections = {"worker": worker, "app": app}
        current = {c: _visible(connections[c[0]], c[1], c[2]) for c in contexts}
        try:
            _set_policy(owner, ORIGINAL_USING)
            original = {c: _visible(connections[c[0]], c[1], c[2]) for c in contexts}
        finally:
            _set_policy(owner, PLANNABLE_USING)
    assert current == original
    # le contexte sans organisation voit les travaux en file ou en cours de l'organisation autorisee
    assert current[("worker", None, None)] and current[("worker", tenant_b.organization_id, tenant_b.owner_id)] == []
    assert current[("worker", revoked.organization_id, revoked.owner_id)] == []
    # contexte evalue une fois (sous-requete); la fonction est celle de `public` (resolue par OID)
    qual = owner.execute("SELECT qual FROM pg_policies WHERE policyname = 'jobs_service_dispatch'").fetchone()[0]
    assert qual.startswith("(( SELECT (app_current_organization_id() IS NULL))"), qual
    assert owner.execute(
        "SELECT count(*) FROM pg_depend d JOIN pg_policy p ON p.oid = d.objid JOIN pg_proc f ON f.oid = d.refobjid "
        "JOIN pg_namespace n ON n.oid = f.pronamespace WHERE p.polname = 'jobs_service_dispatch' "
        "AND f.proname = 'app_current_organization_id' AND n.nspname = 'public'").fetchone() == (1,)


def test_the_isolation_boundary_is_unchanged(owner):
    """0012 ne touche ni la politique restrictive d'autorisation, ni les roles, ni aucune fonction."""
    rows = dict(owner.execute(
        "SELECT policyname, permissive || ' ' || roles::text || ' ' || cmd || ' ' || coalesce(qual, '') "
        "FROM pg_policies WHERE tablename = 'jobs'").fetchall())
    assert rows["jobs_service_authorized"] == (
        "RESTRICTIVE {mervio_worker} ALL ((organization_id = ANY (( SELECT app_service_organizations() "
        "AS app_service_organizations)::uuid[])) IS TRUE)")
    assert rows["jobs_service_dispatch"].startswith("PERMISSIVE {mervio_worker} SELECT ")
    assert {r[0] for r in owner.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.prosecdef").fetchall()} == {
            "app_ensure_service_principal",     # 0007
            "app_redact_customer",              # 0014, effacement client (D-052)
            "app_finalize_raw_object_purge",    # 0015, destruction des octets (D-056)
        }


def test_revision_0012_round_trips(pg):
    """0012: expression de `jobs_service_dispatch` et definition de `jobs_ready_idx` (meme prefixe,
    `available_at` en derniere cle); aucun autre index, aucune table. Descente: etat 0011 exact."""
    from mervio.persistence import migrate
    name = pg.create_empty_database("r0012")
    url = pg.url("migrator", name)
    try:
        def state():
            with psycopg.connect(url) as conn:
                return (conn.execute("SELECT qual FROM pg_policies WHERE policyname = 'jobs_service_dispatch'").fetchone()[0],
                        dict(conn.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'jobs'").fetchall()),
                        conn.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                     "WHERE oid = 'public.jobs'::regclass").fetchone())
        migrate.upgrade(url, "0011_identity_pii_schema")
        before = state()
        migrate.upgrade(url, "0012_dispatch_probe_plan")
        after = state()
        assert before[1]["jobs_ready_idx"] == ("CREATE INDEX jobs_ready_idx ON public.jobs USING btree (organization_id, "
                                               "priority DESC, created_at, id) WHERE (status = 'queued'::text)")
        assert after[1]["jobs_ready_idx"] == ("CREATE INDEX jobs_ready_idx ON public.jobs USING btree (organization_id, "
                                              "priority DESC, created_at, id, available_at) WHERE (status = 'queued'::text)")
        assert {k: v for k, v in after[1].items() if k != "jobs_ready_idx"} == \
               {k: v for k, v in before[1].items() if k != "jobs_ready_idx"}  # aucun autre index
        assert after[0] != before[0] and after[2] == before[2] == (True, True)
        migrate.downgrade(url, "0011_identity_pii_schema")
        assert state() == before
        migrate.upgrade(url, "0012_dispatch_probe_plan")
        assert state() == after
    finally:
        pg.drop_database(name)


# =============================================================================================
# Prise (`_CLAIM_SQL`, inchangee): la condition qui a fait ecarter l'index de disponibilite
# =============================================================================================

#: prise sur une base jamais analysee (100 000 travaux, une organisation): 118 a 127 blocs mesures;
#: l'index ecarte `(organization_id, available_at)` faisait lire 2 028 blocs et trier la file
MAX_CLAIM_BLOCKS = 150


def _claim_plan(worker_db, principal, organization_id):
    """EXPLAIN (ANALYZE, BUFFERS) de la VRAIE instruction de prise, sous le role worker, annulee."""
    from mervio.persistence.jobs import _CLAIM_SQL
    from mervio.persistence.service import ServiceSession
    from mervio.persistence.tenancy import Permission
    session = ServiceSession(worker_db, organization_id, principal)
    with session.transaction(Permission.RUN_JOBS) as conn, conn.transaction(force_rollback=True):
        return conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _CLAIM_SQL,
                            (organization_id, None, ["import", "analysis", "purge"], None, None, None, "plan",
                             None, 300, None, organization_id)).fetchone()[0][0]["Plan"]


def _claim_is_an_ordered_walk(plan):
    """La prise suit un index qui porte son ordre et s'arrete au premier travail: aucun tri, aucun
    bitmap, aucun `Seq Scan` dans tout le plan."""
    shape = _shape(plan)
    kinds = {n["Node Type"] for n in _walk(plan)}
    assert not kinds & {"Sort", "Incremental Sort", "Bitmap Heap Scan", "Seq Scan"}, shape


@pytest.fixture
def fresh_queue(pg):
    """Base NEUVE, file jamais analysee (autovacuum coupe sur `jobs` dans cette base jetable, pour que
    l'etat 'avant analyse' soit deterministe): c'est l'etat d'une table juste chargee."""
    from mervio.persistence import migrate
    from mervio.persistence.database import Database
    from mervio.persistence.service import service_principal
    name = pg.create_empty_database("fq")
    owner_url = pg.url("migrator", name)
    migrate.upgrade(owner_url)
    with psycopg.connect(owner_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE public.jobs SET (autovacuum_enabled = false)")
    app, worker = Database(pg.url("app", name)), Database(pg.url("worker", name))
    try:
        principal = service_principal(worker)
        yield owner_url, app, worker, principal
    finally:
        app.close()
        worker.close()
        pg.drop_database(name)


@pytest.mark.parametrize("delayed_fraction", [0.0, 0.9])
def test_the_claim_never_sorts_the_queue_even_before_the_first_analyze(fresh_queue, delayed_fraction):
    """100 000 travaux dans une organisation (priorite i % 5, comme le banc des travaux), puis la prise
    sous le role worker: jamais analysee, puis analysee, puis apres 400 prises et fins.

    Echoue avec l'index ecarte `(organization_id, available_at) WHERE status = 'queued'` (tri de la
    file entiere a chaque prise, ~50 ms); passe avec la revision 0012. Avec 90 % de travaux differes,
    ils sont sautes dans l'index (D1)."""
    owner_url, app, worker, principal = fresh_queue
    tenant = make_tenant(app, "FQ")
    authorize_service(tenant.session, principal.id)
    with psycopg.connect(owner_url, autocommit=True) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        conn.execute("INSERT INTO jobs (id, organization_id, job_type, status, priority, correlation_id, payload, "
                     "available_at) SELECT gen_random_uuid(), %s, 'analysis', 'queued', (i %% 5)::smallint, "
                     "gen_random_uuid(), '{}'::jsonb, CASE WHEN (hashint4(i) & 1023) < %s "
                     "THEN now() + interval '1 hour' ELSE now() END FROM generate_series(1, 100000) i",
                     (tenant.organization_id, int(delayed_fraction * 1024)))

    def check(state):
        plan = _claim_plan(worker, principal, tenant.organization_id)
        _claim_is_an_ordered_walk(plan)
        budget = _delayed_budget(int(100000 * delayed_fraction)) if delayed_fraction else MAX_CLAIM_BLOCKS
        assert _blocks(plan) <= budget, f"{state}\n{_shape(plan)}"

    check("jamais analysee")
    with psycopg.connect(owner_url, autocommit=True) as conn:
        conn.execute("ANALYZE jobs")
    check("analysee")
    session = Dispatcher(worker, principal).session(tenant.organization_id)
    for _ in range(400):
        job = jobs.claim_next_job(session, worker_id="fq", lease_seconds=300)
        jobs.mark_succeeded(session, job)
    check("apres 400 prises")


def test_the_claim_never_sorts_the_queue_across_many_organizations(fresh_queue):
    """11 organisations x 3 000 travaux, base jamais analysee: l'index ecarte faisait deja trier."""
    owner_url, app, worker, principal = fresh_queue
    tenants = [make_tenant(app, f"FM{i}") for i in range(11)]
    for tenant in tenants:
        authorize_service(tenant.session, principal.id)
    for tenant in random.Random(5).sample(tenants, len(tenants)):
        with psycopg.connect(owner_url, autocommit=True) as conn, conn.transaction():
            conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
            conn.execute("INSERT INTO jobs (id, organization_id, job_type, status, priority, correlation_id, payload, "
                         "available_at) SELECT gen_random_uuid(), %s, 'analysis', 'queued', (i %% 5)::smallint, "
                         "gen_random_uuid(), '{}'::jsonb, now() FROM generate_series(1, 3000) i",
                         (tenant.organization_id,))
    plan = _claim_plan(worker, principal, tenants[0].organization_id)
    _claim_is_an_ordered_walk(plan)
    assert _blocks(plan) <= MAX_CLAIM_BLOCKS, _shape(plan)


# =============================================================================================
# D1 (revision 0012): `available_at`, derniere cle de `jobs_ready_idx`, verifiee DANS l'index
# =============================================================================================

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _delayed_budget(delayed):
    """Blocs admissibles quand `delayed` travaux differes precedent le premier travail pret.

    Propriete: ils sont sautes DANS l'index (une page d'index porte ~100 entrees), jamais avec une
    lecture de table chacun. Mesure D1: 90 000 differes -> ~1 150 blocs; sans D1 -> ~91 000.
    Marge: densite divisee par deux (1 bloc par 50 differes) + le cout d'une prise nominale (150).
    """
    return MAX_CLAIM_BLOCKS + delayed // 50


def _uses_available_at_in_the_index(plan):
    conds = [n.get("Index Cond") or "" for n in _walk(plan) if n.get("Relation Name") == "jobs"
             and n.get("Index Name") == "jobs_ready_idx"]
    assert conds and all("available_at" in c for c in conds), _shape(plan)


@pytest.mark.parametrize("delayed", [10000, 90000])
def test_delayed_jobs_at_the_head_are_skipped_inside_the_index(db, worker_db, principal, tenant_a, owner, delayed):
    """Reprises avec delai: elles gardent leur `created_at`, donc restent EN TETE de l'ordre de priorite.
    La prise et la sonde READY les sautent dans l'index; le travail choisi est le premier disponible."""
    authorize_service(tenant_a.session, principal.id)
    with raw(owner, tenant_a.organization_id) as conn:
        conn.execute("INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, payload, created_at, "
                     "available_at) SELECT gen_random_uuid(), %s, 'analysis', 'queued', gen_random_uuid(), '{}'::jsonb, "
                     "now() - interval '1 day', now() + interval '1 hour' FROM generate_series(1, %s)",
                     (tenant_a.organization_id, delayed))
    first = _enqueue(tenant_a)
    _enqueue(tenant_a)
    owner.execute("ANALYZE jobs")

    claim = _claim_plan(worker_db, principal, tenant_a.organization_id)
    _claim_is_an_ordered_walk(claim)
    _uses_available_at_in_the_index(claim)
    assert _blocks(claim) <= _delayed_budget(delayed), _shape(claim)
    probe = _ready_probe(worker_db, principal)
    _index_driven_on(probe, "available_at")
    assert _jobs_blocks(probe) <= _delayed_budget(delayed), _shape(probe)

    assert ready_organizations(worker_db, principal) == [tenant_a.organization_id]
    session = Dispatcher(worker_db, principal).session(tenant_a.organization_id)
    assert jobs.claim_next_job(session, worker_id="head").id == first.id


def test_a_million_job_queue_is_claimed_without_sorting_before_any_analyze(fresh_queue):
    """Un million de travaux dans une organisation, table JAMAIS analysee: avant D1 la prise lisait et
    triait toute la file (~29 000 blocs, ~0,6 s par prise); avec D1 elle suit l'index."""
    owner_url, app, worker, principal = fresh_queue
    tenant = make_tenant(app, "MILLION")
    authorize_service(tenant.session, principal.id)
    with psycopg.connect(owner_url, autocommit=True) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        conn.execute("INSERT INTO jobs (id, organization_id, job_type, status, priority, correlation_id, payload, "
                     "available_at) SELECT gen_random_uuid(), %s, 'analysis', 'queued', (i %% 5)::smallint, "
                     "gen_random_uuid(), '{}'::jsonb, now() FROM generate_series(1, 1000000) i",
                     (tenant.organization_id,))
    plan = _claim_plan(worker, principal, tenant.organization_id)
    _claim_is_an_ordered_walk(plan)
    assert _blocks(plan) <= MAX_CLAIM_BLOCKS, _shape(plan)
    with worker.transaction() as conn:
        probe = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _READY_SQL,
                             (principal.id, None, None, None, 50)).fetchone()[0][0]["Plan"]
    _index_driven_on(probe, "available_at")
    assert _jobs_blocks(probe) <= MAX_PROBE_BLOCKS, _shape(probe)


# -- le travail CHOISI ne change pas: ordre metier, bornes, verrous ----------------------------

def _insert_job(owner, tenant, *, job_id, priority=0, created_at=T0, available_at=T0, job_type="analysis"):
    with raw(owner, tenant.organization_id) as conn:
        conn.execute("INSERT INTO jobs (id, organization_id, job_type, status, priority, correlation_id, payload, "
                     "created_at, available_at) VALUES (%s, %s, %s, 'queued', %s, gen_random_uuid(), '{}'::jsonb, "
                     "%s, %s)", (job_id, tenant.organization_id, job_type, priority, created_at, available_at))


def _uid(n):
    return __import__("uuid").UUID(int=n)


def test_the_claim_order_is_priority_then_age_then_id_and_the_clock_bound_is_inclusive(db, owner, tenant_a):
    """priority DESC, created_at ASC, id ASC; `available_at <= maintenant` (borne incluse)."""
    rows = [dict(job_id=_uid(9), priority=0, created_at=T0 - timedelta(seconds=30)),   # le plus ancien, prio 0
            dict(job_id=_uid(8), priority=5, created_at=T0 - timedelta(seconds=10)),
            dict(job_id=_uid(3), priority=5, created_at=T0 - timedelta(seconds=20)),   # prio 5 plus ancien
            dict(job_id=_uid(7), priority=5, created_at=T0 - timedelta(seconds=10)),   # meme age que 8: id
            dict(job_id=_uid(1), priority=100, created_at=T0, available_at=T0 + timedelta(microseconds=1)),
            dict(job_id=_uid(2), priority=100, created_at=T0, available_at=T0)]        # borne exacte: pret
    for row in rows:
        _insert_job(owner, tenant_a, **row)
    order = []
    while (job := jobs.claim_next_job(tenant_a.session, worker_id="order", now=T0)) is not None:
        order.append(job.id)
    assert order == [_uid(2), _uid(3), _uid(7), _uid(8), _uid(9)]  # _uid(1): disponible 1 us plus tard
    later = jobs.claim_next_job(tenant_a.session, worker_id="order", now=T0 + timedelta(microseconds=1))
    assert later.id == _uid(1)


def test_the_claim_selection_matches_the_reference_order_on_a_random_queue(db, owner, tenant_a):
    """File aleatoire (priorites, ages, ex aequo, differes, types, tentatives epuisees): la sequence de
    prises est EXACTEMENT celle de l'ordre metier calcule independamment."""
    rng = random.Random(1212)
    rows = []
    for n in range(1, 400):
        rows.append(dict(job_id=_uid(rng.randrange(1, 10 ** 9) * 1000 + n), priority=rng.choice([-100, 0, 0, 5, 100]),
                         created_at=T0 - timedelta(seconds=rng.choice([1, 2, 3, 60, 3600])),
                         available_at=T0 + timedelta(seconds=rng.choice([-60, -1, 0, 0, 1, 3600])),
                         job_type=rng.choice(["analysis", "import"])))
    for row in rows:
        _insert_job(owner, tenant_a, **row)
    eligible = sorted((r for r in rows if r["available_at"] <= T0 and r["job_type"] == "analysis"),
                      key=lambda r: (-r["priority"], r["created_at"], r["job_id"]))
    order = []
    while (job := jobs.claim_next_job(tenant_a.session, worker_id="ref", job_types=["analysis"], now=T0)) is not None:
        order.append(job.id)
    assert order == [r["job_id"] for r in eligible]


def test_skip_locked_passes_over_a_locked_job_and_never_takes_it_twice(db, pg, owner, tenant_a):
    """Le premier travail est verrouille par une autre transaction: la prise prend le SUIVANT, sans
    attendre; une fois le verrou libere, le premier est pris a son tour."""
    _insert_job(owner, tenant_a, job_id=_uid(1), priority=10)
    _insert_job(owner, tenant_a, job_id=_uid(2), priority=5)
    with psycopg.connect(pg.url("app"), autocommit=True) as other:
        with raw(other, tenant_a.organization_id, tenant_a.owner_id) as conn:
            conn.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (_uid(1),))
            assert jobs.claim_next_job(tenant_a.session, worker_id="skip", now=T0).id == _uid(2)
            assert jobs.claim_next_job(tenant_a.session, worker_id="skip", now=T0) is None
    assert jobs.claim_next_job(tenant_a.session, worker_id="skip", now=T0).id == _uid(1)
