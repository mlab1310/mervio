"""Porte CI des migrations contre un vrai PostgreSQL (Mission 004.3).

Les defauts sont injectes en SQL reel entre les etapes: la porte doit les voir dans le
catalogue, pas dans une simulation.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import psycopg
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci_migrations.py"
spec = importlib.util.spec_from_file_location("mervio_ci_migrations_db", SCRIPT)
ci_migrations = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ci_migrations
spec.loader.exec_module(ci_migrations)


def quiet(line: str) -> None:
    pass


@pytest.fixture
def fresh(pg):
    name = pg.create_empty_database("cigate")
    try:
        yield ci_migrations.PostgresTarget(pg.url("migrator", name))
    finally:
        pg.drop_database(name)


class Sabotaged(ci_migrations.PostgresTarget):
    """Execute du SQL reel juste apres une etape donnee."""

    def __init__(self, url, *, after, statement):
        super().__init__(url)
        self.after, self.statement, self.upgrades = after, statement, 0

    def _sabotage(self, step):
        if step == self.after:
            with psycopg.connect(self.url, autocommit=True) as conn:
                conn.execute(self.statement)

    def upgrade(self, revision):
        super().upgrade(revision)
        self.upgrades += 1
        self._sabotage(f"upgrade{self.upgrades}")

    def downgrade(self, revision):
        super().downgrade(revision)
        self._sabotage("downgrade")


def test_the_gate_passes_on_the_real_migrations(pg):
    assert ci_migrations.run(pg.admin_conninfo, log=quiet) == []
    with pg.admin() as conn:
        leftovers = conn.execute(
            "SELECT (SELECT count(*) FROM pg_roles WHERE rolname LIKE 'mervio_ci_migrator_%'), "
            "(SELECT count(*) FROM pg_database WHERE datname LIKE 'mervio_ci_migrations_%')").fetchone()
    assert leftovers == (0, 0)


def test_the_gate_sees_an_object_left_by_a_downgrade(fresh):
    target = Sabotaged(fresh.url, after="downgrade", statement="CREATE TABLE forgotten (id int)")
    reasons = ci_migrations.verify_round_trip(target, log=quiet)
    # la table survit aussi a la remontee: la porte la voit deux fois
    assert reasons == ["objets restants apres downgrade base: relation forgotten",
                       "schema different apres un aller-retour: colonnes, tables"]


def test_the_gate_sees_a_schema_drift_after_a_round_trip(fresh):
    target = Sabotaged(fresh.url, after="upgrade2",
                       statement="ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY")
    reasons = ci_migrations.verify_round_trip(target, log=quiet)
    assert reasons == ["schema different apres un aller-retour: tables"]


def test_the_gate_sees_a_dropped_policy(fresh):
    target = Sabotaged(fresh.url, after="upgrade2", statement="DROP POLICY jobs_purge_terminal_only ON jobs")
    reasons = ci_migrations.verify_round_trip(target, log=quiet)
    assert reasons == ["schema different apres un aller-retour: politiques"]


def test_the_gate_reports_a_failing_downgrade(fresh):
    """Une dependance inattendue empeche la descente: refus explicite, pas de succes partiel."""
    target = Sabotaged(fresh.url, after="upgrade1",
                       statement="CREATE VIEW blocking AS SELECT id FROM organizations")
    reasons = ci_migrations.verify_round_trip(target, log=quiet)
    assert len(reasons) == 1 and reasons[0].startswith("downgrade base en echec")
