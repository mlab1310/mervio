"""La suite de benchmarks s'execute reellement, a petite echelle, sur la base de la suite (Mission 004.3.9).

Toutes les charges tournent: moteur, persistance, file et dispatcher, processus `mervio worker` reel
(import puis analyse), gardien de bail, concurrence de processus. Tailles minuscules: on verifie le
schema des resultats, l'isolation (bases et roles jetables supprimes, aucun privilege), l'absence de
secret, et que les mesures separent bien le cadre du worker du travail reel. Aucun seuil de duree.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks"))

import perf_harness as harness  # noqa: E402
import performance  # noqa: E402

TINY = ["--sizes", "300", "--repetitions", "1", "--queue-depths", "40", "--queue-organizations", "4",
        "--queue-org-scaling", "2", "--queue-org-scaling-depth", "40", "--queue-samples", "10",
        "--noop-jobs", "8", "--noop-threads", "1,2", "--lease-size", "300", "--lease-idle-seconds", "2.5",
        "--lease-imports", "1", "--concurrency-size", "300", "--concurrency-jobs", "2",
        "--concurrency-workers", "1,2", "--timeout", "300", "--force-memory"]


@pytest.fixture(scope="module")
def suite_run(pg, tmp_path_factory):
    directory = tmp_path_factory.mktemp("benchmarks")
    output = directory / "result.json"
    environ = {harness.ADMIN_URL_ENV: pg.admin_conninfo}
    code = performance.main([*TINY, "--output", str(output), "--data-dir", str(directory / "data")], environ=environ)
    return code, json.loads(output.read_text(encoding="utf-8")), output.read_text(encoding="utf-8"), pg


def cases(document, workload):
    return {item["case"]: item for item in document["workloads"][workload]["cases"]}


def test_every_workload_runs_and_the_document_is_valid(suite_run):
    code, document, _, _ = suite_run
    failures = {name: [(c["case"], c["reason"]) for c in entry["cases"] if c["status"] != "ok"]
                for name, entry in document["workloads"].items() if entry["status"] != "ok"}
    assert code == 0, failures
    assert harness.validate_document(document) == []
    assert set(document["workloads"]) == set(performance.WORKLOADS)
    assert all(entry["status"] == "ok" for entry in document["workloads"].values())
    assert document["environment"]["postgresql"].startswith("17.")
    assert document["dataset"]["sizes"]["300"]["config"]["seed"] == 42


def test_disposable_databases_and_roles_are_all_removed(suite_run):
    _, document, _, pg = suite_run
    assert document["leftover_disposable_objects"] == {"databases": 0, "roles": 0}
    assert harness.leftover_objects(pg.admin_conninfo) == {"databases": 0, "roles": 0}


def test_no_secret_and_no_host_path_is_published(suite_run):
    _, _, text, pg = suite_run
    assert not re.search(r"postgres(?:ql)?://[^:/@\s]+:[^@\s]+@", text)
    assert "mervio_perf_" not in text, "aucun nom de role ou de base jetable (ils portent le suffixe aleatoire)"
    for secret in pg.passwords.values():
        assert secret not in text
    assert str(ROOT) not in text


def test_persistence_separates_the_database_call_from_the_application_use_case(suite_run):
    _, document, _, _ = suite_run
    case = cases(document, "persistence")["persistence/300"]
    assert case["category"] == "persistence"
    for metric in ("csv_load_dataset_s", "db_write_snapshot_s", "db_load_dataset_s", "analyze_loaded_dataset_s",
                   "db_persist_report_s", "db_read_report_s", "app_import_csv_snapshot_s", "peak_rss_mb"):
        assert case["metrics"][metric]["n"] == 1, metric
    assert case["details"]["dataset_equal"] and case["details"]["report_bytes_equal"]
    assert case["details"]["import_status"] == ["completed"] and case["details"]["canonical_rows"] > 300
    assert case["details"]["table_sizes_mb_after_first_write"]["orders"] > 0


def test_the_queue_is_measured_under_the_real_worker_role_and_labelled_as_infrastructure(suite_run):
    _, document, _, _ = suite_run
    queue = cases(document, "queue")
    assert set(queue) == {"queue/depth=40/orgs=4", "queue/depth=40/orgs=2"}
    for case in queue.values():
        assert case["category"] == "infrastructure"
        for metric in ("dispatcher_probe_ms", "dispatcher_rotation_ms", "claim_ms", "complete_ms", "enqueue_api_ms"):
            assert case["metrics"][metric]["n"] == 10, metric
        assert case["details"]["claimed"] == 10 and case["metrics"]["claim_ms"]["n"] == 10
        assert case["details"]["noop_top_up_jobs_per_pass"] == 8
        assert case["details"]["probe_plan"]["execution_ms"] > 0
        assert case["details"]["probe_plan"]["nodes"][0].startswith("Limit")
        assert "VIDE" in case["details"]["warning"]
        assert set(case["throughput"]["noop_worker"]) == {"1", "2"}
        assert [case["throughput"]["noop_worker"][k]["jobs"] for k in ("1", "2")] == [8, 8]


def test_real_worker_chains_separate_handler_time_from_framework_overhead(suite_run):
    _, document, _, _ = suite_run
    case = cases(document, "worker")["worker_chain/300"]
    assert case["category"] == "end_to_end"
    for kind in ("import", "analysis"):
        handler = case["metrics"][f"{kind}_handler_ms"]["median"]
        attempt = case["metrics"][f"{kind}_attempt_ms"]["median"]
        overhead = case["metrics"][f"{kind}_framework_overhead_ms"]["median"]
        assert handler > 0 and attempt >= handler and overhead == pytest.approx(attempt - handler, abs=1)
        assert case["metrics"][f"{kind}_queue_wait_ms"]["n"] == 1
    assert case["metrics"]["chain_ms"]["median"] >= case["metrics"]["import_attempt_ms"]["median"]
    details = case["details"]
    assert details["worker_exit_code"] == 0 and details["worker_error_events"] == []
    assert details["worker_peak_rss_mb"] >= details["worker_ready_rss_mb"] > 0
    assert details["audit_events_per_chain"] >= 8 and details["import_rows"] > 300


def test_lease_renewals_are_timed_with_the_product_minimum_lease(suite_run):
    _, document, _, _ = suite_run
    lease = cases(document, "lease")
    idle = lease["lease/idle"]
    assert idle["details"]["lease_seconds"] == 30 and idle["details"]["renew_seconds"] == 1.0
    assert idle["metrics"]["renew_ms"]["n"] >= 1
    assert lease["lease/import_load"]["details"]["result"]["imports"] == 1


def test_concurrency_uses_real_worker_processes_on_real_analyses(suite_run):
    _, document, _, _ = suite_run
    concurrency = cases(document, "concurrency")
    assert set(concurrency) == {"concurrency/workers=1", "concurrency/workers=2"}
    for count, case in ((1, concurrency["concurrency/workers=1"]), (2, concurrency["concurrency/workers=2"])):
        assert case["category"] == "concurrency"
        assert case["metrics"]["handler_ms"]["n"] == 2
        assert sum(case["details"]["per_worker_jobs"].values()) == 2
        assert len(case["details"]["per_worker_jobs"]) == count
        assert case["details"]["worker_exit_codes"] == [0] * count
        assert case["details"]["database"]["xact_commit"] > 0
        assert case["details"]["database"]["deadlocks"] == 0


def test_disposable_roles_have_no_privilege(pg):
    database = harness.DisposableDatabase(pg.admin_conninfo, "roles")
    try:
        database.create()
        with psycopg.connect(pg.admin_conninfo, autocommit=True) as conn:
            rows = conn.execute(
                "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles "
                "WHERE rolname LIKE %s", (f"mervio_perf_%_{database.suffix}",)).fetchall()
            owner = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                                 (database.database,)).fetchone()[0]
        assert len(rows) == 3 and all(row[1:] == (False, False, False, False) for row in rows)
        assert owner == database.role("migrator")
        from mervio.persistence.database import Database
        from mervio.persistence.service import service_principal
        with Database(database.url("worker")) as worker:
            assert service_principal(worker).name == database.role("worker")
    finally:
        database.drop()
    assert harness.leftover_objects(pg.admin_conninfo)["databases"] == 0
