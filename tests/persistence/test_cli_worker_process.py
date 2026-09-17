"""`mervio worker` et `mervio worker healthcheck` en vrais sous-processus contre PostgreSQL (Mission 004.3.6).

Deux lanceurs:
- `python -m mervio.cli worker`: la commande du produit, gestionnaires reels;
- `python -m persistence.worker_launcher`: le MEME point d'entree (`cli.worker.run_worker`) avec le
  gestionnaire pilote des tests, pour tenir un travail en cours pendant les signaux.

Le runtime lui-meme (cycle de vie, bail, fencing) est couvert par test_worker_runtime.py: ici on
verifie le branchement de la commande, ses codes de sortie et ce qu'elle publie.
"""
from __future__ import annotations

import io
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mervio.cli import worker as cli_worker
from mervio.persistence import jobs, migrate
from mervio.persistence.service import authorize_service
from mervio.settings import WorkerSettings
from mervio.workers import runtime as worker_runtime

from .worker_support import enqueue_scripted, wait_until

ROOT = Path(__file__).resolve().parents[2]
HEALTH_KEYS = {"version", "state", "updated_at", "started_at", "pid", "worker_id", "jobs_processed",
               "last_database_ok_at", "degraded_since"}
FAST = {
    "MERVIO_WORKER_POLL_INTERVAL_SECONDS": "0.05", "MERVIO_WORKER_POLL_MAX_SECONDS": "0.2",
    "MERVIO_WORKER_RECOVERY_INTERVAL_SECONDS": "1", "MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS": "5",
}


@pytest.fixture
def authorized(tenant_a, principal):
    authorize_service(tenant_a.session, principal.id)
    return tenant_a


def base_environment(**env):
    variables = {key: value for key, value in os.environ.items() if not key.startswith("MERVIO_")}
    variables["PYTHONPATH"] = f"{ROOT / 'tests'}{os.pathsep}{ROOT / 'src'}"
    variables.update({key: str(value) for key, value in env.items()})
    return variables


def healthcheck(health, *argv):
    result = subprocess.run([sys.executable, "-m", "mervio.cli", "worker", "healthcheck", "--json", *argv],
                            cwd=ROOT, env=base_environment(MERVIO_WORKER_HEALTH_FILE=health),
                            capture_output=True, text=True, timeout=60)
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


class Command:
    """Un processus `mervio worker` (produit) ou le lanceur de test sur le meme point d'entree."""

    def __init__(self, pg, tmp_path, *, name="cli", kind="worker", scripted=False, database=None, **env):
        self.health = tmp_path / f"{name}-health.json"
        self.pg = pg
        variables = base_environment(**{
            "MERVIO_DATABASE_URL": pg.url(kind, database), "MERVIO_WORKER_NAME": name,
            "MERVIO_WORKER_HEALTH_FILE": self.health, **FAST, **env})
        command = (["-m", "persistence.worker_launcher"] if scripted else ["-m", "mervio.cli", "worker"])
        self.process = subprocess.Popen([sys.executable, *command], cwd=ROOT, env=variables,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.stdout = self.stderr = ""
        self.lines = []

    def state(self):
        try:
            return json.loads(self.health.read_text())["state"]
        except (OSError, ValueError):
            return None

    def wait_state(self, state, timeout=20):
        wait_until(lambda: self.state() == state or self.process.poll() is not None, timeout=timeout)
        if self.state() != state:
            self.finish(kill=True)
            raise AssertionError(f"etat {state} jamais atteint: {self.state()} {self.stderr[-2000:]}")

    def signal(self, signum):
        self.process.send_signal(signum)

    def finish(self, timeout=30, kill=False):
        if kill:
            self.process.kill()
        try:
            self.stdout, self.stderr = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.stdout, self.stderr = self.process.communicate()
            raise AssertionError("le processus ne s'arrete pas")
        self.lines = [json.loads(line) for line in self.stderr.splitlines() if line.startswith("{")]
        return self.process.returncode

    def events(self):
        return [line["event"] for line in self.lines]

    def assert_nothing_leaked(self, tmp_path, *health_only):
        """Aucun secret, DSN ni chemin nulle part; `health_only` (organisation...) absent du fichier de sante.

        Les logs d'un travail portent son organisation par contrat (004.2); le fichier de sante, jamais.
        """
        health = self.health.read_text()
        published = self.stdout + self.stderr + health
        for leak in (*self.pg.passwords.values(), str(tmp_path), "postgresql://"):
            assert leak not in published, leak
        for leak in health_only:
            assert leak not in health, leak
        assert set(json.loads(health)) == HEALTH_KEYS
        foreign = [line for line in self.lines if line.get("service") != "worker"]
        assert not foreign, foreign
        assert len(self.lines) == len([line for line in self.stderr.splitlines() if line.strip()]), \
            "chaque ligne du processus est un objet JSON"


# =============================================================================================
# La commande du produit
# =============================================================================================

def test_the_product_command_starts_is_healthy_and_stops_on_sigterm(pg, tmp_path, authorized, principal):
    command = Command(pg, tmp_path)
    command.wait_state("ready")
    assert healthcheck(command.health) == (0, {"check": "liveness", "healthy": True, "live": True,
                                               "ready": True, "state": "ready", "reason": "ok"})
    assert healthcheck(command.health, "--ready")[0] == 0
    command.signal(signal.SIGTERM)
    assert command.finish() == worker_runtime.EXIT_OK
    assert command.stdout == ""
    events = command.events()
    assert events[:2] == ["worker.starting", "worker.config"] and "worker.ready" in events
    stopping = next(line for line in command.lines if line["event"] == "worker.stopping")
    stopped = next(line for line in command.lines if line["event"] == "worker.stopped")
    assert stopping["reason"] == "SIGTERM"
    assert (stopped["exit_code"], stopped["state"], stopped["reason"]) == (0, "stopped", "stopped")
    connected = next(line for line in command.lines if line["event"] == "worker.database_connected")
    assert connected["principal_id"] == str(principal.id) and connected["role"] == pg.role("worker")
    # le champ `service` du contrat JSON n'est jamais ecrase par le nom du principal
    assert connected["principal_name"] == principal.name and connected["service"] == "worker"
    config = next(line for line in command.lines if line["event"] == "worker.config")
    assert config["health_check"] is True and "password" not in json.dumps(config).lower()
    assert healthcheck(command.health)[1]["reason"] == "stopped"
    assert healthcheck(command.health)[0] == 1
    command.assert_nothing_leaked(tmp_path, str(authorized.organization_id))


def test_the_product_command_stops_on_sigint(pg, tmp_path, authorized):
    command = Command(pg, tmp_path)
    command.wait_state("ready")
    command.signal(signal.SIGINT)
    assert command.finish() == worker_runtime.EXIT_OK
    assert next(line for line in command.lines if line["event"] == "worker.stopping")["reason"] == "SIGINT"
    assert "Traceback" not in command.stderr and "KeyboardInterrupt" not in command.stderr


def test_a_role_that_is_not_a_service_exits_with_code_2(pg, tmp_path, db):
    command = Command(pg, tmp_path, kind="app")
    assert command.finish() == worker_runtime.EXIT_CONFIG
    assert "worker.identity_refused" in command.events()
    stopped = next(line for line in command.lines if line["event"] == "worker.stopped")
    assert (stopped["exit_code"], stopped["reason"]) == (2, "config")
    assert healthcheck(command.health)[1]["reason"] == "stopped"
    command.assert_nothing_leaked(tmp_path)


def test_an_unreachable_database_exits_with_code_3(pg, tmp_path):
    url = pg.url("worker").replace(f":{pg.port}/", ":1/")
    variables = base_environment(MERVIO_DATABASE_URL=url, MERVIO_WORKER_HEALTH_FILE=tmp_path / "h.json",
                                 MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS="1")
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-m", "mervio.cli", "worker"], cwd=ROOT, env=variables,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == worker_runtime.EXIT_DATABASE
    assert time.monotonic() - started < 15
    lines = [json.loads(line) for line in result.stderr.splitlines()]
    assert any(line["event"] == "worker.database_unavailable" for line in lines)
    assert lines[-1]["event"] == "worker.stopped" and lines[-1]["reason"] == "database"
    assert pg.passwords["worker"] not in result.stderr and "Traceback" not in result.stderr


def test_an_outdated_schema_exits_with_code_4(pg, tmp_path, db):
    name = pg.create_empty_database("cli_schema")
    try:
        migrate.upgrade(pg.url("migrator", name), "0006_job_leases")
        command = Command(pg, tmp_path, database=name)
        assert command.finish() == worker_runtime.EXIT_SCHEMA
        assert command.lines[-1]["reason"] == "schema"
        command.assert_nothing_leaked(tmp_path)
    finally:
        pg.drop_database(name)


def test_an_invalid_configuration_exits_with_code_2_before_any_connection(pg, tmp_path, db):
    command = Command(pg, tmp_path, MERVIO_JOB_LEASE_SECONDS="5", MERVIO_ENV="production", MERVIO_LOG_LEVEL="DEBUG")
    assert command.finish() == worker_runtime.EXIT_CONFIG
    assert [line["event"] for line in command.lines] == ["worker.config_invalid"]
    assert {p["variable"] for p in command.lines[0]["problems"]} == {"MERVIO_JOB_LEASE_SECONDS", "MERVIO_LOG_LEVEL"}
    assert not command.health.exists(), "aucun demarrage: aucun fichier de sante"
    assert pg.passwords["worker"] not in command.stderr


# =============================================================================================
# Meme point d'entree, gestionnaire pilote: travaux en cours
# =============================================================================================

def test_busy_draining_and_stopped_are_visible_to_the_healthcheck(pg, tmp_path, authorized):
    job = enqueue_scripted(authorized, mode="checkpointed", seconds=2.5)
    command = Command(pg, tmp_path, scripted=True, MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS=30)
    command.wait_state("busy")
    assert healthcheck(command.health, "--ready") == (0, {"check": "readiness", "healthy": True, "live": True,
                                                          "ready": True, "state": "busy", "reason": "ok"})
    command.signal(signal.SIGTERM)
    command.wait_state("draining", timeout=5)
    assert healthcheck(command.health)[0] == 0
    code, document = healthcheck(command.health, "--ready")
    assert (code, document["state"], document["reason"]) == (1, "draining", "draining")
    assert command.finish() == worker_runtime.EXIT_OK
    stored = jobs.get_job(authorized.session, job.id)
    assert (stored.status, stored.attempts) == ("succeeded", 1)
    assert healthcheck(command.health)[1]["state"] == "stopped"
    job_lines = [line for line in command.lines if line.get("job_id") == str(job.id)]
    assert {"job.claimed", "job.succeeded"} <= {line["event"] for line in job_lines}
    assert {line.get("correlation_id") for line in job_lines} == {str(stored.correlation_id)}
    health = json.loads(command.health.read_text())
    assert health["jobs_processed"] == 1
    command.assert_nothing_leaked(tmp_path, str(authorized.organization_id))


def test_an_expired_grace_forces_the_stop_with_code_5(pg, tmp_path, authorized):
    job = enqueue_scripted(authorized, mode="checkpointed", seconds=30)
    command = Command(pg, tmp_path, scripted=True, MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS=1)
    command.wait_state("busy")
    command.signal(signal.SIGTERM)
    assert command.finish() == worker_runtime.EXIT_FORCED
    stored = jobs.get_job(authorized.session, job.id)
    assert (stored.status, stored.attempts, stored.last_error_code) == ("queued", 1, "worker_shutdown")
    assert "worker.shutdown_forced" in command.events()
    assert healthcheck(command.health) == (1, {"check": "liveness", "healthy": False, "live": False,
                                               "ready": False, "state": "forced", "reason": "forced"})
    command.assert_nothing_leaked(tmp_path, str(authorized.organization_id))


def test_a_second_sigint_forces_the_stop_with_code_5(pg, tmp_path, authorized):
    job = enqueue_scripted(authorized, mode="sql", seconds=30)
    command = Command(pg, tmp_path, scripted=True, MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS=60)
    command.wait_state("busy")
    command.signal(signal.SIGINT)
    command.wait_state("draining", timeout=5)
    command.signal(signal.SIGINT)
    assert command.finish(timeout=15) == worker_runtime.EXIT_FORCED
    stored = jobs.get_job(authorized.session, job.id)
    assert (stored.status, stored.last_error_code) == ("queued", "worker_shutdown")
    assert healthcheck(command.health)[1]["state"] == "forced"


def test_the_command_only_serves_organizations_that_authorized_its_service(pg, tmp_path, authorized, tenant_b):
    """Aucun contournement de RLS: une organisation qui n'a pas autorise le service n'est jamais servie."""
    served = enqueue_scripted(authorized)
    ignored = enqueue_scripted(tenant_b)
    command = Command(pg, tmp_path, scripted=True)
    try:
        wait_until(lambda: jobs.get_job(authorized.session, served.id).status == "succeeded", timeout=20)
        time.sleep(0.5)
    finally:
        command.signal(signal.SIGTERM)
        assert command.finish() == worker_runtime.EXIT_OK
    stored = jobs.get_job(tenant_b.session, ignored.id)
    assert (stored.status, stored.attempts, stored.locked_by) == ("queued", 0, None)
    assert str(tenant_b.organization_id) not in command.stderr


# =============================================================================================
# En processus: erreur interne et restauration des signaux
# =============================================================================================

@pytest.fixture
def restore_logging():
    root = logging.getLogger("mervio")
    state = (list(root.handlers), root.propagate, root.level)
    yield
    root.handlers, root.propagate, root.level = state


def test_an_internal_error_exits_with_code_1_without_leaking(pg, tmp_path, authorized, monkeypatch,
                                                             restore_logging):
    secret = "postgresql://svc:" + "Zq9-internal" + "@db/x /Users/alice/export.csv"

    def broken_loop(self):
        raise RuntimeError(secret)

    monkeypatch.setattr(worker_runtime.WorkerRuntime, "_loop", broken_loop)
    environ = {"MERVIO_DATABASE_URL": pg.url("worker"), "MERVIO_WORKER_NAME": "cli-internal",
               "MERVIO_WORKER_HEALTH_FILE": str(tmp_path / "h.json"), **FAST}
    handlers = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    stream = io.StringIO()
    code = cli_worker.run_worker(settings_loader=lambda: WorkerSettings.from_env(environ), stream=stream)
    assert code == worker_runtime.EXIT_INTERNAL
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == handlers
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    crashed = next(line for line in lines if line["event"] == "worker.crashed")
    assert crashed["error_type"] == "RuntimeError"
    assert (lines[-1]["event"], lines[-1]["exit_code"], lines[-1]["reason"]) == ("worker.stopped", 1, "internal")
    raw = stream.getvalue()
    for leak in ("Zq9-internal", "alice", pg.passwords["worker"], str(tmp_path)):
        assert leak not in raw
    assert json.loads((tmp_path / "h.json").read_text())["state"] == "stopped"
