#!/usr/bin/env python3
"""Tableaux Markdown d'un document de benchmarks (Mission 004.3.9). Aucun chiffre recopie a la main.

    python benchmarks/perf_report.py benchmark-results/performance_<...>.json > tables.md

Chaque tableau garde ses charges separees: moteur, persistance, infrastructure (file, bail),
worker reel, concurrence. Un cas non execute ou en echec apparait avec sa raison.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import perf_harness as harness  # noqa: E402


def _cell(summary: Optional[Mapping[str, Any]], unit: str = "", scale: float = 1.0, digits: int = 2) -> str:
    if not summary or not summary.get("n"):
        return "—"
    return f"{summary['median'] * scale:.{digits}f}{unit}"


def _p95(summary: Optional[Mapping[str, Any]], unit: str = "", scale: float = 1.0, digits: int = 2) -> str:
    if not summary or not summary.get("n"):
        return "—"
    mark = " (=max)" if summary.get("p95_is_max") else ""
    return f"{summary['p95'] * scale:.{digits}f}{unit}{mark}"


def _table(headers: Sequence[str], rows: List[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _cases(document: Mapping[str, Any], workload: str) -> List[Mapping[str, Any]]:
    return document.get("workloads", {}).get(workload, {}).get("cases", [])


def _skipped(item: Mapping[str, Any], width: int) -> List[str]:
    return [item["case"], item["status"].upper()] + [item.get("reason") or "—"] + ["—"] * (width - 3)


def environment_table(document: Mapping[str, Any]) -> str:
    env = document["environment"]
    dirty = " (+ modifications locales)" if document.get("git_dirty") else ""
    suite = f"{document['benchmark_name']} {document['benchmark_version']}, duree {document.get('duration_s')} s"
    rows = [("commit", f"{document['git_commit']}{dirty}"),
            ("date (UTC)", document["timestamp_utc"]), ("OS", env["os"]),
            ("CPU", f"{env['cpu_model']} ({env['cpu_count']} coeurs)"),
            ("memoire", f"{env['memory_total_gb']} Go, {env['memory_available_gb_at_start']} Go disponibles au debut"),
            ("Python", f"{env['python']} ({env['python_implementation']})"), ("PostgreSQL", env["postgresql"] or "—"),
            ("conteneur", "oui" if env["container"] else "non"),
            ("GitHub Actions", json.dumps(env["runner"]) if env["github_actions"] else "non"),
            ("suite", suite)]
    return _table(("Element", "Valeur"), rows)


def dataset_table(document: Mapping[str, Any]) -> str:
    rows = []
    for size, info in sorted(document["dataset"]["sizes"].items(), key=lambda item: int(item[0])):
        rows.append((size, info["orders"], info["customers"], info["order_rows"],
                     f"{info['orders_csv_bytes'] / 1e6:.1f} Mo", f"{info['period']['start']} → {info['period']['end']}",
                     info["orders_csv_sha256"][:12]))
    return _table(("Cible", "Commandes", "Clients", "Lignes CSV", "CSV commandes", "Periode", "sha256 (debut)"), rows)


def analytics_table(document: Mapping[str, Any]) -> str:
    rows = []
    for item in _cases(document, "analytics"):
        if item["status"] != "ok":
            rows.append(_skipped(item, 8))
            continue
        m = item["metrics"]
        rows.append((item["size"], item["repetitions"], _cell(m["e2e_wall_s"], " s"), _p95(m["e2e_wall_s"], " s"),
                     _cell(m["e2e_cpu_s"], " s"), _cell(m["e2e_peak_rss_mb"], " Mo", digits=0),
                     _p95(m["e2e_peak_rss_mb"], " Mo", digits=0),
                     f"{item['throughput']['orders_per_second_median']:,.0f}".replace(",", " ")))
    return _table(("Commandes", "N", "Mediane", "P95", "CPU (med.)", "RSS pic (med.)", "RSS pic P95",
                   "Commandes/s"), rows)


def stages_table(document: Mapping[str, Any]) -> str:
    cases = [c for c in _cases(document, "analytics") if c["status"] == "ok"]
    if not cases:
        return "_aucune mesure_"
    headers = ["Etape"] + [f"{c['size']} (med. s / part)" for c in cases]
    rows = []
    for stage in cases[0]["details"]["stages_s"]:
        row = [stage]
        for c in cases:
            share = c["details"]["stage_share_of_median_total"][stage]
            row.append(f"{c['details']['stages_s'][stage]['median']:.3f} / {share:.0%}")
        rows.append(row)
    return _table(headers, rows)


def persistence_table(document: Mapping[str, Any]) -> str:
    keys = ("csv_load_dataset_s", "db_write_snapshot_s", "db_load_dataset_s", "analyze_loaded_dataset_s",
            "db_persist_report_s", "app_import_csv_snapshot_s")
    rows = []
    for item in _cases(document, "persistence"):
        if item["status"] != "ok":
            rows.append(_skipped(item, 10))
            continue
        m = item["metrics"]
        rows.append([item["size"], item["repetitions"], item["details"]["canonical_rows"]]
                    + [f"{_cell(m[k], ' s')} / {_p95(m[k], ' s')}" for k in keys]
                    + [_cell(m["peak_rss_mb"], " Mo", digits=0)])
    return _table(("Commandes", "N", "Lignes canoniques", "Lecture CSV", "Ecriture instantane", "Relecture",
                   "Analyse", "Ecriture rapport", "Import complet (cas d'usage)", "RSS pic"), rows)


def queue_table(document: Mapping[str, Any]) -> str:
    rows = []
    for item in _cases(document, "queue"):
        if item["status"] != "ok":
            rows.append(_skipped(item, 8))
            continue
        m = item["metrics"]
        plan = item["details"]["probe_plan"]
        # Bitmap Heap Scan: la sonde lit TOUS les travaux en file d'une organisation puis les trie;
        # sinon elle suit jobs_ready_idx et s'arrete au premier (un Sort peut alors ne trier que les autorisations)
        scan = ("lecture + tri des travaux" if any(node.startswith("Bitmap Heap Scan") for node in plan["nodes"])
                else "index + LIMIT")
        rows.append((item["size"], item["details"]["organizations"],
                     f"{_cell(m['dispatcher_probe_ms'], digits=2)} / {_p95(m['dispatcher_probe_ms'], digits=2)}",
                     f"{scan}, {plan['execution_ms']:.2f} ms",
                     f"{_cell(m['claim_ms'], digits=2)} / {_p95(m['claim_ms'], digits=2)} (N={m['claim_ms']['n']})",
                     f"{_cell(m['complete_ms'], digits=2)} / {_p95(m['complete_ms'], digits=2)}",
                     f"{_cell(m['enqueue_api_ms'], digits=2)} / {_p95(m['enqueue_api_ms'], digits=2)}",
                     " · ".join(f"{k}: {v['jobs_per_second']}" for k, v in item["throughput"]["noop_worker"].items())))
    return _table(("Travaux en file", "Organisations", "Sonde dispatcher ms (med/p95)", "Plan de la sonde",
                   "Prise ms (med/p95)", "Fin ms (med/p95)", "Mise en file API ms (med/p95)",
                   "Cadre a VIDE, travaux/s par fils"), rows)


def lease_table(document: Mapping[str, Any]) -> str:
    rows = []
    for item in _cases(document, "lease"):
        if item["status"] != "ok":
            rows.append(_skipped(item, 7))
            continue
        s = item["metrics"]["renew_ms"]
        rows.append((item["case"], s["n"], f"{s['median']:.2f}", f"{s['p95']:.2f}", f"{s['p99']:.2f}",
                     f"{s['max']:.2f}", f"{item['details']['job_seconds']:.1f} s"))
    return _table(("Condition", "Renouvellements", "p50 ms", "p95 ms", "p99 ms", "max ms", "Duree du travail"), rows)


def worker_table(document: Mapping[str, Any]) -> str:
    rows = []
    for item in _cases(document, "worker"):
        if item["status"] != "ok":
            rows.append(_skipped(item, 10))
            continue
        m, d = item["metrics"], item["details"]
        waits = (f"{_cell(m['import_queue_wait_ms'], ' ms', digits=0)} / "
                 f"{_cell(m['analysis_queue_wait_ms'], ' ms', digits=0)}")
        rows.append((item["size"], item["repetitions"],
                     _cell(m["import_handler_ms"], " s", 0.001),
                     _cell(m["import_framework_overhead_ms"], " ms", digits=1),
                     _cell(m["analysis_handler_ms"], " s", 0.001),
                     _cell(m["analysis_framework_overhead_ms"], " ms", digits=1),
                     waits,
                     f"{_cell(m['chain_ms'], ' s', 0.001)} / {_p95(m['chain_ms'], ' s', 0.001)}",
                     f"{d['worker_peak_rss_mb']:.0f} Mo", d["lease_renewals_total"]))
    return _table(("Commandes", "N", "Import (gestionnaire)", "Import (cadre)", "Analyse (gestionnaire)",
                   "Analyse (cadre)", "Attente en file imp./ana.", "Chaine med / p95", "RSS pic worker",
                   "Renouvellements"), rows)


def concurrency_table(document: Mapping[str, Any]) -> str:
    rows = []
    for item in _cases(document, "concurrency"):
        if item["status"] != "ok":
            rows.append(_skipped(item, 8))
            continue
        m, d = item["metrics"], item["details"]
        rows.append((d["workers"], item["repetitions"], f"{item['throughput']['analyses_per_second']:.2f}",
                     f"{item['throughput']['seconds']:.1f} s",
                     f"{_cell(m['handler_ms'], ' s', 0.001)} / {_p95(m['handler_ms'], ' s', 0.001)}",
                     f"{_cell(m['job_latency_ms'], ' s', 0.001)} / {_p95(m['job_latency_ms'], ' s', 0.001)}",
                     f"{d['sum_of_sampled_peak_rss_mb']:.0f} Mo", d["database_active_backends_peak"]))
    return _table(("Workers", "Analyses", "Analyses/s", "Duree", "Analyse med/p95", "Latence med/p95",
                   "RSS cumule (echant.)", "Backends actifs max"), rows)


def render(document: Mapping[str, Any]) -> str:
    problems = harness.validate_document(document)
    if problems:
        raise harness.BenchmarkConfigError("document invalide: " + "; ".join(problems))
    sections: Dict[str, str] = {
        "Environnement": environment_table(document), "Jeux de donnees": dataset_table(document),
        "Moteur analytique (sans base)": analytics_table(document), "Etapes du moteur": stages_table(document),
        "Persistance PostgreSQL": persistence_table(document),
        "Infrastructure: file et dispatcher (gestionnaire VIDE pour le cadre)": queue_table(document),
        "Infrastructure: gardien de bail": lease_table(document),
        "Worker reel: import puis analyse (bout en bout)": worker_table(document),
        "Concurrence: processus worker reels, analyses reelles": concurrency_table(document),
    }
    return "\n\n".join(f"### {title}\n\n{body}" for title, body in sections.items()) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: perf_report.py <resultats.json>", file=sys.stderr)
        return 2
    try:
        document = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        print(render(document), end="")
    except (OSError, ValueError, KeyError, harness.BenchmarkConfigError) as exc:
        print(f"perf_report: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
