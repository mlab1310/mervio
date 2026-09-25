"""Revision 0014 (Mission 004.4.5): effacement client controle.

Trois proprietes distinctes:
- la table, la colonne, la fonction privilegiee et le type de travail apparaissent a la montee
  et disparaissent a la descente, SANS residu (politiques, privileges, contraintes);
- les listes `CHECK` s'allongent puis se raccourcissent comme en 0008 et 0013: la descente
  CONSERVE les lignes deja ecrites et refuse seulement les nouvelles (`NOT VALID`);
- le declencheur d'immuabilite retrouve EXACTEMENT son comportement de 0002 a la descente:
  le relachement de D-052 ne survit pas a la revision qui l'introduit.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from mervio.persistence import migrate
from mervio.persistence.audit import Action, ResourceType
from mervio.persistence.jobs import JobType

PREVIOUS = "0013_raw_objects"
CURRENT = "0014_customer_erasure"
ERASURE_ACTIONS = ("customer.redacted",)
ERASURE_RESOURCES = ("customer_redaction",)


@pytest.fixture
def database(pg):
    name = pg.create_empty_database("customer_erasure")
    try:
        yield pg.url("migrator", name)
    finally:
        pg.drop_database(name)


def _organization(conn) -> uuid.UUID:
    organization_id = uuid.uuid4()
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
        conn.execute("INSERT INTO organizations (id, name) VALUES (%s, 'Migration')", (organization_id,))
    return organization_id


def _trace(conn, organization_id, action, resource_type) -> None:
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
        conn.execute(
            "INSERT INTO audit_events (id, organization_id, actor_type, action, resource_type, resource_id, "
            "correlation_id, outcome) VALUES (%s, %s, 'system', %s, %s, %s, %s, 'succeeded')",
            (uuid.uuid4(), organization_id, action, resource_type, uuid.uuid4(), uuid.uuid4()))


def _count(conn, organization_id, table="audit_events") -> int:
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _relations(conn, name: str) -> int:
    return conn.execute("SELECT count(*) FROM pg_tables WHERE tablename = %s", (name,)).fetchone()[0]


def _functions(conn, name: str) -> int:
    return conn.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'public' AND p.proname = %s", (name,)).fetchone()[0]


# -- vocabulaire -------------------------------------------------------------------------------

def test_the_python_vocabulary_matches_the_migration():
    """Le vocabulaire d'AUDIT est connu de Python: la trace ecrite par la fonction est lisible."""
    assert {a.value for a in Action} >= set(ERASURE_ACTIONS)
    assert {r.value for r in ResourceType} >= set(ERASURE_RESOURCES)


def test_the_job_type_is_declared_in_the_database_only_until_its_handler_exists():
    """`redact_customer` existe en base (E2) mais pas encore dans `JobType`: le depot exige que
    tout type declare ait un gestionnaire (`tests/test_jobs_unit.py`), et celui-ci est E4."""
    assert "redact_customer" not in {kind.value for kind in JobType}


def test_upgrade_accepts_erasure_events_and_downgrade_keeps_them_but_refuses_new_ones(database):
    migrate.upgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        organization_id = _organization(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            _trace(conn, organization_id, "customer.redacted", "customer_redaction")

    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        _trace(conn, organization_id, "customer.redacted", "customer_redaction")
        assert _count(conn, organization_id) == 1

    migrate.downgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        assert _count(conn, organization_id) == 1, "le journal est en ajout seul: rien n'est efface"
        with pytest.raises(psycopg.errors.CheckViolation):
            _trace(conn, organization_id, "customer.redacted", "customer_redaction")


def test_the_job_type_appears_and_is_refused_again_after_the_downgrade(database):
    migrate.upgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        organization_id = _organization(conn)

        def enqueue():
            with conn.transaction():
                conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
                conn.execute(
                    "INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, "
                    "available_at, payload) VALUES (%s, %s, 'redact_customer', 'queued', %s, now(), "
                    "'{}'::jsonb)", (uuid.uuid4(), organization_id, uuid.uuid4()))

        with pytest.raises(psycopg.errors.CheckViolation):
            enqueue()
        migrate.upgrade(database, CURRENT)
        enqueue()
        assert _count(conn, organization_id, "jobs") == 1

    with psycopg.connect(database, autocommit=True) as conn:
        # la ligne existante empeche une descente VALIDEE: c'est pourquoi elle est `NOT VALID`
        conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
        conn.execute("DELETE FROM customer_redactions")
    migrate.downgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        assert _count(conn, organization_id, "jobs") == 1, "le travail deja ecrit est conserve"
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
                conn.execute(
                    "INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, "
                    "available_at, payload) VALUES (%s, %s, 'redact_customer', 'queued', %s, now(), "
                    "'{}'::jsonb)", (uuid.uuid4(), organization_id, uuid.uuid4()))


# -- objets de la revision ----------------------------------------------------------------------

def test_the_revision_adds_exactly_one_table_one_column_and_one_definer(database):
    migrate.upgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        before = {r[0] for r in conn.execute(
            "SELECT proname FROM pg_proc WHERE prosecdef AND pronamespace = 'public'::regnamespace").fetchall()}
        assert before == {"app_ensure_service_principal"}
        assert _relations(conn, "customer_redactions") == 0
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = "
                            "'raw_objects' AND column_name = 'purge_reason'").fetchone() == (0,)

    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        after = {r[0] for r in conn.execute(
            "SELECT proname FROM pg_proc WHERE prosecdef AND pronamespace = 'public'::regnamespace").fetchall()}
        assert after == {"app_ensure_service_principal", "app_redact_customer"}
        assert _relations(conn, "customer_redactions") == 1
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = "
                            "'raw_objects' AND column_name = 'purge_reason'").fetchone() == (1,)


def test_the_revision_adds_no_role(database):
    """D-052 contre le roadmap: aucun `mervio_redactor`, aucun `mervio_purger`."""
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname IN "
                            "('mervio_redactor', 'mervio_purger')").fetchone() == (0,)


def test_the_proof_table_is_read_only_for_the_application_role(database):
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        granted = {r[0] for r in conn.execute(
            "SELECT privilege_type FROM information_schema.table_privileges "
            "WHERE table_name = 'customer_redactions' AND grantee = 'mervio_app'").fetchall()}
        assert granted == {"SELECT"}, granted


def test_the_proof_table_carries_forced_row_level_security(database):
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        assert conn.execute(
            "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
            "WHERE oid = 'public.customer_redactions'::regclass").fetchone() == (True,)


def test_the_downgrade_leaves_no_residue(database):
    migrate.upgrade(database, CURRENT)
    migrate.downgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        assert _relations(conn, "customer_redactions") == 0
        assert _functions(conn, "app_redact_customer") == 0
        assert conn.execute("SELECT count(*) FROM pg_policies WHERE tablename = "
                            "'customer_redactions'").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = "
                            "'raw_objects' AND column_name = 'purge_reason'").fetchone() == (0,)


# -- declencheur d'immuabilite --------------------------------------------------------------------

def test_the_immutability_trigger_is_restored_word_for_word_on_downgrade(database):
    """Le relachement de D-052 ne doit pas survivre a la revision qui l'introduit."""
    migrate.upgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        before = conn.execute("SELECT prosrc, proconfig FROM pg_proc "
                              "WHERE proname = 'canonical_rows_forbid_update'").fetchone()
    assert before[1] is None, "0002 n'epingle aucun search_path"

    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        relaxed = conn.execute("SELECT prosrc, proconfig FROM pg_proc "
                               "WHERE proname = 'canonical_rows_forbid_update'").fetchone()
    assert relaxed != before
    assert relaxed[1] == ["search_path=pg_catalog, public, pg_temp"]

    migrate.downgrade(database, PREVIOUS)
    with psycopg.connect(database, autocommit=True) as conn:
        restored = conn.execute("SELECT prosrc, proconfig FROM pg_proc "
                                "WHERE proname = 'canonical_rows_forbid_update'").fetchone()
    assert restored == before, "texte ET configuration identiques a 0002"


def test_the_trigger_still_guards_all_seven_canonical_tables_after_the_upgrade(database):
    migrate.upgrade(database, CURRENT)
    with psycopg.connect(database, autocommit=True) as conn:
        guarded = {r[0] for r in conn.execute(
            "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname LIKE '%_forbid_update' AND NOT t.tgisinternal").fetchall()}
    assert guarded >= {"products", "orders", "order_lines", "payments", "refunds", "campaigns",
                       "ad_daily_performance", "snapshot_sources"}


# -- aller-retour -----------------------------------------------------------------------------------

def test_revision_0014_round_trips_exactly(database):
    """Montee, descente, remontee: le schema revient a l'identique (colonnes et contraintes)."""
    def shape(url):
        with psycopg.connect(url) as conn:
            columns = conn.execute(
                "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' ORDER BY 1, 2").fetchall()
            checks = conn.execute(
                "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2").fetchall()
            policies = conn.execute(
                "SELECT tablename, policyname, qual FROM pg_policies ORDER BY 1, 2").fetchall()
        return columns, checks, policies

    migrate.upgrade(database, CURRENT)
    first = shape(database)
    migrate.downgrade(database, PREVIOUS)
    migrate.upgrade(database, CURRENT)
    assert shape(database) == first
