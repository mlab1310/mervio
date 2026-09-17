"""Le workflow CI ne peut pas produire de faux vert (Mission 004.3). Lecture statique, sans base.

Ces tests ne prouvent pas que GitHub a execute la CI: seulement que le fichier versionne
contient les portes prevues et aucun moyen de les contourner.
"""
from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
LINES = [line.split(" #", 1)[0].rstrip() for line in TEXT.splitlines() if not line.lstrip().startswith("#")]
CODE = "\n".join(LINES)


def job(name: str) -> str:
    """Bloc texte d'un job (de `  name:` au job suivant)."""
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][\w-]*:\n|\Z)", CODE, flags=re.M | re.S)
    assert match, f"job {name} absent"
    return match.group(1)


def run_commands(block: str) -> list:
    return [line.split("run:", 1)[1].strip() for line in block.splitlines() if line.strip().startswith("run:")]


def expected_tests() -> int:
    match = re.search(r'^\s*MERVIO_EXPECTED_TESTS:\s*"(\d+)"\s*$', CODE, flags=re.M)
    assert match, "MERVIO_EXPECTED_TESTS doit etre un entier explicite"
    return int(match.group(1))


def test_the_four_jobs_exist_and_are_bounded():
    assert re.findall(r"^  ([a-z][\w-]*):\n", CODE.split("\njobs:\n", 1)[1], flags=re.M) == [
        "lint", "test", "security", "docker"]
    for name in ("lint", "test", "security", "docker"):
        block = job(name)
        assert "runs-on: ubuntu-24.04" in block
        assert re.search(r"timeout-minutes: \d+", block), name


@pytest.mark.parametrize("forbidden", ["continue-on-error", "|| true", "|| :", "if:", "set +e", "allow-failure",
                                       "fail-fast: false"])
def test_nothing_can_turn_a_failure_green(forbidden):
    assert forbidden not in CODE


def test_the_workflow_has_read_only_permissions():
    assert re.search(r"^permissions:\n  contents: read\n", CODE, flags=re.M)
    assert "write" not in re.search(r"^permissions:\n((?:  .*\n)+)", CODE + "\n", flags=re.M).group(1)
    assert CODE.count("persist-credentials: false") == CODE.count("actions/checkout@")


def test_every_action_is_pinned_to_a_commit():
    uses = re.findall(r"uses:\s*(\S+)", CODE)
    assert uses
    for reference in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", reference), reference


def test_the_database_is_a_pinned_postgresql_17_and_is_mandatory():
    block = job("test")
    assert re.search(r"image: postgres:17\.\d+[\w.-]*@sha256:[0-9a-f]{64}", block)
    assert 'MERVIO_REQUIRE_DATABASE_TESTS: "1"' in block
    url = re.search(r"MERVIO_TEST_ADMIN_DATABASE_URL: (\S+)", block).group(1)
    assert url.startswith("postgresql://") and "@127.0.0.1:" in url
    assert "--health-cmd" in block
    assert "--locale=C" in block


def test_no_credential_is_written_in_the_workflow():
    credential_url = re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:/@\s]+:[^@\s]+@")
    assert not credential_url.search(TEXT)
    passwords = list(re.finditer(r"^\s*(POSTGRES_PASSWORD|PGPASSWORD):[ \t]*(.*)$", TEXT, flags=re.M))
    assert len(passwords) == 2
    for match in passwords:
        assert match.group(2) == "ci-${{ github.run_id }}-${{ github.run_attempt }}", match.group(0)


def test_the_test_job_runs_every_gate_in_order():
    commands = run_commands(job("test"))
    joined = "\n".join(commands)
    order = ["pip install --no-deps -r requirements.lock", "pip check", "scripts/ci_license_check.py requirements.lock",
             "scripts/ci_migrations.py --postgres-major 17", "-m pytest", "scripts/ci_test_gate.py reports/junit.xml"]
    positions = [joined.find(fragment) for fragment in order]
    assert -1 not in positions, dict(zip(order, positions))
    assert positions == sorted(positions)
    assert '--expected-tests "$MERVIO_EXPECTED_TESTS"' in joined


def test_the_test_command_runs_the_whole_suite():
    [pytest_command] = [c for c in run_commands(job("test")) if "-m pytest" in c]
    arguments = shlex.split(pytest_command)
    assert "--junitxml=reports/junit.xml" in arguments
    for option in ("-k", "-m", "--deselect", "--ignore", "--ignore-glob", "-x", "--exitfirst", "--lf", "--last-failed",
                   "--sw", "--stepwise", "--maxfail", "--co", "--collect-only", "--runxfail"):
        assert not any(a == option or a.startswith(option + "=") for a in arguments[3:]), option
    assert not any(a.startswith("tests/") for a in arguments), "la CI execute testpaths, pas un sous-ensemble"


def test_the_pytest_configuration_does_not_narrow_the_suite():
    options = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    assert options["testpaths"] == ["tests"]
    addopts = shlex.split(options.get("addopts", ""))
    assert not any(a.startswith(("-k", "-m", "--deselect", "--ignore", "-p")) for a in addopts), addopts
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert 'pytest_plugins = ("pytest_guard",)' in conftest
    assert "collect_ignore" not in conftest


def test_the_security_job_scans_history_dependencies_and_verifies_its_tool():
    block = job("security")
    assert "fetch-depth: 0" in block
    assert re.search(r'GITLEAKS_VERSION: "\d+\.\d+\.\d+"', block)
    assert re.search(r"GITLEAKS_SHA256: [0-9a-f]{64}\n", block + "\n")
    assert "sha256sum --check --strict" in block
    assert "set -euo pipefail" in block
    assert "--exit-code 1" in block
    assert "pip-audit --strict --no-deps --disable-pip" in block and "-r requirements.lock" in block


def test_the_lint_job_checks_syntax_without_reformatting():
    commands = "\n".join(run_commands(job("lint")))
    assert "compileall -q src tests scripts benchmarks" in commands
    assert "ruff check --no-cache --select E9,F63,F7,F82 src tests scripts benchmarks" in commands
    assert "format" not in commands and "--fix" not in commands


def test_the_ci_tools_are_pinned_exactly():
    for line in (ROOT / "requirements-ci.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", line), line


def test_the_python_version_is_exact():
    match = re.search(r'PYTHON_VERSION: "(\d+)\.(\d+)\.(\d+)"', CODE)
    assert match and match.group(1, 2) == ("3", "11")


def test_the_expected_test_count_matches_the_collected_suite(request):
    """Nombre EXACT, jamais un plancher. Verifie localement des qu'on lance la suite entiere."""
    expected = expected_tests()
    assert expected >= 1097
    config = request.config
    whole_suite = (config.args_source == pytest.Config.ArgsSource.TESTPATHS
                   and not config.option.keyword and not config.option.markexpr
                   and not config.option.deselect
                   # options du plugin de cache, absent sous `-p no:cacheprovider` (CI)
                   and not getattr(config.option, "lf", False) and not getattr(config.option, "failedfirst", False)
                   and not getattr(config.option, "newfirst", False)
                   and not getattr(config.option, "stepwise", False))
    if whole_suite:
        assert len(request.session.items) == expected, (
            f"{len(request.session.items)} tests collectes: mettre MERVIO_EXPECTED_TESTS a jour dans ci.yml")


def test_the_secret_scan_keeps_every_default_rule_with_one_narrow_exception():
    """Une seule exception: une valeur factice exacte ET un chemin de test (condition AND)."""
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert config["extend"] == {"useDefault": True}
    assert "rules" not in config
    [allowlist] = config["allowlists"]
    assert allowlist["condition"] == "AND"
    assert allowlist["regexTarget"] == "secret"
    assert allowlist["paths"] == [r"^tests/[A-Za-z0-9_]+\.py$"]
    assert allowlist["regexes"] == [r"^sk_live_abcd1234efgh$"]
    assert set(allowlist) == {"description", "condition", "regexTarget", "paths", "regexes"}
    assert "--config .gitleaks.toml" in job("security")


def test_the_security_job_also_audits_the_image_build_tool():
    assert "pip-audit --strict --no-deps --disable-pip --progress-spinner off -r requirements-build.lock" in "\n".join(
        run_commands(job("security")))


def test_the_docker_job_builds_then_runs_the_real_smoke_test_then_checks_cleanup():
    block = job("docker")
    assert re.search(r"^    needs: lint$", block, flags=re.M)
    commands = run_commands(block)
    assert commands == [
        "docker version && docker compose version",
        "docker build --tag mervio:ci .",
        "python scripts/container_smoke.py --image mervio:ci --skip-build",
        'test -z "$(docker ps --all --quiet --filter label=com.docker.compose.project)'
        '$(docker volume ls --quiet --filter label=com.docker.compose.project)'
        '$(docker network ls --quiet --filter label=com.docker.compose.project)"',
    ]
    assert "--keep" not in block, "la pile est toujours supprimee"
    assert "services:" not in block, "PostgreSQL vient du compose teste, pas d'un service du runner"


def test_the_docker_job_publishes_nothing_and_uses_no_credential():
    block = job("docker")
    for forbidden in ("docker login", "docker push", "registry", "secrets.", "GITHUB_TOKEN", "--privileged",
                      "PASSWORD", "packages: write", "id-token"):
        assert forbidden not in block, forbidden
    assert "persist-credentials: false" in block
