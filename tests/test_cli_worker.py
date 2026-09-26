"""Commandes `mervio worker` et `mervio worker healthcheck` sans base de donnees (Mission 004.3.6).

Le processus complet contre PostgreSQL est couvert par tests/persistence/test_cli_worker_process.py.
"""
from __future__ import annotations

import io
import json
import logging
import os
import secrets
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from mervio.cli import build_parser, main
from mervio.cli import worker as cli_worker
from mervio.observability.health import HEALTH_SCHEMA_VERSION, HealthSnapshot, WorkerState, write_health
from mervio.settings import HealthCheckSettings, SettingsError, WorkerSettings

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000.0
SECRET = "Zq9-cli-s3cr3t"
ORGANIZATION = "3f0c7c1e-8a55-4d7e-9d0b-6a1f2e0b9c11"


@pytest.fixture
def restore_logging():
    root = logging.getLogger("mervio")
    state = (list(root.handlers), root.propagate, root.level)
    yield
    root.handlers, root.propagate, root.level = state


def snapshot(**changes):
    base = HealthSnapshot(version=HEALTH_SCHEMA_VERSION, state="ready", updated_at=NOW, started_at=NOW - 100,
                          pid=42, worker_id="host/42/abcd1234", jobs_processed=3, last_database_ok_at=NOW,
                          degraded_since=None)
    return replace(base, **changes)


def healthcheck(*argv, environ=None, now=NOW):
    args = build_parser().parse_args(["worker", "healthcheck", *argv])
    out = io.StringIO()
    code = cli_worker.cmd_healthcheck(args, environ={} if environ is None else environ, now=now, stream=out)
    return code, out.getvalue()


def check_file(tmp_path, *argv, now=NOW, **changes):
    path = tmp_path / "health.json"
    write_health(path, snapshot(**changes))
    code, output = healthcheck("--json", *argv, environ={"MERVIO_WORKER_HEALTH_FILE": str(path)}, now=now)
    assert str(tmp_path) not in output
    return code, json.loads(output)


# -- analyse des arguments ---------------------------------------------------------------------

def test_the_worker_command_is_exposed():
    args = build_parser().parse_args(["worker"])
    assert (args.command, args.worker_command) == ("worker", None)
    args = build_parser().parse_args(["worker", "healthcheck", "--ready", "--json", "--health-file", "/h.json"])
    assert (args.worker_command, args.ready, args.json, args.health_file) == ("healthcheck", True, True, "/h.json")


def test_the_worker_help_documents_the_exit_codes(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["worker", "--help"])
    assert raised.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    for code in ("0 arret propre", "1 erreur interne", "2 configuration", "3 base injoignable",
                 "4 schema non migre", "5 arret force"):
        assert code in text


def test_the_pyproject_script_points_to_the_cli():
    import tomllib
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"]["mervio"] == "mervio.cli:main"


def test_main_delegates_the_worker_command(monkeypatch):
    calls = []
    monkeypatch.setattr(sys.modules["mervio.cli.main"], "cmd_worker", lambda args: calls.append(args.command) or 7)
    assert main(["worker"]) == 7
    assert calls == ["worker"]


def test_the_run_command_goes_to_the_runtime_entry_point(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_worker, "run_worker", lambda: calls.append("run") or 0)
    assert main(["worker"]) == 0 and calls == ["run"]


# -- healthcheck: verdicts ---------------------------------------------------------------------

@pytest.mark.parametrize("state, live_code, ready_code, reason", [
    ("ready", 0, 0, "ok"),
    ("busy", 0, 0, "ok"),
    ("starting", 0, 1, "starting"),
    ("draining", 0, 1, "draining"),
    ("stopped", 1, 1, "stopped"),
    ("forced", 1, 1, "forced"),
])
def test_each_worker_state_gives_its_verdict(tmp_path, state, live_code, ready_code, reason):
    code, document = check_file(tmp_path, state=state)
    assert code == live_code
    assert document == {"check": "liveness", "healthy": live_code == 0, "live": live_code == 0,
                        "ready": ready_code == 0, "state": state, "reason": reason}
    code, document = check_file(tmp_path, "--ready", state=state)
    assert (code, document["check"], document["healthy"]) == (ready_code, "readiness", ready_code == 0)


def test_a_short_outage_is_alive_but_not_ready(tmp_path):
    changes = dict(state="degraded", degraded_since=NOW - 10)
    assert check_file(tmp_path, **changes) == (0, {"check": "liveness", "healthy": True, "live": True,
                                                   "ready": False, "state": "degraded", "reason": "degraded"})
    assert check_file(tmp_path, "--ready", **changes)[0] == 1


def test_a_long_outage_is_unhealthy(tmp_path):
    code, document = check_file(tmp_path, state="degraded", degraded_since=NOW - 121)
    assert (code, document["reason"]) == (1, "database_unavailable")


def test_the_degraded_tolerance_comes_from_the_environment(tmp_path):
    path = tmp_path / "health.json"
    write_health(path, snapshot(state="degraded", degraded_since=NOW - 30))
    environ = {"MERVIO_WORKER_HEALTH_FILE": str(path), "MERVIO_WORKER_DEGRADED_GRACE_SECONDS": "20"}
    code, output = healthcheck("--json", environ=environ)
    assert (code, json.loads(output)["reason"]) == (1, "database_unavailable")


def test_a_stale_document_is_unhealthy_even_when_ready(tmp_path):
    assert check_file(tmp_path, updated_at=NOW - 61)[1]["reason"] == "stale"
    assert check_file(tmp_path, updated_at=NOW - 60)[0] == 0
    path = tmp_path / "health.json"
    environ = {"MERVIO_WORKER_HEALTH_FILE": str(path), "MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS": "3600"}
    assert healthcheck(environ=environ)[0] == 0


def test_a_document_from_the_future_is_unhealthy(tmp_path):
    assert check_file(tmp_path, updated_at=NOW + 60)[1]["reason"] == "clock_skew"


def test_the_text_output_is_one_short_line(tmp_path):
    path = tmp_path / "health.json"
    write_health(path, snapshot(state="busy"))
    code, output = healthcheck("--ready", environ={"MERVIO_WORKER_HEALTH_FILE": str(path)})
    assert (code, output) == (0, "healthy check=readiness state=busy reason=ok\n")
    write_health(path, snapshot(state="forced"))
    assert healthcheck(environ={"MERVIO_WORKER_HEALTH_FILE": str(path)}) == (
        1, "unhealthy check=liveness state=forced reason=forced\n")


# -- healthcheck: anomalies --------------------------------------------------------------------

def test_a_missing_or_invalid_file_is_unhealthy_without_revealing_the_path(tmp_path):
    secret_dir = tmp_path / "alice-client-export"
    secret_dir.mkdir()
    path = secret_dir / "health.json"
    code, output = healthcheck("--json", environ={"MERVIO_WORKER_HEALTH_FILE": str(path)})
    assert (code, json.loads(output)["reason"]) == (1, "missing")
    path.write_text("{not json " + str(path))
    code, output = healthcheck("--json", environ={"MERVIO_WORKER_HEALTH_FILE": str(path)})
    assert (code, json.loads(output)["reason"]) == (1, "invalid")
    assert "alice" not in output and str(tmp_path) not in output


def test_without_a_configured_file_the_check_fails(tmp_path):
    code, output = healthcheck("--json")
    assert (code, json.loads(output)["reason"]) == (1, "not_configured")


def test_the_file_option_replaces_the_environment(tmp_path):
    good = tmp_path / "good.json"
    write_health(good, snapshot())
    environ = {"MERVIO_WORKER_HEALTH_FILE": str(tmp_path / "absent.json")}
    assert healthcheck("--health-file", str(good), environ=environ)[0] == 0


def test_a_relative_file_option_is_refused():
    code, output = healthcheck("--json", "--health-file", "health.json")
    assert code == 1
    assert json.loads(output) == {"check": "liveness", "healthy": False, "live": False, "ready": False,
                                  "state": None, "reason": "config", "problems": ["--health-file"]}


def test_an_invalid_health_configuration_fails_with_exit_code_one_only():
    """Convention HEALTHCHECK: 0 ou 1, jamais 2 (reserve), meme pour une configuration invalide."""
    environ = {"MERVIO_WORKER_HEALTH_FILE": "relative/" + SECRET, "MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS": "x",
               "MERVIO_WORKER_DEGRADED_GRACE_SECONDS": "0"}
    code, output = healthcheck("--json", environ=environ)
    document = json.loads(output)
    assert (code, document["reason"], document["healthy"]) == (1, "config", False)
    assert set(document["problems"]) == {"MERVIO_WORKER_HEALTH_FILE", "MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS",
                                         "MERVIO_WORKER_DEGRADED_GRACE_SECONDS"}
    assert SECRET not in output


def test_the_check_never_needs_the_database_url(tmp_path):
    path = tmp_path / "health.json"
    write_health(path, snapshot())
    environ = {"MERVIO_WORKER_HEALTH_FILE": str(path), "MERVIO_DATABASE_URL_FILE": "relative"}
    assert healthcheck(environ=environ)[0] == 0


def test_the_real_environment_is_read_by_default(tmp_path, monkeypatch, capsys):
    path = tmp_path / "health.json"
    write_health(path, snapshot(state="draining", updated_at=__import__("time").time()))
    monkeypatch.setenv("MERVIO_WORKER_HEALTH_FILE", str(path))
    assert main(["worker", "healthcheck"]) == 0
    assert main(["worker", "healthcheck", "--ready"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "healthy check=liveness state=draining reason=draining",
        "unhealthy check=readiness state=draining reason=draining"]


# -- configuration partagee --------------------------------------------------------------------

def test_the_health_settings_follow_the_worker_rules():
    environ = {"MERVIO_DATABASE_URL": "postgresql://w@db/mervio", "MERVIO_WORKER_HEALTH_FILE": "/run/h.json",
               "MERVIO_WORKER_POLL_MAX_SECONDS": "20", "MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS": "61",
               "MERVIO_WORKER_DEGRADED_GRACE_SECONDS": "30", "MERVIO_IDENTITY_MASTER_KEY": secrets.token_hex(32),
               "MERVIO_OBJECT_STORE_ROOT": "/var/lib/mervio/objects"}
    worker = WorkerSettings.from_env(environ)
    health = HealthCheckSettings.from_env(environ)
    assert (health.health_file, health.health_max_age_seconds, health.degraded_grace_seconds) == (
        worker.health_file, worker.health_max_age_seconds, worker.degraded_grace_seconds)
    environ["MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS"] = "59"
    for reader in (WorkerSettings.from_env, HealthCheckSettings.from_env):
        with pytest.raises(SettingsError) as raised:
            reader(environ)
        assert [name for name, _ in raised.value.problems] == ["MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS"]


def test_the_health_settings_defaults():
    health = HealthCheckSettings.from_env({})
    assert (health.health_file, health.health_max_age_seconds, health.degraded_grace_seconds) == (None, 60, 120)


# -- processus: refus avant tout demarrage -----------------------------------------------------

def run_refused(environ):
    stream = io.StringIO()
    code = cli_worker.run_worker(settings_loader=lambda: WorkerSettings.from_env(environ), stream=stream)
    return code, [json.loads(line) for line in stream.getvalue().splitlines()], stream.getvalue()


def test_an_invalid_configuration_exits_with_code_2_and_names_only_the_variables(restore_logging):
    environ = {"MERVIO_DATABASE_URL": f"mysql://svc:{SECRET}@db/x", "MERVIO_WORKER_DISPATCH_BATCH": "0",
               "MERVIO_ENV": "production", "MERVIO_LOG_FORMAT": "text",
               "MERVIO_WORKER_HEALTH_FILE": "var/run/" + ORGANIZATION}
    code, lines, raw = run_refused(environ)
    assert code == 2
    assert len(lines) == 1
    line = lines[0]
    assert (line["event"], line["level"], line["service"], line["exit_code"]) == (
        "worker.config_invalid", "ERROR", "worker", 2)
    # 004.4.2 et 004.4.4: cle maitre d'identite ET magasin d'objets sont obligatoires des que le
    # worker traite des imports (types par defaut)
    assert {problem["variable"] for problem in line["problems"]} == {
        "MERVIO_DATABASE_URL", "MERVIO_WORKER_DISPATCH_BATCH", "MERVIO_LOG_FORMAT", "MERVIO_WORKER_HEALTH_FILE",
        "MERVIO_IDENTITY_MASTER_KEY", "MERVIO_OBJECT_STORE_ROOT"}
    for leak in (SECRET, "mysql", ORGANIZATION, "svc"):
        assert leak not in raw


def test_a_missing_database_url_exits_with_code_2(restore_logging):
    code, lines, _ = run_refused({"MERVIO_IDENTITY_MASTER_KEY": secrets.token_hex(32),
                                  "MERVIO_OBJECT_STORE_ROOT": "/var/lib/mervio/objects"})
    assert code == 2
    assert lines[0]["problems"] == [{"variable": "MERVIO_DATABASE_URL", "rule": "obligatoire (aucune base par defaut)"}]


def test_a_missing_identity_master_key_exits_with_code_2_when_imports_are_served(restore_logging):
    code, lines, _ = run_refused({"MERVIO_DATABASE_URL": "postgresql://w@db/mervio",
                                  "MERVIO_OBJECT_STORE_ROOT": "/var/lib/mervio/objects"})
    assert code == 2
    assert lines[0]["problems"] == [
        {"variable": "MERVIO_IDENTITY_MASTER_KEY",
         "rule": "obligatoire pour les travaux import et pour resolve-customer-ref (cle maitre d'identite)"}]


def test_a_missing_object_store_exits_with_code_2_when_imports_are_served(restore_logging):
    """004.4.4: un worker qui sert des imports doit savoir OU lire les objets bruts (D-054)."""
    code, lines, _ = run_refused({"MERVIO_DATABASE_URL": "postgresql://w@db/mervio",
                                  "MERVIO_IDENTITY_MASTER_KEY": secrets.token_hex(32)})
    assert code == 2
    assert lines[0]["problems"] == [{"variable": "MERVIO_OBJECT_STORE_ROOT",
                                     "rule": "obligatoire pour le pilote filesystem"}]


def test_an_unreadable_secret_file_is_refused_without_its_path(tmp_path, restore_logging):
    code, _, raw = run_refused({"MERVIO_DATABASE_URL_FILE": str(tmp_path / "secret-db-url")})
    assert code == 2 and "secret-db-url" not in raw and str(tmp_path) not in raw


def test_an_unexpected_import_error_is_not_hidden(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def failing(name, *args, **kwargs):
        if name.endswith("workers.runtime"):
            raise ModuleNotFoundError("No module named 'somethingelse'", name="somethingelse")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing)
    with pytest.raises(ModuleNotFoundError):
        cli_worker.run_worker(settings_loader=lambda: pytest.fail("jamais lu"))


def run_python(code, **env):
    variables = {key: value for key, value in os.environ.items() if not key.startswith("MERVIO_")}
    variables.update({"PYTHONPATH": str(ROOT / "src"), **env})
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env=variables,
                          timeout=60)


def test_a_missing_database_driver_exits_with_code_2():
    result = run_python("import sys; sys.modules['psycopg'] = None\n"
                        "from mervio.cli import main; sys.exit(main(['worker']))",
                        MERVIO_DATABASE_URL=f"postgresql://svc:{SECRET}@127.0.0.1:1/x")
    assert result.returncode == 2, result.stderr
    line = json.loads(result.stderr.strip())
    assert (line["event"], line["dependency"]) == ("worker.dependency_missing", "psycopg")
    assert SECRET not in result.stderr and result.stdout == ""


def test_the_healthcheck_never_loads_the_database_layer(tmp_path):
    path = tmp_path / "health.json"
    write_health(path, snapshot(updated_at=__import__("time").time()))
    result = run_python(
        "import sys\nfrom mervio.cli import main\ncode = main(['worker', 'healthcheck', '--json'])\n"
        "loaded = [m for m in sys.modules if m.split('.')[0] in ('psycopg', 'sqlalchemy', 'alembic') "
        "or m.startswith(('mervio.persistence', 'mervio.workers'))]\n"
        "assert not loaded, loaded\nsys.exit(code)",
        MERVIO_WORKER_HEALTH_FILE=str(path))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == WorkerState.READY.value


def test_the_real_command_line_runs_as_a_module(tmp_path):
    variables = {key: value for key, value in os.environ.items() if not key.startswith("MERVIO_")}
    variables.update({"PYTHONPATH": str(ROOT / "src"), "MERVIO_WORKER_HEALTH_FILE": str(tmp_path / "none.json")})
    result = subprocess.run([sys.executable, "-m", "mervio.cli", "worker", "healthcheck"], cwd=ROOT,
                            capture_output=True, text=True, env=variables, timeout=60)
    assert (result.returncode, result.stdout) == (1, "unhealthy check=liveness state=- reason=missing\n")
    variables.pop("MERVIO_WORKER_HEALTH_FILE")
    result = subprocess.run([sys.executable, "-m", "mervio.cli", "worker"], cwd=ROOT,
                            capture_output=True, text=True, env=variables, timeout=60)
    assert result.returncode == 2
    assert json.loads(result.stderr)["event"] == "worker.config_invalid"


def test_the_cli_module_has_no_top_level_database_or_runtime_import():
    import ast
    tree = ast.parse((ROOT / "src" / "mervio" / "cli" / "worker.py").read_text(encoding="utf-8"))
    top_level = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    names = [(node.module or "") for node in top_level if isinstance(node, ast.ImportFrom)]
    names += [alias.name for node in top_level if isinstance(node, ast.Import) for alias in node.names]
    assert not any("workers" in name or "persistence" in name or name.startswith("psycopg") for name in names)
