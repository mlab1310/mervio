"""Benchmark de la persistance PostgreSQL (Mission 004.1). Mesure, n'optimise pas.

Pour chaque taille (jeux `healthy_store` de benchmarks/run_benchmark.py, regeneres s'ils manquent):
- lecture CSV (connecteurs existants), ecriture de l'instantane, relecture du Dataset;
- analyse du Dataset relu, persistance du rapport, relecture du rapport;
- verification: Dataset relu == Dataset CSV, rapport relu == rapport serialise;
- plans d'execution des lectures d'instantane (index attendu, pas de parcours complet).

Base: une base et des roles jetables crees via MERVIO_BENCH_ADMIN_DATABASE_URL (hote local
uniquement), supprimes a la fin. Un processus par taille (memoire de pointe isolee).

    MERVIO_BENCH_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres \\
        python benchmarks/persistence_benchmark.py --sizes 10000,100000
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DATA_DIR = ROOT / "data" / "benchmark"


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def _timed(label, fn, timings):
    start = time.perf_counter()
    value = fn()
    timings[label] = round(time.perf_counter() - start, 3)
    return value


def child(size: int, app_url: str) -> dict:
    from mervio.analytics.pipeline import analyze_loaded_dataset, load_dataset
    from mervio.application.service import annotate_report
    from mervio.config import AnalyticsConfig
    from mervio.persistence import analyses, snapshots
    from mervio.persistence.codec import file_sha256, serialize_report
    from mervio.persistence.database import Database
    from mervio.persistence.snapshots import SourceFile
    from mervio.persistence.stores import create_csv_connection, create_store
    from mervio.persistence.tenancy import TenantContext, TenantSession, create_organization, ensure_user
    from mervio.synthetic.evaluation import analysis_day, engine_paths

    directory = DATA_DIR / f"healthy_store_{size}"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    paths = engine_paths(directory)
    files = {k: v for k, v in vars(paths).items() if v}
    timings: dict = {}
    db = Database(app_url)
    owner = ensure_user(db, f"bench|{size}")
    session = TenantSession(db, TenantContext(create_organization(db, owner_user_id=owner, name="bench"), owner))
    store = create_store(session, name="bench")
    connection = create_csv_connection(session, store_id=store.id, label="bench")

    csv_dataset = _timed("csv_load_dataset", lambda: load_dataset(paths), timings)
    sources = [SourceFile(kind, *file_sha256(path)) for kind, path in files.items()]
    record = _timed("db_write_snapshot", lambda: snapshots.write_snapshot(
        session, store_id=store.id, connection_id=connection.id, dataset=csv_dataset, sources=sources,
        inputs_sha256=secrets.token_hex(32), synthetic=True, synthetic_manifest=manifest), timings)
    db_dataset = _timed("db_load_dataset", lambda: snapshots.load_dataset(session, store.id, record.id), timings)
    equal = _timed("verify_dataset_equality", lambda: db_dataset == csv_dataset, timings)
    today = analysis_day(manifest)
    config = AnalyticsConfig()
    report = _timed("analyze_loaded_dataset", lambda: analyze_loaded_dataset(db_dataset, config, today), timings)
    annotate_report(report, synthetic=True, label="")
    run = analyses.start_run(session, store_id=store.id, snapshot_id=record.id, config=config, as_of_date=today)
    report_record = _timed("db_persist_report", lambda: analyses.complete_run(session, run, report), timings)
    stored = _timed("db_read_report", lambda: analyses.get_report(session, store.id, report_record.id), timings)

    with db.transaction(organization_id=session.organization_id, user_id=owner) as conn:
        plans = {}
        for table, order in (("orders", "position"), ("order_lines", "order_position, position")):
            plan = conn.execute(
                f"EXPLAIN (FORMAT JSON) SELECT * FROM {table} WHERE organization_id = %s AND store_id = %s "
                f"AND snapshot_id = %s ORDER BY {order}", (session.organization_id, store.id, record.id)).fetchone()[0]
            plans[table] = plan[0]["Plan"]["Node Type"] + " / " + (
                plan[0]["Plan"].get("Index Name") or plan[0]["Plan"].get("Plans", [{}])[0].get("Node Type", ""))
        size_rows = conn.execute(
            "SELECT relname, pg_total_relation_size(oid) FROM pg_class WHERE relname IN "
            "('orders', 'order_lines', 'payments', 'refunds', 'reports') ORDER BY relname").fetchall()
    db.close()
    return {
        "orders": len(csv_dataset.orders), "canonical_rows": record.row_count, "timings_s": timings,
        "dataset_equal": equal, "report_bytes_equal": stored.payload_json == serialize_report(report),
        "report_bytes": report_record.payload_bytes, "plans": plans,
        "table_sizes_mb": {name: round(size / 1048576, 1) for name, size in size_rows},
        "peak_rss_mb": _peak_rss_mb(),
    }


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
    from mervio.synthetic import GeneratorConfig, generate_dataset

    admin = os.environ.get("MERVIO_BENCH_ADMIN_DATABASE_URL")
    if not admin:
        print("MERVIO_BENCH_ADMIN_DATABASE_URL requise", file=sys.stderr)
        return 2
    params = conninfo_to_dict(admin)
    if params.get("host", "") not in ("127.0.0.1", "localhost", "::1", ""):
        print("hote non local refuse", file=sys.stderr)
        return 2
    suffix = secrets.token_hex(4)
    database, migrator, app = f"mervio_bench_{suffix}", f"mervio_bench_migrator_{suffix}", f"mervio_bench_app_{suffix}"
    password = secrets.token_hex(16)
    base = f"{params.get('host', '127.0.0.1')}:{params.get('port', '5432')}/{database}"
    sizes = [int(s) for s in args.sizes.split(",")]
    results = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "machine": {"platform": platform.platform(), "python": platform.python_version(),
                           "cpu_count": os.cpu_count()},
               "runs": {}}
    with psycopg.connect(admin, autocommit=True) as conn:
        results["machine"]["postgresql"] = conn.execute("SHOW server_version").fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN CREATEROLE PASSWORD {}").format(
            sql.Identifier(migrator), sql.Literal(password)))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(migrator)))
    try:
        migrate.upgrade(f"postgresql://{migrator}:{password}@{base}")
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE mervio_app").format(
                sql.Identifier(app), sql.Literal(password)))
        for size in sizes:
            directory = DATA_DIR / f"healthy_store_{size}"
            if not (directory / "manifest.json").exists():
                generate_dataset(GeneratorConfig(scenario="healthy_store", orders=size, days=365, seed=42), directory)
            output = subprocess.run([sys.executable, __file__, "--child", str(size), "--app-url",
                                     f"postgresql://{app}:{password}@{base}"],
                                    capture_output=True, text=True, check=True)
            results["runs"][str(size)] = json.loads(output.stdout.strip().splitlines()[-1])
            print(size, json.dumps(results["runs"][str(size)]["timings_s"]), file=sys.stderr)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(app)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(migrator)))
    RESULTS_DIR.mkdir(exist_ok=True)
    target = RESULTS_DIR / f"persistence_{datetime.now(timezone.utc):%Y%m%d}_{platform.machine()}.json"
    target.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
