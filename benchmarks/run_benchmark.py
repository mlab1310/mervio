"""Benchmark reproductible du moteur analytique Mervio (Mission 004.0, ADR-004-012).

Mesurer d'abord, optimiser ensuite. Ce script ne modifie aucun algorithme.

Pour chaque taille:
1. genere `healthy_store` (profil fashion_eu, 365 jours, graine 42) dans
   data/benchmark/ (ignore par Git), sauf si un jeu identique existe deja;
2. lance trois processus Python isoles, pour que la memoire de pointe de l'un
   ne pollue pas l'autre:
   - `parse`: lecture CSV brute de l'export commandes (`read_csv`);
   - `stages`: chaque etape du pipeline, chronometree separement;
   - `end_to_end`: `run_analysis` + serialisation JSON, telle qu'appelee par la CLI.

Usage:
    python benchmarks/run_benchmark.py --sizes 100,1000,10000,100000
    python benchmarks/run_benchmark.py --sizes 1000000 --timeout 3600

Limites de mesure: `ru_maxrss` = memoire residente de pointe du processus
entier (interpreteur compris). Ingestion, normalisation et validation sont
entremelees dans les connecteurs (une seule passe): elles sont mesurees
ensemble, sans modifier le moteur pour les separer.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DATA_DIR = ROOT / "data" / "benchmark"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS: octets; Linux: kilo-octets
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def _timed(label, fn, timings):
    start = time.perf_counter()
    value = fn()
    timings[label] = round(time.perf_counter() - start, 4)
    return value


def child_parse(directory: Path) -> dict:
    from mervio.ingestion.base import read_csv
    timings = {}
    rows = _timed("csv_parse_orders", lambda: read_csv(directory / "shopify_orders.csv", "shopify"), timings)
    return {"timings_s": timings, "rows": len(rows), "peak_rss_mb": _peak_rss_mb()}


def child_stages(directory: Path, today: date) -> dict:
    import logging
    logging.disable(logging.WARNING)
    from mervio.analytics.anomaly import detect_anomalies
    from mervio.analytics.health import compute_business_health
    from mervio.analytics.insights import build_insights
    from mervio.analytics.kpi import compute_kpis
    from mervio.analytics.periods import last_complete_period, previous_period
    from mervio.analytics.pipeline import SERIES_METRICS, build_report, latest_data_day, load_dataset
    from mervio.analytics.profitability import compute_profitability
    from mervio.analytics.root_cause import analyse_revenue_change
    from mervio.analytics.timeseries import build_series, compare_all
    from mervio.config import AnalyticsConfig
    from mervio.synthetic.evaluation import engine_paths

    cfg, t = AnalyticsConfig(), {}
    dataset = _timed("ingest_normalize_validate", lambda: load_dataset(engine_paths(directory)), t)
    period = last_complete_period(latest_data_day(dataset), today, cfg.grain)
    prior = previous_period(period)
    window = _timed("window_slice", lambda: dataset.window(period.start, period.end), t)
    kpis = _timed("kpis", lambda: compute_kpis(window, period), t)
    profitability = _timed("profitability", lambda: compute_profitability(window, period.label), t)
    series = _timed("time_series", lambda: {n: build_series(dataset, period, cfg.lookback_periods, fn)
                                              for n, fn in SERIES_METRICS.items()}, t)
    anomalies = _timed("anomaly_detection", lambda: [a for n, p in series.items()
                                                     for a in detect_anomalies(n, p, cfg.anomaly)], t)
    comparisons = _timed("comparisons", lambda: compare_all(dataset, period, SERIES_METRICS), t)
    root_cause = _timed("root_cause", lambda: analyse_revenue_change(dataset, period, prior), t)
    health = _timed("business_health", lambda: compute_business_health(kpis, comparisons, profitability, window,
                                                                        dataset.quality, cfg.health), t)
    insights = _timed("insights", lambda: build_insights(kpis, anomalies, root_cause, profitability, window,
                                                         dataset.currency), t)
    report = _timed("report_assembly", lambda: build_report(dataset, window, period, prior, kpis, profitability,
                                                            series, anomalies, comparisons, root_cause, health,
                                                            insights, cfg), t)
    payload = _timed("json_serialization", lambda: json.dumps(report), t)
    return {"timings_s": t, "orders": len(dataset.orders), "report_bytes": len(payload), "peak_rss_mb": _peak_rss_mb()}


def child_end_to_end(directory: Path, today: date) -> dict:
    import logging
    logging.disable(logging.WARNING)
    from mervio.analytics.pipeline import run_analysis
    from mervio.synthetic.evaluation import engine_paths
    t = {}
    report = _timed("run_analysis", lambda: run_analysis(engine_paths(directory), today=today), t)
    _timed("json_serialization", lambda: json.dumps(report), t)
    return {"timings_s": t, "peak_rss_mb": _peak_rss_mb(), "health_score": report["business_health_score"]["score"]}


def ensure_dataset(orders: int) -> tuple:
    from mervio.synthetic import GeneratorConfig, generate_dataset
    config = GeneratorConfig(scenario="healthy_store", orders=orders, days=365, seed=42, profile="fashion_eu")
    directory = DATA_DIR / f"healthy_store_{orders}"
    manifest_path = directory / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text())["config"] == config.to_dict():
        return directory, json.loads(manifest_path.read_text()), None
    start = time.perf_counter()
    result = generate_dataset(config, directory)
    return directory, result.manifest, round(time.perf_counter() - start, 3)


def run_child(mode: str, directory: Path, timeout: int) -> dict:
    command = [sys.executable, __file__, "--child", mode, "--dir", str(directory)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "timeout_s": timeout}
    if completed.returncode != 0:
        return {"status": "failed", "stderr_tail": completed.stderr[-800:]}
    return {"status": "ok", **json.loads(completed.stdout.strip().splitlines()[-1])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="100,1000,10000,100000")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--child", choices=("parse", "stages", "end_to_end"))
    parser.add_argument("--dir")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    today = date(2026, 9, 14)

    if args.child:
        directory = Path(args.dir)
        handler = {"parse": lambda: child_parse(directory), "stages": lambda: child_stages(directory, today),
                   "end_to_end": lambda: child_end_to_end(directory, today)}[args.child]
        print(json.dumps(handler()))
        return 0

    results = {
        "benchmark": "mervio-engine", "dataset": "synthetic healthy_store / fashion_eu / 365 days / seed 42",
        "recorded_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "machine": {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
                    "cpu_count": os.cpu_count()},
        "runs": [],
    }
    for size in [int(s) for s in args.sizes.split(",")]:
        directory, manifest, generation_s = ensure_dataset(size)
        orders_bytes = (directory / "shopify_orders.csv").stat().st_size
        run = {"target_orders": size, "orders": manifest["counts"]["orders"],
               "order_rows": manifest["files"]["shopify_orders.csv"]["rows"],
               "orders_csv_mb": round(orders_bytes / 1e6, 2), "generation_s": generation_s}
        for mode in ("parse", "stages", "end_to_end"):
            run[mode] = run_child(mode, directory, args.timeout)
        results["runs"].append(run)
        e2e = run["end_to_end"]
        print(f"{size:>9} orders: generation {generation_s}s | parse {run['parse'].get('timings_s')} | "
              f"end_to_end {e2e.get('timings_s')} peak {e2e.get('peak_rss_mb')} MB [{e2e['status']}]", flush=True)
    out = Path(args.out) if args.out else RESULTS_DIR / f"engine_{date.today():%Y%m%d}_{platform.machine()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
