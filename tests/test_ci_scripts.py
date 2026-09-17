"""Scripts de porte CI: chaque refus attendu est reellement produit (Mission 004.3). Sans base."""
from __future__ import annotations

import importlib.util
import sys
from email.message import Message
from importlib import metadata
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"mervio_ci_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = load("ci_test_gate")
migrations = load("ci_migrations")
licenses = load("ci_license_check")


# -- ci_test_gate -----------------------------------------------------------------------------

def junit(tmp_path: Path, cases, *, declared: dict | None = None, name: str = "junit.xml") -> str:
    body = []
    for classname, test, outcome in cases:
        inner = {"passed": "", "failed": "<failure message='x'/>", "error": "<error message='x'/>",
                 "skipped": "<skipped type='pytest.skip' message='x'/>"}[outcome]
        body.append(f"<testcase classname='{classname}' name='{test}'>{inner}</testcase>")
    counts = {"tests": len(cases), "failures": sum(o == "failed" for *_, o in cases),
              "errors": sum(o == "error" for *_, o in cases), "skipped": sum(o == "skipped" for *_, o in cases)}
    counts.update(declared or {})
    attributes = " ".join(f"{k}='{v}'" for k, v in counts.items())
    path = tmp_path / name
    path.write_text(f"<testsuites><testsuite name='pytest' {attributes}>{''.join(body)}</testsuite></testsuites>",
                    encoding="utf-8")
    return str(path)


def complete_cases(extra=()):
    return [(module, "test_one", "passed") for module in gate.REQUIRED_MODULES] + list(extra)


def run_gate(path: str, expected: int) -> int:
    return gate.main([path, "--expected-tests", str(expected)])


def test_the_gate_accepts_a_complete_run(tmp_path, capsys):
    cases = complete_cases([("tests.test_kpi", "test_x", "passed")])
    assert run_gate(junit(tmp_path, cases), len(cases)) == 0
    assert "porte franchie" in capsys.readouterr().out


@pytest.mark.parametrize("outcome", ["failed", "error", "skipped"])
def test_the_gate_refuses_any_non_passing_test(tmp_path, capsys, outcome):
    cases = complete_cases([("tests.test_kpi", "test_x", outcome)])
    assert run_gate(junit(tmp_path, cases), len(cases)) == 1
    assert "REFUS" in capsys.readouterr().out


def test_the_gate_refuses_a_skip_declared_only_in_the_suite_attributes(tmp_path):
    cases = complete_cases()
    assert run_gate(junit(tmp_path, cases, declared={"skipped": 1}), len(cases)) == 1


def test_the_gate_refuses_a_class_level_skip_inside_a_required_module(tmp_path):
    cases = complete_cases([("tests.persistence.test_persistence_isolation.TestRaw", "test_y", "skipped")])
    assert run_gate(junit(tmp_path, cases), len(cases)) == 1


@pytest.mark.parametrize("delta", [-1, 1])
def test_the_gate_requires_the_exact_number_of_tests(tmp_path, capsys, delta):
    cases = complete_cases()
    assert run_gate(junit(tmp_path, cases), len(cases) + delta) == 1
    assert "attendus exactement" in capsys.readouterr().out


def test_the_gate_refuses_a_run_where_a_required_module_is_missing(tmp_path, capsys):
    cases = [case for case in complete_cases() if case[0] != "tests.persistence.test_persistence_isolation"]
    cases.append(("tests.test_kpi", "test_padding", "passed"))
    assert run_gate(junit(tmp_path, cases), len(cases)) == 1
    assert "module obligatoire non execute: tests.persistence.test_persistence_isolation" in capsys.readouterr().out


def test_a_module_with_a_similar_prefix_does_not_count_as_the_required_one(tmp_path):
    cases = [case for case in complete_cases() if case[0] != "tests.test_jobs_unit"]
    cases.append(("tests.test_jobs_unit_extra", "test_z", "passed"))
    assert run_gate(junit(tmp_path, cases), len(cases)) == 1


def test_the_rls_modules_are_required():
    for module in ("tests.persistence.test_persistence_isolation", "tests.persistence.test_persistence_tenancy",
                   "tests.persistence.test_persistence_migrations", "tests.test_pytest_guard",
                   "tests.test_ci_scripts", "tests.persistence.test_ci_migrations",
                   "tests.persistence.test_jobs_lease", "tests.persistence.test_service_identity",
                   "tests.persistence.test_dispatcher", "tests.test_settings", "tests.test_redaction",
                   "tests.test_logging_contract", "tests.test_worker_health",
                   "tests.persistence.test_published_errors", "tests.test_worker_lifecycle",
                   "tests.persistence.test_worker_lease_keeper", "tests.persistence.test_worker_dispatched",
                   "tests.persistence.test_worker_runtime"):
        assert module in gate.REQUIRED_MODULES


@pytest.mark.parametrize("content", ["", "<not-xml", "<testsuites/>", "<testsuites><testsuite name='p'/></testsuites>",
                                     "<html/>"])
def test_the_gate_rejects_an_unusable_report(tmp_path, content):
    path = tmp_path / "junit.xml"
    path.write_text(content, encoding="utf-8")
    assert run_gate(str(path), 1) == 2


def test_the_gate_rejects_a_missing_report_and_bad_arguments(tmp_path):
    assert run_gate(str(tmp_path / "absent.xml"), 1) == 2
    assert gate.main([str(tmp_path / "absent.xml")]) == 2
    assert run_gate(junit(tmp_path, complete_cases()), 0) == 2


def test_every_required_module_exists():
    """Chaque module obligatoire existe vraiment: une faute de frappe rendrait la porte impossible."""
    root = SCRIPTS.parent
    for module in gate.REQUIRED_MODULES:
        assert (root / (module.replace(".", "/") + ".py")).is_file(), module


# -- ci_migrations ----------------------------------------------------------------------------

class FakeTarget:
    """Cible de migration en memoire: chaque attribut simule un defaut precis."""

    def __init__(self, *, head="0005_audit", stuck_revision=None, residue=(), drift=False, fail_on=None):
        self._head, self.revision = head, None
        self.stuck_revision, self._residue, self.drift, self.fail_on = stuck_revision, list(residue), drift, fail_on
        self.upgrades = 0
        self.calls = []

    def upgrade(self, revision):
        self.calls.append(("upgrade", revision))
        if self.fail_on == ("upgrade", self.upgrades):
            raise RuntimeError("syntax error at or near CREATE")
        self.upgrades += 1
        self.revision = self.stuck_revision or self._head

    def downgrade(self, revision):
        self.calls.append(("downgrade", revision))
        if self.fail_on == ("downgrade", 0):
            raise RuntimeError("cannot drop table jobs because other objects depend on it")
        self.revision = None

    def current(self):
        return self.revision

    def head(self):
        return self._head

    def fingerprint(self):
        drifted = self.drift is True and self.upgrades > 1
        return tuple(((label, "changed" if drifted else "same"),) for label, _ in migrations.FINGERPRINT_QUERIES)

    def residue(self):
        return self._residue


def verify(target):
    return migrations.verify_round_trip(target, log=lambda line: None)


def test_a_clean_round_trip_passes():
    target = FakeTarget()
    assert verify(target) == []
    assert target.calls == [("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")]


def test_an_upgrade_that_does_not_reach_head_is_refused():
    reasons = verify(FakeTarget(stuck_revision="0004_jobs"))
    assert reasons and "head attendue" in reasons[0]


def test_a_failing_upgrade_is_refused_with_its_error():
    reasons = verify(FakeTarget(fail_on=("upgrade", 0)))
    assert reasons == ["upgrade head en echec: RuntimeError: syntax error at or near CREATE"]


def test_a_failing_second_upgrade_is_refused():
    reasons = verify(FakeTarget(fail_on=("upgrade", 1)))
    assert reasons and reasons[0].startswith("second upgrade head en echec")


def test_a_failing_downgrade_is_refused():
    reasons = verify(FakeTarget(fail_on=("downgrade", 0)))
    assert reasons and reasons[0].startswith("downgrade base en echec")


def test_objects_left_after_a_full_downgrade_are_refused():
    reasons = verify(FakeTarget(residue=[("relation", "jobs"), ("fonction", "jobs_guard_insert")]))
    assert reasons == ["objets restants apres downgrade base: relation jobs, fonction jobs_guard_insert"]


def test_a_schema_that_differs_after_a_round_trip_is_refused():
    reasons = verify(FakeTarget(drift=True))
    assert reasons and reasons[0].startswith("schema different apres un aller-retour")


def test_the_fingerprint_covers_rls_and_privileges():
    labels = {label for label, _ in migrations.FINGERPRINT_QUERIES}
    assert {"politiques", "triggers", "fonctions", "tables", "droits de colonne", "contraintes", "index"} <= labels
    tables_query = dict(migrations.FINGERPRINT_QUERIES)["tables"]
    assert "relforcerowsecurity" in tables_query and "relacl" in tables_query


def test_the_migration_gate_requires_an_explicit_local_admin_url():
    with pytest.raises(migrations.ConfigurationError):
        migrations.admin_url({})
    with pytest.raises(migrations.ConfigurationError):
        migrations.admin_url({migrations.ADMIN_URL_ENV: "postgresql://admin@db.example.com/postgres"})
    assert migrations.admin_url({migrations.ADMIN_URL_ENV: "postgresql://admin@db.example.com/postgres",
                                 "MERVIO_TEST_ALLOW_REMOTE_DATABASE": "1"})
    assert migrations.admin_url({migrations.ADMIN_URL_ENV: "postgresql://admin@127.0.0.1:5432/postgres"})


def test_the_migration_gate_refuses_another_postgresql_major():
    migrations.check_server(170011, 17)
    for version in (160004, 180000):
        with pytest.raises(migrations.ConfigurationError):
            migrations.check_server(version, 17)


def test_the_migration_gate_exit_codes_for_configuration_errors(capsys):
    assert migrations.main([], environ={}) == 2
    assert migrations.main(["--postgres-major"], environ={}) == 2
    assert migrations.main(["--other", "17"], environ={}) == 2
    assert "MERVIO_TEST_ADMIN_DATABASE_URL" in capsys.readouterr().err


def test_the_migration_gate_refuses_an_unreachable_server(capsys):
    environ = {migrations.ADMIN_URL_ENV: "postgresql://ci@127.0.0.1:1/postgres?connect_timeout=2"}
    assert migrations.main([], environ=environ) == 1
    assert "REFUS" in capsys.readouterr().out


# -- ci_license_check -------------------------------------------------------------------------

def meta(**fields) -> Message:
    message = Message()
    for key, value in fields.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            message[key.replace("_", "-")] = item
    return message


@pytest.mark.parametrize("expression", ["MIT", "BSD-3-Clause", "Apache-2.0 OR BSD-2-Clause", "MIT AND PSF-2.0",
                                        "GPL-3.0-only OR MIT"])
def test_permissive_licences_are_accepted(expression):
    assert licenses.license_problem("anything", expression) is None


@pytest.mark.parametrize("expression", ["GPL-3.0-only", "AGPL-3.0-or-later", "SSPL-1.0", "Proprietary",
                                        "MIT AND GPL-2.0-only", "LGPL-3.0-only", "(MIT OR GPL-2.0) AND BSD-3-Clause",
                                        "MIT OR Apache-2.0 AND GPL-3.0-only"])
def test_other_licences_are_refused(expression):
    assert licenses.license_problem("anything", expression)


def test_a_reviewed_licence_is_accepted_only_for_its_distribution():
    assert licenses.license_problem("psycopg", "LGPL-3.0-only") is None
    assert licenses.license_problem("psycopg_binary", "LGPL-3.0-only") is None
    assert licenses.license_problem("other-lib", "LGPL-3.0-only")


def test_a_missing_licence_is_refused():
    assert licenses.license_problem("anything", None)
    assert licenses.declared_license(meta(Name="x")) is None


def test_the_declared_licence_is_read_in_order_of_reliability():
    assert licenses.declared_license(meta(License_Expression="MIT", License="GPL")) == "MIT"
    assert licenses.declared_license(meta(License="MIT License")) == "MIT"
    assert licenses.declared_license(meta(Classifier=["License :: OSI Approved :: MIT License"])) == "MIT"
    assert licenses.declared_license(meta(Classifier=["License :: OSI Approved :: MIT License",
                                                      "License :: OSI Approved :: GNU General Public License (GPL)"])) is None
    assert licenses.declared_license(meta(License="x" * 200)) is None


def test_an_unknown_free_text_licence_is_refused():
    assert licenses.license_problem("x", licenses.declared_license(meta(License="Custom Licence v1")))


def test_a_locked_distribution_that_is_not_installed_is_refused():
    def lookup(name):
        raise metadata.PackageNotFoundError(name)
    [verdict] = licenses.check(["ghost"], lookup=lookup)
    assert verdict.problem == "distribution du lock non installee"


def test_the_licence_gate_fails_on_a_refused_dependency(tmp_path, monkeypatch, capsys):
    lock = tmp_path / "requirements.lock"
    lock.write_text("somegpl==1.0\n", encoding="utf-8")
    monkeypatch.setattr(licenses.metadata, "metadata",
                        lambda name: meta(Name=name, Version="1.0", License_Expression="GPL-3.0-only"))
    assert licenses.main([str(lock)]) == 1
    assert "REFUS" in capsys.readouterr().out


def test_the_licence_gate_ignores_lines_for_other_platforms(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text('# commentaire\nalpha==1.0\nbeta==2.0 ; sys_platform == "no-such-platform"\n', encoding="utf-8")
    assert licenses.locked_distributions(lock) == ["alpha"]


def test_the_licence_gate_usage_errors(tmp_path):
    assert licenses.main([]) == 2
    assert licenses.main([str(tmp_path / "absent.lock")]) == 2
    empty = tmp_path / "empty.lock"
    empty.write_text("# rien\n", encoding="utf-8")
    assert licenses.main([str(empty)]) == 2


def test_the_real_lock_passes_the_licence_gate():
    assert licenses.main([str(SCRIPTS.parent / "requirements.lock")]) == 0
