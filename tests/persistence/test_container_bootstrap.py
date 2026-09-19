"""Bootstrap PostgreSQL du conteneur et verifications du smoke test, sur une VRAIE base (Mission 004.3.8).

Docker n'est pas requis ici. Ce module prouve, avec le serveur de la suite:

- `docker/postgres/10-mervio-bootstrap.sql`, execute par psql comme le fait l'image postgres
  (`psql -v ON_ERROR_STOP=1 -f`), cree des roles non privilegies et une base possedee par le role
  de migration, est rejouable, et refuse une configuration incomplete ou dangereuse;
- les migrations passent avec ce role de migration (sans CREATEROLE);
- les verifications SQL et `mervio admin` de `scripts/container_smoke.py` sont justes: elles sont
  executees telles quelles, les commandes `docker compose exec postgres psql` et
  `docker compose run admin` etant traduites en `psql -h` et `python -m mervio.cli admin` locaux,
  avec un vrai processus `mervio worker` sous le role worker du bootstrap.

Le vrai parcours en conteneurs (image, compose, healthcheck Docker, SIGTERM via `docker stop`) est
execute par le job `docker` de la CI.

Noms jetables `mervio_test_boot_<aleatoire>_*`, supprimes a la fin, meme en cas d'echec.
"""
from __future__ import annotations

import importlib.util
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from mervio.persistence import migrate
from mervio.workers import runtime as worker_runtime

from .persistence_support import TEST_MASTER_KEY_HEX
from .worker_support import wait_until

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "docker" / "postgres" / "10-mervio-bootstrap.sql"
KINDS = ("migrator", "app", "worker")
FAST = {
    "MERVIO_WORKER_POLL_INTERVAL_SECONDS": "0.05", "MERVIO_WORKER_POLL_MAX_SECONDS": "0.2",
    "MERVIO_WORKER_RECOVERY_INTERVAL_SECONDS": "1", "MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS": "5",
    # 004.4.2: cle maitre d'identite factice (obligatoire des que le worker traite des imports)
    "MERVIO_IDENTITY_MASTER_KEY": TEST_MASTER_KEY_HEX,
}


def load_smoke():
    spec = importlib.util.spec_from_file_location("mervio_container_smoke_db", ROOT / "scripts" / "container_smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def psql_binary() -> str:
    binary = shutil.which("psql")
    assert binary, "psql (client PostgreSQL >= 15) requis pour executer le bootstrap comme l'image postgres"
    return binary


def clean_environment(**variables) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MERVIO_")}
    env.update({key: str(value) for key, value in variables.items()})
    return env


class Bootstrap:
    """Une base et trois roles jetables, crees par le script de bootstrap."""

    def __init__(self, pg) -> None:
        self.pg = pg
        suffix = secrets.token_hex(5)
        self.database = f"mervio_test_boot_{suffix}"
        self.roles = {kind: f"mervio_test_boot_{suffix}_{kind}" for kind in KINDS}
        self.passwords = {kind: secrets.token_hex(24) for kind in KINDS}
        params = conninfo_to_dict(pg.admin_conninfo)
        self.superuser = params.get("user") or os.environ.get("PGUSER") or ""
        self.superuser_password = params.get("password") or os.environ.get("PGPASSWORD")

    def variables(self, **overrides) -> dict:
        values = {"MERVIO_BOOTSTRAP_DATABASE": self.database}
        for kind in KINDS:
            values[f"MERVIO_BOOTSTRAP_{kind.upper()}_ROLE"] = self.roles[kind]
            values[f"MERVIO_BOOTSTRAP_{kind.upper()}_PASSWORD"] = self.passwords[kind]
        values.update(overrides)
        return {key: value for key, value in values.items() if value is not None}

    def run(self, **overrides) -> subprocess.CompletedProcess:
        env = clean_environment(**self.variables(**overrides))
        return subprocess.run([psql_binary(), "-X", "-v", "ON_ERROR_STOP=1", "--no-psqlrc", "-d",
                               self.pg.admin_conninfo, "-f", str(BOOTSTRAP)],
                              env=env, capture_output=True, text=True, timeout=120)

    def url(self, kind: str) -> str:
        host = self.pg.host if ":" not in self.pg.host else f"[{self.pg.host}]"
        return (f"postgresql://{self.roles[kind]}:{quote(self.passwords[kind], safe='')}@{host}:{self.pg.port}/"
                f"{self.database}")

    def superuser_url(self) -> str:
        host = self.pg.host if ":" not in self.pg.host else f"[{self.pg.host}]"
        secret = f":{quote(self.superuser_password, safe='')}" if self.superuser_password else ""
        return f"postgresql://{quote(self.superuser, safe='')}{secret}@{host}:{self.pg.port}/{self.database}"

    def query(self, statement: str, params=()) -> list:
        with self.pg.admin() as conn:
            return conn.execute(statement, params).fetchall()

    def database_exists(self) -> bool:
        return bool(self.query("SELECT 1 FROM pg_database WHERE datname = %s", (self.database,)))

    def drop(self) -> None:
        with self.pg.admin() as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.database)))
            for role in self.roles.values():
                assert role.startswith("mervio_test_boot_"), role
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture
def boot(pg):
    bootstrap = Bootstrap(pg)
    try:
        yield bootstrap
    finally:
        bootstrap.drop()


def published(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


# -- bootstrap ---------------------------------------------------------------------------------

def test_the_bootstrap_creates_unprivileged_roles_and_an_owned_database_and_is_replayable(boot):
    first = boot.run()
    assert first.returncode == 0, published(first)
    assert "mervio bootstrap: ok" in first.stdout
    second = boot.run()
    assert second.returncode == 0, published(second)

    rows = boot.query(
        "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
        "FROM pg_roles WHERE rolname = ANY(%s)", (list(boot.roles.values()),))
    assert {row[0]: row[1:] for row in rows} == {
        role: (True, False, False, False, False, False) for role in boot.roles.values()}
    for group in ("mervio_app", "mervio_worker"):
        assert boot.query("SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
                          (group,)) == [(False, False, False)]
    memberships = boot.query(
        "SELECT m.rolname, g.rolname FROM pg_auth_members a JOIN pg_roles m ON m.oid = a.member "
        "JOIN pg_roles g ON g.oid = a.roleid WHERE m.rolname = ANY(%s) ORDER BY 1, 2",
        (list(boot.roles.values()),))
    assert memberships == sorted([(boot.roles["app"], "mervio_app"), (boot.roles["worker"], "mervio_app"),
                                  (boot.roles["worker"], "mervio_worker")])
    assert boot.query(
        "SELECT pg_get_userbyid(datdba), pg_encoding_to_char(encoding), datcollate, "
        "has_database_privilege('public', oid, 'CONNECT'), has_database_privilege('public', oid, 'TEMPORARY'), "
        "has_database_privilege(%s, oid, 'CONNECT'), has_database_privilege(%s, oid, 'CONNECT') "
        "FROM pg_database WHERE datname = %s", (boot.roles["app"], boot.roles["worker"], boot.database)) == [
        (boot.roles["migrator"], "UTF8", "C", False, False, True, True)]


def test_nothing_secret_is_published_by_the_bootstrap(boot):
    result = boot.run()
    assert result.returncode == 0, published(result)
    assert published(result).strip() == "mervio bootstrap: ok"
    for secret in boot.passwords.values():
        assert secret not in published(result)


@pytest.mark.parametrize("missing", ["MERVIO_BOOTSTRAP_DATABASE", "MERVIO_BOOTSTRAP_WORKER_ROLE",
                                     "MERVIO_BOOTSTRAP_APP_PASSWORD"])
def test_an_incomplete_configuration_is_refused_before_any_change(boot, missing):
    result = boot.run(**{missing: None})
    assert result.returncode != 0
    assert "configuration incomplete" in published(result)
    assert not boot.database_exists()
    assert boot.query("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (list(boot.roles.values()),)) == [(0,)]


@pytest.mark.parametrize("override, message", [
    ({"MERVIO_BOOTSTRAP_MIGRATOR_PASSWORD": "short"}, "invalid password"),
    ({"MERVIO_BOOTSTRAP_APP_PASSWORD": "x" * 20 + "@:/"}, "invalid password"),
    ({"MERVIO_BOOTSTRAP_WORKER_PASSWORD": ""}, "invalid password"),
    ({"MERVIO_BOOTSTRAP_APP_ROLE": "mervio_worker"}, "invalid name"),
    ({"MERVIO_BOOTSTRAP_DATABASE": "Robert'); DROP TABLE x; --"}, "invalid name"),
    ({"MERVIO_BOOTSTRAP_WORKER_ROLE": "pg_signal_backend"}, "invalid name"),
])
def test_invalid_names_and_passwords_are_refused_before_any_change(boot, override, message):
    result = boot.run(**override)
    assert result.returncode != 0
    assert message in published(result)
    assert not boot.database_exists()
    assert boot.query("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (list(boot.roles.values()),)) == [(0,)]
    for secret in boot.passwords.values():
        assert secret not in published(result)


def test_two_login_roles_cannot_share_a_name(boot):
    result = boot.run(MERVIO_BOOTSTRAP_WORKER_ROLE=boot.roles["app"])
    assert result.returncode != 0 and "invalid name" in published(result)


def test_an_existing_privileged_role_stops_the_bootstrap_before_the_database_is_created(boot):
    with boot.pg.admin() as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN CREATEDB").format(sql.Identifier(boot.roles["app"])))
    result = boot.run()
    assert result.returncode != 0
    assert "unsafe existing role" in published(result)
    assert not boot.database_exists()


# -- migrations et verifications du smoke test --------------------------------------------------

@pytest.fixture
def smoke(boot, monkeypatch, tmp_path):
    """Le smoke test du conteneur, branche sur la base jetable et sur les commandes locales."""
    result = boot.run()
    assert result.returncode == 0, published(result)
    module = load_smoke()
    monkeypatch.setattr(module, "DATABASE", boot.database)
    monkeypatch.setattr(module, "SUPERUSER", boot.superuser)
    monkeypatch.setattr(module, "MIGRATOR_ROLE", boot.roles["migrator"])
    monkeypatch.setattr(module, "APP_ROLE", boot.roles["app"])
    monkeypatch.setattr(module, "WORKER_ROLE", boot.roles["worker"])
    monkeypatch.setattr(module, "DATA_DIR", str(tmp_path / "demo-data"))
    passwords_by_role = {boot.roles[kind]: boot.passwords[kind] for kind in KINDS}
    commands = []

    def local(argv, *, cwd=None, env=None, input=None, capture_output=True, text=True, timeout=None):
        assert argv[:2] == ["docker", "compose"], argv
        rest = argv[argv.index("--profile") + 2:]
        commands.append(rest[:4])
        if rest[:4] == ["exec", "-T", "postgres", "psql"]:
            arguments = rest[4:]
            role = arguments[arguments.index("-U") + 1]
            environment = clean_environment()
            password = passwords_by_role.get(role, boot.superuser_password)
            if password:
                environment["PGPASSWORD"] = password
            return subprocess.run([psql_binary(), "-h", boot.pg.host, "-p", boot.pg.port, *arguments],
                                  env=environment, input=input, capture_output=True, text=True, timeout=timeout)
        if rest[:4] == ["run", "--rm", "-T", "admin"]:
            return subprocess.run([sys.executable, "-m", "mervio.cli", "admin", *rest[4:]], cwd=ROOT,
                                  env=clean_environment(PYTHONPATH=ROOT / "src", MERVIO_DATABASE_URL=boot.url("app")),
                                  input=input, capture_output=True, text=True, timeout=timeout)
        raise AssertionError(f"commande non prevue hors conteneur: {rest}")

    instance = module.Smoke(image="mervio:unused", build=False, keep=True, runner=local, log=lambda _: None,
                            passwords={"MERVIO_POSTGRES_PASSWORD": boot.superuser_password or "",
                                       **{f"MERVIO_{kind.upper()}_DB_PASSWORD": boot.passwords[kind]
                                          for kind in KINDS}},
                            poll_seconds=0.3)
    instance.commands = commands
    instance.module = module
    return instance


class LocalWorker:
    def __init__(self, url: str, tmp_path: Path) -> None:
        self.health = tmp_path / "worker-health.json"
        self.log_path = tmp_path / "worker.log"
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "mervio.cli", "worker"], cwd=ROOT, stdout=self._log, stderr=subprocess.STDOUT,
            env=clean_environment(PYTHONPATH=ROOT / "src", MERVIO_DATABASE_URL=url, MERVIO_WORKER_NAME="container",
                                  MERVIO_WORKER_HEALTH_FILE=self.health, MERVIO_LOG_FORMAT="json", **FAST))

    def state(self):
        try:
            return json.loads(self.health.read_text(encoding="utf-8"))["state"]
        except (OSError, ValueError):
            return None

    def logs(self) -> str:
        if not self._log.closed:
            self._log.flush()
        return self.log_path.read_text(encoding="utf-8")

    def stop(self) -> int:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self._log.close()
        return self.process.returncode


def test_migrations_run_under_the_bootstrap_migrator_without_createrole(smoke, boot):
    process = subprocess.run([sys.executable, "-m", "mervio.persistence", "upgrade"], cwd=ROOT,
                             env=clean_environment(PYTHONPATH=ROOT / "src",
                                                   MERVIO_MIGRATION_DATABASE_URL=boot.url("migrator")),
                             capture_output=True, text=True, timeout=300)
    assert process.returncode == 0, published(process)
    assert migrate.current_revision(boot.url("migrator")) == migrate.head_revision()
    smoke.check_bootstrap()
    smoke.check_schema(migrate.head_revision())
    for secret in boot.passwords.values():
        assert secret not in published(process)


def test_the_smoke_checks_pass_on_a_real_database_with_a_real_worker(smoke, boot, tmp_path):
    migrate.upgrade(boot.url("migrator"))
    smoke.check_bootstrap()
    smoke.check_schema(migrate.head_revision())

    # un superutilisateur est refuse par le worker, sans rien enregistrer
    refused = subprocess.run([sys.executable, "-m", "mervio.cli", "worker"], cwd=ROOT,
                             env=clean_environment(PYTHONPATH=ROOT / "src", MERVIO_DATABASE_URL=boot.superuser_url(),
                                                   **FAST),
                             capture_output=True, text=True, timeout=120)
    assert refused.returncode == worker_runtime.EXIT_CONFIG, published(refused)
    assert "worker.identity_refused" in published(refused)
    assert smoke.scalar("SELECT count(*) FROM users WHERE kind = 'service'") == "0"

    worker = LocalWorker(boot.url("worker"), tmp_path)
    try:
        wait_until(lambda: worker.state() == "ready" or worker.process.poll() is not None, timeout=60)
        assert worker.state() == "ready", worker.logs()
        principal = smoke.check_service_identity(worker.logs())
        demo = smoke.demonstrate(principal)
        smoke.check_isolation(demo)
    finally:
        code = worker.stop()
    assert code == worker_runtime.EXIT_OK, worker.logs()
    smoke.check_stopped(worker.logs())
    smoke.check_transcript(worker.logs())
    assert {tuple(command[:4]) for command in smoke.commands} == {
        ("exec", "-T", "postgres", "psql"), ("run", "--rm", "-T", "admin")}


def test_the_smoke_checks_detect_a_broken_database(smoke, boot):
    """Les verifications ne sont pas complaisantes: un role privilegie ou une RLS retiree echouent."""
    migrate.upgrade(boot.url("migrator"))
    worker_role = sql.Identifier(boot.roles["worker"])
    with boot.pg.admin() as conn:
        conn.execute(sql.SQL("ALTER ROLE {} BYPASSRLS").format(worker_role))
    with pytest.raises(smoke.module.SmokeFailure, match="privilegie"):
        smoke.check_bootstrap()
    with boot.pg.admin() as conn:
        conn.execute(sql.SQL("ALTER ROLE {} NOBYPASSRLS").format(worker_role))
    smoke.check_bootstrap()
    with psycopg.connect(boot.pg.admin_conninfo, dbname=boot.database, autocommit=True) as target:
        target.execute("ALTER TABLE stores NO FORCE ROW LEVEL SECURITY")
    with pytest.raises(smoke.module.SmokeFailure, match="RLS forcee"):
        smoke.check_schema(migrate.head_revision())


# -- credentials, schema non migre, contrat de l'identite (analyse de l'echec CI de ec7ce24) ----

def scram_matches(stored: str, password: str) -> bool:
    """Verifie un secret SCRAM-SHA-256 de pg_authid (RFC 7677), sans passer par pg_hba."""
    import base64
    import hashlib
    import hmac
    method, rest = stored.split("$", 1)
    assert method == "SCRAM-SHA-256", method
    iterations_salt, keys = rest.split("$", 1)
    iterations, salt = iterations_salt.split(":", 1)
    stored_key, server_key = (base64.b64decode(part) for part in keys.split(":", 1))
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    return (hmac.compare_digest(hashlib.sha256(client_key).digest(), stored_key)
            and hmac.compare_digest(hmac.new(salted, b"Server Key", hashlib.sha256).digest(), server_key))


def test_each_login_role_stores_exactly_the_password_it_was_given(boot):
    """Les identifiants du bootstrap sont ceux que compose donne au worker et a l'admin (meme variable).

    Verifie le secret stocke, et non une connexion: sur un serveur local en `trust`, une connexion
    reussirait meme avec un mauvais mot de passe.
    """
    result = boot.run()
    assert result.returncode == 0, published(result)
    stored = dict(boot.query("SELECT rolname, rolpassword FROM pg_authid WHERE rolname = ANY(%s)",
                             (list(boot.roles.values()),)))
    for kind in KINDS:
        secret = stored[boot.roles[kind]]
        assert secret.startswith("SCRAM-SHA-256$"), "jamais de mot de passe en clair ni md5"
        assert scram_matches(secret, boot.passwords[kind]), kind
        for other in KINDS:
            if other != kind:
                assert not scram_matches(secret, boot.passwords[other]), (kind, other)
        assert not scram_matches(secret, secrets.token_hex(24))


def test_login_roles_really_connect_and_are_unprivileged_runtime_roles(boot):
    assert boot.run().returncode == 0
    for kind in KINDS:
        with psycopg.connect(boot.url(kind), autocommit=True) as conn:
            row = conn.execute("SELECT current_user, current_database(), r.rolsuper, r.rolbypassrls, "
                               "r.rolcreaterole, r.rolcreatedb, pg_has_role(current_user, 'mervio_worker', 'MEMBER') "
                               "FROM pg_roles r WHERE r.rolname = current_user").fetchone()
        assert row == (boot.roles[kind], boot.database, False, False, False, False, kind == "worker"), kind


def test_before_migrations_the_worker_refuses_the_schema_and_creates_nothing(boot, tmp_path):
    """Le message `app_ensure_service_principal() does not exist` du log CI est cette etape VOULUE."""
    assert boot.run().returncode == 0
    process = subprocess.run([sys.executable, "-m", "mervio.cli", "worker"], cwd=ROOT,
                             env=clean_environment(PYTHONPATH=ROOT / "src", MERVIO_DATABASE_URL=boot.url("worker"),
                                                   MERVIO_WORKER_HEALTH_FILE=tmp_path / "health.json", **FAST),
                             capture_output=True, text=True, timeout=120)
    assert process.returncode == worker_runtime.EXIT_SCHEMA, published(process)
    with psycopg.connect(boot.pg.admin_conninfo, dbname=boot.database, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = 'public'").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM pg_proc WHERE proname = 'app_ensure_service_principal'"
                            ).fetchone() == (0,)
    for secret in boot.passwords.values():
        assert secret not in published(process)


def test_migration_head_provides_the_service_identity_function_under_the_migrator(boot):
    assert boot.run().returncode == 0
    migrate.upgrade(boot.url("migrator"))
    with psycopg.connect(boot.pg.admin_conninfo, dbname=boot.database, autocommit=True) as conn:
        owner, definer = conn.execute(
            "SELECT pg_get_userbyid(proowner), prosecdef FROM pg_proc WHERE proname = 'app_ensure_service_principal'"
        ).fetchone()
        grants = conn.execute(
            "SELECT has_function_privilege(%s, 'app_ensure_service_principal()', 'EXECUTE'), "
            "has_function_privilege(%s, 'app_ensure_service_principal()', 'EXECUTE'), "
            "has_function_privilege('public', 'app_ensure_service_principal()', 'EXECUTE')",
            (boot.roles["worker"], boot.roles["app"])).fetchone()
    assert (owner, definer) == (boot.roles["migrator"], True)
    assert grants == (True, False, False)
    with psycopg.connect(boot.url("worker"), autocommit=True) as conn:
        principal = conn.execute("SELECT app_ensure_service_principal()").fetchone()[0]
        assert conn.execute("SELECT idp_subject, kind FROM users WHERE id = %s", (principal,)).fetchone() == (
            f"service:{boot.roles['worker']}", "service")
    with psycopg.connect(boot.url("app"), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT app_ensure_service_principal()")
