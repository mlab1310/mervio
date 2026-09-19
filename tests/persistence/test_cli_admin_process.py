"""Scenario de demonstration complet en vrais sous-processus (Mission 004.3.7).

    mervio worker                         demarre AVANT l'organisation (principal enregistre)
    mervio admin demo provision           identite, organisation, boutique, connexion, donnees
                                          synthetiques, autorisation du worker, import en file
    (worker)                              sert la nouvelle organisation sans redemarrer
    mervio admin job enqueue-analysis     depuis l'import reussi
    (worker)                              rapport persiste
    mervio admin job show / org show / audit list
    mervio admin service revoke           le worker ne voit plus l'organisation
    SIGTERM                               arret propre

Aucun code Python ad hoc dans le parcours: seulement les commandes du produit. Les verifications
finales lisent la base par la persistance (role applicatif, session du proprietaire).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from mervio.admin import errors as admin_errors
from mervio.persistence import analyses
from mervio.persistence.database import Database
from mervio.persistence.tenancy import TenantContext, TenantSession
from mervio.workers import runtime as worker_runtime

from .persistence_support import TEST_MASTER_KEY_HEX
from .worker_support import wait_until

ROOT = Path(__file__).resolve().parents[2]
OWNER = "demo|owner"
FAST = {
    "MERVIO_WORKER_POLL_INTERVAL_SECONDS": "0.05", "MERVIO_WORKER_POLL_MAX_SECONDS": "0.2",
    "MERVIO_WORKER_RECOVERY_INTERVAL_SECONDS": "1", "MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS": "5",
    # 004.4.2: cle maitre d'identite factice (obligatoire des que le worker traite des imports)
    "MERVIO_IDENTITY_MASTER_KEY": TEST_MASTER_KEY_HEX,
}


def environment(**variables):
    env = {key: value for key, value in os.environ.items() if not key.startswith("MERVIO_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    env.update({key: str(value) for key, value in variables.items()})
    return env


class AdminCli:
    def __init__(self, pg):
        self.pg = pg
        self.published = []

    def run(self, *argv, as_json=True):
        command = [sys.executable, "-m", "mervio.cli", "admin", *argv] + (["--json"] if as_json else [])
        result = subprocess.run(command, cwd=ROOT, env=environment(MERVIO_DATABASE_URL=self.pg.url("app")),
                                capture_output=True, text=True, timeout=180)
        self.published.append(result.stdout + result.stderr)
        return result

    def __call__(self, *argv):
        result = self.run(*argv)
        assert result.stderr == "", result.stderr
        document = json.loads(result.stdout)
        return result.returncode, (document["result"] if document["ok"] else document["error"])

    def ok(self, *argv):
        code, result = self(*argv)
        assert code == 0, result
        return result


class WorkerProcess:
    def __init__(self, pg, tmp_path):
        self.health = tmp_path / "worker-health.json"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "mervio.cli", "worker"], cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env=environment(MERVIO_DATABASE_URL=pg.url("worker"), MERVIO_WORKER_NAME="demo",
                            MERVIO_WORKER_HEALTH_FILE=self.health, **FAST))
        self.stdout = self.stderr = ""

    def state(self):
        try:
            return json.loads(self.health.read_text())["state"]
        except (OSError, ValueError):
            return None

    def healthcheck(self):
        return subprocess.run([sys.executable, "-m", "mervio.cli", "worker", "healthcheck", "--ready"], cwd=ROOT,
                              env=environment(MERVIO_WORKER_HEALTH_FILE=self.health), capture_output=True,
                              text=True, timeout=60).returncode

    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.stdout, self.stderr = self.process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.stdout, self.stderr = self.process.communicate()
        return self.process.returncode


@pytest.fixture
def worker(db, pg, tmp_path):
    process = WorkerProcess(pg, tmp_path)
    yield process
    process.stop()


def wait_job(pg, organization, job_id, statuses, timeout=120):
    from mervio.admin import operations
    database = Database(pg.url("app"))
    seen = {}

    def done():
        seen["job"] = operations.show_job(database, actor=OWNER, organization=organization, job=job_id)["job"]
        return seen["job"]["status"] in statuses
    try:
        wait_until(done, timeout=timeout, interval=0.2)
    finally:
        database.close()
    return seen["job"]


def test_the_complete_demonstration_runs_through_the_product_commands(pg, tmp_path, worker):
    admin = AdminCli(pg)
    data_dir = tmp_path / "demo-data"

    # 1. le worker demarre seul: il enregistre son principal et n'a encore rien a servir
    wait_until(lambda: worker.state() == "ready" or worker.process.poll() is not None, timeout=30)
    assert worker.state() == "ready", worker.stop()
    assert worker.healthcheck() == 0
    service_name = pg.role("worker")

    # 2. provisionnement de la demonstration, rejouable
    first = admin.ok("demo", "provision", "--owner", OWNER, "--data-dir", str(data_dir),
                     "--service", service_name)
    assert first["synthetic"] is True
    for part in ("user", "organization", "store", "connection", "service_authorization", "import_job"):
        assert first[part]["status"] == "created", part
    organization = first["organization"]["organization"]["id"]
    store = first["store"]["store"]["id"]
    assert first["store"]["store"]["currency"] == first["dataset"]["currency"] == "EUR"
    assert first["import_job"]["job"]["payload"]["synthetic"] is True
    assert set(first["import_job"]["job"]["payload"]["sources"].values()) == {"[redacted]"}

    second = admin.ok("demo", "provision", "--owner", OWNER, "--data-dir", str(data_dir),
                      "--service", service_name)
    for part in ("user", "organization", "store", "connection", "service_authorization", "import_job"):
        assert second[part]["status"] == "existing", part
    assert second["organization"] == {**first["organization"], "status": "existing"}
    assert second["import_job"]["job"]["id"] == first["import_job"]["job"]["id"]
    assert second["dataset"] == first["dataset"] and second["next"] == first["next"]

    text = admin.run("demo", "provision", "--owner", OWNER, "--data-dir", str(data_dir), as_json=False)
    assert text.returncode == 0 and text.stderr == ""
    assert "organization.status: existing" in text.stdout.splitlines()

    # 3. le worker sert l'organisation creee apres son demarrage
    imported = wait_job(pg, organization, first["import_job"]["job"]["id"], {"succeeded", "failed"})
    assert imported["status"] == "succeeded", (imported["last_error_code"], imported["last_error"])
    assert imported["result"]["status"] == "completed" and imported["result"]["row_count"] > 0

    # 4. analyse depuis l'import, puis rejeu idempotent
    analysis_argv = ("job", "enqueue-analysis", "--as", OWNER, "--org", organization, "--store", store,
                     "--from-import", imported["id"], "--as-of", first["next"]["analysis_as_of"],
                     "--grain", "week", "--label", "Demo (synthetic)", "--idempotency-key", "demo-analysis")
    queued = admin.ok(*analysis_argv)
    assert queued["status"] == "created"
    assert queued["job"]["payload"]["snapshot_id"] == imported["result"]["snapshot_id"]
    assert admin.ok(*analysis_argv)["job"]["id"] == queued["job"]["id"]
    analysed = wait_job(pg, organization, queued["job"]["id"], {"succeeded", "failed"})
    assert analysed["status"] == "succeeded", (analysed["last_error_code"], analysed["last_error"])

    shown = admin.ok("job", "show", "--as", OWNER, "--org", organization, "--job", analysed["id"])["job"]
    assert shown["result"]["report_id"] == analysed["result"]["report_id"]

    # 5. le rapport est persiste et lisible par le proprietaire
    database = Database(pg.url("app"))
    try:
        owner_id = database.connection().execute("SELECT id FROM users WHERE idp_subject = %s",
                                                 (OWNER,)).fetchone()[0]
        session = TenantSession(database, TenantContext(uuid.UUID(organization), owner_id))
        stored = analyses.get_report(session, uuid.UUID(store), uuid.UUID(analysed["result"]["report_id"]))
        assert stored.record.payload_sha256 == analysed["result"]["payload_sha256"]
        assert stored.report()["business_health_score"] is not None
    finally:
        database.close()

    # 6. inspection: file, services, journal complet et attribue
    state = admin.ok("org", "show", "--as", OWNER, "--org", organization)
    assert state["jobs"]["by_status"] == {"succeeded": 2}
    assert [(s["service"], s["active"]) for s in state["services"]] == [(service_name, True)]
    trail = admin.ok("audit", "list", "--as", OWNER, "--org", organization, "--limit", "1000")["events"]
    actions = [e["action"] for e in trail]
    assert actions[:5] == ["organization.created", "store.created", "connection.created", "service.authorized",
                           "job.enqueued"]
    for expected in ("job.claimed", "import.started", "import.succeeded", "analysis.started",
                     "analysis.succeeded", "job.succeeded"):
        assert expected in actions, expected
    principal_id = state["services"][0]["service_id"]
    for event in trail:
        if event["actor_type"] == "user":
            assert (event["actor_id"], event["on_behalf_of"]) == (str(owner_id), None), event
        else:
            assert event["actor_type"] == "worker", event
            assert (event["actor_id"], event["on_behalf_of"]) == (principal_id, str(owner_id)), event

    # 7. revocation: effet immediat, le travail suivant reste en file
    revoked = admin.ok("service", "revoke", "--as", OWNER, "--org", organization, "--service", service_name)
    assert revoked["authorization"]["active"] is False
    pending = admin.ok("job", "enqueue-analysis", "--as", OWNER, "--org", organization, "--store", store,
                       "--from-import", imported["id"], "--as-of", first["next"]["analysis_as_of"],
                       "--grain", "month")
    time.sleep(2)
    assert admin.ok("job", "show", "--as", OWNER, "--org", organization,
                    "--job", pending["job"]["id"])["job"]["status"] == "queued"
    assert worker.healthcheck() == 0

    # 8. arret propre et aucune fuite
    assert worker.stop() == worker_runtime.EXIT_OK
    published = "".join(admin.published)
    for leak in (*pg.passwords.values(), str(tmp_path), "postgresql://"):
        assert leak not in published, leak
    assert str(data_dir) not in worker.stdout + worker.stderr


def test_demo_provision_refuses_a_foreign_non_empty_directory(db, pg, tmp_path):
    admin = AdminCli(pg)
    target = tmp_path / "not-a-demo"
    target.mkdir()
    (target / "customer.csv").write_text("email\nalice@example.com\n", encoding="utf-8")
    code, error = admin("demo", "provision", "--owner", OWNER, "--data-dir", str(target))
    assert (code, error["code"]) == (admin_errors.EXIT_CONFLICT, "data_dir_not_empty")
    assert (target / "customer.csv").read_text(encoding="utf-8") == "email\nalice@example.com\n"
    assert sorted(p.name for p in target.iterdir()) == ["customer.csv"]
    assert str(tmp_path) not in "".join(admin.published)


def test_demo_provision_with_an_unknown_service_stops_before_queueing(db, pg, tmp_path):
    admin = AdminCli(pg)
    code, error = admin("demo", "provision", "--owner", OWNER, "--data-dir", str(tmp_path / "demo"),
                        "--service", "no_such_worker")
    assert (code, error["code"]) == (admin_errors.EXIT_NOT_FOUND, "service_not_found")
    organization = admin.ok("org", "list", "--as", OWNER)["organizations"][0]["id"]
    assert admin.ok("job", "list", "--as", OWNER, "--org", organization) == {"jobs": []}
