"""Mesures de base du processus worker (Mission 004.3). Mesure, n'optimise pas.

Pas de benchmark a grande echelle (prevu en 004.3.9): seulement ce qui qualifie le
processus lui-meme, chaque scenario dans son propre sous-processus.

    idle        cout d'un worker sans travail: sondages par seconde, CPU, memoire
    loop        surcout de la boucle complete (dispatcher, identite, delegation,
                gardien de bail, sante, audit) par rapport au Worker 004.2 nu
    renewal     frequence et latence des renouvellements pendant un travail long
    real        import puis analyse reels des fichiers d'exemple (moteur inchange)

    MERVIO_BENCH_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres \\
        python benchmarks/worker_baseline.py
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import platform
import resource
import secrets
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
RESULTS_DIR = Path(__file__).resolve().parent / "results"
SAMPLE = ROOT / "data" / "sample"
LOOP_JOBS = 300
IDLE_SECONDS = 10.0


def _self_usage() -> dict:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak = usage.ru_maxrss / (1024 * 1024) if sys.platform == "darwin" else usage.ru_maxrss / 1024
    return {"cpu_seconds": round(usage.ru_utime + usage.ru_stime, 3), "peak_rss_mb": round(peak, 1)}


def _wait(predicate, timeout=600.0, interval=0.02) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("scenario bloque")
        time.sleep(interval)


def _percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))], 3) if ordered else None


# -- sous-processus ------------------------------------------------------------------------------

def child(scenario: str, app_url: str, worker_url: str, context: dict) -> dict:
    import logging
    from uuid import UUID

    from mervio.observability.logging import configure
    from mervio.persistence import jobs
    from mervio.persistence.database import Database
    from mervio.persistence.jobs import JobType
    from mervio.persistence.tenancy import TenantContext, TenantSession
    from mervio.settings import Secret, WorkerSettings
    from mervio.workers.handlers import HandlerRegistry, default_registry
    from mervio.workers.runtime import WorkerRuntime
    from mervio.workers.worker import Worker

    configure(format="json", level=logging.CRITICAL)  # on mesure le worker, pas l'ecriture des logs
    app = Database(app_url)
    session = TenantSession(app, TenantContext(UUID(context["organization_id"]), UUID(context["owner_id"])))

    def sleeping(ctx):
        deadline = time.monotonic() + float(ctx.payload.get("seconds", 0))
        while time.monotonic() < deadline:
            ctx.checkpoint()
            time.sleep(0.05)
        return {"ok": True}

    registry = HandlerRegistry().register(JobType.ANALYSIS, sleeping)
    settings = WorkerSettings.from_env({
        "MERVIO_DATABASE_URL": worker_url, "MERVIO_WORKER_NAME": f"bench-{scenario}",
        "MERVIO_WORKER_POLL_INTERVAL_SECONDS": "1", "MERVIO_WORKER_POLL_MAX_SECONDS": "5",
    })
    result: dict = {"scenario": scenario}

    def start(runtime):
        thread = threading.Thread(target=runtime.run, daemon=True)
        thread.start()
        _wait(lambda: runtime.lifecycle.state.value == "ready")
        return thread

    if scenario == "idle":
        runtime = WorkerRuntime(settings, registry=registry)
        thread = start(runtime)
        before, polls = _self_usage(), runtime.polls
        time.sleep(IDLE_SECONDS)
        after = _self_usage()
        result.update(polls_per_second=round((runtime.polls - polls) / IDLE_SECONDS, 3),
                      mean_idle_wait_seconds=round(statistics.mean(runtime.idle_waits[-5:]), 3),
                      cpu_percent=round(100 * (after["cpu_seconds"] - before["cpu_seconds"]) / IDLE_SECONDS, 3),
                      peak_rss_mb=after["peak_rss_mb"])
        runtime.request_stop()
        thread.join(30)

    elif scenario == "loop":
        # reference 004.2: Worker nu, sans dispatcher, sans gardien, sans sante
        for _ in range(LOOP_JOBS):
            jobs.enqueue_job(session, job_type=JobType.ANALYSIS, payload={})
        bare = Worker(sessions=[session], registry=registry, worker_id="bench-bare")
        started = time.perf_counter()
        assert len(bare.run_until_empty()) == LOOP_JOBS
        bare_seconds = time.perf_counter() - started
        for _ in range(LOOP_JOBS):
            jobs.enqueue_job(session, job_type=JobType.ANALYSIS, payload={})
        runtime = WorkerRuntime(settings, registry=registry)
        started = time.perf_counter()
        thread = start(runtime)
        _wait(lambda: runtime.health.snapshot.jobs_processed >= LOOP_JOBS)
        full_seconds = time.perf_counter() - started
        runtime.request_stop()
        thread.join(30)
        result.update(jobs=LOOP_JOBS,
                      bare_worker_jobs_per_second=round(LOOP_JOBS / bare_seconds, 1),
                      bare_worker_ms_per_job=round(1000 * bare_seconds / LOOP_JOBS, 2),
                      process_jobs_per_second=round(LOOP_JOBS / full_seconds, 1),
                      process_ms_per_job=round(1000 * full_seconds / LOOP_JOBS, 2),
                      overhead_ms_per_job=round(1000 * (full_seconds - bare_seconds) / LOOP_JOBS, 2),
                      peak_rss_mb=_self_usage()["peak_rss_mb"])

    elif scenario == "renewal":
        latencies = []
        original = jobs.renew_lease

        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                latencies.append((time.perf_counter() - started) * 1000)

        jobs.renew_lease = timed
        seconds = 10.0
        jobs.enqueue_job(session, job_type=JobType.ANALYSIS, payload={"seconds": seconds})
        tuned = dataclasses.replace(settings, lease_seconds=3, lease_renew_seconds=0.5)
        runtime = WorkerRuntime(tuned, registry=registry)
        thread = start(runtime)
        _wait(lambda: runtime.health.snapshot.jobs_processed >= 1)
        runtime.request_stop()
        thread.join(30)
        result.update(job_seconds=seconds, lease_seconds=3, renew_seconds=0.5, renewals=len(latencies),
                      renewals_per_second=round(len(latencies) / seconds, 2),
                      renewal_p50_ms=_percentile(latencies, 0.5), renewal_p95_ms=_percentile(latencies, 0.95),
                      renewal_max_ms=round(max(latencies), 3) if latencies else None)

    elif scenario == "real":
        files = {"shopify_orders": str(SAMPLE / "shopify_orders.csv"),
                 "shopify_products": str(SAMPLE / "shopify_products.csv"),
                 "stripe": str(SAMPLE / "stripe_transactions.csv"),
                 "google_ads": str(SAMPLE / "google_ads.csv")}
        importing = jobs.enqueue_job(session, job_type=JobType.IMPORT, store_id=UUID(context["store_id"]), payload={
            "store_id": context["store_id"], "connection_id": context["connection_id"], "sources": files})
        runtime = WorkerRuntime(settings, registry=default_registry())
        started = time.perf_counter()
        thread = start(runtime)
        _wait(lambda: jobs.get_job(session, importing.id).is_terminal)
        imported = jobs.get_job(session, importing.id)
        analysing = jobs.enqueue_job(session, job_type=JobType.ANALYSIS, store_id=UUID(context["store_id"]), payload={
            "store_id": context["store_id"], "snapshot_id": imported.result["snapshot_id"],
            "as_of_date": "2026-09-14"})
        _wait(lambda: jobs.get_job(session, analysing.id).is_terminal)
        total = time.perf_counter() - started
        analysed = jobs.get_job(session, analysing.id)
        runtime.request_stop()
        thread.join(30)
        result.update(import_status=imported.status, import_rows=imported.result["row_count"],
                      import_ms=imported.result["duration_ms"], analysis_status=analysed.status,
                      analysis_ms=analysed.result["duration_ms"], wall_seconds_including_startup=round(total, 3),
                      peak_rss_mb=_self_usage()["peak_rss_mb"])
    app.close()
    return result


# -- orchestration --------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child")
    parser.add_argument("--scenarios", default="idle,loop,renewal,real")
    args = parser.parse_args()
    if args.child:
        payload = json.loads(sys.stdin.read())
        print(json.dumps(child(args.child, payload["app_url"], payload["worker_url"], payload["context"])))
        return 0

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    from mervio.persistence import migrate
    from mervio.persistence.database import Database
    from mervio.persistence.service import authorize_service, service_principal
    from mervio.persistence.stores import create_csv_connection, create_store
    from mervio.persistence.tenancy import TenantContext, TenantSession, create_organization, ensure_user

    admin = os.environ.get("MERVIO_BENCH_ADMIN_DATABASE_URL")
    if not admin:
        print("MERVIO_BENCH_ADMIN_DATABASE_URL requise", file=sys.stderr)
        return 2
    params = conninfo_to_dict(admin)
    if params.get("host", "") not in ("127.0.0.1", "localhost", "::1", ""):
        print("hote non local refuse", file=sys.stderr)
        return 2
    suffix = secrets.token_hex(4)
    database = f"mervio_worker_bench_{suffix}"
    roles = {kind: f"mervio_wbench_{kind}_{suffix}" for kind in ("migrator", "app", "worker")}
    password = secrets.token_hex(16)
    host = f"{params.get('host') or '127.0.0.1'}:{params.get('port', '5432')}"

    def url(kind):
        return "postgresql://" + roles[kind] + ":" + quote(password, safe="") + "@" + host + "/" + database

    results = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "machine": {"platform": platform.platform(), "python": platform.python_version(),
                           "cpu_count": os.cpu_count()},
               "scope": "mesures de base du processus (004.3.5); grande echelle en 004.3.9",
               "scenarios": {}}
    with psycopg.connect(admin, autocommit=True) as conn:
        results["machine"]["postgresql"] = conn.execute("SHOW server_version").fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN CREATEROLE PASSWORD {}").format(
            sql.Identifier(roles["migrator"]), sql.Literal(password)))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(database), sql.Identifier(roles["migrator"])))
    try:
        migrate.upgrade(url("migrator"))
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE mervio_app").format(
                sql.Identifier(roles["app"]), sql.Literal(password)))
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE mervio_app, mervio_worker").format(
                sql.Identifier(roles["worker"]), sql.Literal(password)))
        for scenario in args.scenarios.split(","):
            with Database(url("app")) as app, Database(url("worker")) as worker:
                owner = ensure_user(app, f"bench|worker|{scenario}")
                organization_id = create_organization(app, owner_user_id=owner, name=f"bench {scenario}")
                session = TenantSession(app, TenantContext(organization_id, owner))
                store = create_store(session, name="bench")
                connection = create_csv_connection(session, store_id=store.id, label="bench")
                authorize_service(session, service_principal(worker).id)
            context = {"organization_id": str(organization_id), "owner_id": str(owner), "store_id": str(store.id),
                       "connection_id": str(connection.id)}
            output = subprocess.run(
                [sys.executable, __file__, "--child", scenario], capture_output=True, text=True, check=True,
                input=json.dumps({"app_url": url("app"), "worker_url": url("worker"), "context": context}))
            results["scenarios"][scenario] = json.loads(output.stdout.strip().splitlines()[-1])
            print(scenario, json.dumps(results["scenarios"][scenario]), file=sys.stderr)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
            for role in ("worker", "app", "migrator"):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(roles[role])))
    RESULTS_DIR.mkdir(exist_ok=True)
    target = RESULTS_DIR / f"worker_{datetime.now(timezone.utc):%Y%m%d}_{platform.machine()}.json"
    target.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
