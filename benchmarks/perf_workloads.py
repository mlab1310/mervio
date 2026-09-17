"""Charges mesurees par benchmarks/performance.py (Mission 004.3.9). Chacune tourne dans un processus NEUF.

Aucune charge ne modifie le produit. Les seules instrumentations sont des chronometres autour d'appels
existants (`jobs.renew_lease` pour le gardien de bail), jamais un changement de comportement.

    analytics_parse     lecture CSV brute de l'export commandes (`read_csv`)
    analytics_stages    chaque etape du moteur, chronometree separement (reprise de run_benchmark.py)
    analytics_e2e       `run_analysis` + JSON, comme la CLI
    persistence         PostgreSQL reel, role applicatif et RLS: ecriture/relecture d'instantane, analyse,
                        rapport; plus le cas d'usage complet `import_csv_snapshot`
    queue               file et dispatcher sous le VRAI role worker: sonde du dispatcher, prise, fin,
                        mise en file (API + audit), surcout du Worker avec un gestionnaire VIDE
    worker_chain        processus `mervio worker` REEL (CLI): import puis analyse reels, chaines
    lease               latence des renouvellements du gardien de bail, au repos et pendant des imports reels
    concurrency         1..N processus `mervio worker` reels sur des analyses reelles
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import perf_harness as harness  # noqa: E402

ANALYSIS_DAY = date.fromisoformat(harness.ANALYSIS_DAY)
SOURCES = {"shopify_orders": "shopify_orders.csv", "shopify_products": "shopify_products.csv",
           "stripe": "stripe_transactions.csv", "google_ads": "google_ads.csv"}


def _quiet_logs() -> None:
    import logging
    logging.disable(logging.WARNING)


def _usage(baseline_rss: Optional[float]) -> Dict[str, Any]:
    return {"baseline_rss_mb": baseline_rss, "final_rss_mb": harness.rss_mb(),
            "child_peak_rss_mb": harness.peak_rss_mb(), "cpu_s": harness.cpu_seconds()}


def _timed(label: str, function: Callable[[], Any], timings: Dict[str, float]) -> Any:
    started = time.perf_counter()
    value = function()
    timings[label] = round(time.perf_counter() - started, 4)
    return value


# =============================================================================================
# A. Moteur analytique (sans base)
# =============================================================================================

def analytics_parse(payload: Mapping[str, Any]) -> Dict[str, Any]:
    from mervio.ingestion.base import read_csv
    baseline = harness.rss_mb()
    started = time.perf_counter()
    rows = read_csv(Path(payload["directory"]) / "shopify_orders.csv", "shopify")
    wall = time.perf_counter() - started
    return {"wall_s": round(wall, 4), "rows": len(rows), **_usage(baseline)}


def analytics_stages(payload: Mapping[str, Any]) -> Dict[str, Any]:
    import run_benchmark  # decoupage historique (004.0), reutilise tel quel
    _quiet_logs()
    baseline = harness.rss_mb()
    started = time.perf_counter()
    result = run_benchmark.child_stages(Path(payload["directory"]), ANALYSIS_DAY)
    wall = time.perf_counter() - started
    return {"wall_s": round(wall, 4), "stages_s": result["timings_s"], "orders": result["orders"],
            "report_bytes": result["report_bytes"], **_usage(baseline)}


def analytics_e2e(payload: Mapping[str, Any]) -> Dict[str, Any]:
    from mervio.analytics.pipeline import run_analysis
    from mervio.synthetic.evaluation import engine_paths
    _quiet_logs()
    baseline = harness.rss_mb()
    cpu_before = harness.cpu_seconds()
    started = time.perf_counter()
    report = run_analysis(engine_paths(payload["directory"]), today=ANALYSIS_DAY)
    text = json.dumps(report)
    wall = time.perf_counter() - started
    usage = _usage(baseline)
    usage["cpu_s"] = round(usage["cpu_s"] - cpu_before, 3)
    return {"wall_s": round(wall, 4), "report_bytes": len(text.encode("utf-8")),
            "health_score": report["business_health_score"]["score"], **usage}


# =============================================================================================
# Contexte PostgreSQL commun
# =============================================================================================

def _tenant(app, subject: str, name: str):
    from mervio.persistence.tenancy import TenantContext, TenantSession, create_organization, ensure_user
    owner = ensure_user(app, subject)
    organization = create_organization(app, owner_user_id=owner, name=name)
    return TenantSession(app, TenantContext(organization, owner))


def _store(session, label: str):
    from mervio.persistence.stores import create_csv_connection, create_store
    store = create_store(session, name=label)
    connection = create_csv_connection(session, store_id=store.id, label=label)
    return store, connection


def _manifest(directory: Path) -> dict:
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


def _sources(directory: Path) -> Dict[str, str]:
    return {kind: str(directory / name) for kind, name in SOURCES.items()}


def _table_sizes(conn) -> Dict[str, float]:
    rows = conn.execute(
        "SELECT relname, pg_total_relation_size(oid) FROM pg_class WHERE relkind = 'r' AND relname IN "
        "('orders', 'order_lines', 'payments', 'refunds', 'products', 'campaigns', 'ad_daily_performance', "
        "'reports', 'jobs', 'audit_events') ORDER BY relname").fetchall()
    return {name: round(size / 1048576, 1) for name, size in rows}


# =============================================================================================
# B. Persistance
# =============================================================================================

def persistence(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Une repetition: memes fichiers, nouvelle boutique (l'empreinte d'import est par boutique)."""
    from mervio.analytics.pipeline import analyze_loaded_dataset, load_dataset
    from mervio.application.persisted_analysis import SnapshotImportRequest, import_csv_snapshot
    from mervio.application.service import annotate_report
    from mervio.config import AnalyticsConfig
    from mervio.persistence import analyses, snapshots
    from mervio.persistence.codec import file_sha256, serialize_report
    from mervio.persistence.database import Database
    from mervio.persistence.snapshots import SourceFile
    from mervio.synthetic.evaluation import engine_paths
    _quiet_logs()

    directory = Path(payload["directory"])
    manifest = _manifest(directory)
    baseline = harness.rss_mb()
    timings: Dict[str, float] = {}
    app = Database(payload["urls"]["app"])
    session = _tenant(app, f"perf|persistence|{uuid.uuid4()}", "perf persistence")
    store, connection = _store(session, "perf-db")
    paths = engine_paths(directory)
    files = {kind: path for kind, path in vars(paths).items() if path}

    dataset = _timed("csv_load_dataset", lambda: load_dataset(paths), timings)
    order_count = len(dataset.orders)
    sources = [SourceFile(kind, *file_sha256(path)) for kind, path in files.items()]
    record = _timed("db_write_snapshot", lambda: snapshots.write_snapshot(
        session, store_id=store.id, connection_id=connection.id, dataset=dataset, sources=sources,
        inputs_sha256=uuid.uuid4().hex + uuid.uuid4().hex, synthetic=True, synthetic_manifest=manifest), timings)
    with app.transaction(organization_id=session.organization_id, user_id=session.user_id) as conn:
        first_sizes = _table_sizes(conn)
    loaded = _timed("db_load_dataset", lambda: snapshots.load_dataset(session, store.id, record.id), timings)
    equal = loaded == dataset
    dataset = None  # libere la copie CSV avant l'analyse
    config = AnalyticsConfig()
    report = _timed("analyze_loaded_dataset", lambda: analyze_loaded_dataset(loaded, config, ANALYSIS_DAY), timings)
    loaded = None
    annotate_report(report, synthetic=True, label="")
    run = analyses.start_run(session, store_id=store.id, snapshot_id=record.id, config=config, as_of_date=ANALYSIS_DAY)
    stored_record = _timed("db_persist_report", lambda: analyses.complete_run(session, run, report), timings)
    stored = _timed("db_read_report", lambda: analyses.get_report(session, store.id, stored_record.id), timings)
    report_equal = stored.payload_json == serialize_report(report)

    # cas d'usage complet (ce qu'execute le travail d'import): empreintes + connecteurs + ecriture
    other_store, other_connection = _store(session, "perf-app")
    request = SnapshotImportRequest(synthetic=True, synthetic_manifest=manifest,
                                    **{kind: str(path) for kind, path in files.items()})
    imported = _timed("app_import_csv_snapshot", lambda: import_csv_snapshot(
        session, store_id=other_store.id, connection_id=other_connection.id, request=request), timings)

    with app.transaction(organization_id=session.organization_id, user_id=session.user_id) as conn:
        sizes = _table_sizes(conn)
    app.close()
    return {"timings_s": timings, "canonical_rows": record.row_count, "orders": order_count,
            "dataset_equal": equal, "report_bytes_equal": report_equal, "report_bytes": stored_record.payload_bytes,
            "import_status": imported.status, "import_rows": imported.snapshot.row_count,
            "table_sizes_mb_after_first_write": first_sizes, "table_sizes_mb_cumulative": sizes,
            **_usage(baseline)}


# =============================================================================================
# C. File et dispatcher (infrastructure)
# =============================================================================================

def _fill(app, organization_id, owner, store_id, count: int) -> None:
    """Remplissage EN LOT sous le role applicatif et RLS (memes colonnes, contraintes et triggers)."""
    if count <= 0:
        return
    ids = [uuid.uuid4() for _ in range(count)]
    correlations = [uuid.uuid4() for _ in range(count)]
    priorities = [index % 5 for index in range(count)]
    with app.transaction(organization_id=organization_id, user_id=owner) as conn:
        conn.execute(
            "INSERT INTO jobs (id, organization_id, store_id, job_type, status, priority, correlation_id, "
            "enqueued_by, payload, available_at) SELECT t.id, %s, %s, 'analysis', 'queued', t.priority, "
            "t.correlation_id, %s, '{}'::jsonb, now() "
            "FROM unnest(%s::uuid[], %s::smallint[], %s::uuid[]) AS t(id, priority, correlation_id)",
            (organization_id, store_id, owner, ids, priorities, correlations))


def _latencies(function: Callable[[], Any], samples: int) -> List[float]:
    values = []
    for _ in range(samples):
        started = time.perf_counter()
        function()
        values.append((time.perf_counter() - started) * 1000)
    return values


def queue(payload: Mapping[str, Any]) -> Dict[str, Any]:
    import psycopg

    from mervio.observability.logging import configure
    from mervio.persistence import jobs
    from mervio.persistence.database import Database
    from mervio.persistence.dispatch import _READY_SQL, Dispatcher, ready_organizations
    from mervio.persistence.jobs import JobType
    from mervio.persistence.service import ServiceSession, authorize_service, service_principal
    from mervio.persistence.stores import create_store
    from mervio.workers.handlers import HandlerRegistry
    from mervio.workers.worker import Worker, enqueue
    import logging

    configure(format="json", level=logging.CRITICAL)  # on mesure la file, pas l'ecriture des logs
    urls = payload["urls"]
    depth, organizations, samples = int(payload["depth"]), int(payload["organizations"]), int(payload["samples"])
    app, worker_db = Database(urls["app"]), Database(urls["worker"])
    principal = service_principal(worker_db)
    tenants = []
    setup_started = time.perf_counter()
    for index in range(organizations):
        session = _tenant(app, f"perf|queue|{index}", f"perf queue {index}")
        store = create_store(session, name="perf")
        authorize_service(session, principal.id)
        tenants.append((session, store))
    per_org, remainder = divmod(depth, organizations)
    fill_started = time.perf_counter()
    for index, (session, store) in enumerate(tenants):
        _fill(app, session.organization_id, session.user_id, store.id, per_org + (1 if index < remainder else 0))
    fill_seconds = time.perf_counter() - fill_started
    # statistiques a jour: operation de MAINTENANCE du proprietaire, pas un comportement applicatif
    with psycopg.connect(urls["migrator"], autocommit=True) as conn:
        conn.execute("ANALYZE jobs")
        conn.execute("ANALYZE service_authorizations")
    setup_seconds = time.perf_counter() - setup_started

    dispatcher = Dispatcher(worker_db, principal, batch=50)
    probe = _latencies(lambda: ready_organizations(worker_db, principal, limit=50), samples)
    rotation = _latencies(dispatcher.next_organizations, samples)

    # prise et fin, une par une, sur le chemin du service (ServiceSession, RLS du role worker)
    # jamais plus de prises que de travaux: une prise sur file vide n'est pas une latence de prise
    sessions = [ServiceSession(worker_db, s.organization_id, principal) for s, _ in tenants]
    claimed, claim_ms = [], []
    index = 0
    while len(claimed) < min(samples, depth) and index < 4 * samples:
        service_session = sessions[index % len(sessions)]
        index += 1
        started = time.perf_counter()
        job = jobs.claim_next_job(service_session, worker_id="perf-claim", lease_seconds=300)
        elapsed = (time.perf_counter() - started) * 1000
        if job is not None:
            claim_ms.append(elapsed)
            claimed.append((service_session, job))
    complete_ms = []
    for service_session, job in claimed:
        if job is None:
            continue
        started = time.perf_counter()
        jobs.mark_succeeded(service_session, job, worker_id="perf-claim", result={"n": 1})
        complete_ms.append((time.perf_counter() - started) * 1000)

    # mise en file par l'API reelle (travail + audit dans la meme transaction), role applicatif
    owner_session, owner_store = tenants[0]
    enqueue_ms = _latencies(lambda: enqueue(owner_session, job_type=JobType.ANALYSIS, store_id=owner_store.id,
                                            payload={"perf": True}), samples)

    # surcout complet du Worker dispatche (identite, delegation, gardien de bail, audit) avec un
    # gestionnaire VIDE: mesure du cadre, JAMAIS un debit analytique
    registry = HandlerRegistry().register(JobType.ANALYSIS, lambda context: {"noop": True})
    noop = {}
    top_up = int(payload["noop_jobs"])
    for threads in payload["thread_counts"]:
        # chaque passage dispose de ses propres travaux (la file de base garde sa profondeur)
        per_org_top_up, extra = divmod(top_up, len(tenants))
        for position, (session, store) in enumerate(tenants):
            _fill(app, session.organization_id, session.user_id, store.id,
                  per_org_top_up + (1 if position < extra else 0))
        workers = []
        for index in range(threads):
            main, lease = Database(urls["worker"]), Database(urls["worker"])
            workers.append((Worker(dispatcher=Dispatcher(main, service_principal(main)), registry=registry,
                                   worker_id=f"perf-noop-{threads}-{index}", lease_database=lease,
                                   lease_seconds=300, renew_seconds=100.0), main, lease))
        per_worker = int(payload["noop_jobs"]) // threads
        barrier = threading.Barrier(threads)
        counts = [0] * threads

        def run(position: int) -> None:
            barrier.wait()
            counts[position] = len(workers[position][0].run_until_empty(max_jobs=per_worker))

        started = time.perf_counter()
        pool = [threading.Thread(target=run, args=(i,)) for i in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join()
        seconds = time.perf_counter() - started
        for _, main, lease in workers:
            main.close(), lease.close()
        done = sum(counts)
        noop[str(threads)] = {"threads": threads, "jobs": done, "seconds": round(seconds, 3),
                              "jobs_per_second": harness.rate(done, seconds),
                              "ms_per_job": round(1000 * seconds / done, 3) if done else None}

    with app.transaction(organization_id=tenants[0][0].organization_id, user_id=tenants[0][0].user_id) as conn:
        sizes = _table_sizes(conn)
    with worker_db.transaction() as conn:  # vue du dispatcher: role worker, sans contexte d'organisation
        explained = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _READY_SQL,
                                 (principal.id, None, None, None, 50)).fetchone()[0][0]
    app.close(), worker_db.close()
    return {
        "depth": depth, "organizations": organizations, "setup_s": round(setup_seconds, 3),
        "bulk_fill": {"jobs": depth, "seconds": round(fill_seconds, 3),
                      "jobs_per_second": harness.rate(depth, fill_seconds)},
        "dispatcher_probe_ms": harness.summarize(probe), "dispatcher_rotation_ms": harness.summarize(rotation),
        "claim_ms": harness.summarize(claim_ms), "claimed": sum(1 for _, j in claimed if j is not None),
        "complete_ms": harness.summarize(complete_ms), "enqueue_api_ms": harness.summarize(enqueue_ms),
        "noop_worker": noop, "noop_top_up_jobs_per_pass": top_up, "table_sizes_mb": sizes,
        "probe_plan": {"nodes": _plan_nodes(explained["Plan"]), "execution_ms": explained["Execution Time"],
                       "shared_hit_blocks": explained["Plan"].get("Shared Hit Blocks")},
        **_usage(None),
    }


def _plan_nodes(node: Mapping[str, Any]) -> List[str]:
    """Noeuds du plan execute, en profondeur: type, index, lignes et boucles reelles."""
    label = node["Node Type"] + (f" {node['Index Name']}" if node.get("Index Name") else "")
    nodes = [f"{label} rows={node.get('Actual Rows')} loops={node.get('Actual Loops')}"]
    for child in node.get("Plans", []):
        nodes.extend(_plan_nodes(child))
    return nodes


# =============================================================================================
# D/G. Processus worker reel: import puis analyse
# =============================================================================================

class WorkerProcess:
    """`python -m mervio.cli worker` reel, journaux JSON dans un fichier, RSS echantillonne."""

    def __init__(self, url: str, workdir: Path, name: str, *, log_level: str = "DEBUG",
                 extra: Optional[Mapping[str, str]] = None) -> None:
        self.health = workdir / f"{name}-health.json"
        self.log_path = workdir / f"{name}.log"
        environment = {key: value for key, value in os.environ.items() if not key.startswith("MERVIO_")}
        environment.update({
            "PYTHONPATH": str(ROOT / "src"), "MERVIO_DATABASE_URL": url, "MERVIO_WORKER_NAME": name,
            "MERVIO_WORKER_HEALTH_FILE": str(self.health), "MERVIO_LOG_FORMAT": "json",
            "MERVIO_LOG_LEVEL": log_level, "MERVIO_ENV": "development",
            # attente de file courte et explicite: la latence de sondage est rapportee a part
            "MERVIO_WORKER_POLL_INTERVAL_SECONDS": "0.1", "MERVIO_WORKER_POLL_MAX_SECONDS": "1",
        })
        environment.update(extra or {})
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.process = subprocess.Popen([sys.executable, "-m", "mervio.cli", "worker"], cwd=ROOT,
                                        stdout=self._log, stderr=subprocess.STDOUT, env=environment)
        self.samples: List[float] = []
        self._stop = threading.Event()
        self._sampler = threading.Thread(target=self._sample, daemon=True)
        self._sampler.start()

    def _sample(self) -> None:
        while not self._stop.wait(0.25):
            value = harness.rss_mb(self.process.pid)
            if value is not None:
                self.samples.append(value)

    def state(self) -> Optional[str]:
        try:
            return json.loads(self.health.read_text(encoding="utf-8"))["state"]
        except (OSError, ValueError, KeyError):
            return None

    def wait_ready(self, timeout: float = 60) -> None:
        if not harness.wait_until(lambda: self.state() == "ready" or self.process.poll() is not None,
                                  timeout=timeout):
            raise RuntimeError("worker jamais pret")
        if self.state() != "ready":
            raise RuntimeError(f"worker sorti avant d'etre pret (code {self.process.poll()})")

    def stop(self) -> int:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self._stop.set()
        self._sampler.join(5)
        self._log.close()
        return self.process.returncode

    def events(self) -> List[dict]:
        events = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        return events


def _wait_job(session, job_id, timeout: float, interval: float = 0.05):
    from mervio.persistence import jobs
    deadline = time.monotonic() + timeout
    while True:
        job = jobs.get_job(session, job_id)
        if job.is_terminal:
            return job, time.perf_counter()
        if time.monotonic() > deadline:
            raise TimeoutError(f"travail {job.job_type} toujours {job.status}")
        time.sleep(interval)


def _ms(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000, 1)


def _job_timings(job, handler_ms: Optional[int], enqueued_at: float, observed_at: float) -> Dict[str, Any]:
    attempt_ms = _ms(job.started_at, job.finished_at)
    return {
        "status": job.status, "attempts": job.attempts,
        # attente en file: de la disponibilite a la prise (inclut l'intervalle de sondage du worker)
        "queue_wait_ms": _ms(job.available_at, job.started_at),
        # execution du gestionnaire (mesuree par le gestionnaire lui-meme)
        "handler_ms": handler_ms,
        # de la prise a la publication du sort (gestionnaire + delegation + gardien + audit + publication)
        "attempt_ms": attempt_ms,
        "framework_overhead_ms": round(attempt_ms - handler_ms, 1) if attempt_ms is not None and handler_ms else None,
        # vu du client: de la mise en file a l'etat terminal observe (sondage client toutes les 50 ms)
        "observed_ms": round((observed_at - enqueued_at) * 1000, 1),
    }


def worker_chain(payload: Mapping[str, Any]) -> Dict[str, Any]:
    from mervio.persistence import analyses, audit
    from mervio.persistence.database import Database
    from mervio.persistence.jobs import JobType
    from mervio.persistence.service import authorize_service, service_principal
    from mervio.workers.worker import enqueue
    _quiet_logs()

    directory = Path(payload["directory"])
    manifest = _manifest(directory)
    urls = payload["urls"]
    app = Database(urls["app"])
    with Database(urls["worker"]) as worker_db:
        principal = service_principal(worker_db)
    session = _tenant(app, f"perf|chain|{uuid.uuid4()}", "perf chain")
    authorize_service(session, principal.id)
    runs = []
    with tempfile.TemporaryDirectory() as workdir:
        worker = WorkerProcess(urls["worker"], Path(workdir), "perf-chain")
        try:
            worker.wait_ready()
            ready_rss = harness.rss_mb(worker.process.pid)
            for repetition in range(int(payload["repetitions"])):
                store, connection = _store(session, f"perf-chain-{repetition}")
                chain_started = time.perf_counter()
                importing = enqueue(session, job_type=JobType.IMPORT, store_id=store.id, payload={
                    "store_id": str(store.id), "connection_id": str(connection.id), "sources": _sources(directory),
                    "synthetic": True, "synthetic_manifest": manifest})
                imported, import_seen = _wait_job(session, importing.id, float(payload["job_timeout"]))
                if imported.status != "succeeded":
                    raise RuntimeError(f"import {imported.status}: {imported.last_error_code}")
                analysis_enqueued = time.perf_counter()
                analysing = enqueue(session, job_type=JobType.ANALYSIS, store_id=store.id, payload={
                    "store_id": str(store.id), "snapshot_id": imported.result["snapshot_id"],
                    "as_of_date": harness.ANALYSIS_DAY, "label": f"perf chain {repetition}"})
                analysed, analysis_seen = _wait_job(session, analysing.id, float(payload["job_timeout"]))
                if analysed.status != "succeeded":
                    raise RuntimeError(f"analyse {analysed.status}: {analysed.last_error_code}")
                report = analyses.get_report(session, store.id, uuid.UUID(analysed.result["report_id"]))
                trail = audit.list_events(session, store_id=store.id, limit=1000)
                actions = [event.action for event in trail]
                runs.append({
                    "repetition": repetition,
                    "import": {**_job_timings(imported, imported.result.get("duration_ms"), chain_started,
                                              import_seen), "rows": imported.result["row_count"],
                               "result_status": imported.result["status"]},
                    "analysis": {**_job_timings(analysed, analysed.result.get("duration_ms"), analysis_enqueued,
                                                analysis_seen), "report_bytes": report.record.payload_bytes,
                                 "result_status": analysed.result["status"]},
                    "chain_ms": round((analysis_seen - chain_started) * 1000, 1),
                    "audit_events": len(actions),
                    "audit_complete": all(action in actions for action in (
                        "job.enqueued", "job.claimed", "import.started", "import.succeeded", "analysis.started",
                        "analysis.succeeded", "job.succeeded")),
                    "report_persisted": report.record.payload_sha256 == analysed.result["payload_sha256"],
                    "worker_rss_mb_after": harness.rss_mb(worker.process.pid),
                })
        finally:
            exit_code = worker.stop()
        events = worker.events()
    app.close()
    renewals: Dict[str, int] = {}
    for event in events:
        if event.get("event") == "job.lease_renewed":
            renewals[event.get("job_id")] = renewals.get(event.get("job_id"), 0) + 1
    errors = [e.get("event") for e in events if e.get("level") in ("ERROR", "CRITICAL")]
    return {
        "runs": runs, "worker_exit_code": exit_code, "worker_ready_rss_mb": ready_rss,
        "worker_sampled_peak_rss_mb": max(worker.samples) if worker.samples else None,
        # pic exact du processus worker termine (seul fils attendu par ce processus)
        "worker_peak_rss_mb": harness.peak_rss_mb(children=True),
        "worker_cpu_s": harness.cpu_seconds(children=True),
        "lease_renewals_total": sum(renewals.values()), "worker_error_events": errors,
        "lease_seconds": 300, "renew_seconds": 100,
        **_usage(None),
    }


# =============================================================================================
# E. Gardien de bail
# =============================================================================================

def lease(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Latence de `jobs.renew_lease` appelee par le VRAI LeaseKeeper, sur sa propre connexion.

    Bail de 30 s renouvele chaque seconde: les minima acceptes par `WorkerSettings` (pas de bail
    raccourci hors des regles du produit). Deux conditions: gestionnaire au repos (attente
    cooperative) et gestionnaire qui importe reellement des fichiers sur la connexion principale.
    """
    from mervio.application.persisted_analysis import SnapshotImportRequest, import_csv_snapshot
    from mervio.observability.logging import configure
    from mervio.persistence import jobs
    from mervio.persistence.database import Database
    from mervio.persistence.dispatch import Dispatcher
    from mervio.persistence.jobs import JobType
    from mervio.persistence.service import authorize_service, service_principal
    from mervio.workers.handlers import HandlerRegistry
    from mervio.workers.worker import Worker, enqueue
    import logging

    configure(format="json", level=logging.CRITICAL)
    urls = payload["urls"]
    directory = Path(payload["directory"])
    manifest = _manifest(directory)
    app = Database(urls["app"])
    session = _tenant(app, f"perf|lease|{uuid.uuid4()}", "perf lease")
    main, lease_db = Database(urls["worker"]), Database(urls["worker"])
    principal = service_principal(main)
    authorize_service(session, principal.id)

    latencies: Dict[str, List[float]] = {"idle": [], "import_load": []}
    current = {"condition": "idle"}
    original = jobs.renew_lease

    def timed(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            latencies[current["condition"]].append((time.perf_counter() - started) * 1000)

    jobs.renew_lease = timed  # chronometre seulement: meme fonction, memes arguments

    def handler(context):
        if context.payload["mode"] == "idle":
            deadline = time.monotonic() + float(context.payload["seconds"])
            while time.monotonic() < deadline:
                context.checkpoint()
                time.sleep(0.05)
            return {"imports": 0}
        count = 0
        for pair in context.payload["targets"]:
            context.checkpoint()
            result = import_csv_snapshot(
                context.session, store_id=uuid.UUID(pair["store_id"]), connection_id=uuid.UUID(pair["connection_id"]),
                request=SnapshotImportRequest(synthetic=True, synthetic_manifest=manifest,
                                              **_sources(directory)))
            count += result.status == "completed"
        return {"imports": count}

    registry = HandlerRegistry().register(JobType.ANALYSIS, handler)
    worker = Worker(dispatcher=Dispatcher(main, principal), registry=registry, worker_id="perf-lease",
                    lease_database=lease_db, lease_seconds=30, renew_seconds=1.0, heartbeat_seconds=5.0)
    results = {}
    for condition in ("idle", "import_load"):
        current["condition"] = condition
        if condition == "idle":
            job_payload = {"mode": "idle", "seconds": float(payload["idle_seconds"])}
        else:
            targets = []
            for index in range(int(payload["imports"])):
                store, connection = _store(session, f"perf-lease-{index}")
                targets.append({"store_id": str(store.id), "connection_id": str(connection.id)})
            job_payload = {"mode": "import", "targets": targets}
        enqueue(session, job_type=JobType.ANALYSIS, payload=job_payload)
        started = time.perf_counter()
        outcome = worker.run_once()
        seconds = time.perf_counter() - started
        results[condition] = {"job_outcome": outcome.outcome if outcome else None,
                              "job_seconds": round(seconds, 3),
                              "result": outcome.result if outcome else None,
                              "renewals": len(latencies[condition]),
                              "renewals_per_second": harness.rate(len(latencies[condition]), seconds, 2),
                              "renew_ms": harness.summarize(latencies[condition])}
    jobs.renew_lease = original
    main.close(), lease_db.close(), app.close()
    return {"lease_seconds": 30, "renew_seconds": 1.0, "conditions": results, **_usage(None)}


# =============================================================================================
# F. Concurrence: processus worker reels, analyses reelles
# =============================================================================================

def concurrency(payload: Mapping[str, Any]) -> Dict[str, Any]:
    from mervio.persistence import jobs
    from mervio.persistence.database import Database
    from mervio.persistence.jobs import JobType
    from mervio.persistence.service import authorize_service, service_principal
    from mervio.workers.worker import enqueue
    _quiet_logs()

    urls = payload["urls"]
    directory = Path(payload["directory"])
    manifest = _manifest(directory)
    app = Database(urls["app"])
    with Database(urls["worker"]) as worker_db:
        principal = service_principal(worker_db)
    session = _tenant(app, f"perf|concurrency|{uuid.uuid4()}", "perf concurrency")
    authorize_service(session, principal.id)
    store, connection = _store(session, "perf-concurrency")
    per_config = int(payload["jobs"])
    configurations = {}
    with tempfile.TemporaryDirectory() as workdir:
        # preparation (non mesuree): un instantane importe par un worker reel
        setup = WorkerProcess(urls["worker"], Path(workdir), "perf-setup", log_level="INFO")
        try:
            setup.wait_ready()
            importing = enqueue(session, job_type=JobType.IMPORT, store_id=store.id, payload={
                "store_id": str(store.id), "connection_id": str(connection.id), "sources": _sources(directory),
                "synthetic": True, "synthetic_manifest": manifest})
            imported, _ = _wait_job(session, importing.id, float(payload["job_timeout"]))
        finally:
            setup.stop()
        if imported.status != "succeeded":
            raise RuntimeError(f"preparation: import {imported.status}")
        snapshot_id = imported.result["snapshot_id"]

        for count in payload["worker_counts"]:
            processes = [WorkerProcess(urls["worker"], Path(workdir), f"perf-c{count}-{i}", log_level="INFO")
                         for i in range(count)]
            try:
                for process in processes:
                    process.wait_ready()
                time.sleep(1.5)  # compteurs des processus precedents publies
                stats_before = _database_stats(payload["admin"], payload["database"])
                active_peak = [0]
                sampling = threading.Event()
                sampler = threading.Thread(target=_sample_active, args=(payload["admin"], payload["database"],
                                                                        active_peak, sampling), daemon=True)
                sampler.start()
                started = time.perf_counter()
                queued = [enqueue(session, job_type=JobType.ANALYSIS, store_id=store.id, payload={
                    "store_id": str(store.id), "snapshot_id": snapshot_id, "as_of_date": harness.ANALYSIS_DAY,
                    "label": f"perf concurrency {count}-{index}"}) for index in range(per_config)]
                pending = {job.id for job in queued}
                finished = {}
                deadline = time.monotonic() + float(payload["job_timeout"]) * per_config
                while pending and time.monotonic() < deadline:
                    for job_id in list(pending):
                        job = jobs.get_job(session, job_id)
                        if job.is_terminal:
                            finished[job_id] = job
                            pending.discard(job_id)
                    time.sleep(0.05)
                seconds = time.perf_counter() - started
                sampling.set()
                sampler.join(5)
            finally:
                exit_codes = [process.stop() for process in processes]
            # une connexion publie ses compteurs avec retard ou a sa fermeture: lecture APRES l'arret des workers
            time.sleep(1.5)
            stats_after = _database_stats(payload["admin"], payload["database"])
            samples = [p.samples for p in processes]
            done = list(finished.values())
            succeeded = [job for job in done if job.status == "succeeded"]
            latency = [_ms(job.available_at, job.finished_at) for job in done]
            handler = [job.result.get("duration_ms") for job in succeeded if job.result]
            # repartition lue dans les journaux de chaque processus (le detenteur est efface a la fin)
            spread = {p.log_path.stem: sum(1 for e in p.events() if e.get("event") == "job.claimed")
                      for p in processes}
            configurations[str(count)] = {
                "workers": count, "jobs": per_config, "succeeded": len(succeeded), "unfinished": len(pending),
                "seconds": round(seconds, 3), "jobs_per_second": harness.rate(len(succeeded), seconds, 3),
                "job_latency_ms": harness.summarize([v for v in latency if v is not None]),
                "handler_ms": harness.summarize([v for v in handler if v is not None]),
                "per_worker_jobs": spread,
                "sum_of_sampled_peak_rss_mb": round(sum(max(s) for s in samples if s), 1),
                "max_sampled_peak_rss_mb": max((max(s) for s in samples if s), default=None),
                "database": {key: stats_after[key] - stats_before[key] for key in stats_after},
                "database_active_backends_peak": active_peak[0],
                "worker_exit_codes": exit_codes,
            }
    app.close()
    return {"size_rows": imported.result["row_count"], "configurations": configurations,
            "workers_cpu_s_total": harness.cpu_seconds(children=True),
            "workers_peak_rss_mb_max": harness.peak_rss_mb(children=True), **_usage(None)}


def _database_stats(admin: str, database: str) -> Dict[str, int]:
    """Compteurs cumules du serveur (lecture de supervision par le compte d'administration du benchmark)."""
    import psycopg
    with psycopg.connect(admin, autocommit=True) as conn:
        row = conn.execute(
            "SELECT xact_commit, xact_rollback, tup_returned, tup_fetched, tup_inserted, tup_updated, blks_read, "
            "blks_hit, temp_bytes, deadlocks FROM pg_stat_database WHERE datname = %s", (database,)).fetchone()
    keys = ("xact_commit", "xact_rollback", "tup_returned", "tup_fetched", "tup_inserted", "tup_updated",
            "blks_read", "blks_hit", "temp_bytes", "deadlocks")
    return dict(zip(keys, (int(v) for v in row)))


def _sample_active(admin: str, database: str, peak: List[int], stop: threading.Event) -> None:
    import psycopg
    with psycopg.connect(admin, autocommit=True) as conn:
        while not stop.wait(0.2):
            active = conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND state = 'active' "
                                  "AND pid <> pg_backend_pid()", (database,)).fetchone()[0]
            peak[0] = max(peak[0], int(active))


WORKLOADS = {
    "analytics_parse": analytics_parse, "analytics_stages": analytics_stages, "analytics_e2e": analytics_e2e,
    "persistence": persistence, "queue": queue, "worker_chain": worker_chain, "lease": lease,
    "concurrency": concurrency,
}


def main(argv: List[str]) -> int:
    if len(argv) != 2 or argv[0] != "--child" or argv[1] not in WORKLOADS:
        print("usage: perf_workloads.py --child <charge> < payload.json", file=sys.stderr)
        return 2
    payload = json.loads(sys.stdin.read())
    result = WORKLOADS[argv[1]](payload)
    result.setdefault("child_peak_rss_mb", harness.peak_rss_mb())
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
