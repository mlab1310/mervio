#!/usr/bin/env python3
"""Porte CI: le rapport JUnit de pytest prouve une execution COMPLETE (Mission 004.3).

    python scripts/ci_test_gate.py reports/junit.xml --expected-tests 1733

Independante de la configuration pytest (conftest, plugins): meme si une garde de la suite
etait retiree par erreur, cette porte relit le resultat brut et refuse:

- un rapport illisible ou vide;
- un test en echec, en erreur ou IGNORE (skipped, xfail) - aucun seuil de tolerance;
- un nombre de tests different du nombre attendu (egalite exacte: un test supprime, non
  collecte ou deselectionne fait echouer; un test ajoute impose de relever le nombre);
- un module obligatoire (isolation, RLS, migrations, file, audit, worker) absent ou sans
  aucun test passe.

Les comptes sont recalcules test par test; les attributs agreges du rapport sont aussi
verifies, et le plus defavorable l'emporte.

Codes de sortie: 0 porte franchie, 1 porte refusee, 2 usage ou rapport invalide.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

#: Modules dont l'execution reelle est exigee: ce sont eux qui prouvent l'isolation des tenants
#: (RLS, depot et SQL brut), les migrations et les garanties de la file et de l'audit.
REQUIRED_MODULES = (
    "tests.persistence.test_persistence_isolation",
    "tests.persistence.test_persistence_tenancy",
    "tests.persistence.test_persistence_migrations",
    "tests.persistence.test_persistence_deletion",
    "tests.persistence.test_persistence_jobs",
    "tests.persistence.test_persistence_audit",
    "tests.persistence.test_jobs_worker",
    "tests.persistence.test_jobs_concurrency",
    "tests.persistence.test_jobs_query_plans",
    # 004.3: bail renouvelable, jeton d'exclusion, courses reelles
    "tests.persistence.test_jobs_lease",
    # 004.3: identite de service, autorisations explicites, delegation, dispatcher
    "tests.persistence.test_service_identity",
    "tests.persistence.test_dispatcher",
    # 004.3: configuration, redaction, journalisation, sante
    "tests.test_settings",
    "tests.test_redaction",
    "tests.test_logging_contract",
    "tests.test_observability_logging",
    "tests.test_worker_health",
    "tests.persistence.test_published_errors",
    # 004.3: processus worker (cycle de vie, gardien de bail, dispatcher, signaux)
    "tests.test_worker_lifecycle",
    "tests.persistence.test_worker_lease_keeper",
    "tests.persistence.test_worker_dispatched",
    "tests.persistence.test_worker_runtime",
    "tests.test_jobs_unit",
    # 004.3.6: commandes `mervio worker` et `mervio worker healthcheck`
    "tests.test_cli_worker",
    "tests.persistence.test_cli_worker_process",
    # 004.3.7: administration operateur (autorisation, isolation, audit, idempotence, demonstration)
    "tests.test_cli_admin",
    "tests.persistence.test_admin_operations",
    "tests.persistence.test_admin_migration",
    "tests.persistence.test_cli_admin_process",
    # 004.3.8: conteneur (artefacts, pilote du smoke test, bootstrap PostgreSQL et verifications reelles)
    "tests.test_container_artifacts",
    "tests.test_container_smoke",
    "tests.persistence.test_container_bootstrap",
    # 004.3.9: outillage des benchmarks (statistiques, schema, isolation, execution reelle a petite echelle)
    "tests.test_benchmark_harness",
    "tests.persistence.test_benchmark_suite",
    # les gardes elles-memes: si elles ne s'executent plus, la porte ne prouve plus rien
    "tests.test_pytest_guard",
    "tests.test_requirements_lock",
    "tests.test_ci_scripts",
    "tests.test_ci_workflow",
    "tests.persistence.test_ci_migrations",
)

OUTCOMES = ("passed", "failed", "errors", "skipped")


class ReportError(Exception):
    """Rapport JUnit absent, illisible ou vide."""


@dataclass
class Summary:
    counts: Counter = field(default_factory=Counter)
    passed_by_module: Counter = field(default_factory=Counter)
    declared: Dict[str, int] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts[o] for o in OUTCOMES)


def _outcome(case: ET.Element) -> str:
    if case.find("failure") is not None:
        return "failed"
    if case.find("error") is not None:
        return "errors"
    if case.find("skipped") is not None:
        return "skipped"
    return "passed"


def _module_of(classname: str, modules: Iterable[str]) -> Optional[str]:
    for module in modules:
        if classname == module or classname.startswith(module + "."):
            return module
    return None


def summarize(path: str, required_modules: Sequence[str] = REQUIRED_MODULES) -> Summary:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ReportError(f"rapport JUnit illisible: {type(exc).__name__}") from None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if root.tag not in ("testsuite", "testsuites") or not suites:
        raise ReportError("rapport JUnit sans testsuite")
    summary = Summary()
    for suite in suites:
        for name in ("tests", "failures", "errors", "skipped"):
            summary.declared[name] = summary.declared.get(name, 0) + int(suite.get(name, "0"))
        for case in suite.iter("testcase"):
            outcome = _outcome(case)
            summary.counts[outcome] += 1
            if outcome != "passed":
                summary.problems.append(f"{outcome.upper()} {case.get('classname', '')}::{case.get('name', '')}")
            module = _module_of(case.get("classname", ""), required_modules)
            if module is not None and outcome == "passed":
                summary.passed_by_module[module] += 1
    if summary.total == 0:
        raise ReportError("rapport JUnit sans aucun test")
    return summary


def evaluate(summary: Summary, *, expected_tests: int,
             required_modules: Sequence[str] = REQUIRED_MODULES) -> List[str]:
    """Raisons de refus; liste vide = porte franchie."""
    reasons: List[str] = []
    failed = max(summary.counts["failed"], summary.declared.get("failures", 0))
    errors = max(summary.counts["errors"], summary.declared.get("errors", 0))
    skipped = max(summary.counts["skipped"], summary.declared.get("skipped", 0))
    total = max(summary.total, summary.declared.get("tests", 0))
    if failed:
        reasons.append(f"{failed} test(s) en echec")
    if errors:
        reasons.append(f"{errors} erreur(s)")
    if skipped:
        reasons.append(f"{skipped} test(s) ignore(s): aucun n'est tolere en CI")
    if total != expected_tests:
        reasons.append(f"{total} test(s) executes, {expected_tests} attendus exactement")
    for module in required_modules:
        if summary.passed_by_module[module] == 0:
            reasons.append(f"module obligatoire non execute: {module}")
    return reasons


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", help="rapport JUnit produit par pytest --junitxml")
    parser.add_argument("--expected-tests", type=int, required=True,
                        help="nombre exact de tests attendus")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.expected_tests <= 0:
        print("ci_test_gate: --expected-tests doit etre positif", file=sys.stderr)
        return 2
    try:
        summary = summarize(args.report)
    except ReportError as exc:
        print(f"ci_test_gate: {exc}", file=sys.stderr)
        return 2
    print("ci_test_gate: " + ", ".join(f"{summary.counts[o]} {o}" for o in OUTCOMES)
          + f" (attendus: {args.expected_tests})")
    for module in REQUIRED_MODULES:
        print(f"  {summary.passed_by_module[module]:4d} passes  {module}")
    reasons = evaluate(summary, expected_tests=args.expected_tests)
    if reasons:
        for line in summary.problems[:50]:
            print(f"  {line}")
        for reason in reasons:
            print(f"REFUS: {reason}")
        return 1
    print("ci_test_gate: porte franchie")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
