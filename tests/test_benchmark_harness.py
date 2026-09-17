"""Outillage des benchmarks de performance (Mission 004.3.9). Sans base, petites tailles seulement.

Ces tests ne mesurent rien: ils prouvent que les statistiques sont justes, que les resultats sont
complets et sans secret, que la configuration est validee, que les jeux sont deterministes et
qu'aucune charge ne peut viser une base qui n'est pas un serveur local de benchmark.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCHMARKS = ROOT / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))

import perf_harness as harness  # noqa: E402
import performance  # noqa: E402
import perf_workloads  # noqa: E402

LOCAL_ADMIN = "postgresql://bench_admin@127.0.0.1:5432/postgres"


# -- statistiques -----------------------------------------------------------------------------

def test_the_nearest_rank_percentile_always_returns_an_observed_value():
    values = list(range(1, 101))
    assert harness.nearest_rank(values, 0.95) == 95
    assert harness.nearest_rank(values, 0.99) == 99
    assert harness.nearest_rank(values, 1.0) == 100
    assert harness.nearest_rank([4, 1, 3, 2], 0.5) == 2
    assert harness.nearest_rank([7.5], 0.95) == 7.5
    assert harness.nearest_rank([1, 2, 3], 0.01) == 1


@pytest.mark.parametrize("values, fraction", [([], 0.5), ([1], 0.0), ([1], 1.5), ([1], -0.1)])
def test_invalid_percentile_requests_are_refused(values, fraction):
    with pytest.raises(ValueError):
        harness.nearest_rank(values, fraction)


def test_a_summary_reports_n_median_p95_p99_and_extremes():
    summary = harness.summarize(range(1, 21))
    assert summary == {"n": 20, "median": 10.5, "p95": 19.0, "p99": 20.0, "min": 1.0, "max": 20.0,
                       "mean": 10.5, "p95_is_max": False}


def test_a_small_sample_says_that_its_p95_is_the_maximum():
    summary = harness.summarize([3.0, 1.0, 2.0])
    assert summary["median"] == 2.0 and summary["p95"] == 3.0 and summary["p95_is_max"] is True


def test_an_empty_sample_is_explicit_and_rounding_is_applied():
    assert harness.summarize([]) == {"n": 0}
    assert harness.summarize([1.23456], digits=2)["median"] == 1.23


def test_a_rate_over_no_time_is_unknown():
    assert harness.rate(10, 0) is None
    assert harness.rate(10, 4) == 2.5


# -- memoire ----------------------------------------------------------------------------------

def test_ru_maxrss_units_are_normalized_per_platform():
    assert harness._maxrss_to_mb(1024 * 1024 * 50, "darwin") == 50.0
    assert harness._maxrss_to_mb(1024 * 50, "linux") == 50.0


def test_current_and_peak_rss_are_measured_for_this_process():
    current, peak = harness.rss_mb(), harness.peak_rss_mb()
    assert current is not None and 5 < current < 100_000
    assert peak >= current * 0.5  # pic depuis le demarrage, meme unite
    assert harness.cpu_seconds() > 0


def test_the_rss_of_a_missing_process_is_unknown():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert harness.rss_mb(process.pid) is None


def test_available_memory_is_reported_or_explicitly_unknown():
    available, total = harness.available_memory_gb(), harness.total_memory_gb()
    assert total is None or total > 0
    assert available is None or 0 <= available <= (total or available)


@pytest.mark.parametrize("size, available, force, refused", [
    (1_000_000, 4.0, False, True), (1_000_000, None, False, True), (1_000_000, 4.0, True, False),
    (1_000_000, 16.0, False, False), (100_000, 1.0, False, True), (100_000, 2.5, False, False),
])
def test_the_memory_guard_refuses_instead_of_shrinking(size, available, force, refused):
    reason = harness.check_memory(size, available=available, force=force)
    assert (reason is not None) == refused
    if refused:
        assert str(size) in reason and "Go" in reason


# -- configuration ----------------------------------------------------------------------------

def test_size_lists_are_parsed_strictly():
    assert harness.parse_positive_list("10000, 100_000,1000000", name="x") == [10000, 100000, 1000000]


@pytest.mark.parametrize("text", ["", "0", "-5", "abc", "1.5", "10,10", "2000000", "10,,20"])
def test_invalid_size_lists_are_refused(text):
    with pytest.raises(harness.BenchmarkConfigError):
        harness.parse_positive_list(text, name="--sizes")


def test_the_default_matrix_covers_10k_100k_and_1m_with_declared_repetitions():
    args = performance.parse_args([])
    assert args.sizes == [10_000, 100_000, 1_000_000]
    assert args.workloads == list(performance.WORKLOADS)
    assert args.queue_depths == [100, 1000, 10_000, 100_000]
    assert args.concurrency_workers == [1, 2, 4, 8]
    assert [performance.repetitions_for("analytics", s, None) for s in args.sizes] == [7, 5, 3]
    assert [performance.repetitions_for("persistence", s, None) for s in args.sizes] == [5, 3, 1]
    assert [performance.repetitions_for("worker", s, None) for s in args.sizes] == [5, 3, 1]
    assert performance.repetitions_for("worker", 1_000_000, 2) == 2


@pytest.mark.parametrize("argv", [
    ["--workloads", "analytics,gpu"], ["--workloads", ""], ["--sizes", "0"], ["--repetitions", "0"],
    ["--noop-jobs", "2", "--noop-threads", "4"], ["--lease-size", "5000000"], ["--timeout", "0"],
    ["--concurrency-workers", "1,1"], ["--queue-samples", "-1"],
])
def test_invalid_suite_configurations_are_refused(argv):
    with pytest.raises(harness.BenchmarkConfigError):
        performance.parse_args(argv)


def test_an_invalid_configuration_exits_with_code_2(capsys):
    assert performance.main(["--sizes", "abc"], environ={}) == 2
    assert "--sizes" in capsys.readouterr().err


# -- jeux de donnees --------------------------------------------------------------------------

def test_the_dataset_uses_the_documented_seed_and_profile():
    config = harness.dataset_config(10_000)
    assert config == {"generator": "mervio.synthetic", "scenario": "healthy_store", "profile": "fashion_eu",
                      "days": 365, "end_date": "2026-09-13", "seed": 42, "orders": 10_000}


def test_datasets_are_deterministic_and_reused(tmp_path):
    first = harness.ensure_dataset(300, tmp_path / "a")
    again = harness.ensure_dataset(300, tmp_path / "a")
    other = harness.ensure_dataset(300, tmp_path / "b")
    assert first["generation_s"] is not None and again["generation_s"] is None
    for key in ("orders", "customers", "order_rows", "orders_csv_bytes", "orders_csv_sha256", "period"):
        assert first[key] == other[key], key
    assert first["config"]["seed"] == 42 and first["orders_csv_sha256"]
    assert (Path(first["directory"]) / "shopify_orders.csv").read_bytes() == (
        Path(other["directory"]) / "shopify_orders.csv").read_bytes()


def test_a_dataset_with_another_configuration_is_regenerated(tmp_path):
    info = harness.ensure_dataset(300, tmp_path)
    manifest_path = Path(info["directory"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["config"]["seed"] = 7
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    regenerated = harness.ensure_dataset(300, tmp_path)
    assert regenerated["generation_s"] is not None and regenerated["config"]["seed"] == 42


# -- base de benchmark ------------------------------------------------------------------------

def test_the_admin_url_is_required():
    with pytest.raises(harness.BenchmarkConfigError, match="requise"):
        harness.admin_url({})


@pytest.mark.parametrize("url", [
    "postgresql://admin@db.example.com:5432/postgres", "postgresql://admin@10.0.0.5/postgres",
    "postgresql://admin@prod-db/postgres",
])
def test_a_remote_server_is_never_a_benchmark_target(url):
    with pytest.raises(harness.BenchmarkConfigError, match="non local"):
        harness.admin_url({harness.ADMIN_URL_ENV: url})


@pytest.mark.parametrize("variable", harness.APPLICATION_URL_ENVS)
def test_the_application_connection_is_never_reused_for_benchmarks(variable):
    with pytest.raises(harness.BenchmarkConfigError, match=variable):
        harness.admin_url({harness.ADMIN_URL_ENV: LOCAL_ADMIN, variable: LOCAL_ADMIN})


def test_local_servers_and_sockets_are_accepted():
    assert harness.admin_url({harness.ADMIN_URL_ENV: LOCAL_ADMIN}) == LOCAL_ADMIN
    socket = "postgresql://admin@%2Ftmp/postgres"
    assert harness.admin_url({harness.ADMIN_URL_ENV: socket}) == socket


def test_an_unreadable_admin_url_is_refused():
    with pytest.raises(harness.BenchmarkConfigError):
        harness.admin_url({harness.ADMIN_URL_ENV: "not a url = = ="})


def test_disposable_databases_have_prefixed_names_and_quoted_secrets():
    database = harness.DisposableDatabase(LOCAL_ADMIN, "queue")
    assert database.database.startswith("mervio_perf_queue_")
    assert {database.role(k) for k in ("migrator", "app", "worker")} == {
        f"mervio_perf_{k}_{database.suffix}" for k in ("migrator", "app", "worker")}
    assert re.fullmatch(r"[0-9a-f]{48}", database.password)
    assert database.url("app") == (f"postgresql://mervio_perf_app_{database.suffix}:{database.password}"
                                   f"@127.0.0.1:5432/{database.database}")
    assert set(database.urls()) == {"migrator", "app", "worker"}
    for label in ("Queue", "a-b", "x" * 21, ""):
        with pytest.raises(harness.BenchmarkConfigError):
            harness.DisposableDatabase(LOCAL_ADMIN, label)


def test_without_a_benchmark_database_the_database_workloads_are_not_executed(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "available_memory_gb", lambda: 0.1)
    output = tmp_path / "result.json"
    code = performance.main(["--sizes", "300", "--output", str(output), "--data-dir", str(tmp_path / "data")],
                            environ={"MERVIO_DATABASE_URL": "postgresql://app:secret-value@db.example.com/prod"})
    document = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert harness.validate_document(document) == []
    for name in performance.DB_WORKLOADS:
        assert document["workloads"][name]["status"] == "not_executed"
        assert harness.ADMIN_URL_ENV in document["workloads"][name]["reason"]
    # memoire insuffisante: refus explicite, jamais une plus petite taille
    analytics = document["workloads"]["analytics"]
    assert analytics["status"] == "not_executed" and analytics["cases"][0]["size"] == 300
    text = output.read_text(encoding="utf-8")
    assert "secret-value" not in text and "db.example.com" not in text


# -- processus fils ---------------------------------------------------------------------------

def child_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "child.py"
    script.write_text("import json, sys, time\n" + body, encoding="utf-8")
    return script


def test_a_child_result_is_the_last_json_line(tmp_path):
    script = child_script(tmp_path, "sys.stdin.read()\nprint('noise')\nprint(json.dumps({'wall_s': 1.5}))\n")
    assert harness.run_child(script, "x", {}, timeout=30) == {"wall_s": 1.5, "status": "ok"}


def test_a_failing_child_is_reported_with_its_secrets_redacted(tmp_path):
    secret = "0" * 20 + "abcdef"
    script = child_script(tmp_path, f"sys.stdin.read()\nsys.stderr.write('boom {secret} "
                                    "postgresql://role:pw123@127.0.0.1/db')\nsys.exit(3)\n")
    result = harness.run_child(script, "x", {}, timeout=30, secrets_=[secret])
    assert result["status"] == "failed" and result["exit_code"] == 3
    assert secret not in result["reason"] and "pw123" not in result["reason"]
    assert "[redacted]" in result["reason"]


def test_a_slow_child_times_out(tmp_path):
    script = child_script(tmp_path, "time.sleep(30)\n")
    result = harness.run_child(script, "x", {}, timeout=0.5)
    assert result["status"] == "timeout" and "0.5" in result["reason"]


def test_a_child_without_json_is_a_failure(tmp_path):
    script = child_script(tmp_path, "sys.stdin.read()\nprint('{not json')\n")
    assert harness.run_child(script, "x", {}, timeout=30)["status"] == "failed"


def test_the_child_payload_travels_by_stdin_never_by_argv(tmp_path):
    script = child_script(tmp_path, "payload = json.loads(sys.stdin.read())\n"
                                    "print(json.dumps({'argv': sys.argv, 'secret': payload['url']}))\n")
    result = harness.run_child(script, "queue", {"url": "postgresql://r:pw@127.0.0.1/db"}, timeout=30)
    assert result["argv"][1:] == ["--child", "queue"]
    assert result["secret"] == "postgresql://r:pw@127.0.0.1/db"


def test_the_workload_entry_point_refuses_unknown_workloads(capsys):
    assert perf_workloads.main(["--child", "gpu"]) == 2
    assert perf_workloads.main([]) == 2
    assert set(perf_workloads.WORKLOADS) == {"analytics_parse", "analytics_stages", "analytics_e2e", "persistence",
                                             "queue", "worker_chain", "lease", "concurrency"}


def test_the_real_analytics_workload_runs_at_a_tiny_size(tmp_path):
    output = tmp_path / "analytics.json"
    code = performance.main(["--workloads", "analytics", "--sizes", "300", "--repetitions", "2", "--output",
                             str(output), "--data-dir", str(tmp_path / "data"), "--force-memory"], environ={})
    assert code == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert harness.validate_document(document) == []
    [case] = document["workloads"]["analytics"]["cases"]
    assert case["status"] == "ok" and case["category"] == "business_analytics" and case["repetitions"] == 2
    for metric in ("e2e_wall_s", "e2e_cpu_s", "e2e_peak_rss_mb", "e2e_baseline_rss_mb", "e2e_final_rss_mb",
                   "parse_wall_s", "report_bytes"):
        assert case["metrics"][metric]["n"] == 2, metric
    assert case["metrics"]["e2e_peak_rss_mb"]["min"] >= case["metrics"]["e2e_baseline_rss_mb"]["max"] * 0.9
    assert set(case["details"]["stages_s"]) == set(performance.ENGINE_STAGES)
    assert abs(sum(case["details"]["stage_share_of_median_total"].values()) - 1) < 0.02
    assert case["throughput"]["orders_per_second_median"] > 0
    assert document["dataset"]["sizes"]["300"]["path"] == "healthy_store_300"
    assert str(tmp_path) not in output.read_text(encoding="utf-8")


# -- worker: separation cadre / execution -----------------------------------------------------

def test_job_timings_separate_handler_time_from_framework_overhead():
    from datetime import datetime, timedelta, timezone

    from mervio.persistence.jobs import JobRecord
    base = datetime(2026, 9, 17, tzinfo=timezone.utc)
    job = JobRecord(id=None, organization_id=None, store_id=None, job_type="import", status="succeeded",
                    priority=0, attempts=1, max_attempts=5, idempotency_key=None, correlation_id=None,
                    enqueued_by=None, available_at=base, locked_at=None, locked_by=None, lease_expires_at=None,
                    started_at=base + timedelta(milliseconds=120), finished_at=base + timedelta(milliseconds=620),
                    last_error_code=None, last_error=None, payload={}, result={}, created_at=base, updated_at=base)
    timings = perf_workloads._job_timings(job, 450, enqueued_at=10.0, observed_at=10.7)
    assert timings == {"status": "succeeded", "attempts": 1, "queue_wait_ms": 120.0, "handler_ms": 450,
                       "attempt_ms": 500.0, "framework_overhead_ms": 50.0, "observed_ms": 700.0}


def test_worker_processes_never_inherit_application_connections(tmp_path, monkeypatch):
    captured = {}

    class FakePopen:
        pid = os.getpid()

        def __init__(self, argv, **options):
            captured.update(options["env"], argv=argv)

        def poll(self):
            return 0

    monkeypatch.setenv("MERVIO_DATABASE_URL", "postgresql://prod:secret@db.example.com/prod")
    monkeypatch.setenv("MERVIO_MIGRATION_DATABASE_URL", "postgresql://owner:secret@db.example.com/prod")
    monkeypatch.setattr(perf_workloads.subprocess, "Popen", FakePopen)
    process = perf_workloads.WorkerProcess("postgresql://w:pw@127.0.0.1/perf", tmp_path, "perf-test")
    process._stop.set()
    process._log.close()
    assert captured["MERVIO_DATABASE_URL"] == "postgresql://w:pw@127.0.0.1/perf"
    assert "MERVIO_MIGRATION_DATABASE_URL" not in captured
    assert captured["argv"][1:] == ["-m", "mervio.cli", "worker"]
    assert captured["MERVIO_ENV"] == "development" and captured["MERVIO_LOG_FORMAT"] == "json"


# -- document de resultats --------------------------------------------------------------------

def good_document():
    document = harness.new_document(suite_run_id="abc", configuration={"sizes": [10]}, postgresql="17.11")
    document["workloads"]["analytics"] = {"status": "ok", "cases": [harness.case(
        "analytics/10", category="business_analytics", size=10, status="ok", repetitions=3,
        metrics={"e2e_wall_s": harness.summarize([1, 2, 3])})]}
    return document


def test_a_complete_document_has_environment_commit_seed_and_statistics():
    document = good_document()
    assert harness.validate_document(document) == []
    assert document["schema_version"] == harness.RESULT_SCHEMA_VERSION
    assert document["benchmark_version"] == harness.BENCHMARK_VERSION
    assert re.fullmatch(r"[0-9a-f]{40}", document["git_commit"])
    assert document["dataset"]["seed"] == 42 and document["dataset"]["analysis_day"] == "2026-09-14"
    for key in ("os", "machine", "cpu_count", "python", "postgresql", "memory_total_gb", "github_actions"):
        assert key in document["environment"], key
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", document["timestamp_utc"])


@pytest.mark.parametrize("mutate, problem", [
    (lambda d: d.pop("git_commit"), "git_commit"),
    (lambda d: d["dataset"].update(seed=7), "graine"),
    (lambda d: d["environment"].pop("python"), "python"),
    (lambda d: d.update(schema_version=99), "schema_version"),
    (lambda d: d["workloads"]["analytics"].update(status="great"), "statut"),
    (lambda d: d["workloads"]["analytics"]["cases"][0].update(status="failed"), "sans raison"),
    (lambda d: d["workloads"]["analytics"]["cases"][0].update(category="magic"), "categorie"),
    (lambda d: d["workloads"]["analytics"]["cases"][0].pop("metrics"), "metrics"),
    (lambda d: d["workloads"]["analytics"]["cases"][0]["metrics"]["e2e_wall_s"].pop("p95"), "incomplet"),
])
def test_incomplete_documents_are_rejected(mutate, problem):
    document = good_document()
    mutate(document)
    problems = harness.validate_document(document)
    assert problems and any(problem in p for p in problems), problems


def test_the_github_runner_is_recorded_only_on_github():
    local = harness.environment(environ={})
    assert local["github_actions"] is False and local["runner"] is None
    github = harness.environment(environ={"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux", "ImageOS": "ubuntu24",
                                          "ImageVersion": "20260901.1", "GITHUB_TOKEN": "ghs_x"})
    assert github["github_actions"] is True
    assert github["runner"]["ImageOS"] == "ubuntu24" and github["runner"]["RUNNER_OS"] == "Linux"
    assert "GITHUB_TOKEN" not in json.dumps(github)


def test_documents_are_written_with_a_generated_name(tmp_path):
    path = harness.write_document(good_document(), tmp_path)
    assert path.parent == tmp_path and re.fullmatch(r"performance_\d{8}T\d{6}Z_\w+_[0-9a-f]{8}\.json", path.name)
    assert json.loads(path.read_text(encoding="utf-8"))["suite_run_id"] == "abc"
    explicit = harness.write_document(good_document(), tmp_path / "sub" / "named.json")
    assert explicit == tmp_path / "sub" / "named.json" and explicit.exists()


@pytest.mark.parametrize("leak", ["s3cr3t-password-value", "postgresql://role:pw@127.0.0.1/db"])
def test_a_document_carrying_a_secret_is_never_written(tmp_path, leak):
    document = good_document()
    document["workloads"]["analytics"]["cases"][0]["details"]["note"] = leak
    with pytest.raises(harness.BenchmarkConfigError, match="refuse"):
        harness.write_document(document, tmp_path, ["s3cr3t-password-value"])
    assert list(tmp_path.iterdir()) == []


def test_an_invalid_document_is_never_written(tmp_path):
    document = good_document()
    document.pop("environment")
    with pytest.raises(harness.BenchmarkConfigError, match="invalide"):
        harness.write_document(document, tmp_path)


def test_redaction_masks_secrets_and_credential_urls():
    text = harness.redact("a=supersecretvalue url=postgresql://u:p4ss@h/db", ["supersecretvalue"])
    assert text == "a=[redacted] url=postgresql://u:[redacted]@h/db"
    assert harness.leaked("postgres://u:x@h/db", []) == ["credential_url"]
    assert harness.leaked("postgresql://u@127.0.0.1/db", ["abc"]) == []


# -- depot et CI ------------------------------------------------------------------------------

def test_local_results_are_ignored_by_git():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "benchmark-results/" in ignored
    assert harness.DEFAULT_OUTPUT_DIR == ROOT / "benchmark-results"
    assert "data/benchmark/*" in ignored


def test_benchmarks_are_not_part_of_the_normal_ci():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "performance.py" not in ci and "MERVIO_BENCH_ADMIN_DATABASE_URL" not in ci


WORKFLOW_TEXT = (ROOT / ".github" / "workflows" / "benchmarks.yml").read_text(encoding="utf-8")
#: le fichier sans ses commentaires (ils parlent de push et de pull request pour dire qu'ils sont exclus)
WORKFLOW = "\n".join(line for line in WORKFLOW_TEXT.splitlines() if not line.lstrip().startswith("#")) + "\n"


def test_the_benchmark_workflow_is_manual_read_only_and_pinned():
    assert re.search(r"^on:\n  workflow_dispatch:\n", WORKFLOW, flags=re.M)
    head = WORKFLOW.split("\njobs:", 1)[0]
    assert "push" not in head and "pull_request" not in head and "schedule" not in head
    assert re.search(r"^permissions:\n  contents: read\n", WORKFLOW, flags=re.M)
    assert "write" not in re.search(r"^permissions:\n((?:  .*\n)+)", WORKFLOW, flags=re.M).group(1)
    for reference in re.findall(r"uses:\s*(\S+)", WORKFLOW):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", reference), reference
    assert WORKFLOW.count("persist-credentials: false") == WORKFLOW.count("actions/checkout@")
    assert re.search(r"timeout-minutes: \d+", WORKFLOW)


def test_the_benchmark_workflow_never_interpolates_its_inputs_into_a_script():
    for line in WORKFLOW.splitlines():
        if line.strip().startswith("run:"):
            assert "${{" not in line, line
    assert "BENCH_SIZES: ${{ inputs.sizes }}" in WORKFLOW
    assert '--sizes "$BENCH_SIZES"' in WORKFLOW and '--workloads "$BENCH_WORKLOADS"' in WORKFLOW
    assert "continue-on-error" not in WORKFLOW
    assert not re.search(r"postgres(?:ql)?://[^:/@\s]+:[^@\s]+@", WORKFLOW_TEXT)
    assert "MANUEL uniquement" in WORKFLOW_TEXT
    assert "postgres:17.11-bookworm@sha256:" in WORKFLOW


# -- tableaux de resultats --------------------------------------------------------------------

import perf_report  # noqa: E402


def full_document():
    document = good_document()
    document["duration_s"] = 12.0
    document["dataset"]["sizes"]["10"] = {"orders": 10, "customers": 7, "order_rows": 13, "orders_csv_bytes": 2000,
                                          "period": {"start": "2025-09-14", "end": "2026-09-13"},
                                          "orders_csv_sha256": "ab" * 32}
    stages = {name: harness.summarize([0.1]) for name in performance.ENGINE_STAGES}
    document["workloads"]["analytics"]["cases"] = [
        harness.case("analytics/10", category="business_analytics", size=10, status="ok", repetitions=3,
                     metrics={"e2e_wall_s": harness.summarize([1, 2, 3]), "e2e_cpu_s": harness.summarize([1, 2, 3]),
                              "e2e_peak_rss_mb": harness.summarize([50, 51, 52])},
                     throughput={"orders_per_second_median": 5.0},
                     details={"stages_s": stages,
                              "stage_share_of_median_total": {n: 1 / len(stages) for n in stages}}),
        harness.case("analytics/1000000", category="business_analytics", size=1_000_000, status="not_executed",
                     reason="memoire disponible 4 Go < 8.0 Go exiges"),
    ]
    document["workloads"]["queue"] = {"status": "ok", "cases": [harness.case(
        "queue/depth=10/orgs=1", category="infrastructure", size=10, status="ok", repetitions=5,
        metrics={key: harness.summarize([1.0, 2.0]) for key in ("dispatcher_probe_ms", "claim_ms", "complete_ms",
                                                                "enqueue_api_ms")},
        throughput={"noop_worker": {"1": {"jobs_per_second": 200.0}}},
        details={"organizations": 1, "probe_plan": {"nodes": ["Limit rows=1 loops=1",
                                                              "Sort rows=1 loops=1",
                                                              "Index Scan jobs_ready_idx rows=1 loops=1"],
                                                    "execution_ms": 0.4}})]}
    return document


def test_the_report_renders_separate_tables_from_the_json_only():
    text = perf_report.render(full_document())
    for title in ("Environnement", "Moteur analytique (sans base)", "Persistance PostgreSQL",
                  "Infrastructure: file et dispatcher (gestionnaire VIDE pour le cadre)",
                  "Worker reel: import puis analyse (bout en bout)", "Concurrence"):
        assert f"### {title}" in text, title
    assert "| 10 | 3 | 2.00 s | 3.00 s (=max) |" in text
    assert "NOT_EXECUTED" in text and "memoire disponible 4 Go" in text
    assert "index + LIMIT, 0.40 ms" in text and "1: 200.0" in text


def test_the_report_refuses_an_invalid_document(tmp_path, capsys):
    document = full_document()
    document.pop("environment")
    with pytest.raises(harness.BenchmarkConfigError):
        perf_report.render(document)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert perf_report.main([str(path)]) == 2
    assert perf_report.main([]) == 2
    assert perf_report.main([str(tmp_path / "missing.json")]) == 2
    good = tmp_path / "good.json"
    good.write_text(json.dumps(full_document()), encoding="utf-8")
    capsys.readouterr()
    assert perf_report.main([str(good)]) == 0
    assert "### Environnement" in capsys.readouterr().out


# -- document de performance ------------------------------------------------------------------

REFERENCE = BENCHMARKS / "results" / "performance_20260917_arm64.json"
PERFORMANCE_DOC = ROOT / "docs" / "PERFORMANCE.md"


def test_the_versioned_reference_is_a_valid_complete_run():
    document = json.loads(REFERENCE.read_text(encoding="utf-8"))
    assert harness.validate_document(document) == []
    assert all(entry["status"] == "ok" for entry in document["workloads"].values())
    assert set(document["workloads"]) == set(performance.WORKLOADS)
    assert document["configuration"]["sizes"] == [10_000, 100_000, 1_000_000]
    assert document["leftover_disposable_objects"] == {"databases": 0, "roles": 0}
    assert document["environment"]["github_actions"] is False
    text = REFERENCE.read_text(encoding="utf-8")
    assert "/Users/" not in text and "/home/" not in text and harness.leaked(text, []) == []


def test_the_performance_document_tables_are_generated_from_the_reference():
    """Aucun chiffre des tableaux n'est recopie a la main: ils doivent etre ceux du JSON versionne."""
    rendered = perf_report.render(json.loads(REFERENCE.read_text(encoding="utf-8")))
    expected = rendered.replace("### ", "#### ").rstrip()
    assert expected in PERFORMANCE_DOC.read_text(encoding="utf-8")


def test_the_performance_document_separates_observations_from_inferences():
    text = PERFORMANCE_DOC.read_text(encoding="utf-8")
    for marker in ("**OBSERVÉ", "**INFÉRÉ", "**LIMITE", "gestionnaire VIDE", "Aucune exécution GitHub",
                   "benchmarks/results/performance_20260917_arm64.json", "graine", "not_executed"):
        assert marker in text, marker
    assert "commandes/s" in text and "p95" in text
