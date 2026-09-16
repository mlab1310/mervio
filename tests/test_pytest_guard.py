"""La garde anti-faux-vert fait vraiment echouer une session qui ignore des tests (Mission 004.3).

Chaque cas lance un pytest reel dans un repertoire isole, avec la garde chargee par `-p`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent


def run_pytest(tmp_path: Path, files: dict, *, required: bool) -> subprocess.CompletedProcess:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("MERVIO_REQUIRE_DATABASE_TESTS", "PYTEST_ADDOPTS")}
    env["PYTHONPATH"] = str(TESTS_DIR)
    if required:
        env["MERVIO_REQUIRE_DATABASE_TESTS"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "pytest_guard", "-p", "no:cacheprovider", "-q", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)


SKIPPING_TEST = "import pytest\n\ndef test_ok():\n    pass\n\ndef test_skipped():\n    pytest.skip('base absente')\n"


def test_a_skipped_test_fails_the_session_when_the_database_is_required(tmp_path):
    result = run_pytest(tmp_path, {"test_a.py": SKIPPING_TEST}, required=True)
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout
    assert "pytest_guard" in result.stdout
    assert "test_skipped" in result.stdout


def test_a_skipped_test_is_accepted_when_the_database_is_not_required(tmp_path):
    result = run_pytest(tmp_path, {"test_a.py": SKIPPING_TEST}, required=False)
    assert result.returncode == pytest.ExitCode.OK, result.stdout
    assert "pytest_guard" not in result.stdout


def test_a_missing_driver_skipped_at_module_level_fails_the_session(tmp_path):
    module = ("import pytest\npsycopg = pytest.importorskip('mervio_module_qui_n_existe_pas')\n\n"
              "def test_never_runs():\n    pass\n")
    result = run_pytest(tmp_path, {"test_a.py": module, "test_b.py": "def test_ok():\n    pass\n"}, required=True)
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout


def test_a_missing_driver_skipped_by_a_conftest_fails_the_session(tmp_path):
    """Cas de tests/persistence/conftest.py: `importorskip("psycopg")` ignore tout le repertoire."""
    files = {
        "db/conftest.py": "import pytest\npytest.importorskip('mervio_module_qui_n_existe_pas')\n",
        "db/test_db.py": "def test_rls():\n    pass\n",
        "test_b.py": "def test_ok():\n    pass\n",
    }
    result = run_pytest(tmp_path, files, required=True)
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout
    assert run_pytest(tmp_path, {}, required=False).returncode == pytest.ExitCode.OK


def test_a_skipping_fixture_fails_the_session(tmp_path):
    module = ("import pytest\n\n@pytest.fixture\ndef database():\n    pytest.skip('URL absente')\n\n"
              "def test_uses_database(database):\n    pass\n")
    result = run_pytest(tmp_path, {"test_a.py": module}, required=True)
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout


def test_skip_markers_and_expected_failures_fail_the_session(tmp_path):
    module = ("import pytest\n\n@pytest.mark.skipif(True, reason='x')\ndef test_a():\n    pass\n\n"
              "@pytest.mark.xfail(reason='y')\ndef test_b():\n    assert False\n")
    result = run_pytest(tmp_path, {"test_a.py": module}, required=True)
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout


def test_a_complete_session_still_succeeds_when_the_database_is_required(tmp_path):
    result = run_pytest(tmp_path, {"test_a.py": "def test_ok():\n    pass\n"}, required=True)
    assert result.returncode == pytest.ExitCode.OK, result.stdout


def test_a_real_failure_is_never_hidden_by_the_guard(tmp_path):
    result = run_pytest(tmp_path, {"test_a.py": "def test_ko():\n    assert False\n"}, required=True)
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout


def test_the_project_suite_loads_the_guard(request):
    assert request.config.pluginmanager.has_plugin("pytest_guard")


# -- la vraie suite du projet, dans les pannes que la CI doit refuser ---------------------------

ROOT = TESTS_DIR.parent
#: collecte de toute la suite, comme en CI, puis selection de deux modules dependants de la base
DATABASE_SELECTION = "test_jobs_unit or test_persistence_isolation"


def run_project_suite(env_changes: dict, extra_path: Path | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("MERVIO_REQUIRE_DATABASE_TESTS", "MERVIO_TEST_ADMIN_DATABASE_URL", "PYTEST_ADDOPTS")}
    env.update({"MERVIO_REQUIRE_DATABASE_TESTS": "1", **env_changes})
    if extra_path is not None:
        env["PYTHONPATH"] = str(extra_path)
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "tests", "-k", DATABASE_SELECTION],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def test_the_project_suite_fails_without_the_database_driver(tmp_path):
    """psycopg absent: les `importorskip` deviennent un echec, pas 60 + N tests ignores."""
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'psycopg' or name.startswith('psycopg.'):\n"
        "            raise ModuleNotFoundError(\"No module named 'psycopg'\", name=name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n", encoding="utf-8")
    result = run_project_suite({"MERVIO_TEST_ADMIN_DATABASE_URL": "postgresql://ci@127.0.0.1:1/postgres"},
                               extra_path=tmp_path)
    assert result.returncode != pytest.ExitCode.OK, result.stdout
    # module sans pilote: signale par la garde; conftest PostgreSQL: erreur de collecte explicite
    assert "SKIPPED tests/test_jobs_unit.py" in result.stdout, result.stdout
    assert "ModuleNotFoundError: No module named 'psycopg'" in result.stdout, result.stdout


def test_the_project_suite_fails_without_a_database_url():
    result = run_project_suite({})
    assert result.returncode != pytest.ExitCode.OK, result.stdout


def test_the_project_suite_fails_when_postgresql_is_unreachable():
    result = run_project_suite({"MERVIO_TEST_ADMIN_DATABASE_URL": "postgresql://ci@127.0.0.1:1/postgres"})
    assert result.returncode != pytest.ExitCode.OK, result.stdout
