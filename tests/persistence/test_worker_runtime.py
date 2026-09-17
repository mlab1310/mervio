"""Processus worker complet contre PostgreSQL (Mission 004.3).

Deux niveaux:
- en processus: le runtime tourne dans un fil, l'arret est demande par programme
  (meme chemin que le gestionnaire de signal);
- en sous-processus: vrais SIGTERM, SIGINT et SIGKILL.
"""
from __future__ import annotations

import dataclasses
import io
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mervio.observability.health import read_health
from mervio.observability.logging import configure
from mervio.persistence import audit, jobs, migrate
from mervio.persistence.jobs import JobStatus
from mervio.persistence.service import authorize_service
from mervio.settings import WorkerSettings
from mervio.workers.lifecycle import ProcessState
from mervio.workers.runtime import (
    EXIT_CONFIG, EXIT_DATABASE, EXIT_FORCED, EXIT_OK, EXIT_SCHEMA, WorkerRuntime,
)

from .worker_support import enqueue_scripted, scripted_registry, terminate_backends, wait_until

S = ProcessState
ROOT = Path(__file__).resolve().parents[2]


def settings_for(pg, tmp_path, *, kind="worker", database=None, lease=3, renew=0.5, **env) -> WorkerSettings:
    variables = {
        "MERVIO_DATABASE_URL": pg.url(kind, database), "MERVIO_WORKER_NAME": "rt",
        "MERVIO_WORKER_HEALTH_FILE": str(tmp_path / "health.json"),
        "MERVIO_WORKER_POLL_INTERVAL_SECONDS": "0.05", "MERVIO_WORKER_POLL_MAX_SECONDS": "0.2",
        "MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS": "2", "MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS": "5",
        "MERVIO_WORKER_RECOVERY_INTERVAL_SECONDS": "1", "MERVIO_LOG_LEVEL": "DEBUG",
    }
    variables.update({key: str(value) for key, value in env.items()})
    settings = WorkerSettings.from_env(variables)
    return dataclasses.replace(settings, lease_seconds=lease, lease_renew_seconds=renew)


class Running:
    """Un runtime dans un fil; `forced_exit` est enregistre au lieu de terminer pytest."""

    def __init__(self, settings, **options):
        self.exits = []
        options.setdefault("forced_exit", self.exits.append)
        self.runtime = WorkerRuntime(settings, registry=scripted_registry(), **options)
        self.result = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.result = self.runtime.run()

    def state(self):
        return self.runtime.lifecycle.state

    def wait_state(self, state, timeout=15):
        wait_until(lambda: state in self.runtime.lifecycle.history, timeout=timeout,
                   message=f"etat {state.value} jamais atteint: {self.runtime.lifecycle.history}")

    def join(self, timeout=30):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "le runtime ne s'arrete pas"
        return self.result


@pytest.fixture
def authorized(tenant_a, principal):
    authorize_service(tenant_a.session, principal.id)
    return tenant_a


@pytest.fixture
def json_logs():
    root = logging.getLogger("mervio")
    state = (list(root.handlers), root.propagate, root.level)
    buffer = io.StringIO()
    configure(format="json", level=logging.DEBUG, stream=buffer)
    yield buffer
    root.handlers, root.propagate, root.level = state


def log_events(buffer):
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def audit_trail(tenant, job_id):
    return [(e.action, e.actor_type, e.actor_id, e.metadata.get("reason"))
            for e in audit.list_events(tenant.session, resource_id=job_id, limit=100)]


# =============================================================================================
# En processus
# =============================================================================================

def test_start_idle_and_stop(pg, tmp_path, authorized, principal, json_logs):
    running = Running(settings_for(pg, tmp_path))
    running.wait_state(S.READY)
    wait_until(lambda: running.runtime.polls >= 6)
    assert read_health(tmp_path / "health.json").state == "ready"
    asked = time.monotonic()
    running.runtime.request_stop("test")
    result = running.join()
    assert time.monotonic() - asked < 1.0
    assert (result.state, result.exit_code, result.reason) == (S.STOPPED, EXIT_OK, "stopped")
    assert running.runtime.lifecycle.history == [S.STARTING, S.READY, S.DRAINING, S.STOPPED]
    waits = running.runtime.idle_waits
    assert 0.045 <= waits[0] <= 0.056 and max(waits) <= 0.22 + 1e-9
    assert waits[3] > waits[0], "l'attente grandit tant que la file est vide"
    assert read_health(tmp_path / "health.json").state == "stopped"
    names = [line["event"] for line in log_events(json_logs)
             if line["event"].startswith("worker.") and line["event"] != "worker.state_changed"]
    changes = [(line["previous"], line["state"]) for line in log_events(json_logs)
               if line["event"] == "worker.state_changed"]
    assert changes == [("starting", "ready"), ("ready", "draining"), ("draining", "stopped")]
    assert names[:4] == ["worker.starting", "worker.config", "worker.database_connected", "worker.ready"]
    assert names[-2:] == ["worker.stopping", "worker.stopped"]
    config = next(line for line in log_events(json_logs) if line["event"] == "worker.config")
    assert config["database"]["role"] == pg.role("worker") and config["health_check"] is True
    raw_logs = json_logs.getvalue()
    assert pg.passwords["worker"] not in raw_logs and str(tmp_path) not in raw_logs
    connected = next(line for line in log_events(json_logs) if line["event"] == "worker.database_connected")
    assert connected["principal_id"] == str(principal.id)


def test_existing_jobs_are_processed_and_counted(pg, tmp_path, authorized):
    queued = [enqueue_scripted(authorized).id for _ in range(3)]
    running = Running(settings_for(pg, tmp_path))
    wait_until(lambda: all(jobs.get_job(authorized.session, j).status == "succeeded" for j in queued))
    wait_until(lambda: running.runtime.health.snapshot.jobs_processed == 3)
    running.runtime.request_stop()
    result = running.join()
    assert (result.exit_code, result.jobs_processed) == (EXIT_OK, 3)
    history = running.runtime.lifecycle.history
    assert history.count(S.BUSY) == 3 and history[-2:] == [S.DRAINING, S.STOPPED]
    assert read_health(tmp_path / "health.json").jobs_processed == 3


def test_a_graceful_stop_lets_the_current_job_finish_and_takes_no_other(pg, tmp_path, authorized):
    current = enqueue_scripted(authorized, mode="checkpointed", seconds=1.5)
    waiting = enqueue_scripted(authorized)
    running = Running(settings_for(pg, tmp_path, MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS=10))
    running.wait_state(S.BUSY)
    running.runtime.request_stop("SIGTERM")
    assert running.state() is S.DRAINING
    assert read_health(tmp_path / "health.json").state == "draining"
    result = running.join()
    assert (result.state, result.exit_code) == (S.STOPPED, EXIT_OK)
    assert jobs.get_job(authorized.session, current.id).status == "succeeded"
    assert (jobs.get_job(authorized.session, waiting.id).status,
            jobs.get_job(authorized.session, waiting.id).attempts) == ("queued", 0)
    assert running.runtime.lifecycle.history == [S.STARTING, S.READY, S.BUSY, S.DRAINING, S.STOPPED]
    assert running.exits == []


def test_a_job_longer_than_the_grace_is_released_and_the_stop_is_forced(pg, tmp_path, authorized, principal):
    job = enqueue_scripted(authorized, mode="checkpointed", seconds=20)
    running = Running(settings_for(pg, tmp_path, MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS=1))
    running.wait_state(S.BUSY)
    asked = time.monotonic()
    running.runtime.request_stop("SIGTERM")
    result = running.join()
    assert 0.9 <= time.monotonic() - asked < 4
    assert (result.state, result.exit_code, result.reason) == (S.FORCED, EXIT_FORCED, "forced")
    stored = jobs.get_job(authorized.session, job.id)
    assert (stored.status, stored.attempts, stored.last_error_code) == ("queued", 1, "worker_shutdown")
    trail = audit_trail(authorized, job.id)
    assert ("job.requeued", "worker", principal.id, "worker_shutdown") in trail
    assert not any(action == "job.succeeded" for action, *_ in trail)
    assert running.exits == []
    assert read_health(tmp_path / "health.json").state == "forced"
    assert running.runtime.lifecycle.history[-2:] == [S.DRAINING, S.FORCED]


def test_a_second_request_forces_the_stop_immediately(pg, tmp_path, authorized):
    job = enqueue_scripted(authorized, mode="sql", seconds=30)
    running = Running(settings_for(pg, tmp_path, MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS=60))
    running.wait_state(S.BUSY)
    running.runtime.request_stop("SIGINT")
    time.sleep(0.2)
    asked = time.monotonic()
    running.runtime.request_stop("SIGINT")
    result = running.join()
    assert time.monotonic() - asked < 3
    assert result.exit_code == EXIT_FORCED
    assert jobs.get_job(authorized.session, job.id).last_error_code == "worker_shutdown"


def test_a_handler_that_never_yields_triggers_the_hard_exit(pg, tmp_path, authorized):
    job = enqueue_scripted(authorized, mode="cpu", seconds=3)
    running = Running(settings_for(pg, tmp_path, MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS=0), forced_exit_delay=0.5)
    running.wait_state(S.BUSY)
    running.runtime.request_stop("SIGTERM")
    wait_until(lambda: running.exits, timeout=5, message="aucune sortie forcee")
    assert running.exits == [EXIT_FORCED]
    result = running.join()
    assert result.exit_code == EXIT_FORCED
    stored = jobs.get_job(authorized.session, job.id)
    assert (stored.status, stored.last_error_code) == ("queued", "worker_shutdown")
    assert not any(action == "job.succeeded" for action, *_ in audit_trail(authorized, job.id))


def test_an_unreachable_database_ends_the_startup(pg, tmp_path):
    settings = settings_for(pg, tmp_path, MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS=1)
    unreachable = pg.url("worker").replace(f":{pg.port}/", ":1/")
    from mervio.settings import Secret
    settings = dataclasses.replace(settings, database_url=Secret(unreachable))
    started = time.monotonic()
    result = Running(settings).join()
    assert (result.exit_code, result.reason) == (EXIT_DATABASE, "database")
    assert time.monotonic() - started < 5
    assert read_health(tmp_path / "health.json").state == "stopped"


@pytest.mark.parametrize("kind", ["app", "bypass"])
def test_a_role_that_is_not_a_safe_service_is_refused(pg, tmp_path, db, kind):
    result = Running(settings_for(pg, tmp_path, kind=kind)).join()
    assert (result.exit_code, result.reason) == (EXIT_CONFIG, "config")


def test_a_database_without_the_service_schema_is_refused(pg, tmp_path, db):
    name = pg.create_empty_database("rt_schema")
    try:
        migrate.upgrade(pg.url("migrator", name), "0006_job_leases")
        result = Running(settings_for(pg, tmp_path, database=name)).join()
        assert (result.exit_code, result.reason) == (EXIT_SCHEMA, "schema")
    finally:
        pg.drop_database(name)


def test_a_stop_during_startup_ends_cleanly(pg, tmp_path):
    settings = settings_for(pg, tmp_path, MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS=60)
    from mervio.settings import Secret
    settings = dataclasses.replace(settings, database_url=Secret(pg.url("worker").replace(f":{pg.port}/", ":1/")))
    running = Running(settings)
    time.sleep(0.3)
    running.runtime.request_stop()
    result = running.join(timeout=5)
    assert (result.state, result.exit_code) == (S.STOPPED, EXIT_OK)
    assert running.runtime.lifecycle.history == [S.STARTING, S.DRAINING, S.STOPPED]


def test_a_database_outage_degrades_then_recovers(pg, tmp_path, authorized, json_logs):
    running = Running(settings_for(pg, tmp_path))
    running.wait_state(S.READY)
    assert terminate_backends(pg, "mervio-worker:rt") >= 1
    running.wait_state(S.DEGRADED)
    wait_until(lambda: running.state() is S.READY, timeout=10)
    job = enqueue_scripted(authorized)
    wait_until(lambda: jobs.get_job(authorized.session, job.id).status == "succeeded")
    running.runtime.request_stop()
    assert running.join().exit_code == EXIT_OK
    names = [line["event"] for line in log_events(json_logs)]
    assert "worker.database_unavailable" in names and "worker.database_recovered" in names
    history = running.runtime.lifecycle.history
    assert history.index(S.DEGRADED) < len(history) - 1 - history[::-1].index(S.READY)


def test_the_runtime_recovers_a_dead_worker_job(pg, tmp_path, authorized):
    job = enqueue_scripted(authorized)
    jobs.claim_next_job(authorized.session, worker_id="dead", lease_seconds=1)
    running = Running(settings_for(pg, tmp_path))
    wait_until(lambda: jobs.get_job(authorized.session, job.id).status == "succeeded", timeout=10)
    running.runtime.request_stop()
    running.join()
    stored = jobs.get_job(authorized.session, job.id)
    assert stored.attempts == 2
    assert [a for a, *_ in audit_trail(authorized, job.id)].count("job.recovered") == 1


def test_the_health_file_follows_a_busy_worker_without_leaking(pg, tmp_path, authorized):
    enqueue_scripted(authorized, mode="checkpointed", seconds=1.5)
    running = Running(settings_for(pg, tmp_path))
    running.wait_state(S.BUSY)
    first = read_health(tmp_path / "health.json")
    time.sleep(0.6)
    second = read_health(tmp_path / "health.json")
    assert first.state == second.state == "busy"
    assert second.updated_at > first.updated_at, "le gardien rafraichit la sante pendant le travail"
    text = (tmp_path / "health.json").read_text()
    for leak in (str(authorized.organization_id), pg.passwords["worker"], "postgresql"):
        assert leak not in text
    running.runtime.request_stop()
    assert running.join().exit_code == EXIT_OK


# =============================================================================================
# Sous-processus: vrais signaux
# =============================================================================================

class Process:
    def __init__(self, pg, tmp_path, name="proc", **env):
        self.health = tmp_path / f"{name}.json"
        variables = {key: value for key, value in os.environ.items() if not key.startswith("MERVIO_")}
        variables.update({
            "PYTHONPATH": f"{ROOT / 'tests'}{os.pathsep}{ROOT / 'src'}",
            "MERVIO_DATABASE_URL": pg.url("worker"), "MERVIO_WORKER_NAME": name,
            "MERVIO_WORKER_HEALTH_FILE": str(self.health), "MERVIO_WORKER_POLL_INTERVAL_SECONDS": "0.05",
            "MERVIO_WORKER_POLL_MAX_SECONDS": "0.2", "MERVIO_WORKER_RECOVERY_INTERVAL_SECONDS": "1",
            "MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS": "10", "MERVIO_TEST_LEASE_SECONDS": "3",
            "MERVIO_TEST_RENEW_SECONDS": "0.5",
        })
        variables.update({key: str(value) for key, value in env.items()})
        self.process = subprocess.Popen([sys.executable, "-m", "persistence.worker_launcher"], cwd=ROOT,
                                        env=variables, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def state(self):
        try:
            return read_health(self.health).state
        except Exception:  # noqa: BLE001 - fichier pas encore ecrit
            return None

    def wait_state(self, state, timeout=20):
        wait_until(lambda: self.state() == state or self.process.poll() is not None, timeout=timeout)
        assert self.state() == state, self.finish(kill=True)

    def signal(self, signum):
        self.process.send_signal(signum)

    def finish(self, timeout=30, kill=False):
        if kill:
            self.process.kill()
        try:
            _, stderr = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _, stderr = self.process.communicate()
            raise AssertionError("le processus ne s'arrete pas")
        self.lines = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
        return self.process.returncode


def test_sigterm_stops_an_idle_process(pg, tmp_path, authorized):
    process = Process(pg, tmp_path)
    process.wait_state("ready")
    asked = time.monotonic()
    process.signal(signal.SIGTERM)
    assert process.finish() == EXIT_OK
    assert time.monotonic() - asked < 3
    stopping = next(line for line in process.lines if line["event"] == "worker.stopping")
    stopped = next(line for line in process.lines if line["event"] == "worker.stopped")
    assert stopping["reason"] == "SIGTERM" and (stopped["exit_code"], stopped["state"]) == (0, "stopped")
    assert pg.passwords["worker"] not in json.dumps(process.lines)


def test_sigterm_during_a_job_lets_it_finish(pg, tmp_path, authorized):
    job = enqueue_scripted(authorized, mode="checkpointed", seconds=2)
    process = Process(pg, tmp_path)
    process.wait_state("busy")
    process.signal(signal.SIGTERM)
    assert process.finish() == EXIT_OK
    assert jobs.get_job(authorized.session, job.id).status == "succeeded"
    assert process.state() == "stopped"


def test_two_sigints_force_the_stop_and_release_the_job(pg, tmp_path, authorized):
    job = enqueue_scripted(authorized, mode="checkpointed", seconds=30)
    process = Process(pg, tmp_path)
    process.wait_state("busy")
    process.signal(signal.SIGINT)
    time.sleep(0.3)
    process.signal(signal.SIGINT)
    assert process.finish() == EXIT_FORCED
    stored = jobs.get_job(authorized.session, job.id)
    assert (stored.status, stored.last_error_code, stored.attempts) == ("queued", "worker_shutdown", 1)
    assert process.state() == "forced"


def test_a_killed_process_job_is_finished_once_by_the_next_process(pg, tmp_path, authorized):
    job = enqueue_scripted(authorized, mode="checkpointed", seconds=30, only_attempt=1)
    first = Process(pg, tmp_path, name="victim")
    first.wait_state("busy")
    first.process.send_signal(signal.SIGKILL)
    first.finish()
    assert jobs.get_job(authorized.session, job.id).status == JobStatus.RUNNING.value
    second = Process(pg, tmp_path, name="rescuer")
    try:
        wait_until(lambda: jobs.get_job(authorized.session, job.id).status == "succeeded", timeout=20)
    finally:
        second.signal(signal.SIGTERM)
        assert second.finish() == EXIT_OK
    stored = jobs.get_job(authorized.session, job.id)
    assert stored.attempts == 2 and stored.result["attempt"] == 2
    trail = [a for a, *_ in audit_trail(authorized, job.id)]
    assert trail.count("job.recovered") == 1 and trail.count("job.succeeded") == 1
