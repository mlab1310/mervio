"""Outillage commun des benchmarks de performance (Mission 004.3.9). Bibliotheque standard seulement.

Ce module ne mesure rien lui-meme: il fournit ce que chaque charge partage.

    statistiques     mediane, p95/p99 au rang le plus proche (jamais interpoles), min, max, moyenne
    memoire          RSS courant (/proc ou `ps`), RSS de pointe (`ru_maxrss`, unite normalisee en Mo),
                     memoire disponible avant une grosse charge
    environnement    OS, CPU, memoire, Python, PostgreSQL, commit Git, runner GitHub, conteneur
    base jetable     une base et des roles `mervio_perf_*` sur un serveur LOCAL, supprimes a la fin
    resultats        document JSON versionne, valide et verifie sans secret avant ecriture

Metriques de memoire (a ne pas confondre avec le tas Python):
    rss_mb         memoire residente du processus a un instant (`VmRSS` ou `ps -o rss`)
    peak_rss_mb    pic de memoire residente du processus depuis son demarrage (`ru_maxrss`),
                   interpreteur et bibliotheques compris; pour un processus fils termine,
                   `RUSAGE_CHILDREN` donne le pic du plus gros fils attendu.
"""
from __future__ import annotations

import json
import math
import os
import platform
import re
import resource
import secrets
import shutil
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_VERSION = "1.0.0"
RESULT_SCHEMA_VERSION = 1
DEFAULT_OUTPUT_DIR = ROOT / "benchmark-results"
DATA_DIR = ROOT / "data" / "benchmark"
ADMIN_URL_ENV = "MERVIO_BENCH_ADMIN_DATABASE_URL"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})
#: variables de l'application: un benchmark ne s'y connecte jamais
APPLICATION_URL_ENVS = ("MERVIO_DATABASE_URL", "MERVIO_MIGRATION_DATABASE_URL", "MERVIO_TEST_ADMIN_DATABASE_URL")
NAME_PREFIX = "mervio_perf_"
MAX_SIZE = 1_000_000
STATUSES = ("ok", "failed", "timeout", "not_executed")
CATEGORIES = ("business_analytics", "persistence", "infrastructure", "worker", "end_to_end", "concurrency")

#: jeu synthetique de reference: memes parametres que benchmarks/run_benchmark.py (004.0)
DATASET = {"generator": "mervio.synthetic", "scenario": "healthy_store", "profile": "fashion_eu", "days": 365,
           "end_date": "2026-09-13", "seed": 42}
#: jour d'analyse: lendemain de la fin du jeu (derniere semaine ISO close)
ANALYSIS_DAY = "2026-09-14"


class BenchmarkConfigError(ValueError):
    """Configuration de benchmark refusee (tailles, repetitions, base, memoire)."""


# =============================================================================================
# Statistiques
# =============================================================================================

def nearest_rank(values: Sequence[float], fraction: float) -> float:
    """Percentile au rang le plus proche: toujours une valeur OBSERVEE, jamais une interpolation."""
    if not values:
        raise ValueError("aucune valeur")
    if not 0 < fraction <= 1:
        raise ValueError("fraction dans ]0, 1]")
    ordered = sorted(values)
    return ordered[max(1, math.ceil(fraction * len(ordered))) - 1]


def summarize(values: Iterable[float], digits: int = 3) -> Dict[str, Any]:
    """N, mediane, p95, p99, min, max, moyenne. `p95_is_max` signale un echantillon trop petit."""
    data = [float(v) for v in values]
    if not data:
        return {"n": 0}
    p95 = nearest_rank(data, 0.95)
    return {
        "n": len(data),
        "median": round(statistics.median(data), digits),
        "p95": round(p95, digits),
        "p99": round(nearest_rank(data, 0.99), digits),
        "min": round(min(data), digits),
        "max": round(max(data), digits),
        "mean": round(statistics.fmean(data), digits),
        # avec moins de 20 valeurs, le p95 au rang le plus proche EST le maximum
        "p95_is_max": p95 == max(data),
    }


def rate(count: float, seconds: float, digits: int = 1) -> Optional[float]:
    return round(count / seconds, digits) if seconds > 0 else None


# =============================================================================================
# Memoire et CPU
# =============================================================================================

def _maxrss_to_mb(value: int, platform_name: str = sys.platform) -> float:
    # macOS: octets; Linux: kilo-octets
    return round(value / (1024 * 1024) if platform_name == "darwin" else value / 1024, 1)


def peak_rss_mb(children: bool = False) -> float:
    who = resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF
    return _maxrss_to_mb(resource.getrusage(who).ru_maxrss)


def cpu_seconds(children: bool = False) -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF)
    return round(usage.ru_utime + usage.ru_stime, 3)


def rss_mb(pid: Optional[int] = None) -> Optional[float]:
    """RSS courant d'un processus en Mo, ou None s'il n'existe plus."""
    pid = os.getpid() if pid is None else pid
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        try:
            for line in status.read_text(encoding="ascii", errors="replace").splitlines():
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
        except OSError:
            return None
        return None
    try:
        output = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True,
                                timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return round(int(output) / 1024, 1) if output.isdigit() else None


def total_memory_gb() -> Optional[float]:
    if sys.platform == "darwin":
        output = _command(["sysctl", "-n", "hw.memsize"])
        return round(int(output) / 1024 ** 3, 1) if output and output.isdigit() else None
    meminfo = _meminfo()
    return round(meminfo["MemTotal"] / 1024 ** 2, 1) if "MemTotal" in meminfo else None


def available_memory_gb() -> Optional[float]:
    """Memoire utilisable sans pression: MemAvailable (Linux), taux libre de `memory_pressure` (macOS)."""
    if sys.platform == "darwin":
        output = _command(["memory_pressure"]) or ""
        match = re.search(r"free percentage:\s*(\d+)%", output)
        total = total_memory_gb()
        return round(total * int(match.group(1)) / 100, 1) if match and total else None
    meminfo = _meminfo()
    return round(meminfo["MemAvailable"] / 1024 ** 2, 1) if "MemAvailable" in meminfo else None


def _meminfo() -> Dict[str, int]:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except OSError:
        return {}
    values = {}
    for line in lines:
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0])
    return values


#: memoire disponible exigee avant une charge, en Go (pics observes en 004.0/004.1: 3,7 Go et 4,6 Go
#: a 1 M de commandes, plus une marge pour le serveur PostgreSQL local)
def required_memory_gb(size: int) -> float:
    return 8.0 if size >= 500_000 else 2.0


def check_memory(size: int, *, available: Optional[float], force: bool = False) -> Optional[str]:
    """Raison de NE PAS executer, ou None. Jamais de remplacement silencieux par une taille plus petite."""
    needed = required_memory_gb(size)
    if force:
        return None
    if available is None:
        return f"memoire disponible inconnue; {needed} Go exiges pour {size} commandes (--force-memory pour passer)"
    if available < needed:
        return f"memoire disponible {available} Go < {needed} Go exiges pour {size} commandes"
    return None


# =============================================================================================
# Environnement
# =============================================================================================

def _command(argv: Sequence[str], cwd: Optional[Path] = None) -> Optional[str]:
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=20, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def cpu_model() -> Optional[str]:
    if sys.platform == "darwin":
        return _command(["sysctl", "-n", "machdep.cpu.brand_string"])
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith(("model name", "hardware")):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def git_state(root: Path = ROOT) -> Dict[str, Any]:
    commit = _command(["git", "rev-parse", "HEAD"], cwd=root)
    status = _command(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root)
    return {"commit": commit, "dirty": bool(status) if status is not None else None,
            "branch": _command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)}


def environment(postgresql: Optional[str] = None, environ: Mapping[str, str] = os.environ) -> Dict[str, Any]:
    github = environ.get("GITHUB_ACTIONS") == "true"
    return {
        "os": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu_model": cpu_model(),
        "cpu_count": os.cpu_count(),
        "memory_total_gb": total_memory_gb(),
        "memory_available_gb_at_start": available_memory_gb(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "postgresql": postgresql,
        "container": Path("/.dockerenv").exists(),
        "github_actions": github,
        "runner": ({key: environ.get(key) for key in ("RUNNER_OS", "RUNNER_ARCH", "RUNNER_NAME", "ImageOS",
                                                       "ImageVersion", "GITHUB_RUN_ID", "GITHUB_SHA")}
                   if github else None),
    }


# =============================================================================================
# Tailles et jeux de donnees
# =============================================================================================

def parse_positive_list(text: str, *, name: str, maximum: int = MAX_SIZE) -> List[int]:
    values = []
    for item in str(text).split(","):
        item = item.strip().replace("_", "")
        if not item.isdigit() or int(item) < 1:
            raise BenchmarkConfigError(f"{name}: entier positif attendu ({item or 'vide'})")
        value = int(item)
        if value > maximum:
            raise BenchmarkConfigError(f"{name}: {value} depasse le maximum {maximum}")
        if value in values:
            raise BenchmarkConfigError(f"{name}: {value} en double")
        values.append(value)
    return values


def dataset_config(orders: int) -> Dict[str, Any]:
    return {**DATASET, "orders": orders}


def ensure_dataset(orders: int, data_dir: Path = DATA_DIR) -> Dict[str, Any]:
    """Jeu deterministe (graine 42); regenere seulement si la configuration differe."""
    from datetime import date

    sys.path.insert(0, str(ROOT / "src"))
    from mervio.synthetic import GeneratorConfig, generate_dataset

    config = GeneratorConfig(scenario=DATASET["scenario"], orders=orders, days=DATASET["days"],
                             end_date=date.fromisoformat(DATASET["end_date"]), seed=DATASET["seed"],
                             profile=DATASET["profile"])
    directory = data_dir / f"healthy_store_{orders}"
    manifest_path = directory / "manifest.json"
    generation_s = None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    if manifest is None or manifest.get("config") != config.to_dict():
        started = time.perf_counter()
        manifest = generate_dataset(config, directory).manifest
        generation_s = round(time.perf_counter() - started, 3)
    orders_file = manifest["files"]["shopify_orders.csv"]
    return {
        "directory": str(directory), "target_orders": orders, "orders": manifest["counts"]["orders"],
        "customers": manifest["counts"].get("customers"), "order_rows": orders_file["rows"],
        "orders_csv_bytes": (directory / "shopify_orders.csv").stat().st_size,
        "orders_csv_sha256": orders_file.get("sha256"), "generation_s": generation_s,
        "period": {"start": manifest["config"]["start_date"], "end": manifest["config"]["end_date"]},
        "config": manifest["config"],
    }


# =============================================================================================
# Base jetable
# =============================================================================================

def admin_url(environ: Mapping[str, str] = os.environ) -> str:
    """URL d'administration du serveur LOCAL de benchmark. Jamais celle de l'application."""
    url = environ.get(ADMIN_URL_ENV)
    if not url:
        raise BenchmarkConfigError(f"{ADMIN_URL_ENV} requise pour les charges PostgreSQL")
    for name in APPLICATION_URL_ENVS:
        if environ.get(name) and environ.get(name) == url:
            raise BenchmarkConfigError(f"{ADMIN_URL_ENV} ne doit pas etre la connexion {name}")
    from psycopg.conninfo import conninfo_to_dict
    try:
        params = conninfo_to_dict(url)
    except Exception:  # noqa: BLE001 - URL illisible = configuration refusee
        raise BenchmarkConfigError(f"{ADMIN_URL_ENV} invalide") from None
    host = params.get("host", "")
    if host not in LOCAL_HOSTS and not host.startswith("/"):
        raise BenchmarkConfigError(f"{ADMIN_URL_ENV}: serveur non local refuse (benchmarks isoles seulement)")
    return url


@dataclass
class DisposableDatabase:
    """Base et roles `mervio_perf_*` crees pour une charge, supprimes a la fin (meme en echec).

    Roles, comme le bootstrap du conteneur (004.3.8): migrator proprietaire SANS CREATEROLE,
    app membre de mervio_app, worker membre de mervio_app et mervio_worker; aucun superutilisateur
    ni BYPASSRLS. Les roles de groupe sont crees s'ils manquent, avec la definition des migrations.
    """

    admin: str
    label: str
    suffix: str = field(default_factory=lambda: secrets.token_hex(4))
    password: str = field(default_factory=lambda: secrets.token_hex(24))
    host: str = "127.0.0.1"
    port: str = "5432"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,20}", self.label):
            raise BenchmarkConfigError("libelle de base invalide")
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(self.admin)
        self.host = params.get("host") or "127.0.0.1"
        self.port = str(params.get("port") or "5432")

    @property
    def database(self) -> str:
        return f"{NAME_PREFIX}{self.label}_{self.suffix}"

    def role(self, kind: str) -> str:
        return f"{NAME_PREFIX}{kind}_{self.suffix}"

    def url(self, kind: str) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"postgresql://{self.role(kind)}:{quote(self.password, safe='')}@{host}:{self.port}/{self.database}"

    def secrets(self) -> List[str]:
        return [self.password]

    def create(self) -> None:
        import psycopg
        from psycopg import sql

        with psycopg.connect(self.admin, autocommit=True) as conn:
            for group in ("mervio_app", "mervio_worker"):
                if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (group,)).fetchone() is None:
                    conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE")
                                 .format(sql.Identifier(group)))
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}")
                         .format(sql.Identifier(self.role("migrator")), sql.Literal(self.password)))
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(
                sql.Identifier(self.database), sql.Identifier(self.role("migrator"))))
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {} IN ROLE mervio_app")
                         .format(sql.Identifier(self.role("app")), sql.Literal(self.password)))
            conn.execute(sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {} IN ROLE mervio_app, mervio_worker")
                .format(sql.Identifier(self.role("worker")), sql.Literal(self.password)))
        sys.path.insert(0, str(ROOT / "src"))
        from mervio.persistence import migrate
        migrate.upgrade(self.url("migrator"))

    def drop(self) -> None:
        import psycopg
        from psycopg import sql

        with psycopg.connect(self.admin, autocommit=True) as conn:
            assert self.database.startswith(NAME_PREFIX)
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.database)))
            for kind in ("worker", "app", "migrator"):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(self.role(kind))))

    def urls(self) -> Dict[str, str]:
        return {kind: self.url(kind) for kind in ("migrator", "app", "worker")}


@contextmanager
def disposable_database(admin: str, label: str) -> Iterator[DisposableDatabase]:
    database = DisposableDatabase(admin, label)
    try:
        database.create()
        yield database
    finally:
        database.drop()


def server_version(admin: str) -> str:
    import psycopg
    with psycopg.connect(admin, autocommit=True) as conn:
        return conn.execute("SHOW server_version").fetchone()[0]


def leftover_objects(admin: str) -> Dict[str, int]:
    import psycopg
    with psycopg.connect(admin, autocommit=True) as conn:
        databases = conn.execute("SELECT count(*) FROM pg_database WHERE datname LIKE %s",
                                 (NAME_PREFIX + "%",)).fetchone()[0]
        roles = conn.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE %s", (NAME_PREFIX + "%",)).fetchone()[0]
    return {"databases": databases, "roles": roles}


# =============================================================================================
# Processus fils
# =============================================================================================

def redact(text: str, secrets_: Iterable[str]) -> str:
    for secret in sorted({s for s in secrets_ if s}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return re.sub(r"(postgres(?:ql)?://[^:/@\s]+):[^@\s]+@", r"\1:[redacted]@", text)


def run_child(script: Path, workload: str, payload: Mapping[str, Any], *, timeout: float,
              secrets_: Iterable[str] = (), python: str = sys.executable) -> Dict[str, Any]:
    """Execute une charge dans un processus neuf (memoire de pointe isolee). Payload par stdin, JSON sur stdout."""
    started = time.perf_counter()
    try:
        completed = subprocess.run([python, str(script), "--child", workload], input=json.dumps(payload),
                                   capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "reason": f"delai de {timeout} s depasse",
                "elapsed_s": round(time.perf_counter() - started, 1)}
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or not lines:
        return {"status": "failed", "exit_code": completed.returncode,
                "reason": redact((completed.stderr or completed.stdout)[-1500:], secrets_)}
    try:
        document = json.loads(lines[-1])
    except ValueError:
        return {"status": "failed", "reason": "sortie JSON illisible"}
    document.setdefault("status", "ok")
    return document


# =============================================================================================
# Document de resultats
# =============================================================================================

def new_document(*, suite_run_id: str, configuration: Mapping[str, Any], postgresql: Optional[str]) -> Dict[str, Any]:
    git = git_state()
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "benchmark_name": "mervio-performance",
        "benchmark_version": BENCHMARK_VERSION,
        "suite_run_id": suite_run_id,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
        "git_branch": git["branch"],
        "environment": environment(postgresql),
        "dataset": {**DATASET, "analysis_day": ANALYSIS_DAY, "sizes": {}},
        "configuration": dict(configuration),
        "workloads": {},
    }


def case(name: str, *, category: str, size: Optional[int], status: str, metrics: Mapping[str, Any] = (),
         repetitions: int = 0, throughput: Optional[Mapping[str, Any]] = None, reason: Optional[str] = None,
         details: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    return {"case": name, "category": category, "size": size, "status": status, "repetitions": repetitions,
            "metrics": dict(metrics), "throughput": dict(throughput or {}), "reason": reason,
            "details": dict(details or {})}


REQUIRED_TOP = ("schema_version", "benchmark_name", "benchmark_version", "suite_run_id", "timestamp_utc",
                "git_commit", "environment", "dataset", "configuration", "workloads")
REQUIRED_CASE = ("case", "category", "size", "status", "repetitions", "metrics", "throughput")


def validate_document(document: Mapping[str, Any]) -> List[str]:
    problems = [f"champ absent: {key}" for key in REQUIRED_TOP if key not in document]
    if problems:
        return problems
    if document["schema_version"] != RESULT_SCHEMA_VERSION:
        problems.append("schema_version inattendue")
    if document["dataset"].get("seed") != DATASET["seed"]:
        problems.append("graine du jeu absente ou differente")
    for key in ("os", "machine", "python", "cpu_count"):
        if key not in document["environment"]:
            problems.append(f"environnement sans {key}")
    for workload, entry in document["workloads"].items():
        if entry.get("status") not in STATUSES:
            problems.append(f"{workload}: statut invalide")
        for item in entry.get("cases", []):
            missing = [key for key in REQUIRED_CASE if key not in item]
            if missing:
                problems.append(f"{workload}: cas sans {', '.join(missing)}")
                continue
            if item["status"] not in STATUSES:
                problems.append(f"{workload}/{item['case']}: statut invalide")
            if item["category"] not in CATEGORIES:
                problems.append(f"{workload}/{item['case']}: categorie invalide")
            if item["status"] != "ok" and not item.get("reason"):
                problems.append(f"{workload}/{item['case']}: statut {item['status']} sans raison")
            for metric, summary in item["metrics"].items():
                if isinstance(summary, dict) and summary.get("n", 0) > 0 and not {"median", "p95"} <= set(summary):
                    problems.append(f"{workload}/{item['case']}/{metric}: resume incomplet")
    return problems


def leaked(text: str, secrets_: Iterable[str]) -> List[str]:
    found = [f"secret#{index}" for index, secret in enumerate(secrets_) if secret and secret in text]
    if re.search(r"postgres(?:ql)?://[^:/@\s]+:[^@\s]+@", text):
        found.append("credential_url")
    return found


def write_document(document: Mapping[str, Any], output: Path, secrets_: Iterable[str] = ()) -> Path:
    problems = validate_document(document)
    if problems:
        raise BenchmarkConfigError("document de resultats invalide: " + "; ".join(problems))
    text = json.dumps(document, indent=2, sort_keys=False, default=str) + "\n"
    leaks = leaked(text, list(secrets_))
    if leaks:
        raise BenchmarkConfigError("document de resultats refuse: " + ", ".join(leaks))
    if output.suffix != ".json":
        commit = (document.get("git_commit") or "nogit")[:8]
        stamp = document["timestamp_utc"].replace("-", "").replace(":", "")
        output = output / f"performance_{stamp}_{document['environment']['machine']}_{commit}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output


def which(binary: str) -> Optional[str]:
    return shutil.which(binary)


def wait_until(predicate: Callable[[], bool], *, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
