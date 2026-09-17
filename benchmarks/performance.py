#!/usr/bin/env python3
"""Suite de benchmarks de performance de Mervio (Mission 004.3.9). Mesurer d'abord, optimiser ensuite.

    python benchmarks/performance.py                                   # matrice complete (~45 min)
    python benchmarks/performance.py --workloads analytics --sizes 10000,100000
    python benchmarks/performance.py --workloads queue --queue-depths 100,1000 --queue-org-scaling 10

Charges (chaque mesure dans un processus NEUF; aucune modification du produit):

    analytics     moteur seul, sans base: lecture CSV, etapes du pipeline, `run_analysis` complet
    persistence   PostgreSQL reel (role applicatif, RLS): instantane, relecture, rapport, import complet
    queue         INFRASTRUCTURE: sonde du dispatcher, prise, fin, mise en file, cadre du worker a vide
    worker        processus `mervio worker` reel: import puis analyse reels, chaines de bout en bout
    lease         renouvellements du gardien de bail, au repos et pendant des imports reels
    concurrency   1..N processus `mervio worker` reels sur des analyses reelles

Donnees: `mervio.synthetic`, scenario healthy_store, profil fashion_eu, 365 jours, graine 42, dans
data/benchmark/ (ignore par Git), regenerees seulement si leur configuration differe.

Base: MERVIO_BENCH_ADMIN_DATABASE_URL (serveur LOCAL uniquement). Chaque charge PostgreSQL cree sa base
et ses roles `mervio_perf_*` (aucun superutilisateur ni BYPASSRLS pour Mervio) et les supprime.

Resultats: benchmark-results/ (ignore par Git), un document JSON par execution.
Codes de sortie: 0 toutes les charges demandees executees (ou explicitement non executees),
1 une charge en echec ou hors delai, 2 configuration invalide.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import perf_harness as harness  # noqa: E402

WORKLOADS = ("analytics", "persistence", "queue", "worker", "lease", "concurrency")
DB_WORKLOADS = ("persistence", "queue", "worker", "lease", "concurrency")
CHILD = HERE / "perf_workloads.py"
ENGINE_STAGES = ("ingest_normalize_validate", "window_slice", "kpis", "profitability", "time_series",
                 "anomaly_detection", "comparisons", "root_cause", "business_health", "insights",
                 "report_assembly", "json_serialization")

#: repetitions par defaut: nombreuses quand c'est rapide, reduites (et declarees) quand c'est lourd
DEFAULT_REPETITIONS = {
    "analytics": ((10_000, 7), (100_000, 5), (harness.MAX_SIZE, 3)),
    "persistence": ((10_000, 5), (100_000, 3), (harness.MAX_SIZE, 1)),
    "worker": ((10_000, 5), (100_000, 3), (harness.MAX_SIZE, 1)),
}


def repetitions_for(workload: str, size: int, override: Optional[int]) -> int:
    if override is not None:
        return override
    for limit, count in DEFAULT_REPETITIONS[workload]:
        if size <= limit:
            return count
    return 1


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


class Suite:
    def __init__(self, args: argparse.Namespace, environ: Mapping[str, str]) -> None:
        self.args = args
        self.environ = environ
        self.secrets: List[str] = []
        self.admin: Optional[str] = None
        self.admin_problem: Optional[str] = None
        if set(args.workloads) & set(DB_WORKLOADS):
            try:
                self.admin = harness.admin_url(environ)
            except harness.BenchmarkConfigError as exc:
                self.admin_problem = str(exc)
        postgresql = harness.server_version(self.admin) if self.admin else None
        self.document = harness.new_document(
            suite_run_id=secrets.token_hex(6), postgresql=postgresql,
            configuration={key: value for key, value in vars(args).items() if key not in ("output", "data_dir")})
        self.datasets: Dict[int, Dict[str, Any]] = {}

    # -- utilitaires ---------------------------------------------------------------------------
    def dataset(self, size: int) -> Dict[str, Any]:
        if size not in self.datasets:
            log(f"jeu de donnees {size} commandes")
            info = harness.ensure_dataset(size, Path(self.args.data_dir))
            self.datasets[size] = info
            public = {key: value for key, value in info.items() if key != "directory"}
            directory = Path(info["directory"]).resolve()
            public["path"] = (str(directory.relative_to(harness.ROOT)) if directory.is_relative_to(harness.ROOT)
                              else directory.name)
            self.document["dataset"]["sizes"][str(size)] = public
        return self.datasets[size]

    def child(self, workload: str, payload: Mapping[str, Any], secrets_: Sequence[str] = ()) -> Dict[str, Any]:
        return harness.run_child(CHILD, workload, payload, timeout=self.args.timeout,
                                 secrets_=[*self.secrets, *secrets_])

    def memory_refusal(self, size: int) -> Optional[str]:
        return harness.check_memory(size, available=harness.available_memory_gb(), force=self.args.force_memory)

    def database(self, label: str):
        database = harness.DisposableDatabase(self.admin, label)
        self.secrets.append(database.password)
        return database

    def record(self, workload: str, cases: List[Dict[str, Any]], extra: Optional[Mapping[str, Any]] = None) -> None:
        statuses = {item["status"] for item in cases}
        status = ("failed" if "failed" in statuses else "timeout" if "timeout" in statuses
                  else "not_executed" if statuses == {"not_executed"} else "ok")
        entry = {"status": status, "cases": cases}
        if status == "not_executed":
            entry["reason"] = cases[0].get("reason") if cases else "aucun cas"
        entry.update(extra or {})
        self.document["workloads"][workload] = entry

    def skipped_database(self, workload: str) -> None:
        self.record(workload, [harness.case(workload, category="infrastructure", size=None, status="not_executed",
                                            reason=self.admin_problem)])

    # -- A. moteur -----------------------------------------------------------------------------
    def run_analytics(self) -> None:
        cases = []
        for size in self.args.sizes:
            refusal = self.memory_refusal(size)
            if refusal:
                cases.append(harness.case(f"analytics/{size}", category="business_analytics", size=size,
                                          status="not_executed", reason=refusal))
                continue
            data = self.dataset(size)
            payload = {"directory": data["directory"]}
            count = repetitions_for("analytics", size, self.args.repetitions)
            log(f"analytics {size}: 1 premier passage + {count} repetitions")
            first = {mode: self.child(mode, payload) for mode in ("analytics_stages", "analytics_e2e")}
            runs = {mode: [self.child(mode, payload) for _ in range(count)]
                    for mode in ("analytics_parse", "analytics_stages", "analytics_e2e")}
            failures = [r for group in [list(first.values()), *runs.values()] for r in group if r["status"] != "ok"]
            if failures:
                cases.append(harness.case(f"analytics/{size}", category="business_analytics", size=size,
                                          status=failures[0]["status"], reason=failures[0].get("reason"),
                                          repetitions=count))
                continue
            e2e, stages, parse = runs["analytics_e2e"], runs["analytics_stages"], runs["analytics_parse"]
            stage_medians = {name: harness.summarize(r["stages_s"][name] for r in stages) for name in ENGINE_STAGES}
            total = sum(summary["median"] for summary in stage_medians.values()) or 1.0
            median_wall = harness.summarize(r["wall_s"] for r in e2e)["median"]
            cases.append(harness.case(
                f"analytics/{size}", category="business_analytics", size=size, status="ok", repetitions=count,
                metrics={
                    "e2e_wall_s": harness.summarize(r["wall_s"] for r in e2e),
                    "e2e_cpu_s": harness.summarize(r["cpu_s"] for r in e2e),
                    "e2e_peak_rss_mb": harness.summarize(r["child_peak_rss_mb"] for r in e2e),
                    "e2e_baseline_rss_mb": harness.summarize(r["baseline_rss_mb"] for r in e2e),
                    "e2e_final_rss_mb": harness.summarize(r["final_rss_mb"] for r in e2e),
                    "parse_wall_s": harness.summarize(r["wall_s"] for r in parse),
                    "parse_peak_rss_mb": harness.summarize(r["child_peak_rss_mb"] for r in parse),
                    "stages_peak_rss_mb": harness.summarize(r["child_peak_rss_mb"] for r in stages),
                    "report_bytes": harness.summarize(r["report_bytes"] for r in e2e),
                },
                throughput={"orders_per_second_median": harness.rate(data["orders"], median_wall),
                            "order_rows_per_second_median": harness.rate(data["order_rows"], median_wall)},
                details={
                    "stages_s": stage_medians,
                    "stage_share_of_median_total": {name: round(stage_medians[name]["median"] / total, 3)
                                                    for name in ENGINE_STAGES},
                    "first_process_run": {"e2e_wall_s": first["analytics_e2e"]["wall_s"],
                                          "e2e_peak_rss_mb": first["analytics_e2e"]["child_peak_rss_mb"],
                                          "stages_wall_s": first["analytics_stages"]["wall_s"]},
                    "health_score": e2e[0]["health_score"],
                    "input": {"orders": data["orders"], "order_rows": data["order_rows"],
                              "orders_csv_bytes": data["orders_csv_bytes"]},
                }))
        self.record("analytics", cases)

    # -- B. persistance ------------------------------------------------------------------------
    def run_persistence(self) -> None:
        cases = []
        for size in self.args.sizes:
            refusal = self.memory_refusal(size)
            if refusal:
                cases.append(harness.case(f"persistence/{size}", category="persistence", size=size,
                                          status="not_executed", reason=refusal))
                continue
            data = self.dataset(size)
            count = repetitions_for("persistence", size, self.args.repetitions)
            log(f"persistence {size}: {count} repetitions (base neuve pour cette taille)")
            database = self.database("db")
            try:
                database.create()
                runs = [self.child("persistence", {"directory": data["directory"], "urls": database.urls()})
                        for _ in range(count)]
            finally:
                database.drop()
            failures = [r for r in runs if r["status"] != "ok"]
            if failures:
                cases.append(harness.case(f"persistence/{size}", category="persistence", size=size,
                                          status=failures[0]["status"], reason=failures[0].get("reason"),
                                          repetitions=count))
                continue
            keys = runs[0]["timings_s"].keys()
            write = harness.summarize(r["timings_s"]["db_write_snapshot"] for r in runs)
            cases.append(harness.case(
                f"persistence/{size}", category="persistence", size=size, status="ok", repetitions=count,
                metrics={**{f"{key}_s": harness.summarize(r["timings_s"][key] for r in runs) for key in keys},
                         "peak_rss_mb": harness.summarize(r["child_peak_rss_mb"] for r in runs)},
                throughput={"canonical_rows_written_per_second_median":
                            harness.rate(runs[0]["canonical_rows"], write["median"])},
                details={"canonical_rows": runs[0]["canonical_rows"], "orders": runs[0]["orders"],
                         "dataset_equal": all(r["dataset_equal"] for r in runs),
                         "report_bytes_equal": all(r["report_bytes_equal"] for r in runs),
                         "import_status": sorted({r["import_status"] for r in runs}),
                         "report_bytes": runs[0]["report_bytes"],
                         "table_sizes_mb_after_first_write": runs[0]["table_sizes_mb_after_first_write"],
                         "table_sizes_mb_after_all_repetitions": runs[-1]["table_sizes_mb_cumulative"],
                         "note": "chaque repetition ecrit DEUX instantanes (appel de persistance et cas d'usage) "
                                 "dans la meme base: les tables grossissent d'une repetition a l'autre"}))
        self.record("persistence", cases)

    # -- C. file et dispatcher -----------------------------------------------------------------
    def run_queue(self) -> None:
        combinations = [(depth, self.args.queue_organizations) for depth in self.args.queue_depths]
        combinations += [(self.args.queue_org_scaling_depth, organizations)
                         for organizations in self.args.queue_org_scaling
                         if (self.args.queue_org_scaling_depth, organizations) not in combinations]
        cases = []
        for depth, organizations in combinations:
            log(f"queue: {depth} travaux en attente sur {organizations} organisations")
            database = self.database("queue")
            try:
                database.create()
                result = self.child("queue", {
                    "urls": database.urls(), "depth": depth, "organizations": organizations,
                    "samples": self.args.queue_samples, "noop_jobs": self.args.noop_jobs,
                    "thread_counts": self.args.noop_threads})
            finally:
                database.drop()
            name = f"queue/depth={depth}/orgs={organizations}"
            if result["status"] != "ok":
                cases.append(harness.case(name, category="infrastructure", size=depth, status=result["status"],
                                          reason=result.get("reason")))
                continue
            cases.append(harness.case(
                name, category="infrastructure", size=depth, status="ok", repetitions=self.args.queue_samples,
                metrics={"dispatcher_probe_ms": result["dispatcher_probe_ms"],
                         "dispatcher_rotation_ms": result["dispatcher_rotation_ms"],
                         "claim_ms": result["claim_ms"], "complete_ms": result["complete_ms"],
                         "enqueue_api_ms": result["enqueue_api_ms"]},
                throughput={"noop_worker": result["noop_worker"], "bulk_fill": result["bulk_fill"]},
                details={"organizations": organizations, "claimed": result["claimed"],
                         "table_sizes_mb": result["table_sizes_mb"], "probe_plan": result["probe_plan"],
                         "noop_top_up_jobs_per_pass": result["noop_top_up_jobs_per_pass"],
                         "peak_rss_mb": result["child_peak_rss_mb"], "setup_s": result["setup_s"],
                         "warning": "noop_worker = cadre du worker avec un gestionnaire VIDE: ce n'est pas un "
                                    "debit analytique"}))
        self.record("queue", cases)

    # -- D/G. worker reel ----------------------------------------------------------------------
    def run_worker(self) -> None:
        cases = []
        for size in self.args.sizes:
            refusal = self.memory_refusal(size)
            if refusal:
                cases.append(harness.case(f"worker_chain/{size}", category="end_to_end", size=size,
                                          status="not_executed", reason=refusal))
                continue
            data = self.dataset(size)
            count = repetitions_for("worker", size, self.args.repetitions)
            log(f"worker {size}: {count} chaines import -> analyse dans un processus `mervio worker`")
            database = self.database("worker")
            try:
                database.create()
                result = self.child("worker_chain", {"directory": data["directory"], "urls": database.urls(),
                                                     "repetitions": count, "job_timeout": self.args.timeout})
            finally:
                database.drop()
            name = f"worker_chain/{size}"
            if result["status"] != "ok":
                cases.append(harness.case(name, category="end_to_end", size=size, status=result["status"],
                                          reason=result.get("reason"), repetitions=count))
                continue
            runs = result["runs"]
            metrics = {}
            for kind in ("import", "analysis"):
                for key in ("queue_wait_ms", "handler_ms", "attempt_ms", "framework_overhead_ms", "observed_ms"):
                    values = [r[kind][key] for r in runs if r[kind][key] is not None]
                    metrics[f"{kind}_{key}"] = harness.summarize(values)
            metrics["chain_ms"] = harness.summarize(r["chain_ms"] for r in runs)
            chain = metrics["chain_ms"]["median"] / 1000
            ok = all(r["import"]["status"] == "succeeded" and r["analysis"]["status"] == "succeeded"
                     and r["audit_complete"] and r["report_persisted"] for r in runs)
            cases.append(harness.case(
                name, category="end_to_end", size=size, status="ok" if ok else "failed", repetitions=count,
                reason=None if ok else "un travail, un rapport ou une trace d'audit manque",
                metrics=metrics,
                throughput={"orders_per_second_chain_median": harness.rate(data["orders"], chain)},
                details={"worker_peak_rss_mb": result["worker_peak_rss_mb"],
                         "worker_sampled_peak_rss_mb": result["worker_sampled_peak_rss_mb"],
                         "worker_ready_rss_mb": result["worker_ready_rss_mb"],
                         "worker_rss_after_each_chain_mb": [r["worker_rss_mb_after"] for r in runs],
                         "worker_cpu_s": result["worker_cpu_s"], "worker_exit_code": result["worker_exit_code"],
                         "worker_error_events": result["worker_error_events"],
                         "lease_seconds": result["lease_seconds"], "renew_seconds": result["renew_seconds"],
                         "lease_renewals_total": result["lease_renewals_total"],
                         "import_rows": runs[0]["import"]["rows"], "report_bytes": runs[0]["analysis"]["report_bytes"],
                         "audit_events_per_chain": runs[0]["audit_events"],
                         "same_worker_process_for_all_repetitions": True}))
        self.record("worker", cases)

    # -- E. bail ------------------------------------------------------------------------------
    def run_lease(self) -> None:
        size = self.args.lease_size
        data = self.dataset(size)
        log(f"lease: repos {self.args.lease_idle_seconds} s puis {self.args.lease_imports} imports de {size}")
        database = self.database("lease")
        try:
            database.create()
            result = self.child("lease", {"urls": database.urls(), "directory": data["directory"],
                                          "idle_seconds": self.args.lease_idle_seconds,
                                          "imports": self.args.lease_imports})
        finally:
            database.drop()
        if result["status"] != "ok":
            self.record("lease", [harness.case("lease", category="infrastructure", size=size,
                                               status=result["status"], reason=result.get("reason"))])
            return
        cases = []
        for condition, entry in result["conditions"].items():
            ok = entry["job_outcome"] == "succeeded"
            cases.append(harness.case(
                f"lease/{condition}", category="infrastructure", size=size if condition != "idle" else None,
                status="ok" if ok else "failed", reason=None if ok else f"travail {entry['job_outcome']}",
                repetitions=entry["renewals"], metrics={"renew_ms": entry["renew_ms"]},
                throughput={"renewals_per_second": entry["renewals_per_second"]},
                details={"job_seconds": entry["job_seconds"], "result": entry["result"],
                         "lease_seconds": result["lease_seconds"], "renew_seconds": result["renew_seconds"]}))
        self.record("lease", cases)

    # -- F. concurrence -----------------------------------------------------------------------
    def run_concurrency(self) -> None:
        size = self.args.concurrency_size
        data = self.dataset(size)
        log(f"concurrency: {self.args.concurrency_jobs} analyses de {size}, "
            f"workers {self.args.concurrency_workers}")
        database = self.database("conc")
        try:
            database.create()
            result = self.child("concurrency", {
                "urls": database.urls(), "directory": data["directory"], "jobs": self.args.concurrency_jobs,
                "worker_counts": self.args.concurrency_workers, "job_timeout": self.args.timeout,
                "admin": self.admin, "database": database.database})
        finally:
            database.drop()
        if result["status"] != "ok":
            self.record("concurrency", [harness.case("concurrency", category="concurrency", size=size,
                                                     status=result["status"], reason=result.get("reason"))])
            return
        cases = []
        for count, entry in result["configurations"].items():
            ok = entry["succeeded"] == entry["jobs"] and entry["unfinished"] == 0
            cases.append(harness.case(
                f"concurrency/workers={count}", category="concurrency", size=size,
                status="ok" if ok else "failed", reason=None if ok else "analyses non terminees ou en echec",
                repetitions=entry["jobs"],
                metrics={"job_latency_ms": entry["job_latency_ms"], "handler_ms": entry["handler_ms"]},
                throughput={"analyses_per_second": entry["jobs_per_second"], "seconds": entry["seconds"]},
                details={key: entry[key] for key in ("workers", "per_worker_jobs", "sum_of_sampled_peak_rss_mb",
                                                     "max_sampled_peak_rss_mb", "database",
                                                     "database_active_backends_peak", "worker_exit_codes")}))
        self.record("concurrency", cases, {"workers_cpu_s_total": result["workers_cpu_s_total"],
                                           "workers_peak_rss_mb_max": result["workers_peak_rss_mb_max"],
                                           "snapshot_rows": result["size_rows"]})

    # -- execution ----------------------------------------------------------------------------
    def execute(self) -> Dict[str, Any]:
        started = time.perf_counter()
        for workload in WORKLOADS:
            if workload not in self.args.workloads:
                continue
            if workload in DB_WORKLOADS and self.admin is None:
                self.skipped_database(workload)
                continue
            try:
                getattr(self, f"run_{workload}")()
            except Exception as exc:  # noqa: BLE001 - une charge en echec n'arrete pas les autres
                reason = harness.redact(f"{type(exc).__name__}: {exc}", self.secrets)[:800]
                self.record(workload, [harness.case(workload, category="infrastructure", size=None,
                                                    status="failed", reason=reason)])
            log(f"{workload}: {self.document['workloads'][workload]['status']}")
        self.document["duration_s"] = round(time.perf_counter() - started, 1)
        if self.admin:
            self.document["leftover_disposable_objects"] = harness.leftover_objects(self.admin)
        return self.document


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmarks de performance de Mervio (004.3.9)")
    parser.add_argument("--workloads", default=",".join(WORKLOADS))
    parser.add_argument("--sizes", default="10000,100000,1000000")
    parser.add_argument("--repetitions", type=int, help="remplace la politique de repetitions par defaut")
    parser.add_argument("--queue-depths", default="100,1000,10000,100000")
    parser.add_argument("--queue-organizations", type=int, default=10)
    parser.add_argument("--queue-org-scaling", default="1,100,1000")
    parser.add_argument("--queue-org-scaling-depth", type=int, default=10000)
    parser.add_argument("--queue-samples", type=int, default=200)
    parser.add_argument("--noop-jobs", type=int, default=1000)
    parser.add_argument("--noop-threads", default="1,2,4,8")
    parser.add_argument("--lease-size", type=int, default=100000)
    parser.add_argument("--lease-idle-seconds", type=float, default=30.0)
    parser.add_argument("--lease-imports", type=int, default=3)
    parser.add_argument("--concurrency-size", type=int, default=100000)
    parser.add_argument("--concurrency-jobs", type=int, default=16)
    parser.add_argument("--concurrency-workers", default="1,2,4,8")
    parser.add_argument("--timeout", type=float, default=3600.0, help="delai maximal d'un processus fils (s)")
    parser.add_argument("--force-memory", action="store_true", help="ignorer la garde de memoire disponible")
    parser.add_argument("--data-dir", default=str(harness.DATA_DIR), help="jeux synthetiques (ignore par Git)")
    parser.add_argument("--output", default=str(harness.DEFAULT_OUTPUT_DIR),
                        help="repertoire (nom automatique) ou fichier .json")
    args = parser.parse_args(argv)
    workloads = [w.strip() for w in args.workloads.split(",") if w.strip()]
    unknown = sorted(set(workloads) - set(WORKLOADS))
    if unknown or not workloads:
        raise harness.BenchmarkConfigError(f"charges inconnues: {', '.join(unknown) or 'aucune'}")
    args.workloads = workloads
    args.sizes = harness.parse_positive_list(args.sizes, name="--sizes")
    args.queue_depths = harness.parse_positive_list(args.queue_depths, name="--queue-depths")
    args.queue_org_scaling = harness.parse_positive_list(args.queue_org_scaling, name="--queue-org-scaling",
                                                         maximum=10_000)
    args.noop_threads = harness.parse_positive_list(args.noop_threads, name="--noop-threads", maximum=64)
    args.concurrency_workers = harness.parse_positive_list(args.concurrency_workers, name="--concurrency-workers",
                                                           maximum=64)
    for name in ("repetitions", "queue_organizations", "queue_samples", "noop_jobs", "lease_imports",
                 "concurrency_jobs", "lease_size", "concurrency_size", "queue_org_scaling_depth"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise harness.BenchmarkConfigError(f"--{name.replace('_', '-')}: entier positif attendu")
    for name in ("lease_size", "concurrency_size", "queue_org_scaling_depth"):
        if getattr(args, name) > harness.MAX_SIZE:
            raise harness.BenchmarkConfigError(f"--{name.replace('_', '-')}: au plus {harness.MAX_SIZE}")
    if args.timeout <= 0 or args.lease_idle_seconds <= 0:
        raise harness.BenchmarkConfigError("--timeout et --lease-idle-seconds doivent etre positifs")
    for threads in args.noop_threads:
        if args.noop_jobs < threads:
            raise harness.BenchmarkConfigError("--noop-jobs doit etre au moins egal au nombre de fils")
    return args


def main(argv: Optional[Sequence[str]] = None, environ: Mapping[str, str] = os.environ) -> int:
    try:
        args = parse_args(argv)
    except harness.BenchmarkConfigError as exc:
        print(f"performance: {exc}", file=sys.stderr)
        return 2
    suite = Suite(args, environ)
    document = suite.execute()
    path = harness.write_document(document, Path(args.output), suite.secrets + ([suite.admin] if suite.admin else []))
    print(path)
    failed = [name for name, entry in document["workloads"].items() if entry["status"] in ("failed", "timeout")]
    for name in failed:
        for item in document["workloads"][name]["cases"]:
            if item["status"] in ("failed", "timeout"):
                print(f"ECHEC {item['case']}: {item['reason']}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
