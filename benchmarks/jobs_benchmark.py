"""Benchmark de la file de travaux PostgreSQL (Mission 004.2). Mesure, n'optimise pas.

Pour chaque profondeur de file (10 k, 100 k, 1 M travaux en attente):
- cout d'une mise en file par l'API reelle (`workers.enqueue`, audit compris);
- latence de selection du prochain travail (p50 / p95 / max) a cette profondeur;
- debit de prise et d'execution avec 1, 2, 4 puis 8 workers concurrents, chacun
  sur SA connexion PostgreSQL, via le vrai `Worker.run_until_empty`;
- debit de purge;
- taille des tables et memoire de pointe;
- plan d'execution de la prise (garde anti-regression: index, pas de tri).

Ce qui est mesure est le vrai chemin: `jobs.claim_next_job` (SELECT ... FOR UPDATE
SKIP LOCKED), les vrais triggers de machine a etats, la vraie RLS et le vrai audit.

Seul le REMPLISSAGE de la file est fait en lot (INSERT ... SELECT FROM unnest, comme
les lignes canoniques de 004.1): amener un million de travaux un par un mesurerait
la latence du reseau local, pas la file. Le cout unitaire de la mise en file reelle
est mesure separement, sur un echantillon.

    MERVIO_BENCH_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres \\
        python benchmarks/jobs_benchmark.py --sizes 10000,100000,1000000
"""
from __future__ import annotations

import argparse
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
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
RESULTS_DIR = Path(__file__).resolve().parent / "results"

#: travaux reellement executes par configuration de workers (la file, elle, reste pleine)
SAMPLE = 2000
#: mises en file mesurees une par une avec l'API reelle
ENQUEUE_SAMPLE = 500
#: prises mesurees individuellement pour la latence de selection
LATENCY_SAMPLE = 200
#: travaux termines et dates d'avant-hier, pour mesurer la purge
PURGE_SAMPLE = 2000
WORKER_COUNTS = (1, 2, 4, 8)
FILL_BATCH = 10000


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def _rate(count: int, seconds: float) -> float:
    return round(count / seconds, 1) if seconds > 0 else 0.0


def child(size: int, app_url: str) -> dict:
    from mervio.observability.logging import configure_json_logging
    from mervio.persistence import audit, jobs
    from mervio.persistence.database import Database
    from mervio.persistence.jobs import JobStatus, JobType
    from mervio.persistence.stores import create_store
    from mervio.persistence.tenancy import TenantContext, TenantSession, create_organization, ensure_user
    from mervio.workers.handlers import HandlerRegistry
    from mervio.workers.worker import Worker, enqueue

    import logging
    configure_json_logging(level=logging.CRITICAL)  # le benchmark mesure la file, pas l'ecriture des logs

    database = Database(app_url)
    owner = ensure_user(database, f"bench|jobs|{size}")
    organization_id = create_organization(database, owner_user_id=owner, name="bench")
    session = TenantSession(database, TenantContext(organization_id, owner))
    store = create_store(session, name="bench")
    timings: dict = {}

    # -- 1. mise en file par l'API reelle (travail + evenement d'audit, une transaction) ------
    start = time.perf_counter()
    for index in range(ENQUEUE_SAMPLE):
        enqueue(session, job_type=JobType.ANALYSIS, store_id=store.id,
                payload={"store_id": str(store.id), "n": index})
    enqueue_seconds = time.perf_counter() - start
    timings["enqueue_api"] = {"jobs": ENQUEUE_SAMPLE, "seconds": round(enqueue_seconds, 3),
                              "per_job_ms": round(enqueue_seconds * 1000 / ENQUEUE_SAMPLE, 3),
                              "jobs_per_second": _rate(ENQUEUE_SAMPLE, enqueue_seconds)}

    # -- 2. remplissage en lot jusqu'a la profondeur voulue ----------------------------------
    remaining = size - ENQUEUE_SAMPLE
    start = time.perf_counter()
    while remaining > 0:
        batch = min(FILL_BATCH, remaining)
        _fill(database, organization_id, owner, store.id, batch)
        remaining -= batch
    fill_seconds = time.perf_counter() - start
    timings["bulk_fill"] = {"jobs": max(0, size - ENQUEUE_SAMPLE), "seconds": round(fill_seconds, 3),
                            "jobs_per_second": _rate(max(0, size - ENQUEUE_SAMPLE), fill_seconds)}

    with database.transaction(organization_id=organization_id, user_id=owner) as conn:
        conn.execute("ANALYZE jobs")
        conn.execute("ANALYZE audit_events")

    queued = jobs.queue_statistics(session)
    assert queued["by_status"].get(JobStatus.QUEUED.value) == size, queued

    # -- 3. latence de selection a cette profondeur -------------------------------------------
    # Deux mesures, parce qu'elles ne disent pas la meme chose:
    # - a froid, juste apres le chargement en lot: pages d'index absentes du cache et
    #   autovacuum encore actif sur la table fraichement ecrite. La PREMIERE prise paie
    #   les deux (centaines de millisecondes a 1 M), les suivantes non;
    # - en regime etabli: ce que voit un worker en production sur une file deja chaude.
    timings["claim_latency_cold_ms"] = _latency(jobs, session, "bench-cold")
    timings["claim_latency_ms"] = _latency(jobs, session, "bench-latency")

    # -- 4. plan de l'instruction REELLE de prise, a cette profondeur --------------------------
    timings["claim_plan"] = _describe(jobs.explain_claim(session))

    # -- 5. debit reel, 1 a 8 workers concurrents --------------------------------------------
    registry = HandlerRegistry().register(JobType.ANALYSIS, lambda context: {"n": 1})
    throughput = {}
    for count in WORKER_COUNTS:
        sessions = [TenantSession(Database(app_url), TenantContext(organization_id, owner))
                    for _ in range(count)]
        workers = [Worker(sessions=[s], registry=registry, worker_id=f"bench-{count}-{index}")
                   for index, s in enumerate(sessions)]
        per_worker = SAMPLE // count
        barrier = threading.Barrier(count, timeout=120)

        def run(worker):
            barrier.wait()
            return worker.run_until_empty(max_jobs=per_worker)

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=count) as pool:
            done = [len(future.result()) for future in [pool.submit(run, w) for w in workers]]
        seconds = time.perf_counter() - start
        for s in sessions:
            s.database.close()
        throughput[str(count)] = {"workers": count, "jobs": sum(done), "seconds": round(seconds, 3),
                                  "jobs_per_second": _rate(sum(done), seconds),
                                  "spread": done}
    timings["throughput"] = throughput

    # -- 6. purge --------------------------------------------------------------------------------
    # La base refuse de supprimer un travail termine depuis moins d'une heure. On prepare
    # donc un lot date d'avant-hier, avec les VRAIES transitions (prise puis succes), et on
    # mesure le cout de la selection et de la suppression.
    aged = datetime.now(timezone.utc) - timedelta(days=2)
    _fill(database, organization_id, owner, store.id, PURGE_SAMPLE, available_at=aged)
    for _ in range(PURGE_SAMPLE):
        job = jobs.claim_next_job(session, worker_id="bench-purge", now=aged)
        jobs.mark_succeeded(session, job, worker_id="bench-purge", now=aged + timedelta(seconds=1))
    purged, start = 0, time.perf_counter()
    while True:
        deleted = jobs.purge_terminal_jobs(session, before=datetime.now(timezone.utc) - timedelta(days=1),
                                           statuses=[JobStatus.SUCCEEDED], limit=1000)
        purged += deleted
        if deleted == 0:
            break
    purge_seconds = time.perf_counter() - start
    timings["purge"] = {"deleted": purged, "seconds": round(purge_seconds, 3),
                        "rows_per_second": _rate(purged, purge_seconds)}

    # -- 7. tailles et compteurs ----------------------------------------------------------------
    with database.transaction(organization_id=organization_id, user_id=owner) as conn:
        sizes = dict(conn.execute(
            "SELECT relname, pg_total_relation_size(oid) FROM pg_class "
            "WHERE relname IN ('jobs', 'audit_events') ORDER BY relname").fetchall())
        index_sizes = dict(conn.execute(
            "SELECT indexrelname, pg_relation_size(indexrelid) FROM pg_stat_user_indexes "
            "WHERE relname = 'jobs' ORDER BY indexrelname").fetchall())
    final = jobs.queue_statistics(session)
    events = audit.count_events(session)
    database.close()

    return {
        "queue_depth": size,
        "timings": timings,
        "final_by_status": final["by_status"],
        "audit_events": events,
        "table_sizes_mb": {name: round(value / 1048576, 1) for name, value in sizes.items()},
        "jobs_index_sizes_mb": {name: round(value / 1048576, 1) for name, value in index_sizes.items()},
        "peak_rss_mb": _peak_rss_mb(),
    }


def _latency(jobs, session, worker_id: str) -> dict:
    """Latence de `claim_next_job`, mesuree une prise a la fois, sur le vrai chemin."""
    latencies, claimed = [], []
    for _ in range(LATENCY_SAMPLE):
        moment = time.perf_counter()
        job = jobs.claim_next_job(session, worker_id=worker_id)
        latencies.append((time.perf_counter() - moment) * 1000)
        claimed.append(job)
    for job in claimed:
        jobs.mark_succeeded(session, job, worker_id=worker_id)
    latencies.sort()
    return {"samples": LATENCY_SAMPLE,
            "p50": round(statistics.median(latencies), 3),
            "p95": round(latencies[int(0.95 * (LATENCY_SAMPLE - 1))], 3),
            "max": round(latencies[-1], 3),
            "over_50ms": sum(1 for value in latencies if value > 50)}


def _fill(database, organization_id, owner, store_id, count: int, *, available_at=None) -> None:
    """Remplit la file en lot, avec le role applicatif et sous RLS (COPY est refuse sous RLS).

    Memes colonnes, memes contraintes, memes triggers qu'une mise en file unitaire:
    seul le nombre d'allers-retours change.
    """
    ids = [uuid.uuid4() for _ in range(count)]
    correlations = [uuid.uuid4() for _ in range(count)]
    priorities = [index % 5 for index in range(count)]
    with database.transaction(organization_id=organization_id, user_id=owner) as conn:
        conn.execute(
            "INSERT INTO jobs (id, organization_id, store_id, job_type, status, priority, correlation_id, "
            "enqueued_by, payload, available_at) "
            "SELECT t.id, %s, %s, 'analysis', 'queued', t.priority, t.correlation_id, %s, '{}'::jsonb, "
            "       COALESCE(%s::timestamptz, now()) "
            "FROM unnest(%s::uuid[], %s::smallint[], %s::uuid[]) AS t(id, priority, correlation_id)",
            (organization_id, store_id, owner, available_at, ids, priorities, correlations),
        )


def _describe(plan: dict) -> str:
    parts = []
    stack = [plan]
    while stack:
        node = stack.pop()
        label = node["Node Type"]
        if node.get("Index Name"):
            label += f" / {node['Index Name']}"
        parts.append(label)
        stack.extend(node.get("Plans", []))
    return " <- ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="10000,100000")
    parser.add_argument("--child", type=int)
    parser.add_argument("--app-url")
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(args.child, args.app_url)))
        return 0

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    from mervio.persistence import migrate

    admin = os.environ.get("MERVIO_BENCH_ADMIN_DATABASE_URL")
    if not admin:
        print("MERVIO_BENCH_ADMIN_DATABASE_URL requise", file=sys.stderr)
        return 2
    params = conninfo_to_dict(admin)
    if params.get("host", "") not in ("127.0.0.1", "localhost", "::1", ""):
        print("hote non local refuse", file=sys.stderr)
        return 2
    suffix = secrets.token_hex(4)
    database = f"mervio_jobs_bench_{suffix}"
    migrator, app = f"mervio_jobs_migrator_{suffix}", f"mervio_jobs_app_{suffix}"
    password = secrets.token_hex(16)
    base = f"{params.get('host', '127.0.0.1')}:{params.get('port', '5432')}/{database}"
    sizes = [int(s) for s in args.sizes.split(",")]
    results = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "machine": {"platform": platform.platform(), "python": platform.python_version(),
                           "cpu_count": os.cpu_count()},
               "method": {"sample_jobs_executed": SAMPLE, "enqueue_sample": ENQUEUE_SAMPLE,
                          "latency_sample": LATENCY_SAMPLE, "purge_sample": PURGE_SAMPLE,
                          "worker_counts": list(WORKER_COUNTS)},
               "runs": {}}
    with psycopg.connect(admin, autocommit=True) as conn:
        results["machine"]["postgresql"] = conn.execute("SHOW server_version").fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN CREATEROLE PASSWORD {}").format(
            sql.Identifier(migrator), sql.Literal(password)))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(database), sql.Identifier(migrator)))
    try:
        migrate.upgrade(f"postgresql://{migrator}:{password}@{base}")
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE mervio_app").format(
                sql.Identifier(app), sql.Literal(password)))
        for size in sizes:
            output = subprocess.run([sys.executable, __file__, "--child", str(size), "--app-url",
                                     f"postgresql://{app}:{password}@{base}"],
                                    capture_output=True, text=True, check=True)
            results["runs"][str(size)] = json.loads(output.stdout.strip().splitlines()[-1])
            print(size, json.dumps(results["runs"][str(size)]["timings"]["throughput"]), file=sys.stderr)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(app)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(migrator)))
    RESULTS_DIR.mkdir(exist_ok=True)
    target = RESULTS_DIR / f"jobs_{datetime.now(timezone.utc):%Y%m%d}_{platform.machine()}.json"
    target.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
