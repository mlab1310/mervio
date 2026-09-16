"""Garde anti-faux-vert (Mission 004.3): aucun test ignore quand la base est obligatoire.

Avec `MERVIO_REQUIRE_DATABASE_TESTS=1` (CI), la suite doit s'executer EN ENTIER. Un test
ignore n'est alors jamais un succes, quelle qu'en soit la cause:

- `pytest.importorskip("psycopg")` d'un module ou d'un conftest (pilote absent);
- `pytest.skip(...)` d'une fixture (fichiers d'exemple absents, base non configuree);
- `skip`, `skipif` ou `xfail` ajoutes plus tard.

La garde relit les rapports de collecte ET d'execution, puis force un code de sortie
d'echec. Sans la variable, elle ne fait rien: les 301 tests PostgreSQL restent ignores
en local sans base, volontairement.

Chargee par `tests/conftest.py` (`pytest_plugins`), ou explicitement par `-p pytest_guard`.
"""
from __future__ import annotations

import os

import pytest

REQUIRE_ENV = "MERVIO_REQUIRE_DATABASE_TESTS"

_skipped: list = []


def required() -> bool:
    return os.environ.get(REQUIRE_ENV) == "1"


def _reason(report) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(getattr(report, "wasxfail", "") or longrepr or "")


def pytest_collectreport(report) -> None:
    if required() and report.skipped:
        _skipped.append((report.nodeid or "<collecte>", _reason(report)))


def pytest_runtest_logreport(report) -> None:
    if required() and report.skipped:
        _skipped.append((report.nodeid, _reason(report)))


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session) -> None:
    if required() and _skipped and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter) -> None:
    if not (required() and _skipped):
        return
    terminalreporter.section("pytest_guard", sep="=", red=True, bold=True)
    terminalreporter.write_line(
        f"{len(_skipped)} test(s) ignore(s) alors que {REQUIRE_ENV}=1: la session echoue.")
    for nodeid, reason in _skipped[:50]:
        terminalreporter.write_line(f"  SKIPPED {nodeid}: {reason[:200]}")
