"""Risque residuel de D-049 ferme par la revision 0010 (Mission 004.4.1).

Vecteur: un nom de relation NON qualifie dans un corps de fonction est resolu A L'EXECUTION, et
`pg_temp` est recherche implicitement en premier. Une table temporaire du meme nom, creee par un
role qui a le privilege `TEMPORARY`, detourne alors la fonction.

Chaque attaque est reelle: table temporaire forgee, puis ecriture qu'une garde doit refuser.
Deux niveaux:
- sur la base de session (tete de migration), sous les vrais roles `app` et `worker`;
- sur une base vide montee a 0009 puis 0010, puis redescendue: l'attaque PASSE a 0009 (preuve du
  vecteur), ECHOUE a 0010, repasse apres la descente (descente fidele) et echoue apres remontee.

Point etabli par ces tests (et non suppose): l'expression d'une POLITIQUE RLS est stockee sous
forme d'arbre deja resolu (OID de relation lie au `CREATE POLICY`). La sous-requete `FROM jobs` de
`audit_events_service_actor` n'etait donc PAS detournable, meme a 0009; la qualification de 0010
y est une coherence de source, sans effet sur le catalogue.
"""
from __future__ import annotations

import re
import uuid
from contextlib import contextmanager

import psycopg
import pytest

from mervio.application.persisted_analysis import SnapshotImportRequest, import_csv_snapshot
from mervio.persistence import migrate
from mervio.persistence.service import authorize_service

from .persistence_support import SAMPLE_FILES, TEST_MASTER_KEY

BEFORE = "0009_qualify_security_functions"
AFTER = "0010_qualify_residual_security"
HEX = "0" * 64


def _can_create_temp(conn) -> bool:
    return conn.execute("SELECT has_database_privilege(current_database(), 'TEMPORARY')").fetchone()[0]


def _require_temp(conn):
    # Vrai dans la configuration de test; faux sous le bootstrap Docker (qui rend l'attaque impossible).
    if not _can_create_temp(conn):
        pytest.skip("ce role n'a pas TEMPORARY (config type Docker): ombrage impossible")


@contextmanager
def _attempt(conn, organization_id, user_id=None):
    """Une tentative isolee: contexte de tenant, puis ANNULATION (tables temporaires comprises)."""
    with conn.transaction(force_rollback=True):
        conn.execute("SELECT set_config('app.organization_id', %s, true), set_config('app.user_id', %s, true)",
                     (str(organization_id), str(user_id) if user_id else ""))
        yield conn


def _insert_run(conn, *, organization_id, store_id, snapshot_id):
    conn.execute(
        "INSERT INTO analysis_runs (id, organization_id, store_id, snapshot_id, status, engine_version, grain, "
        "config, config_sha256, started_at) VALUES (%s, %s, %s, %s, 'running', 'test', 'week', '{}', %s, now())",
        (uuid.uuid4(), organization_id, store_id, snapshot_id, HEX))


def _copy_order(conn, *, snapshot_id):
    """Ajoute a l'instantane une copie d'une commande existante (nouvelle position, nouvelle cle)."""
    columns = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'orders' "
        "ORDER BY ordinal_position").fetchall()]
    replaced = {"position": "position + 1000000", "source_record_id": "source_record_id || '-forged'",
                "order_ref": "order_ref || '-forged'"}
    select = ", ".join(replaced.get(c, c) for c in columns)
    conn.execute(f"INSERT INTO orders ({', '.join(columns)}) SELECT {select} FROM public.orders "
                 "WHERE snapshot_id = %s ORDER BY position LIMIT 1", (snapshot_id,))


def _ingesting_snapshot(owner, tenant) -> uuid.UUID:
    """Un instantane NON scelle (en cours d'ingestion), ecrit par le proprietaire sous RLS forcee."""
    snapshot_id = uuid.uuid4()
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant.organization_id),))
        owner.execute(
            "INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, source, connector, "
            "connector_version, schema_version, normalization_version, status, ingestion_started_at, synthetic, "
            "not_for_production) VALUES (%s, %s, %s, %s, 'csv', 'test', 'test', 'test', 'test', 'ingesting', now(), "
            "false, false)", (snapshot_id, tenant.organization_id, tenant.store_id, tenant.connection_id))
    return snapshot_id


# -- 1. garde d'execution d'analyse (0003) -------------------------------------------------------

def test_a_forged_temp_snapshot_cannot_open_an_analysis_on_an_unsealed_snapshot(owner, app_conn, tenant_a):
    _require_temp(app_conn)
    snapshot_id = _ingesting_snapshot(owner, tenant_a)
    with _attempt(app_conn, tenant_a.organization_id) as conn:
        conn.execute("CREATE TEMP TABLE data_snapshots (id uuid, organization_id uuid, status text)")
        conn.execute("INSERT INTO pg_temp.data_snapshots VALUES (%s, %s, 'completed')",
                     (snapshot_id, tenant_a.organization_id))
        # le vecteur est vivant dans cette session: un nom nu lit la table forgee
        assert conn.execute("SELECT status FROM data_snapshots WHERE id = %s", (snapshot_id,)).fetchone() == \
            ("completed",)
        with pytest.raises(psycopg.errors.RestrictViolation, match="requires a completed data snapshot"):
            _insert_run(conn, organization_id=tenant_a.organization_id, store_id=tenant_a.store_id,
                        snapshot_id=snapshot_id)


# -- 2. garde des lignes canoniques (0002) -------------------------------------------------------

@pytest.fixture
def sealed(db, tenant_a):
    imported = import_csv_snapshot(tenant_a.session, store_id=tenant_a.store_id,
                                   connection_id=tenant_a.connection_id, request=SnapshotImportRequest(**SAMPLE_FILES), master_key=TEST_MASTER_KEY)
    assert imported.snapshot.status == "completed"
    return tenant_a, imported.snapshot.id


def test_a_forged_temp_snapshot_cannot_reopen_a_sealed_snapshot(app_conn, sealed):
    _require_temp(app_conn)
    tenant, snapshot_id = sealed
    with _attempt(app_conn, tenant.organization_id) as conn:
        conn.execute("CREATE TEMP TABLE data_snapshots (id uuid, status text)")
        conn.execute("INSERT INTO pg_temp.data_snapshots VALUES (%s, 'ingesting')", (snapshot_id,))
        assert conn.execute("SELECT status FROM data_snapshots").fetchone() == ("ingesting",)
        with pytest.raises(psycopg.errors.RestrictViolation, match="sealed data snapshot are immutable"):
            _copy_order(conn, snapshot_id=snapshot_id)


def test_a_temp_changed_rows_cannot_hide_the_transition_table(app_conn, sealed):
    """`changed_rows` reste non qualifie (table de transition): elle est resolue AVANT pg_temp."""
    _require_temp(app_conn)
    tenant, snapshot_id = sealed
    with _attempt(app_conn, tenant.organization_id) as conn:
        conn.execute("CREATE TEMP TABLE changed_rows (snapshot_id uuid)")  # vide: masquerait tout ecart
        with pytest.raises(psycopg.errors.RestrictViolation, match="sealed data snapshot are immutable"):
            _copy_order(conn, snapshot_id=snapshot_id)


# -- 3. politique d'audit du worker (0007) -------------------------------------------------------

def _audit_as_worker(conn, *, organization_id, principal_id, resource_id, on_behalf_of):
    conn.execute(
        "INSERT INTO audit_events (id, organization_id, actor_type, actor_id, action, resource_type, resource_id, "
        "correlation_id, outcome, on_behalf_of) VALUES (%s, %s, 'worker', %s, 'job.claimed', 'job', %s, %s, "
        "'succeeded', %s)",
        (uuid.uuid4(), organization_id, principal_id, resource_id, uuid.uuid4(), on_behalf_of))


def test_a_forged_temp_job_cannot_attribute_worker_audit_to_another_human(worker_conn, principal, tenant_a,
                                                                          tenant_b):
    _require_temp(worker_conn)
    authorize_service(tenant_a.session, principal.id)
    victim = tenant_b.owner_id  # un humain reel, etranger a l'organisation
    job_id = uuid.uuid4()
    with _attempt(worker_conn, tenant_a.organization_id) as conn:
        conn.execute("CREATE TEMP TABLE jobs (organization_id uuid, id uuid, enqueued_by uuid)")
        conn.execute("INSERT INTO pg_temp.jobs VALUES (%s, %s, %s)", (tenant_a.organization_id, job_id, victim))
        assert conn.execute("SELECT enqueued_by FROM jobs WHERE id = %s", (job_id,)).fetchone() == (victim,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
            _audit_as_worker(conn, organization_id=tenant_a.organization_id, principal_id=principal.id,
                             resource_id=job_id, on_behalf_of=victim)


def test_the_same_worker_audit_is_accepted_for_a_real_job(worker_conn, principal, tenant_a):
    """Temoin: le refus ci-dessus vient bien de la politique, pas d'un autre defaut de la requete."""
    from mervio.persistence import jobs
    authorize_service(tenant_a.session, principal.id)
    job = jobs.enqueue_job(tenant_a.session, job_type="analysis", payload={"k": "v"})
    with _attempt(worker_conn, tenant_a.organization_id) as conn:
        _audit_as_worker(conn, organization_id=tenant_a.organization_id, principal_id=principal.id,
                         resource_id=job.id, on_behalf_of=tenant_a.owner_id)


# -- 4. inventaire: plus aucune fonction ne lit une relation par un nom nu ----------------------

def _unqualified_relation_reads(conn):
    relations = [r[0] for r in conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')").fetchall()]
    pattern = re.compile(r"\b(?:FROM|JOIN)\s+(" + "|".join(map(re.escape, relations)) + r")\b", re.IGNORECASE)
    found = {}
    for name, source in conn.execute(
            "SELECT p.proname, p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public'").fetchall():
        hits = sorted(set(pattern.findall(source)))
        if hits:
            found[name] = hits
    return found


def test_no_public_function_reads_a_relation_by_an_unqualified_name(owner):
    assert _unqualified_relation_reads(owner) == {}


def test_the_hygiene_changes_no_privilege_and_adds_no_definer(owner):
    definers = owner.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.prosecdef ORDER BY 1").fetchall()
    # 004.4.5: `app_redact_customer` s'y ajoute, etroite et prevue par D-052. Toute AUTRE
    # fonction `SECURITY DEFINER` doit faire echouer ce test: c'est l'inventaire du privilege.
    assert definers == [("app_ensure_service_principal",), ("app_redact_customer",)]
    assert owner.execute("SELECT has_function_privilege('public', 'app_ensure_service_principal()', "
                         "'EXECUTE')").fetchone()[0] is False
    for function in ("analysis_runs_require_sealed_snapshot()", "canonical_rows_guard_sealed()"):
        acl, is_owner = owner.execute(
            "SELECT proacl, pg_get_userbyid(proowner) = current_user FROM pg_proc WHERE oid = %s::regprocedure",
            (function,)).fetchone()
        assert acl is None and is_owner  # droits par defaut et proprietaire inchanges par CREATE OR REPLACE
    permissive, roles, command = owner.execute(
        "SELECT permissive, roles::text[], cmd FROM pg_policies WHERE policyname = 'audit_events_service_actor'"
    ).fetchone()
    assert (permissive, roles, command) == ("RESTRICTIVE", ["mervio_worker"], "INSERT")


# -- 5. preuve du vecteur et fidelite de la descente, sur une base vide -------------------------

@pytest.fixture
def fresh(pg):
    name = pg.create_empty_database("residual")
    try:
        yield pg, pg.url("migrator", name)
    finally:
        pg.drop_database(name)


def _seed(url):
    """Organisation, boutique, connexion, un instantane scelle avec une commande, un non scelle."""
    ids = {k: uuid.uuid4() for k in ("org", "store", "connection", "sealed", "source", "open", "owner")}
    with psycopg.connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(ids["org"]),))
        conn.execute("INSERT INTO users (id, idp_subject) VALUES (%s, 'test|residual|owner')", (ids["owner"],))
        conn.execute("INSERT INTO organizations (id, name) VALUES (%s, 'residual')", (ids["org"],))
        conn.execute("INSERT INTO memberships (id, organization_id, user_id, role) VALUES (%s, %s, %s, 'owner')",
                     (uuid.uuid4(), ids["org"], ids["owner"]))
        conn.execute("INSERT INTO stores (id, organization_id, name) VALUES (%s, %s, 'residual')",
                     (ids["store"], ids["org"]))
        conn.execute("INSERT INTO connections (id, organization_id, store_id, kind, label) "
                     "VALUES (%s, %s, %s, 'csv_upload', 'residual')", (ids["connection"], ids["org"], ids["store"]))
        for key in ("sealed", "open"):
            conn.execute(
                "INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, source, connector, "
                "connector_version, schema_version, normalization_version, status, ingestion_started_at, "
                "synthetic, not_for_production) VALUES (%s, %s, %s, %s, 'csv', 't', 't', 't', 't', 'ingesting', "
                "now(), true, true)", (ids[key], ids["org"], ids["store"], ids["connection"]))
        conn.execute("INSERT INTO snapshot_sources (id, organization_id, store_id, snapshot_id, source_kind, "
                     "file_sha256, byte_size) VALUES (%s, %s, %s, %s, 'shopify_orders', %s, 1)",
                     (ids["source"], ids["org"], ids["store"], ids["sealed"], HEX))
        conn.execute(
            "INSERT INTO orders (organization_id, store_id, snapshot_id, snapshot_source_id, position, source, "
            "source_record_id, order_ref, customer_ref, created_at, currency, subtotal, discount, shipping, tax, "
            "total, financial_status) VALUES (%s, %s, %s, %s, 0, 'shopify', '1', '1', 'guest:1', now(), 'EUR', "
            "10, 0, 0, 0, 10, 'paid')", (ids["org"], ids["store"], ids["sealed"], ids["source"]))
        conn.execute(
            "UPDATE data_snapshots SET status = 'completed', ingested_at = now(), inputs_sha256 = %s, "
            "currency = 'EUR', row_count = 1, record_counts = '{}', quality = '{}' WHERE id = %s",
            (HEX, ids["sealed"]))
    return ids


def _attacks_succeed(url, ids) -> tuple:
    """(analyse sur instantane non scelle acceptee?, ligne ajoutee a un instantane scelle acceptee?)"""
    outcomes = []
    with psycopg.connect(url, autocommit=True) as conn:
        _require_temp(conn)
        for attack in ("analysis", "canonical"):
            try:
                with _attempt(conn, ids["org"]):
                    if attack == "analysis":
                        conn.execute("CREATE TEMP TABLE data_snapshots (id uuid, organization_id uuid, status text)")
                        conn.execute("INSERT INTO pg_temp.data_snapshots VALUES (%s, %s, 'completed')",
                                     (ids["open"], ids["org"]))
                        _insert_run(conn, organization_id=ids["org"], store_id=ids["store"], snapshot_id=ids["open"])
                    else:
                        conn.execute("CREATE TEMP TABLE data_snapshots (id uuid, status text)")
                        conn.execute("INSERT INTO pg_temp.data_snapshots VALUES (%s, 'ingesting')", (ids["sealed"],))
                        _copy_order(conn, snapshot_id=ids["sealed"])
                outcomes.append(True)
            except psycopg.errors.RestrictViolation:
                outcomes.append(False)
    return tuple(outcomes)


def _residual_objects(url):
    with psycopg.connect(url) as conn:
        functions = conn.execute(
            "SELECT proname, prosrc, proacl::text, prosecdef, proconfig FROM pg_proc "
            "WHERE proname IN ('analysis_runs_require_sealed_snapshot', 'canonical_rows_guard_sealed') ORDER BY 1"
        ).fetchall()
        policy = conn.execute(
            "SELECT permissive, roles::text, cmd, qual, with_check FROM pg_policies "
            "WHERE policyname = 'audit_events_service_actor'").fetchone()
    return functions, policy


def test_0010_closes_the_vector_that_0009_left_open_and_downgrades_faithfully(fresh):
    _, url = fresh
    migrate.upgrade(url, BEFORE)
    ids = _seed(url)
    before = _residual_objects(url)
    assert _attacks_succeed(url, ids) == (True, True), "a 0009, le vecteur doit etre demontrable"

    migrate.upgrade(url, AFTER)
    assert _attacks_succeed(url, ids) == (False, False)
    with psycopg.connect(url, autocommit=True) as conn:
        assert _unqualified_relation_reads(conn) == {}

    migrate.downgrade(url, BEFORE)
    assert _residual_objects(url) == before  # corps, droits et politique d'origine, a l'identique
    assert _attacks_succeed(url, ids) == (True, True)

    migrate.upgrade(url, AFTER)
    assert _attacks_succeed(url, ids) == (False, False)


def test_the_audit_policy_was_already_bound_to_the_real_jobs_table_at_0009(fresh):
    """Une politique est stockee resolue (OID): une table temporaire `jobs` ne la detournait pas a 0009.
    La qualification de 0010 ne change donc ni le catalogue ni le comportement de cette politique."""
    pg, url = fresh
    migrate.upgrade(url, BEFORE)
    ids = _seed(url)
    worker_url = pg.url("worker", url.rsplit("/", 1)[1])
    with psycopg.connect(worker_url, autocommit=True) as worker:
        _require_temp(worker)
        principal = worker.execute("SELECT app_ensure_service_principal()").fetchone()[0]
    with psycopg.connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(ids["org"]),))
        conn.execute("INSERT INTO service_authorizations (id, organization_id, service_user_id, granted_by) "
                     "VALUES (%s, %s, %s, %s)", (uuid.uuid4(), ids["org"], principal, ids["owner"]))
        victim = uuid.uuid4()
        conn.execute("INSERT INTO users (id, idp_subject) VALUES (%s, 'test|residual|victim')", (victim,))
    policy_at_0009 = _residual_objects(url)[1]

    with psycopg.connect(worker_url, autocommit=True) as worker:
        job_id = uuid.uuid4()
        with _attempt(worker, ids["org"]) as conn:  # temoin: sans on_behalf_of, la meme trace passe
            _audit_as_worker(conn, organization_id=ids["org"], principal_id=principal, resource_id=job_id,
                             on_behalf_of=None)
        with _attempt(worker, ids["org"]) as conn:
            conn.execute("CREATE TEMP TABLE jobs (organization_id uuid, id uuid, enqueued_by uuid)")
            conn.execute("INSERT INTO pg_temp.jobs VALUES (%s, %s, %s)", (ids["org"], job_id, victim))
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
                _audit_as_worker(conn, organization_id=ids["org"], principal_id=principal, resource_id=job_id,
                                 on_behalf_of=victim)

    migrate.upgrade(url, AFTER)
    assert _residual_objects(url)[1] == policy_at_0009
