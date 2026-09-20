"""Infrastructure des tests PostgreSQL (Mission 004.1).

Securite:
- les tests n'utilisent QUE `MERVIO_TEST_ADMIN_DATABASE_URL`, explicitement dediee
  aux tests (aucune valeur par defaut, jamais MERVIO_DATABASE_URL);
- hote local obligatoire (127.0.0.1, localhost, ::1 ou socket), sauf
  MERVIO_TEST_ALLOW_REMOTE_DATABASE=1;
- chaque session cree SA base `mervio_test_<aleatoire>` et SES roles
  `mervio_test_*_<meme aleatoire>`, puis ne supprime que ceux-la. Aucune base
  ou role existant n'est jamais modifie (sauf le role de groupe partage mervio_app,
  cree par la migration s'il manque, jamais supprime).

Roles d'une session:
- migrator : proprietaire de la base et du schema, NON superutilisateur;
- app      : LOGIN, membre de mervio_app, ni superutilisateur ni BYPASSRLS (role applicatif reel);
- bypass   : BYPASSRLS, membre de mervio_app; sert uniquement a prouver la barriere applicative seule;
- worker, worker2 (004.3): LOGIN, membres de mervio_app ET mervio_worker, ni superutilisateur ni
  BYPASSRLS; deux services distincts, donc deux principaux `service:<role>`.

Sans MERVIO_TEST_ADMIN_DATABASE_URL, les tests PostgreSQL sont ignores, sauf si
MERVIO_REQUIRE_DATABASE_TESTS=1 (CI): ils echouent alors.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from urllib.parse import quote

import pytest

if os.environ.get("MERVIO_REQUIRE_DATABASE_TESTS") == "1":
    # 004.3: base obligatoire -> pilote absent = erreur de collecte, jamais un repertoire ignore
    import psycopg
else:
    psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict  # noqa: E402

ADMIN_URL_ENV = "MERVIO_TEST_ADMIN_DATABASE_URL"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", ""}
TABLES = ("service_authorizations", "audit_events", "jobs", "raw_objects", "reports", "analysis_runs",
          "ad_daily_performance", "campaigns", "refunds", "payments", "order_lines", "orders", "products",
          "snapshot_sources",
          "data_snapshots", "connections", "stores", "memberships", "users", "organization_identity_keys",
          "organizations")


@dataclass
class PgCluster:
    admin_conninfo: str
    host: str
    port: str
    database: str
    suffix: str
    passwords: dict

    def role(self, kind: str) -> str:
        return f"mervio_test_{kind}_{self.suffix}"

    def url(self, kind: str, database: str | None = None) -> str:
        host = self.host if ":" not in self.host else f"[{self.host}]"
        return (f"postgresql://{self.role(kind)}:{quote(self.passwords[kind], safe='')}@{host}:{self.port}/"
                f"{database or self.database}")

    def admin(self):
        return psycopg.connect(self.admin_conninfo, autocommit=True)

    def create_empty_database(self, label: str) -> str:
        name = f"mervio_test_{self.suffix}_{label}"
        with self.admin() as conn:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(self.role("migrator"))))
        return name

    def drop_database(self, name: str) -> None:
        assert name.startswith(f"mervio_test_{self.suffix}"), "refus de supprimer une base non creee par cette session"
        with self.admin() as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _admin_conninfo() -> str | None:
    url = os.environ.get(ADMIN_URL_ENV)
    if not url:
        if os.environ.get("MERVIO_REQUIRE_DATABASE_TESTS") == "1":
            pytest.fail(f"{ADMIN_URL_ENV} requise (MERVIO_REQUIRE_DATABASE_TESTS=1)")
        return None
    return url


@pytest.fixture(scope="session")
def pg() -> PgCluster:
    admin = _admin_conninfo()
    if admin is None:
        pytest.skip(f"{ADMIN_URL_ENV} non definie: tests PostgreSQL ignores")
    params = conninfo_to_dict(admin)
    host = params.get("host", "")
    if (host not in LOCAL_HOSTS and not host.startswith("/")
            and os.environ.get("MERVIO_TEST_ALLOW_REMOTE_DATABASE") != "1"):
        pytest.fail(f"{ADMIN_URL_ENV} pointe vers un hote non local ({host}): refus par securite")

    suffix = secrets.token_hex(5)
    passwords = {kind: secrets.token_hex(16) for kind in ("migrator", "app", "bypass", "worker", "worker2")}
    cluster = PgCluster(admin, host or "127.0.0.1", str(params.get("port", "5432")), f"mervio_test_{suffix}",
                        suffix, passwords)
    from mervio.persistence import migrate

    with cluster.admin() as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS CREATEROLE PASSWORD {}").format(
            sql.Identifier(cluster.role("migrator")), sql.Literal(passwords["migrator"])))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(cluster.database), sql.Identifier(cluster.role("migrator"))))
    try:
        migrate.upgrade(cluster.url("migrator"))
        with cluster.admin() as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {} IN ROLE mervio_app").format(
                sql.Identifier(cluster.role("app")), sql.Literal(passwords["app"])))
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER BYPASSRLS PASSWORD {} IN ROLE mervio_app").format(
                sql.Identifier(cluster.role("bypass")), sql.Literal(passwords["bypass"])))
            for kind in ("worker", "worker2"):
                conn.execute(sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {} IN ROLE mervio_app, mervio_worker").format(
                    sql.Identifier(cluster.role(kind)), sql.Literal(passwords[kind])))
        yield cluster
    finally:
        cluster.drop_database(cluster.database)
        with cluster.admin() as conn:
            for kind in ("worker2", "worker", "bypass", "app", "migrator"):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(cluster.role(kind))))


@pytest.fixture
def clean(pg):
    """Base vide avant chaque test (TRUNCATE par le proprietaire: ni RLS ni trigger DELETE)."""
    with psycopg.connect(pg.url("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE " + ", ".join(TABLES))
    yield pg


@pytest.fixture
def db(clean):
    from mervio.persistence.database import Database
    database = Database(clean.url("app"))
    yield database
    database.close()


@pytest.fixture
def owner(clean):
    """Connexion proprietaire (migrations, purge): soumise a RLS FORCEE, non superutilisateur."""
    with psycopg.connect(clean.url("migrator"), autocommit=True) as conn:
        yield conn


@pytest.fixture
def app_conn(clean):
    """Connexion SQL brute du role applicatif, pour tester RLS sans passer par les depots."""
    with psycopg.connect(clean.url("app"), autocommit=True) as conn:
        yield conn


# -- donnees ---------------------------------------------------------------------------------

from .persistence_support import SYNTHETIC_CONFIG, make_tenant  # noqa: E402


@pytest.fixture(scope="session")
def synthetic_set(tmp_path_factory):
    """Jeu SYNTHETIQUE deterministe (mervio.synthetic), genere une fois par session."""
    from mervio.synthetic import GeneratorConfig, generate_dataset
    directory = tmp_path_factory.mktemp("synthetic") / "set"
    return generate_dataset(GeneratorConfig(**SYNTHETIC_CONFIG), directory)


@pytest.fixture
def tenant_a(db):
    return make_tenant(db, "A")


@pytest.fixture
def tenant_b(db):
    return make_tenant(db, "B")


# -- services (004.3) ------------------------------------------------------------------------

@pytest.fixture
def worker_db(db, pg):
    """Connexion du role worker (membre de mervio_worker). Depend de `db`: base deja videe."""
    from mervio.persistence.database import Database
    database = Database(pg.url("worker"))
    yield database
    database.close()


@pytest.fixture
def worker2_db(db, pg):
    from mervio.persistence.database import Database
    database = Database(pg.url("worker2"))
    yield database
    database.close()


@pytest.fixture
def principal(worker_db):
    from mervio.persistence.service import service_principal
    return service_principal(worker_db)


@pytest.fixture
def principal2(worker2_db):
    from mervio.persistence.service import service_principal
    return service_principal(worker2_db)


@pytest.fixture
def worker_conn(worker_db, pg):
    """SQL brut sous le role worker."""
    with psycopg.connect(pg.url("worker"), autocommit=True) as conn:
        yield conn
