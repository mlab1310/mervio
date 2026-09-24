"""Identite client a cle et absence d'e-mail persiste (Mission 004.4.2, revision 0011). PostgreSQL.

Prouve en base, sous les vrais roles, ce que 004.4.2 affirme:
- aucune colonne ni aucune valeur e-mail dans les donnees persistees (lignes, qualite, travaux,
  audit, rapports, logs), la cle maitre absente de partout;
- une reference client ne peut plus etre un e-mail (contrainte `orders_customer_ref_keyed`);
- le sel d'identite: cree a la premiere utilisation, unique meme sous concurrence, immuable sauf
  destruction, isole par organisation (RLS forcee, worker soumis a l'autorisation et au delegue);
- une autre cle maitre ou un sel detruit: refus explicite, jamais une cle de substitution;
- references stables dans une organisation, differentes entre organisations, KPI identiques.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace

import psycopg
import pytest

from mervio.application.persisted_analysis import SnapshotImportRequest, analyze_snapshot, import_csv_snapshot
from mervio.identity import REF_PATTERN, MasterKey
from mervio.observability.logging import configure
from mervio.persistence import analyses, snapshots
from mervio.persistence.codec import inputs_sha256
from mervio.persistence.database import Database
from mervio.persistence.errors import IdentityKeyUnavailable, PermissionDenied
from mervio.persistence.identity_keys import ensure_identity_key
from mervio.persistence.jobs import JobType
from mervio.persistence.service import authorize_service
from mervio.persistence.stores import create_csv_connection, create_store
from mervio.persistence.tenancy import Role, TenantContext, TenantSession, add_member, ensure_user
from mervio.workers.handlers import default_registry
from mervio.workers.worker import enqueue

from .persistence_support import (
    SAMPLE_FILES, TEST_MASTER_KEY, TEST_MASTER_KEY_HEX, TEST_OBJECT_STORE, deposit_sources,
    org_identity,
)
from .service_support import raw
from .worker_support import DispatchedWorker

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
KEYS = "organization_identity_keys"


def _import(tenant, files=SAMPLE_FILES, *, master=TEST_MASTER_KEY, **extra):
    return import_csv_snapshot(tenant.session, store_id=tenant.store_id, connection_id=tenant.connection_id,
                               request=SnapshotImportRequest(**files, **extra), master_key=master)


def _refs(tenant, snapshot_id):
    return [o.customer_id for o in snapshots.load_dataset(tenant.session, tenant.store_id, snapshot_id).orders]


def member(db, tenant, role, label):
    user_id = ensure_user(db, f"test|identity|{label}")
    add_member(tenant.session, user_id=user_id, role=role)
    return user_id, TenantSession(db, TenantContext(tenant.organization_id, user_id))


# -- schema ----------------------------------------------------------------------------------

def test_no_email_column_remains_anywhere_in_the_schema(owner):
    assert owner.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name ILIKE '%%email%%'").fetchall() == []


def test_the_store_and_connection_contract_columns(owner):
    columns = dict(((t, c), (n, d)) for t, c, n, d in owner.execute(
        "SELECT table_name, column_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND (table_name, column_name) IN "
        "(('stores', 'timezone'), ('stores', 'timezone_source'), ('connections', 'credential_ref'))").fetchall())
    assert columns[("stores", "timezone")] == ("NO", "'UTC'::text")
    assert columns[("stores", "timezone_source")] == ("NO", "'default'::text")
    assert columns[("connections", "credential_ref")] == ("YES", None)


def test_a_new_store_defaults_to_utc_signalled_as_default(db, tenant_a, app_conn):
    with raw(app_conn, tenant_a.organization_id) as conn:
        assert conn.execute("SELECT timezone, timezone_source FROM stores WHERE id = %s",
                            (tenant_a.store_id,)).fetchone() == ("UTC", "default")


@pytest.mark.parametrize("timezone, source, accepted", [
    ("Europe/Paris", "explicit", True), ("America/Argentina/Buenos_Aires", "explicit", True),
    ("Etc/GMT+3", "explicit", True), ("UTC", "default", True),
    ("Paris; DROP TABLE stores", "explicit", False), ("", "explicit", False), ("x" * 65, "explicit", False),
    ("Europe/Paris", "guessed", False),
])
def test_the_timezone_columns_only_hold_a_zone_name_and_a_known_source(app_conn, tenant_a, timezone, source,
                                                                       accepted):
    def insert():
        with raw(app_conn, tenant_a.organization_id) as conn:
            conn.execute("INSERT INTO stores (id, organization_id, name, timezone, timezone_source) "
                         "VALUES (%s, %s, 'tz', %s, %s)", (uuid.uuid4(), tenant_a.organization_id, timezone, source))
    if accepted:
        insert()
    else:
        with pytest.raises(psycopg.errors.CheckViolation):
            insert()


def test_a_csv_connection_never_carries_a_credential_reference(app_conn, owner, tenant_a):
    with pytest.raises(psycopg.errors.CheckViolation):
        with raw(app_conn, tenant_a.organization_id) as conn:
            conn.execute("INSERT INTO connections (id, organization_id, store_id, kind, label, credential_ref) "
                         "VALUES (%s, %s, %s, 'csv_upload', 'x', 'vault:any')",
                         (uuid.uuid4(), tenant_a.organization_id, tenant_a.store_id))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # aucun droit UPDATE sur la colonne
        with raw(app_conn, tenant_a.organization_id) as conn:
            conn.execute("UPDATE connections SET credential_ref = NULL WHERE id = %s", (tenant_a.connection_id,))
    with pytest.raises(psycopg.errors.CheckViolation):  # meme le proprietaire
        with raw(owner, tenant_a.organization_id) as conn:
            conn.execute("UPDATE connections SET credential_ref = 'vault:any' WHERE id = %s",
                         (tenant_a.connection_id,))


# -- reference client: la base refuse un e-mail ----------------------------------------------

def _insert_order(conn, tenant, customer_ref):
    snapshot_id, source_id = uuid.uuid4(), uuid.uuid4()
    conn.execute("INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, source, connector, "
                 "connector_version, schema_version, normalization_version, status, ingestion_started_at, synthetic, "
                 "not_for_production) VALUES (%s, %s, %s, %s, 'csv', 't', 't', 't', 't', 'ingesting', now(), false, "
                 "false)", (snapshot_id, tenant.organization_id, tenant.store_id, tenant.connection_id))
    conn.execute("INSERT INTO snapshot_sources (id, organization_id, store_id, snapshot_id, source_kind, file_sha256, "
                 "byte_size) VALUES (%s, %s, %s, %s, 'shopify_orders', %s, 1)",
                 (source_id, tenant.organization_id, tenant.store_id, snapshot_id, "0" * 64))
    conn.execute("INSERT INTO orders (organization_id, store_id, snapshot_id, snapshot_source_id, position, source, "
                 "source_record_id, order_ref, customer_ref, created_at, currency, subtotal, discount, shipping, tax, "
                 "total, financial_status) VALUES (%s, %s, %s, %s, 0, 'shopify', '1', '1', %s, now(), 'EUR', 1, 0, "
                 "0, 0, 1, 'paid')", (tenant.organization_id, tenant.store_id, snapshot_id, source_id, customer_ref))


@pytest.mark.parametrize("customer_ref", ["x", "john@example.com", "guest:1", "c1:" + "0" * 31, "C1:" + "a" * 32,
                                          "c1:" + "g" * 32, "c2:" + "a" * 32, "redacted:not-a-uuid!"])
def test_a_customer_reference_that_is_not_keyed_is_refused_by_the_database(app_conn, tenant_a, customer_ref):
    with pytest.raises(psycopg.errors.CheckViolation, match="orders_customer_ref_keyed"):
        with raw(app_conn, tenant_a.organization_id) as conn:
            _insert_order(conn, tenant_a, customer_ref)


@pytest.mark.parametrize("customer_ref", ["c1:" + "0123456789abcdef" * 2, "g1:" + "f" * 32,
                                          "redacted:" + str(uuid.UUID(int=7))])
def test_keyed_references_and_redaction_tombstones_are_accepted(app_conn, tenant_a, customer_ref):
    with app_conn.transaction(force_rollback=True):
        app_conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(tenant_a.organization_id),))
        _insert_order(app_conn, tenant_a, customer_ref)


def test_the_customer_reference_guard_is_not_validated_on_historical_rows(owner):
    assert owner.execute("SELECT convalidated FROM pg_constraint WHERE conname = 'orders_customer_ref_keyed'"
                         ).fetchone() == (False,)


# -- sel d'identite d'organisation ------------------------------------------------------------

def _key_rows(owner, organization_id):
    with raw(owner, organization_id) as conn:
        return conn.execute(f"SELECT scheme, salt, master_key_id, destroyed_at FROM {KEYS}").fetchall()


def test_the_key_is_created_on_first_use_then_reused(db, owner, tenant_a):
    assert _key_rows(owner, tenant_a.organization_id) == []
    first = ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    again = ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    assert first.ref_email("a@x.com") == again.ref_email("a@x.com")
    [(scheme, salt, master_key_id, destroyed_at)] = _key_rows(owner, tenant_a.organization_id)
    assert (scheme, len(bytes(salt)), master_key_id, destroyed_at) == (
        "hmac-sha256-v1", 32, TEST_MASTER_KEY.key_id, None)
    # seul le sel est persiste: ni la cle maitre, ni la cle d'organisation
    assert TEST_MASTER_KEY_HEX not in bytes(salt).hex() and first.export_hex() not in bytes(salt).hex()


def test_two_sessions_racing_on_first_use_get_one_and_the_same_key(pg, db, owner, tenant_a):
    barrier, results, errors = threading.Barrier(2), [], []
    databases = [Database(pg.url("app")) for _ in range(2)]

    def use(database):
        try:
            session = TenantSession(database, TenantContext(tenant_a.organization_id, tenant_a.owner_id))
            barrier.wait()
            results.append(ensure_identity_key(session, TEST_MASTER_KEY).ref_email("race@x.com"))
        except BaseException as exc:  # noqa: BLE001 - rapporte au fil principal
            errors.append(exc)

    threads = [threading.Thread(target=use, args=(d,)) for d in databases]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    for database in databases:
        database.close()
    assert errors == [] and len(results) == 2 and results[0] == results[1]
    assert len(_key_rows(owner, tenant_a.organization_id)) == 1


def test_another_master_key_is_refused_explicitly(db, tenant_a):
    ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    with pytest.raises(IdentityKeyUnavailable) as refused:
        ensure_identity_key(tenant_a.session, MasterKey(secrets.token_bytes(32)))
    assert (refused.value.code, refused.value.reason) == ("identity_key_unavailable", "master_key_mismatch")


def test_a_viewer_cannot_use_the_identity_key(db, tenant_a):
    _, viewer = member(db, tenant_a, Role.VIEWER, "viewer")
    with pytest.raises(PermissionDenied):
        ensure_identity_key(viewer, TEST_MASTER_KEY)


def test_the_salt_changes_only_once_by_destruction_and_is_then_refused(db, owner, tenant_a):
    ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    organization = tenant_a.organization_id

    def update(statement, *params):
        with raw(owner, organization) as conn:
            conn.execute(statement, params)

    for statement, params in [
        (f"UPDATE {KEYS} SET salt = %s", (secrets.token_bytes(32),)),
        (f"UPDATE {KEYS} SET master_key_id = %s", ("0" * 16,)),
        (f"UPDATE {KEYS} SET organization_id = %s", (uuid.uuid4(),)),
        (f"UPDATE {KEYS} SET created_at = now() - interval '1 day'", ()),
        (f"UPDATE {KEYS} SET destroyed_at = now()", ()),          # detruit sans effacer le sel
        (f"DELETE FROM {KEYS}", ()),
    ]:
        with pytest.raises(psycopg.errors.RestrictViolation):
            update(statement, *params)
    update(f"UPDATE {KEYS} SET salt = NULL, destroyed_at = now()")  # la seule transition permise
    for statement, params in [
        (f"UPDATE {KEYS} SET destroyed_at = now()", ()),                                   # seconde destruction
        (f"UPDATE {KEYS} SET salt = %s, destroyed_at = NULL", (secrets.token_bytes(32),)),  # restauration
        (f"DELETE FROM {KEYS}", ()),
    ]:
        with pytest.raises(psycopg.errors.RestrictViolation):
            update(statement, *params)
    with pytest.raises(IdentityKeyUnavailable) as refused:
        ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    assert refused.value.reason == "destroyed"


# -- isolation -------------------------------------------------------------------------------

def test_the_application_role_can_neither_update_nor_delete_a_key(db, app_conn, tenant_a):
    ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    for statement in (f"UPDATE {KEYS} SET salt = NULL, destroyed_at = now()", f"DELETE FROM {KEYS}"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn:
                conn.execute(statement)


def test_a_tenant_never_reads_nor_writes_another_tenant_key(db, app_conn, tenant_a, tenant_b):
    ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    ensure_identity_key(tenant_b.session, TEST_MASTER_KEY)
    with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn:
        assert conn.execute(f"SELECT organization_id FROM {KEYS}").fetchall() == [(tenant_a.organization_id,)]
        assert conn.execute(f"SELECT count(*) FROM {KEYS} WHERE organization_id = %s",
                            (tenant_b.organization_id,)).fetchone() == (0,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        with raw(app_conn, tenant_a.organization_id, tenant_a.owner_id) as conn:
            conn.execute(f"INSERT INTO {KEYS} (organization_id, scheme, salt, master_key_id) VALUES "
                         "(%s, 'hmac-sha256-v1', %s, %s)",
                         (tenant_b.organization_id, secrets.token_bytes(32), "0" * 16))


def test_the_worker_needs_an_authorization_and_an_analyst_delegate(db, worker_conn, principal, tenant_a):
    ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    _, viewer = member(db, tenant_a, Role.VIEWER, "wviewer")

    def visible(user_id=None):
        with raw(worker_conn, tenant_a.organization_id, user_id) as conn:
            return conn.execute(f"SELECT count(*) FROM {KEYS}").fetchone()[0]

    assert visible(tenant_a.owner_id) == 0  # service non autorise: rien
    authorize_service(tenant_a.session, principal.id)
    assert visible() == 0                   # autorise, sans delegue: rien
    assert visible(viewer.user_id) == 0     # delegue lecteur: rien
    assert visible(tenant_a.owner_id) == 1  # delegue proprietaire (>= analyst): la cle de SON organisation
    for statement in (f"UPDATE {KEYS} SET salt = NULL, destroyed_at = now()", f"DELETE FROM {KEYS}"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with raw(worker_conn, tenant_a.organization_id, tenant_a.owner_id) as conn:
                conn.execute(statement)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        with raw(worker_conn, tenant_a.organization_id) as conn:  # insertion sans delegue: refusee
            conn.execute(f"INSERT INTO {KEYS} (organization_id, scheme, salt, master_key_id) VALUES "
                         "(%s, 'hmac-sha256-v1', %s, %s) ON CONFLICT DO NOTHING",
                         (tenant_a.organization_id, secrets.token_bytes(32), "0" * 16))


def _shadow_key_table(db, *rows):
    """Ombrage pg_temp (004.4.2, F-04) sur la connexion MEME qu'utilise `ensure_identity_key`.

    Table temporaire homonyme (meme forme, meme cle primaire, donc `ON CONFLICT` s'y appliquerait),
    `pg_temp` place explicitement AVANT `public`. Faux si le role n'a pas TEMPORARY: l'ombrage est
    alors impossible, ce qui est prouve (et non ignore).
    """
    conn = db.connection()
    if not conn.execute("SELECT has_database_privilege(current_database(), 'TEMPORARY')").fetchone()[0]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"CREATE TEMP TABLE {KEYS} (organization_id uuid PRIMARY KEY)")
        return False
    conn.execute(f"CREATE TEMP TABLE {KEYS} (organization_id uuid PRIMARY KEY, scheme text NOT NULL, "
                 "salt bytea NULL, master_key_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), "
                 "destroyed_at timestamptz NULL)")
    for row in rows:
        conn.execute(f"INSERT INTO pg_temp.{KEYS} (organization_id, scheme, salt, master_key_id) "
                     "VALUES (%s, %s, %s, %s)", row)
    conn.execute("SET search_path = pg_temp, public")
    # le nom NON qualifie designe bien la table temporaire dans cette session
    assert conn.execute(f"SELECT relnamespace = pg_my_temp_schema() FROM pg_class "
                        f"WHERE oid = to_regclass('{KEYS}')").fetchone() == (True,)
    return True


def _temp_rows(db):
    return db.connection().execute(f"SELECT organization_id, salt, master_key_id FROM pg_temp.{KEYS}").fetchall()


def test_a_shadowing_temp_table_cannot_supply_a_forged_identity_key(db, owner, tenant_a):
    """Le VRAI chemin (`ensure_identity_key`) sous ombrage: la table temporaire fournit un sel forge
    avec le bon identifiant de cle maitre. La cle retournee doit venir du sel CANONIQUE."""
    forged_salt = secrets.token_bytes(32)
    if not _shadow_key_table(db, (tenant_a.organization_id, "hmac-sha256-v1", forged_salt, TEST_MASTER_KEY.key_id)):
        return
    identity = ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    [(_, real_salt, _, _)] = _key_rows(owner, tenant_a.organization_id)  # la ligne canonique a ete creee
    forged = TEST_MASTER_KEY.organization_identity(tenant_a.organization_id, forged_salt)
    canonical = TEST_MASTER_KEY.organization_identity(tenant_a.organization_id, bytes(real_salt))
    assert identity.key_id == canonical.key_id != forged.key_id
    assert identity.ref_email("shadow@x.invalid") == canonical.ref_email("shadow@x.invalid")
    # rien n'a ete ecrit dans la table temporaire: seule la ligne forgee y reste
    assert _temp_rows(db) == [(tenant_a.organization_id, forged_salt, TEST_MASTER_KEY.key_id)]


def test_a_shadowing_temp_table_never_receives_the_identity_key(db, owner, tenant_a):
    """Premiere utilisation sous ombrage d'une table temporaire VIDE: l'ecriture va a la table canonique."""
    if not _shadow_key_table(db):
        return
    identity = ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    assert _temp_rows(db) == []
    [(_, real_salt, master_key_id, _)] = _key_rows(owner, tenant_a.organization_id)
    assert master_key_id == TEST_MASTER_KEY.key_id
    assert identity.key_id == TEST_MASTER_KEY.organization_identity(tenant_a.organization_id,
                                                                   bytes(real_salt)).key_id


def test_a_shadowing_temp_table_cannot_resurrect_a_destroyed_key(db, owner, tenant_a):
    """Cle canonique detruite + sel valide dans une table temporaire: toujours refusee (`destroyed`)."""
    ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    with raw(owner, tenant_a.organization_id) as conn:
        conn.execute(f"UPDATE {KEYS} SET salt = NULL, destroyed_at = now()")
    if not _shadow_key_table(db, (tenant_a.organization_id, "hmac-sha256-v1", secrets.token_bytes(32),
                                  TEST_MASTER_KEY.key_id)):
        return
    with pytest.raises(IdentityKeyUnavailable) as refused:
        ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    assert refused.value.reason == "destroyed"


def test_the_identity_key_sql_is_schema_qualified_and_the_trigger_reads_no_table(app_conn):
    """Garde statique: toute relation de `identity_keys.py` est qualifiee `public.`; le declencheur ne lit rien."""
    import ast
    import inspect
    from mervio.persistence import identity_keys
    statements = [node.args[0].value for node in ast.walk(ast.parse(inspect.getsource(identity_keys)))
                  if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "execute"
                  and node.args and isinstance(node.args[0], ast.Constant)]
    assert len(statements) == 2  # INSERT puis relecture
    for statement in statements:
        relations = re.findall(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([\w.]+)", statement)
        assert relations and all(r == f"public.{KEYS}" for r in relations), statement
    assert "FROM" not in app_conn.execute(
        "SELECT upper(prosrc) FROM pg_proc WHERE proname = %s", (f"{KEYS}_guard",)).fetchone()[0]


# -- identite, KPI et idempotence ------------------------------------------------------------

def test_references_are_stable_in_an_organization_and_differ_across_organizations(db, tenant_a, tenant_b):
    first = _import(tenant_a)
    other_store = create_store(tenant_a.session, name="Seconde boutique")
    second = replace(tenant_a, store_id=other_store.id, connection_id=create_csv_connection(
        tenant_a.session, store_id=other_store.id, label="Imports 2").id)
    again = _import(second)
    elsewhere = _import(tenant_b)
    refs_a, refs_a2, refs_b = (_refs(t, r.snapshot.id) for t, r in
                               ((tenant_a, first), (second, again), (tenant_b, elsewhere)))
    assert all(REF_PATTERN.match(r) for r in refs_a + refs_b)
    assert refs_a == refs_a2
    assert set(refs_a).isdisjoint(refs_b)
    assert len(set(refs_a)) == len(set(refs_b))  # meme correspondance un pour un des clients


def test_customer_kpis_do_not_depend_on_the_key(db, tenant_a, tenant_b):
    from datetime import date
    reports = []
    for tenant in (tenant_a, tenant_b):
        snapshot = _import(tenant).snapshot
        outcome = analyze_snapshot(tenant.session, store_id=tenant.store_id, snapshot_id=snapshot.id,
                                   today=date(2026, 9, 14))
        reports.append(analyses.get_report(tenant.session, tenant.store_id, outcome.report.id).report())
    ids = []
    for report in reports:
        report["_meta"].pop("generated_at")
        ids.append([c.pop("customer_id") for c in report["customers"]["top_customers"]])
    assert reports[0] == reports[1]  # KPI, clients, constats: identiques; seule la cle differe
    assert reports[0]["customers"]["total_active"] > 0 and ids[0] != ids[1]


def test_the_import_fingerprint_depends_on_the_identity_key_and_the_new_normalization(db, tenant_a):
    from mervio.persistence.codec import file_sha256
    hashes = {kind: file_sha256(path)[0] for kind, path in SAMPLE_FILES.items()}
    common = dict(file_hashes=hashes, connector=snapshots.CSV_CONNECTOR, schema_version=snapshots.CSV_SCHEMA_VERSION,
                  normalization_version=snapshots.NORMALIZATION_VERSION, synthetic=False)
    assert inputs_sha256(**common, identity_key_id="a" * 16) != inputs_sha256(**common, identity_key_id="b" * 16)
    first = _import(tenant_a)
    assert first.status == "completed" and first.snapshot.normalization_version == "mervio-normalization/2"
    assert first.snapshot.inputs_sha256 == inputs_sha256(**common, identity_key_id=org_identity(tenant_a).key_id)
    assert _import(tenant_a).status == "reused"  # memes octets, meme cle: meme instantane


#: chemins ouverts pendant une fenetre d'observation (crochet d'audit Python: open/os.open)
_OPENED: list = []
_WATCHING: list = []


def _record_open(event, args):
    if _WATCHING and event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        _OPENED.append(os.fsdecode(args[0]))


sys.addaudithook(_record_open)


@contextmanager
def _watch_opens():
    _OPENED.clear()
    _WATCHING.append(True)
    try:
        yield _OPENED
    finally:
        _WATCHING.clear()


def _snapshot_count(owner, tenant):
    with raw(owner, tenant.organization_id) as conn:
        return conn.execute("SELECT count(*) FROM data_snapshots").fetchone()[0]


@pytest.mark.parametrize("state", ["master_key_mismatch", "destroyed"])
def test_an_import_without_a_usable_key_is_refused_before_reading_any_file(db, owner, tenant_a, tmp_path, state):
    """F-02/F-05: un chemin qui ECHOUERAIT s'il etait ouvert; le refus vient de l'identite, avant tout acces."""
    ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    master = MasterKey(secrets.token_bytes(32))
    if state == "destroyed":
        master = TEST_MASTER_KEY
        with raw(owner, tenant_a.organization_id) as conn:
            conn.execute(f"UPDATE {KEYS} SET salt = NULL, destroyed_at = now()")
    trap = tmp_path / "never_opened" / "shopify_orders.csv"  # n'existe pas: toute lecture echouerait
    started = []
    with _watch_opens() as opened, pytest.raises(IdentityKeyUnavailable) as refused:
        import_csv_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                            request=SnapshotImportRequest(shopify_orders=str(trap)), master_key=master,
                            on_started=lambda: started.append(True))
    assert refused.value.reason == state
    assert started == []                                           # jamais considere comme commence
    assert [p for p in opened if "never_opened" in p] == []         # aucune tentative d'ouverture
    assert _snapshot_count(owner, tenant_a) == 0                    # aucun instantane, aucune reference


def test_the_trap_path_would_have_failed_with_a_usable_key(db, owner, tenant_a, tmp_path):
    """Temoin du test precedent: avec une cle utilisable, le MEME chemin est ouvert (le crochet le voit)
    et l'ouverture echoue explicitement, APRES `on_started`."""
    trap = tmp_path / "never_opened" / "shopify_orders.csv"
    started = []
    with _watch_opens() as opened, pytest.raises(FileNotFoundError):
        import_csv_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                            request=SnapshotImportRequest(shopify_orders=str(trap)), master_key=TEST_MASTER_KEY,
                            on_started=lambda: started.append(len(opened)))
    assert started == [0]                                           # commence AVANT toute ouverture
    assert str(trap) in opened                                      # puis le fichier est bien ouvert


# -- worker: cle liee au processus, jamais dans une charge, un audit ou un log -----------------

@pytest.fixture
def json_logs():
    root = logging.getLogger("mervio")
    state = (list(root.handlers), root.propagate, root.level)
    buffer = io.StringIO()
    configure(format="json", level=logging.DEBUG, stream=buffer, environment="test")
    yield buffer
    root.handlers, root.propagate, root.level = state


def _import_job(session, tenant):
    return enqueue(session, job_type=JobType.IMPORT, store_id=tenant.store_id, payload={
        "store_id": str(tenant.store_id), "connection_id": str(tenant.connection_id),
        "raw_objects": deposit_sources(tenant)})


def test_a_dispatched_import_and_analysis_persist_no_email_and_no_key(pg, db, owner, tenant_a, principal, json_logs):
    authorize_service(tenant_a.session, principal.id)
    worker = DispatchedWorker(pg, principal, worker_id="pii", lease_seconds=30, renew_seconds=5,
                              registry=default_registry(identity_master=TEST_MASTER_KEY, object_store=TEST_OBJECT_STORE))
    try:
        _import_job(tenant_a.session, tenant_a)
        imported = worker.worker.run_once()
        assert imported.succeeded, imported.error_code
        enqueue(tenant_a.session, job_type=JobType.ANALYSIS, store_id=tenant_a.store_id, payload={
            "store_id": str(tenant_a.store_id), "snapshot_id": imported.result["snapshot_id"],
            "as_of_date": "2026-09-14"})
        assert worker.worker.run_once().succeeded
    finally:
        worker.close()

    salt = bytes(_key_rows(owner, tenant_a.organization_id)[0][1]).hex()
    with raw(owner, tenant_a.organization_id) as conn:
        persisted = {
            "orders": conn.execute("SELECT orders::text FROM orders").fetchall(),
            "payments": conn.execute("SELECT payments::text FROM payments").fetchall(),
            "snapshots": conn.execute("SELECT data_snapshots::text FROM data_snapshots").fetchall(),
            "jobs": conn.execute("SELECT payload::text || coalesce(result::text, '') || coalesce(last_error, '') "
                                 "FROM jobs").fetchall(),
            "audit": conn.execute("SELECT audit_events::text FROM audit_events").fetchall(),
            "reports": conn.execute("SELECT payload_json FROM reports").fetchall(),
        }
        refs = [r for (r,) in conn.execute("SELECT customer_ref FROM orders").fetchall()]
    assert refs and all(REF_PATTERN.match(r) for r in refs)
    assert persisted["orders"] and persisted["payments"] and persisted["reports"] and persisted["audit"]
    logs = json_logs.getvalue()
    for place, rows in [*persisted.items(), ("logs", [(logs,)])]:
        text = "\n".join(str(row[0]) for row in rows)
        assert EMAIL.findall(text) == [], place
        for secret in (TEST_MASTER_KEY_HEX, salt, org_identity(tenant_a).export_hex()):
            assert secret not in text, place


def _job_audit(owner, tenant, job_id):
    with raw(owner, tenant.organization_id) as conn:
        return [(action, outcome, metadata.get("error_code")) for action, outcome, metadata in conn.execute(
            "SELECT action, outcome, metadata FROM audit_events WHERE resource_id = %s ORDER BY created_at, action",
            (job_id,)).fetchall()]


def _run_import(pg, principal, tenant, name, registry):
    worker = DispatchedWorker(pg, principal, worker_id=f"key-{name}", registry=registry,
                              lease_seconds=30, renew_seconds=5)
    try:
        job = _import_job(tenant.session, tenant)
        return job, worker.worker.run_once()
    finally:
        worker.close()


@pytest.mark.parametrize("case", ["no_master_key", "other_master_key", "destroyed_key"])
def test_a_worker_without_a_usable_identity_key_refuses_imports_without_starting_them(pg, db, owner, tenant_a,
                                                                                      principal, case):
    """F-02/F-05: refus permanent ET audit coherent: `job.claimed` puis `job.failed`, JAMAIS `import.started`."""
    authorize_service(tenant_a.session, principal.id)
    ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)  # identite deja creee avec la cle de reference
    master = {"no_master_key": None, "other_master_key": MasterKey(secrets.token_bytes(32)),
              "destroyed_key": TEST_MASTER_KEY}[case]
    if case == "destroyed_key":
        with raw(owner, tenant_a.organization_id) as conn:
            conn.execute(f"UPDATE {KEYS} SET salt = NULL, destroyed_at = now()")
    job, outcome = _run_import(pg, principal, tenant_a, case, default_registry(identity_master=master, object_store=TEST_OBJECT_STORE))
    assert (outcome.job.id, outcome.outcome, outcome.error_code) == (job.id, "failed", "identity_key_unavailable")
    trail = _job_audit(owner, tenant_a, job.id)
    assert [action for action, _, _ in trail] == ["job.enqueued", "job.claimed", "job.failed"], trail
    assert trail[-1] == ("job.failed", "failed", "identity_key_unavailable")
    assert _snapshot_count(owner, tenant_a) == 0


def test_a_worker_with_a_usable_identity_key_starts_the_import(pg, db, owner, tenant_a, principal):
    """Temoin: cle utilisable -> `import.started` puis `import.succeeded`, dans cet ordre."""
    authorize_service(tenant_a.session, principal.id)
    job, outcome = _run_import(pg, principal, tenant_a, "valid", default_registry(identity_master=TEST_MASTER_KEY, object_store=TEST_OBJECT_STORE))
    assert outcome.succeeded, outcome.error_code
    actions = [action for action, _, _ in _job_audit(owner, tenant_a, job.id)]
    assert actions[:3] == ["job.enqueued", "job.claimed", "import.started"]
    assert actions.count("import.started") == 1
    with raw(owner, tenant_a.organization_id) as conn:  # import.succeeded porte sur l'instantane
        assert conn.execute("SELECT count(*) FROM audit_events WHERE action = 'import.succeeded'").fetchone() == (1,)


def test_the_worker_settings_log_no_key(json_logs):
    from mervio.observability.logging import get_event_logger
    from mervio.settings import WorkerSettings
    settings = WorkerSettings.from_env({"MERVIO_DATABASE_URL": "postgresql://w@db/mervio",
                                        "MERVIO_IDENTITY_MASTER_KEY": TEST_MASTER_KEY_HEX,
                                        "MERVIO_OBJECT_STORE_ROOT": "/var/lib/mervio/objects"})
    get_event_logger("worker").info("worker.config", **settings.public())
    assert TEST_MASTER_KEY_HEX not in json_logs.getvalue() + repr(settings)
    assert json.loads(json_logs.getvalue().splitlines()[-1])["identity_master_configured"] is True


# -- F-08: aucun jeu non lie a la cle d'organisation n'entre en base --------------------------

def _write(tenant, dataset, identity):
    from mervio.persistence.snapshots import SourceFile, write_snapshot
    return write_snapshot(tenant.session, store_id=tenant.store_id, connection_id=tenant.connection_id,
                          dataset=dataset, sources=[SourceFile(k, "5" * 64, 1) for k in SAMPLE_FILES],
                          inputs_sha256=secrets.token_hex(32), synthetic=False, identity=identity)


def _local_dataset(identity=None):
    from mervio.analytics.pipeline import SourcePaths, load_dataset
    return load_dataset(SourcePaths(**SAMPLE_FILES), identity)


@pytest.mark.parametrize("case", ["helper_default_ephemeral", "explicit_ephemeral", "explicit_key_file_of_the_org",
                                  "dataset_from_default_helper", "other_organization_key"])
def test_the_persistent_path_refuses_any_non_organization_identity(db, owner, tenant_a, tenant_b, tmp_path, case):
    """F-08: le repli ephemere des aides locales, une cle CLI (meme le fichier de la cle de l'organisation),
    un jeu normalise sans cle, ou la cle d'une autre organisation: refuses AVANT toute ecriture."""
    from mervio.identity import CustomerIdentity
    from mervio.persistence.errors import SnapshotIntegrityError
    org = org_identity(tenant_a)
    if case == "helper_default_ephemeral":
        dataset = _local_dataset()
        identity = CustomerIdentity.ephemeral()
    elif case == "explicit_ephemeral":
        identity = CustomerIdentity.ephemeral()
        dataset = _local_dataset(identity)
    elif case == "explicit_key_file_of_the_org":  # meme materiel de cle, provenance CLI: refuse
        key = tmp_path / "org.key"
        key.write_text(org.export_hex(), encoding="utf-8")
        identity = CustomerIdentity.from_key_file(key)
        dataset = _local_dataset(identity)
        assert identity.key_id == org.key_id and identity.origin == "explicit"
    elif case == "dataset_from_default_helper":  # bonne cle fournie, mais jeu normalise avec le repli
        identity, dataset = org, _local_dataset()
    else:
        identity = org_identity(tenant_b)
        dataset = _local_dataset(identity)
    with pytest.raises(SnapshotIntegrityError):
        _write(tenant_a, dataset, identity)
    assert _snapshot_count(owner, tenant_a) == 0


def test_the_persistent_path_requires_an_identity_argument(db, tenant_a):
    with pytest.raises(TypeError):
        from mervio.persistence.snapshots import SourceFile, write_snapshot
        write_snapshot(tenant_a.session, store_id=tenant_a.store_id, connection_id=tenant_a.connection_id,
                       dataset=_local_dataset(), sources=[SourceFile("shopify_orders", "5" * 64, 1)],
                       inputs_sha256="6" * 64, synthetic=False)


def test_the_organization_identity_is_accepted_by_the_persistent_path(db, owner, tenant_a):
    identity = org_identity(tenant_a)
    assert (identity.origin, identity.organization_id) == ("organization", tenant_a.organization_id)
    record = _write(tenant_a, _local_dataset(identity), identity)
    assert record.status == "completed" and _snapshot_count(owner, tenant_a) == 1
