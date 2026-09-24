"""Artefacts conteneur (Mission 004.3.8): lecture statique, sans Docker ni base.

Ces tests ne prouvent pas que l'image se construit ni que la pile demarre (job `docker` de la CI,
scripts/container_smoke.py): seulement que les fichiers versionnes respectent les regles de securite
et de reproductibilite, et restent coherents entre eux (Dockerfile, compose, bootstrap, smoke, CI).
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from test_requirements_lock import applicable, lock_entries, pinned_version

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "docker-compose.yml"
BOOTSTRAP = ROOT / "docker" / "postgres" / "10-mervio-bootstrap.sql"
RUNTIME_LOCK = ROOT / "requirements-runtime.lock"
BUILD_LOCK = ROOT / "requirements-build.lock"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SMOKE = ROOT / "scripts" / "container_smoke.py"

CREDENTIAL_URL = re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:/@\s]+:(?!\$\{)[^@\s]+@")
PASSWORD_VARIABLES = ("MERVIO_POSTGRES_PASSWORD", "MERVIO_MIGRATOR_DB_PASSWORD", "MERVIO_APP_DB_PASSWORD",
                      "MERVIO_WORKER_DB_PASSWORD")
OUT_OF_SCOPE = ("redis", "kafka", "rabbitmq", "celery", "nginx", "traefik", "caddy", "minio", "elasticsearch",
                "memcached", "zookeeper", "kubernetes", "helm", "terraform", "supervisord", "systemd")


def lines_of(path: Path) -> list:
    return [line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()]


# =============================================================================================
# Dockerfile
# =============================================================================================

def dockerfile_instructions() -> list:
    """(INSTRUCTION, arguments) avec continuations de ligne jointes, commentaires retires."""
    instructions, current = [], ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#"):
            continue
        current += " " + stripped.rstrip("\\").strip() if current else stripped.rstrip("\\").strip()
        if not stripped.endswith("\\"):
            keyword, _, arguments = current.partition(" ")
            instructions.append((keyword.upper(), arguments.strip()))
            current = ""
    assert not current, "continuation de ligne non terminee"
    return instructions


INSTRUCTIONS = dockerfile_instructions()


def stages() -> list:
    """Liste de (nom, instructions) par etape FROM."""
    result = []
    for keyword, arguments in INSTRUCTIONS:
        if keyword == "FROM":
            result.append((arguments.split(" AS ")[-1].strip() if " AS " in arguments else "", []))
        elif result:
            result[-1][1].append((keyword, arguments))
    return result


def final_stage() -> list:
    return stages()[-1][1]


def base_image() -> str:
    [argument] = [a for k, a in INSTRUCTIONS if k == "ARG" and a.startswith("PYTHON_IMAGE=")]
    return argument.split("=", 1)[1]


def ci_python_version() -> str:
    return re.search(r'PYTHON_VERSION:\s*"([\d.]+)"', WORKFLOW.read_text(encoding="utf-8")).group(1)


def test_the_dockerfile_is_a_two_stage_build_from_one_pinned_python_image():
    froms = [a for k, a in INSTRUCTIONS if k == "FROM"]
    assert froms == ["${PYTHON_IMAGE} AS build", "${PYTHON_IMAGE} AS runtime"]
    image = base_image()
    match = re.fullmatch(r"python:(\d+\.\d+\.\d+)-slim-bookworm@sha256:[0-9a-f]{64}", image)
    assert match, image
    assert match.group(1) == ci_python_version(), "meme Python que la CI"
    assert "latest" not in DOCKERFILE.read_text(encoding="utf-8")
    assert INSTRUCTIONS[0][0] == "ARG", "l'image de base est declaree avant tout FROM"


def test_the_runtime_stage_runs_as_a_fixed_non_root_user():
    final = final_stage()
    users = [a for k, a in final if k == "USER"]
    assert users == ["10001:10001"]
    # l'utilisateur est cree avec ces identifiants, sans shell de connexion
    [create] = [a for k, a in final if k == "RUN" and "useradd" in a]
    assert "--uid 10001" in create and "--gid 10001" in create and "groupadd --system --gid 10001" in create
    assert "--shell /usr/sbin/nologin" in create
    # rien ne s'execute en root apres le passage a l'utilisateur
    after = final[[k for k, _ in final].index("USER"):]
    assert not any(k in ("RUN", "COPY", "ADD") for k, _ in after)
    assert not any(k == "USER" and a.split(":")[0] in ("0", "root") for k, a in INSTRUCTIONS)


def test_the_entrypoint_is_the_existing_cli_in_exec_form():
    final = dict((k, a) for k, a in final_stage() if k in ("ENTRYPOINT", "CMD", "STOPSIGNAL"))
    assert json.loads(final["ENTRYPOINT"]) == ["mervio"]
    assert json.loads(final["CMD"]) == ["worker"]
    assert final["STOPSIGNAL"] == "SIGTERM"
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {"mervio": "mervio.cli:main"}


def test_the_healthcheck_reuses_the_worker_health_command():
    [healthcheck] = [a for k, a in final_stage() if k == "HEALTHCHECK"]
    options, _, command = healthcheck.partition(" CMD ")
    assert json.loads(command) == ["mervio", "worker", "healthcheck"]
    assert re.search(r"--start-period=\d+s", options) and re.search(r"--interval=\d+s", options)
    text = DOCKERFILE.read_text(encoding="utf-8").lower()
    for forbidden in ("curl", "wget", "http://", "https://", "nc -z"):
        assert forbidden not in text, forbidden
    env = " ".join(a for k, a in final_stage() if k == "ENV")
    assert "MERVIO_WORKER_HEALTH_FILE=/run/mervio/worker-health.json" in env


def test_no_port_no_add_and_no_system_package_is_installed():
    keywords = {k for k, _ in INSTRUCTIONS}
    assert "EXPOSE" not in keywords, "le worker n'est pas un serveur HTTP"
    assert "ADD" not in keywords, "COPY seulement (pas de telechargement ni d'archive implicite)"
    runs = " ".join(a for k, a in INSTRUCTIONS if k == "RUN")
    for forbidden in ("apt-get", "apt ", "apk ", "curl", "wget", "sudo", "chmod 777", "chmod -R 777"):
        assert forbidden not in runs, forbidden


def test_dependencies_come_only_from_the_lock_files_as_wheels():
    runs = " ".join(a for k, a in INSTRUCTIONS if k == "RUN")
    installs = [part for k, a in INSTRUCTIONS if k == "RUN" for part in a.split("&&")]
    pip_commands = [c.strip() for c in installs if re.search(r"\bpip\b.*\b(install|wheel)\b", c)]
    assert len(pip_commands) == 4, pip_commands
    for command in pip_commands:
        assert "--no-deps" in command, command
        assert "--trusted-host" not in command and "--index-url" not in command and "--extra-index" not in command
        if " install " in f" {command} ":
            assert re.search(r"-r requirements-(runtime|build)\.lock", command) or (
                "--no-index" in command and "/build/dist/mervio-" in command), command
        if "-r requirements-" in command:
            assert "--only-binary=:all:" in command, command
    assert "--no-build-isolation" in runs, "setuptools vient de requirements-build.lock, pas d'un telechargement"
    assert "pip --python /opt/venv/bin/python check" in runs
    assert "python -m venv --without-pip /opt/venv" in runs


def test_only_the_package_sources_are_copied():
    copies = [a for k, a in INSTRUCTIONS if k == "COPY"]
    assert copies == ["requirements-build.lock requirements-runtime.lock ./", "pyproject.toml ./", "src ./src",
                      "--from=build /opt/venv /opt/venv"]
    # l'etape finale ne recoit que l'environnement construit
    assert [a for k, a in final_stage() if k == "COPY"] == ["--from=build /opt/venv /opt/venv"]


def test_the_image_embeds_no_secret_and_no_host_path():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert not CREDENTIAL_URL.search(text)
    assert "postgresql://" not in text and "postgres://" not in text
    assert "/Users/" not in text and "/home/" not in text
    for keyword, arguments in INSTRUCTIONS:
        if keyword in ("ENV", "ARG", "LABEL"):
            upper = arguments.upper()
            for fragment in ("PASSWORD", "SECRET", "TOKEN", "DATABASE_URL", "API_KEY", "PRIVATE"):
                assert fragment not in upper, (keyword, arguments)
    assert "--mount=type=secret" not in text and ".ssh" not in text and ".env" not in text


def test_the_runtime_environment_is_reproducible_and_unbuffered():
    env = " ".join(a for k, a in final_stage() if k == "ENV")
    for setting in ("PATH=/opt/venv/bin:$PATH", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1"):
        assert setting in env, setting


# =============================================================================================
# Locks de l'image
# =============================================================================================

def runtime_entries() -> dict:
    return {canonicalize_name(Requirement(line).name): Requirement(line) for line in lines_of(RUNTIME_LOCK)}


def test_the_runtime_lock_is_a_strict_subset_of_the_project_lock():
    project_lines = set(lines_of(ROOT / "requirements.lock"))
    runtime_lines = lines_of(RUNTIME_LOCK)
    assert runtime_lines and len(runtime_lines) == len(set(runtime_lines))
    for line in runtime_lines:
        assert line in project_lines, f"{line} differe de requirements.lock"
    assert len(runtime_lines) < len(project_lines)


def test_the_runtime_lock_contains_no_test_tooling():
    assert not {"pytest", "iniconfig", "pluggy", "pygments", "packaging", "pip", "setuptools", "wheel"} & set(
        runtime_entries())


def test_the_runtime_lock_is_exactly_the_closure_of_the_persistence_extra():
    from importlib import metadata
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == [], "le moteur n'a aucune dependance: seul l'extra persistence est installe"
    entries = runtime_entries()
    locked = lock_entries()
    reached = set()
    pending = [(Requirement(text), ("",)) for text in project["optional-dependencies"]["persistence"]]
    while pending:
        requirement, parent_extras = pending.pop()
        if not applicable(requirement, parent_extras):
            continue
        name = canonicalize_name(requirement.name)
        assert name in entries, f"dependance d'execution absente de l'image: {requirement}"
        assert requirement.specifier.contains(pinned_version(entries[name]), prereleases=True), requirement
        extras = tuple(sorted(requirement.extras)) or ("",)
        if (name, extras) in reached:
            continue
        reached.add((name, extras))
        for child in metadata.distribution(name).requires or ():
            pending.append((Requirement(child), extras))
    used = {name for name, _ in reached}
    expected = {name for name, requirement in entries.items() if applicable(requirement)}
    assert used == expected, f"lignes inutiles dans l'image: {sorted(expected - used)}"
    for name in entries:
        assert str(entries[name]) == str(locked[name])


def test_the_build_lock_pins_only_the_declared_build_backend():
    [line] = lines_of(BUILD_LOCK)
    requirement = Requirement(line)
    assert canonicalize_name(requirement.name) == "setuptools"
    version = pinned_version(requirement)
    build = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]
    assert build["build-backend"] == "setuptools.build_meta"
    [declared] = [Requirement(r) for r in build["requires"]]
    assert canonicalize_name(declared.name) == "setuptools"
    assert declared.specifier.contains(version)
    assert "setuptools" not in runtime_entries()


# =============================================================================================
# .dockerignore
# =============================================================================================

def ignore_patterns() -> list:
    return lines_of(DOCKERIGNORE)


def excluded(relative: str, patterns=None) -> bool:
    """Semantique de Docker: le dernier motif correspondant l'emporte; un repertoire exclu exclut son contenu."""
    patterns = ignore_patterns() if patterns is None else patterns
    parts = relative.split("/")
    prefixes = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
    decision = False
    for raw in patterns:
        negated = raw.startswith("!")
        pattern = raw[1:] if negated else raw
        pattern = pattern.strip("/")
        regex = "^" + re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(
            r"\*", "[^/]*").replace(r"\?", "[^/]") + "$"
        regex = regex.replace(r"\[", "[").replace(r"\]", "]")
        if any(re.match(regex, prefix) for prefix in prefixes):
            decision = not negated
    return decision


def repository_files() -> list:
    files = []
    for path in ROOT.rglob("*"):
        if path.is_file():
            files.append(path.relative_to(ROOT).as_posix())
    return files


def test_the_build_context_is_an_allow_list():
    patterns = ignore_patterns()
    assert patterns[0] == "*"
    assert sorted(p for p in patterns if p.startswith("!")) == sorted(
        ["!pyproject.toml", "!requirements-runtime.lock", "!requirements-build.lock", "!src/"])


def test_the_build_context_contains_only_the_package_sources():
    context = [path for path in repository_files() if not excluded(path)]
    assert "pyproject.toml" in context and "requirements-runtime.lock" in context
    assert "src/mervio/cli/main.py" in context
    assert any(path.startswith("src/mervio/persistence/migrations/versions/") for path in context)
    for path in context:
        assert path in ("pyproject.toml", "requirements-runtime.lock", "requirements-build.lock") or (
            path.startswith("src/") and "__pycache__" not in path and not path.endswith(".pyc")), path


@pytest.mark.parametrize("path", [
    ".git/config", ".git/HEAD", ".venv/bin/python", ".env", ".env.production", "src/.env", "src/mervio/.env.local",
    "secrets/db.txt", "src/secrets/token", "server.pem", "src/mervio/private.key", "credentials-prod.json",
    "src/mervio/__pycache__/cli.cpython-311.pyc", "src/mervio/cli.pyc", ".pytest_cache/v/x", ".mypy_cache/x",
    "node_modules/x/index.js", "src/node_modules/x.js", ".coverage", "coverage.xml", "htmlcov/index.html",
    "local.sqlite3", "src/mervio/state.db", ".pgdata/PG_VERSION", "data/uploads/customer.csv",
    "analysis/latest/report.json", "reports/junit.xml", "tests/conftest.py", "scripts/container_smoke.py",
    "data/sample/shopify_orders.csv", "docs/CONTAINER.md", ".idea/workspace.xml", ".vscode/settings.json",
    "docker-compose.yml", ".dockerignore", "docker/postgres/10-mervio-bootstrap.sql", "src/mervio.egg-info/PKG-INFO",
    ".DS_Store", "src/.DS_Store", "requirements.lock", "requirements-ci.txt",
])
def test_sensitive_and_useless_files_never_enter_the_build_context(path):
    assert excluded(path), path


def test_every_file_copied_by_the_dockerfile_is_in_the_context():
    for source in ("requirements-build.lock", "requirements-runtime.lock", "pyproject.toml",
                   "src/mervio/__init__.py"):
        assert (ROOT / source).exists() and not excluded(source), source


def test_the_ignore_matcher_follows_docker_semantics():
    patterns = ["*", "!src/", "**/__pycache__"]
    assert excluded("a.txt", patterns) and not excluded("src/a.py", patterns)
    assert excluded("src/pkg/__pycache__/a.pyc", patterns)
    assert not excluded("a.txt", ["*", "!a.txt"]) and excluded("a.txt", ["!a.txt", "*"])
    assert excluded("x/.env.prod", ["**/.env.*"]) and not excluded("x/env.prod", ["**/.env.*"])


# =============================================================================================
# docker-compose.yml (sous-ensemble YAML de ce fichier: mappings, listes, scalaires)
# =============================================================================================

def _scalar(text: str):
    text = text.strip()
    if text.startswith("[") or text == "{}":
        return json.loads(text)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _strip_comment(line: str) -> str:
    quote = None
    for index, char in enumerate(line):
        if char in "\"'" and quote in (None, char):
            quote = None if quote else char
        elif char == "#" and quote is None and (index == 0 or line[index - 1] == " "):
            return line[:index].rstrip()
    return line.rstrip()


def load_compose(text: str) -> dict:
    rows = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if line.strip():
            rows.append((len(line) - len(line.lstrip(" ")), line.strip()))

    def block(index: int, indent: int):
        if rows[index][1].startswith("- "):
            items = []
            while index < len(rows) and rows[index][0] == indent and rows[index][1].startswith("- "):
                items.append(_scalar(rows[index][1][2:]))
                index += 1
            return items, index
        mapping = {}
        while index < len(rows) and rows[index][0] == indent:
            key, _, value = rows[index][1].partition(":")
            assert key and not key.startswith("- "), rows[index]
            index += 1
            if value.strip():
                mapping[key.strip()] = _scalar(value)
            elif index < len(rows) and rows[index][0] > indent:
                mapping[key.strip()], index = block(index, rows[index][0])
            elif index < len(rows) and rows[index][0] == indent and rows[index][1].startswith("- "):
                mapping[key.strip()], index = block(index, indent)
            else:
                mapping[key.strip()] = None
        return mapping, index

    document, end = block(0, 0)
    assert end == len(rows), f"ligne non analysee: {rows[end]}"
    return document


COMPOSE_TEXT = COMPOSE.read_text(encoding="utf-8")
COMPOSE_DOC = load_compose(COMPOSE_TEXT)
SERVICES = COMPOSE_DOC["services"]


def test_the_compose_parser_reads_this_file():
    assert COMPOSE_DOC["name"] == "mervio"
    assert load_compose("a:\n  b: [\"x\", \"y\"]\n  c:\n    - \"1\"\n  d: true # note\n") == {
        "a": {"b": ["x", "y"], "c": ["1"], "d": True}}


def test_the_stack_is_postgres_and_mervio_only():
    assert set(SERVICES) == {"postgres", "worker", "migrate", "admin"}
    lowered = COMPOSE_TEXT.lower()
    for name in OUT_OF_SCOPE:
        assert f"image: {name}" not in lowered and f"\n  {name}:" not in lowered, name
    for service in SERVICES.values():
        assert "privileged" not in service and service.get("network_mode") is None
        assert "pid" not in service and "ipc" not in service
        for volume in service.get("volumes", []):
            assert "docker.sock" not in volume and not volume.startswith("/"), volume
    assert set(COMPOSE_DOC) == {"name", "services", "networks", "volumes"}


def test_postgres_is_the_pinned_postgresql_17_of_the_ci():
    image = SERVICES["postgres"]["image"]
    assert re.fullmatch(r"postgres:17\.\d+-bookworm@sha256:[0-9a-f]{64}", image), image
    assert f"image: {image}" in WORKFLOW.read_text(encoding="utf-8"), "meme image que le job test"
    environment = SERVICES["postgres"]["environment"]
    assert environment["POSTGRES_INITDB_ARGS"] == "--encoding=UTF8 --locale=C"
    assert environment["POSTGRES_PASSWORD"].startswith("${MERVIO_POSTGRES_PASSWORD:?")
    assert "POSTGRES_HOST_AUTH_METHOD" not in environment, "jamais trust sur le reseau"
    assert "./docker/postgres/10-mervio-bootstrap.sql:/docker-entrypoint-initdb.d/10-mervio-bootstrap.sql:ro" in (
        SERVICES["postgres"]["volumes"])
    assert SERVICES["postgres"]["ports"] == ["127.0.0.1:${MERVIO_POSTGRES_PORT:-55432}:5432"]
    test = SERVICES["postgres"]["healthcheck"]["test"]
    assert test[:2] == ["CMD", "pg_isready"] and "127.0.0.1" in test, "TCP: jamais pret pendant le bootstrap"


def test_mervio_services_use_one_image_and_never_latest():
    assert "latest" not in COMPOSE_TEXT
    for name in ("worker", "migrate", "admin"):
        assert SERVICES[name]["image"] == "${MERVIO_IMAGE:-mervio:local}", name
    assert SERVICES["worker"]["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    assert SERVICES["migrate"]["pull_policy"] == SERVICES["admin"]["pull_policy"] == "never"


def test_no_secret_is_written_in_the_compose_file():
    assert not CREDENTIAL_URL.search(COMPOSE_TEXT)
    urls = re.findall(r"postgresql://(\w+):\$\{(MERVIO_\w+_DB_PASSWORD):\?[^}]*\}@postgres:5432/mervio(?=\s|$)",
                      COMPOSE_TEXT, flags=re.M)
    assert len(urls) == COMPOSE_TEXT.count("postgresql://") == 3
    assert sorted(urls) == [("mervio_app_user", "MERVIO_APP_DB_PASSWORD"),
                            ("mervio_migrator", "MERVIO_MIGRATOR_DB_PASSWORD"),
                            ("mervio_worker_svc", "MERVIO_WORKER_DB_PASSWORD")]
    for variable in PASSWORD_VARIABLES:
        occurrences = re.findall(r"\$\{" + variable + r"([^}]*)\}", COMPOSE_TEXT)
        assert occurrences, variable
        assert all(o.startswith(":?") for o in occurrences), f"{variable} doit etre obligatoire partout"
    for key, value in re.findall(r"^\s*(\w*(?:PASSWORD|SECRET|TOKEN)\w*):\s*(\S+)", COMPOSE_TEXT, flags=re.M):
        assert value.startswith("${MERVIO_") and ":?" in value, key
    assert "env_file" not in COMPOSE_TEXT


def test_the_worker_is_hardened_and_healthchecked():
    worker = SERVICES["worker"]
    assert worker["healthcheck"]["test"] == ["CMD", "mervio", "worker", "healthcheck"]
    assert worker["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert worker["read_only"] is True and worker["init"] is True
    assert worker["cap_drop"] == ["ALL"] and worker["security_opt"] == ["no-new-privileges:true"]
    assert "ports" not in worker and "expose" not in worker
    assert worker["networks"] == ["backend"]
    assert worker["tmpfs"] == ["/run/mervio:uid=10001,gid=10001,mode=0700", "/tmp"]
    assert worker["volumes"] == ["mervio_data:/var/lib/mervio/data:ro",
                                 "mervio_objects:/var/lib/mervio/objects:ro"]
    assert "entrypoint" not in worker and "command" not in worker, "l'image lance `mervio worker`"
    assert "user" not in worker, "l'utilisateur non-root de l'image n'est jamais remplace"
    grace = int(worker["stop_grace_period"].rstrip("s"))
    assert grace > 30, "au-dela de MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS par defaut"
    environment = worker["environment"]
    assert environment["MERVIO_DATABASE_URL"].startswith(
        "postgresql://mervio_worker_svc:${MERVIO_WORKER_DB_PASSWORD:?")
    assert environment["MERVIO_DATABASE_URL"].endswith("@postgres:5432/mervio")
    assert environment["MERVIO_WORKER_HEALTH_FILE"] == "/run/mervio/worker-health.json"
    assert environment["MERVIO_LOG_FORMAT"] == "json"


def test_migrations_are_an_explicit_step_with_the_schema_owner():
    migrate = SERVICES["migrate"]
    assert migrate["profiles"] == ["ops"]
    assert migrate["entrypoint"] == ["python", "-m", "mervio.persistence"]
    assert migrate["command"] == ["upgrade"]
    assert migrate["healthcheck"] == {"disable": True}
    assert set(migrate["environment"]) == {"MERVIO_MIGRATION_DATABASE_URL"}
    assert migrate["environment"]["MERVIO_MIGRATION_DATABASE_URL"].startswith("postgresql://mervio_migrator:")
    # personne d'autre ne migre: ni le worker, ni l'admin, ni une dependance de demarrage
    for name in ("worker", "admin", "postgres"):
        service = SERVICES[name]
        assert "migrate" not in (service.get("depends_on") or {}), name
        assert "MERVIO_MIGRATION_DATABASE_URL" not in (service.get("environment") or {}), name
        assert "upgrade" not in json.dumps(service.get("command", "")), name


def test_admin_uses_the_application_role_and_the_shared_data_volume():
    admin = SERVICES["admin"]
    assert admin["profiles"] == ["ops"]
    assert admin["entrypoint"] == ["mervio", "admin"]
    assert admin["healthcheck"] == {"disable": True}
    assert admin["environment"]["MERVIO_DATABASE_URL"].startswith("postgresql://mervio_app_user:")
    assert admin["volumes"] == ["mervio_data:/var/lib/mervio/data",
                                "mervio_objects:/var/lib/mervio/objects"]
    for name in ("migrate", "admin"):
        service = SERVICES[name]
        assert service["read_only"] is True and service["cap_drop"] == ["ALL"], name
        assert service["networks"] == ["backend"] and "ports" not in service, name


def test_no_mervio_service_connects_as_the_superuser():
    for name in ("worker", "migrate", "admin"):
        for value in (SERVICES[name].get("environment") or {}).values():
            role = re.match(r"postgresql://(\w+)", str(value))
            assert role is None or role.group(1) not in ("mervio_admin", "postgres"), name
            assert "mervio_admin" not in str(value), name


def test_the_backend_network_is_internal():
    assert COMPOSE_DOC["networks"] == {"backend": {"internal": True}, "edge": {}}
    assert SERVICES["postgres"]["networks"] == ["backend", "edge"]
    assert set(COMPOSE_DOC["volumes"]) == {"mervio_pgdata", "mervio_data", "mervio_objects"}


def test_compose_bootstrap_and_smoke_agree_on_names():
    environment = SERVICES["postgres"]["environment"]
    smoke = SMOKE.read_text(encoding="utf-8")
    expected = {"MERVIO_BOOTSTRAP_DATABASE": ("mervio", "DATABASE"),
                "MERVIO_BOOTSTRAP_MIGRATOR_ROLE": ("mervio_migrator", "MIGRATOR_ROLE"),
                "MERVIO_BOOTSTRAP_APP_ROLE": ("mervio_app_user", "APP_ROLE"),
                "MERVIO_BOOTSTRAP_WORKER_ROLE": ("mervio_worker_svc", "WORKER_ROLE")}
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    for variable, (value, constant) in expected.items():
        assert environment[variable] == value
        assert f'{constant} = "{value}"' in smoke, constant
        assert f"\\getenv {variable.split('MERVIO_BOOTSTRAP_')[1].lower()} {variable}" in bootstrap, variable
    assert environment["POSTGRES_USER"] == "mervio_admin" and 'SUPERUSER = "mervio_admin"' in smoke
    for variable in PASSWORD_VARIABLES:
        assert f'"{variable}"' in smoke


def test_the_env_example_documents_every_required_variable_without_value():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for variable in PASSWORD_VARIABLES:
        assert f"{variable}=" in lines, variable


# =============================================================================================
# Bootstrap PostgreSQL
# =============================================================================================

BOOTSTRAP_TEXT = BOOTSTRAP.read_text(encoding="utf-8")
BOOTSTRAP_CODE = "\n".join(line for line in BOOTSTRAP_TEXT.splitlines() if not line.lstrip().startswith("--"))


def test_the_bootstrap_stops_on_the_first_error_and_hides_its_statements():
    assert "\\set ON_ERROR_STOP on" in BOOTSTRAP_CODE
    assert "SET log_min_error_statement = 'panic';" in BOOTSTRAP_CODE
    assert "SET log_statement = 'none';" in BOOTSTRAP_CODE


def test_the_bootstrap_contains_no_secret_and_reads_everything_from_the_environment():
    assert not re.search(r"PASSWORD\s+'", BOOTSTRAP_CODE, flags=re.I), "aucun mot de passe litteral"
    assert "PASSWORD %L" in BOOTSTRAP_CODE, "mot de passe passe en litteral echappe par format()"
    for variable in ("DATABASE", "MIGRATOR_ROLE", "MIGRATOR_PASSWORD", "APP_ROLE", "APP_PASSWORD", "WORKER_ROLE",
                     "WORKER_PASSWORD"):
        assert f"MERVIO_BOOTSTRAP_{variable}" in BOOTSTRAP_CODE, variable
    assert "\\echo :" not in BOOTSTRAP_CODE and "ECHO" not in BOOTSTRAP_CODE


def test_the_bootstrap_grants_no_privilege_and_creates_no_schema():
    upper = BOOTSTRAP_CODE.upper()
    statements = re.findall(r"CREATE ROLE [^;\\]*", upper)
    assert len(statements) == 3, statements
    for statement in statements:
        tokens = set(re.findall(r"[A-Z_%]+", statement))
        assert not tokens & {"SUPERUSER", "BYPASSRLS", "CREATEROLE", "CREATEDB", "REPLICATION", "INHERIT"}, statement
        assert {"NOSUPERUSER", "NOBYPASSRLS", "NOCREATEROLE", "NOCREATEDB"} <= tokens, statement
    assert "ALTER ROLE" not in upper and "WITH ADMIN" not in upper and "WITH GRANT" not in upper
    for forbidden in ("CREATE TABLE", "CREATE POLICY", "ALTER TABLE", "CREATE FUNCTION", "ALTER SYSTEM",
                      "DISABLE ROW LEVEL", "PG_HBA", "TRUST", "GRANT ALL", "CREATE EXTENSION", "INSERT INTO"):
        assert forbidden not in upper, forbidden
    assert "REVOKE ALL ON DATABASE %I FROM PUBLIC" in BOOTSTRAP_CODE
    assert "GRANT CONNECT ON DATABASE %I TO %I, %I" in BOOTSTRAP_CODE


def test_the_bootstrap_group_roles_are_exactly_those_of_the_migrations():
    versions = ROOT / "src" / "mervio" / "persistence" / "migrations" / "versions"
    tenancy = (versions / "0001_tenancy.py").read_text(encoding="utf-8")
    identity = (versions / "0007_service_identity.py").read_text(encoding="utf-8").replace(
        "{WORKER_ROLE}", "mervio_worker")
    for group, migration in (("mervio_app", tenancy), ("mervio_worker", identity)):
        statement = f"CREATE ROLE {group} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;"
        assert statement in BOOTSTRAP_CODE, group
        assert statement in migration, group



# =============================================================================================
# tmpfs et proprietaire (echec CI de ec7ce24: worker unhealthy)
# =============================================================================================
#
# runc (1.1 a 1.3) monte un tmpfs sur un repertoire existant avec `mode=<mode de ce repertoire>`
# place AVANT les options fournies, mais sans son proprietaire: le tmpfs appartient a root.
# /run/mervio (0750, 10001 dans l'image) devenait donc root:root 0750: le worker ne pouvait plus
# ecrire son fichier de sante, `healthcheck` repondait `missing`, le conteneur etait unhealthy.

def image_directories() -> dict:
    """Repertoires crees par le Dockerfile: chemin -> (uid, gid, mode)."""
    directories = {}
    for keyword, arguments in final_stage():
        if keyword != "RUN":
            continue
        for command in arguments.split("&&"):
            tokens = command.split()
            if tokens[:2] != ["install", "-d"]:
                continue
            options = dict(zip(tokens[2::2], tokens[3::2]))
            paths = [t for t in tokens[2:] if t.startswith("/")]
            for path in paths:
                directories[path] = (options["-o"], options["-g"], options["-m"])
    return directories


def tmpfs_mount(entry: str):
    path, _, raw = entry.partition(":")
    options = dict(item.partition("=")[::2] for item in raw.split(",") if item)
    return path, options


def runc_tmpfs_root(entry: str, image_dirs: dict):
    """(uid, gid, mode) de la racine du tmpfs, selon la regle de runc et du noyau (derniere valeur retenue)."""
    path, options = tmpfs_mount(entry)
    inherited = image_dirs.get(path, (None, None, "1777"))[2]
    return options.get("uid", "0"), options.get("gid", "0"), options.get("mode", inherited)


def writable_by(uid: str, gid: str, root) -> bool:
    owner, group, mode = root
    bits = int(mode, 8)
    if owner == uid:
        return bits & 0o300 == 0o300
    if group == gid:
        return bits & 0o030 == 0o030
    return bits & 0o003 == 0o003


def test_the_image_creates_the_health_directory_for_its_user():
    assert image_directories()["/run/mervio"] == ("10001", "10001", "0750")


def test_the_rule_reproduces_the_ci_failure():
    image_dirs = image_directories()
    assert not writable_by("10001", "10001", runc_tmpfs_root("/run/mervio", image_dirs)), "ec7ce24"
    assert writable_by("10001", "10001", runc_tmpfs_root("/tmp", image_dirs))
    assert writable_by("10001", "10001", runc_tmpfs_root("/run/mervio:uid=10001,gid=10001,mode=0700", image_dirs))
    assert not writable_by("10001", "10001", runc_tmpfs_root("/run/mervio:mode=0700", image_dirs))


def test_every_tmpfs_of_a_mervio_service_is_writable_by_the_image_user():
    image_dirs = image_directories()
    for name in ("worker", "migrate", "admin"):
        for entry in SERVICES[name].get("tmpfs", []):
            assert writable_by("10001", "10001", runc_tmpfs_root(entry, image_dirs)), (name, entry)
            path, options = tmpfs_mount(entry)
            assert set(options) <= {"uid", "gid", "mode", "size"}, entry
            if path in image_dirs:
                assert (options.get("uid"), options.get("gid")) == ("10001", "10001"), entry


def test_the_health_file_lives_on_the_worker_tmpfs_and_is_private():
    health = Path(SERVICES["worker"]["environment"]["MERVIO_WORKER_HEALTH_FILE"])
    [entry] = [e for e in SERVICES["worker"]["tmpfs"] if tmpfs_mount(e)[0] == str(health.parent)]
    assert tmpfs_mount(entry)[1] == {"uid": "10001", "gid": "10001", "mode": "0700"}
    env = " ".join(a for k, a in final_stage() if k == "ENV")
    assert f"MERVIO_WORKER_HEALTH_FILE={health}" in env


def test_the_smoke_negative_worker_uses_the_same_tmpfs_as_compose():
    smoke = SMOKE.read_text(encoding="utf-8")
    assert 'HEALTH_TMPFS = f"/run/mervio:uid={IMAGE_UID},gid={IMAGE_UID},mode=0700"' in smoke
    assert '"--tmpfs", HEALTH_TMPFS' in smoke and '"--tmpfs", "/run/mervio"' not in smoke


def test_each_role_uses_the_password_its_bootstrap_role_was_created_with():
    environment = SERVICES["postgres"]["environment"]
    created = {}
    for kind in ("MIGRATOR", "APP", "WORKER"):
        role = environment[f"MERVIO_BOOTSTRAP_{kind}_ROLE"]
        variable = re.fullmatch(r"\$\{(\w+):\?[^}]*\}", environment[f"MERVIO_BOOTSTRAP_{kind}_PASSWORD"]).group(1)
        created[role] = variable
    used = dict(re.findall(r"postgresql://(\w+):\$\{(\w+):\?", COMPOSE_TEXT))
    assert used == created
    assert len(set(created.values())) == 3 and "MERVIO_POSTGRES_PASSWORD" not in created.values()
    for service, variable_name in (("worker", "MERVIO_DATABASE_URL"), ("admin", "MERVIO_DATABASE_URL"),
                                   ("migrate", "MERVIO_MIGRATION_DATABASE_URL")):
        role = re.match(r"postgresql://(\w+):", SERVICES[service]["environment"][variable_name]).group(1)
        assert role == {"worker": "mervio_worker_svc", "admin": "mervio_app_user",
                        "migrate": "mervio_migrator"}[service]


def test_only_the_worker_receives_the_identity_master_key_and_never_a_value():
    """004.4.2 (D-053): la cle maitre d'identite est obligatoire pour le worker, fournie par .env, jamais ecrite."""
    worker = SERVICES["worker"]["environment"]["MERVIO_IDENTITY_MASTER_KEY"]
    assert worker.startswith("${MERVIO_IDENTITY_MASTER_KEY:?") and worker.endswith("}")
    for service in set(SERVICES) - {"worker"}:
        assert "MERVIO_IDENTITY_MASTER_KEY" not in (SERVICES[service].get("environment") or {}), service
    assert sum("MERVIO_IDENTITY_MASTER_KEY" in line for line in COMPOSE_TEXT.splitlines()) == 1  # le worker seul
    assert "MERVIO_IDENTITY_MASTER_KEY=" in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
