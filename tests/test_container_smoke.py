"""`scripts/container_smoke.py` ne peut ni mentir ni fuir (Mission 004.3.8). Sans Docker ni base.

Le parcours reel est execute par le job `docker` de la CI; ses verifications SQL et `mervio admin`
sont executees sur une vraie base par tests/persistence/test_container_bootstrap.py. Ici: les regles
du pilote lui-meme, avec un executeur de commandes factice.
"""
from __future__ import annotations

import importlib.util
import json
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("mervio_container_smoke", ROOT / "scripts" / "container_smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke_module = load()
SmokeFailure = smoke_module.SmokeFailure

GOOD_CONFIG = {"User": "10001:10001", "ExposedPorts": None, "Entrypoint": ["mervio"], "Cmd": ["worker"],
               "Healthcheck": {"Test": ["CMD", "mervio", "worker", "healthcheck"]},
               "Env": ["PATH=/opt/venv/bin:/usr/local/bin", "MERVIO_WORKER_HEALTH_FILE=/run/mervio/worker-health.json"]}
GOOD_FACTS = {"uid": 10001, "gid": 10001, "python": "3.11.16",
              "absent": ["pytest", "pip", "setuptools", "iniconfig", "pluggy", "pygments"], "paths": []}


class FakeDocker:
    """Executeur factice: reponses par motif, journal complet des appels (argv et environnement)."""

    def __init__(self, responses=(), *, missing=False):
        self.responses = list(responses)
        self.calls = []
        self.missing = missing

    def __call__(self, argv, **options):
        self.calls.append({"argv": list(argv), "env": options.get("env")})
        if self.missing:
            raise FileNotFoundError(argv[0])
        for predicate, code, stdout, stderr in self.responses:
            if predicate(argv):
                return subprocess.CompletedProcess(argv, code, stdout, stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def argvs(self):
        return [call["argv"] for call in self.calls]


def contains(*fragments):
    return lambda argv: all(fragment in argv for fragment in fragments)


def image_responses(config=None, facts=None):
    return [
        (contains("image", "inspect"), 0, json.dumps(GOOD_CONFIG if config is None else config), ""),
        (contains("--entrypoint", "/opt/venv/bin/python"), 0, json.dumps(GOOD_FACTS if facts is None else facts), ""),
        (contains("--version"), 0, "mervio 0.1.0\n", ""),
    ]


def make(runner, **options):
    options.setdefault("image", "mervio:test")
    options.setdefault("build", False)
    options.setdefault("keep", False)
    return smoke_module.Smoke(runner=runner, log=lambda _: None, port=55999, **options)


# -- fonctions pures ----------------------------------------------------------------------------

def test_generated_passwords_are_distinct_and_accepted_by_the_bootstrap():
    import re
    passwords = smoke_module.generate_passwords()
    assert set(passwords) == set(smoke_module.PASSWORD_VARIABLES)
    assert len(set(passwords.values())) == 4
    for value in passwords.values():
        assert re.fullmatch(r"[A-Za-z0-9._~-]{16,128}", value)
    assert smoke_module.generate_passwords() != passwords


def test_the_redactor_removes_every_secret_longest_first():
    redact = smoke_module.Redactor(["abc", "abcdef", ""])
    assert redact("x abcdef y abc z") == "x [redacted] y [redacted] z"


def test_leaks_are_reported_by_code_never_by_value():
    secret = secrets.token_hex(10)  # construit a l'execution: aucun faux jeton dans le depot
    leaks = smoke_module.find_leaks(f"log {secret} postgresql://role@host/db", ["", secret])
    assert leaks == ["password#1", "connection_url"]
    assert secret not in " ".join(leaks)
    assert smoke_module.find_leaks("POSTGRES://x", []) == ["connection_url"]
    assert smoke_module.find_leaks("clean", [secret]) == []


def test_the_compose_environment_never_inherits_host_mervio_or_compose_settings():
    base = {"PATH": "/bin", "MERVIO_DATABASE_URL": "postgresql://host", "COMPOSE_FILE": "other.yml",
            "COMPOSE_PROJECT_NAME": "prod", "HOME": "/root"}
    environment = smoke_module.compose_environment(base, {"MERVIO_APP_DB_PASSWORD": "p" * 20},
                                                   image="mervio:x", port=1234)
    assert environment == {"PATH": "/bin", "HOME": "/root", "MERVIO_APP_DB_PASSWORD": "p" * 20,
                           "MERVIO_IMAGE": "mervio:x", "MERVIO_POSTGRES_PORT": "1234", "MERVIO_ENV": "test",
                           "MERVIO_LOG_LEVEL": "INFO"}


@pytest.mark.parametrize("stdout", ["", "not json", "[1, 2]", '{"result": 1}'])
def test_an_unreadable_admin_output_is_a_failure(stdout):
    with pytest.raises(SmokeFailure):
        smoke_module.parse_admin(smoke_module.Result(["x"], 0, stdout, ""))


def test_the_admin_document_is_the_last_json_line():
    result = smoke_module.Result(["x"], 5, 'noise\n{"ok": false, "error": {"code": "not_found"}}\n\n', "")
    assert smoke_module.parse_admin(result)["error"]["code"] == "not_found"


def event(**fields):
    base = {"timestamp": "2026-09-17T10:00:00.000+00:00", "level": "INFO", "service": "worker",
            "event": "worker.ready"}
    base.update(fields)
    return base


def test_the_log_contract_accepts_the_product_format():
    lines = "\n".join(json.dumps(e) for e in [event(), event(event="worker.stopped", password="[redacted]")])
    events = smoke_module.parse_json_logs("warning: not json\n" + lines)
    smoke_module.check_log_contract(events)
    assert [e["event"] for e in events] == ["worker.ready", "worker.stopped"]


@pytest.mark.parametrize("bad", [
    event(timestamp="2026-09-17T10:00:00+02:00"),
    {"timestamp": "2026-09-17T10:00:00Z", "level": "INFO", "service": "worker"},
    event(database_url="postgresql://x"),
    event(api_token="abc"),
])
def test_the_log_contract_refuses_non_utc_incomplete_or_unmasked_lines(bad):
    with pytest.raises(SmokeFailure):
        smoke_module.check_log_contract([bad])


def test_empty_or_broken_json_logs_are_failures():
    with pytest.raises(SmokeFailure):
        smoke_module.check_log_contract([])
    with pytest.raises(SmokeFailure):
        smoke_module.parse_json_logs('{"broken": ')


def audit_trail(owner="o", principal="p"):
    trail = [{"action": action, "actor_type": "user", "actor_id": owner, "on_behalf_of": None}
             for action in smoke_module.EXPECTED_ADMIN_ACTIONS]
    trail += [{"action": action, "actor_type": "worker", "actor_id": principal, "on_behalf_of": owner}
              for action in smoke_module.EXPECTED_WORKER_ACTIONS]
    return trail


def test_a_complete_and_attributed_audit_trail_is_accepted():
    assert smoke_module.audit_problems(audit_trail(), owner_id="o", principal_id="p") == []


@pytest.mark.parametrize("mutate", [
    lambda trail: trail.pop(0),
    lambda trail: trail.pop(),
    lambda trail: trail[0].update(actor_id="someone-else"),
    lambda trail: trail[0].update(on_behalf_of="o"),
    lambda trail: trail[-1].update(actor_type="system"),
    lambda trail: trail[-1].update(actor_id="other-service"),
    lambda trail: trail[-1].update(on_behalf_of=None),
])
def test_an_incomplete_or_misattributed_audit_trail_is_refused(mutate):
    trail = audit_trail()
    mutate(trail)
    assert smoke_module.audit_problems(trail, owner_id="o", principal_id="p")


# -- pilote -------------------------------------------------------------------------------------

def test_a_missing_docker_is_a_prerequisite_error_never_a_success():
    docker = FakeDocker(missing=True)
    with pytest.raises(smoke_module.Prerequisite):
        make(docker).execute()
    assert docker.argvs() == [["docker", "version"]]


def test_main_returns_2_without_docker(monkeypatch, capsys):
    monkeypatch.setenv("PATH", "")
    assert smoke_module.main(["--skip-build"]) == 2
    assert "prerequis absent" in capsys.readouterr().err


@pytest.mark.parametrize("raised, code", [(None, 0), ("failure", 1), ("timeout", 1), ("bug", 1)])
def test_main_exit_codes(monkeypatch, capsys, raised, code):
    def execute(self):
        secret = self.passwords["MERVIO_APP_DB_PASSWORD"]
        if raised == "failure":
            raise SmokeFailure(f"broken {secret}")
        if raised == "timeout":
            raise subprocess.TimeoutExpired(["docker", "compose", secret], 1)
        if raised == "bug":
            raise KeyError(secret)
    captured = {}
    original = smoke_module.Smoke.__post_init__

    def remember(self):
        original(self)
        captured["secrets"] = list(self.passwords.values())
    monkeypatch.setattr(smoke_module.Smoke, "__post_init__", remember)
    monkeypatch.setattr(smoke_module.Smoke, "execute", execute)
    assert smoke_module.main(["--skip-build"]) == code
    output = "".join(capsys.readouterr())
    assert ("smoke test reussi" in output) == (code == 0)
    for secret in captured["secrets"]:
        assert secret not in output


def test_a_failure_after_start_always_removes_the_stack_and_redacts_everything():
    holder = {}

    def leaking(argv):
        return "up" in argv and "postgres" in argv

    def logs(argv):
        return "logs" in argv

    class Leaky(FakeDocker):
        def __call__(self, argv, **options):
            result = super().__call__(argv, **options)
            if leaking(argv) or logs(argv):
                secret = holder["smoke"].passwords["MERVIO_POSTGRES_PASSWORD"]
                result = subprocess.CompletedProcess(argv, 1 if leaking(argv) else 0,
                                                     f"out {secret}", f"err {secret}")
            return result

    docker = Leaky(image_responses())
    printed = []
    smoke = smoke_module.Smoke(image="mervio:test", build=False, keep=False, runner=docker, log=printed.append,
                               port=55999)
    holder["smoke"] = smoke
    with pytest.raises(SmokeFailure) as failure:
        smoke.execute()
    secret = smoke.passwords["MERVIO_POSTGRES_PASSWORD"]
    assert secret not in str(failure.value) and "[redacted]" in str(failure.value)
    assert all(secret not in line for line in printed)
    last = docker.argvs()[-1]
    assert last[:2] == ["docker", "compose"] and last[-5:] == ["down", "--volumes", "--remove-orphans",
                                                                "--timeout", "60"]
    assert ["--project-name", smoke.project] == last[2:4] and smoke.project.startswith("mervio-smoke-")
    for argv in docker.argvs():
        for value in smoke.passwords.values():
            assert value not in " ".join(argv), "un secret ne passe jamais par argv"


def test_an_unexpected_error_also_removes_the_stack(monkeypatch):
    docker = FakeDocker(image_responses())
    smoke = make(docker)
    monkeypatch.setattr(smoke, "start_postgres", lambda: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        smoke.execute()
    assert docker.argvs()[-1][-5:] == ["down", "--volumes", "--remove-orphans", "--timeout", "60"]


def test_keep_leaves_the_stack_for_diagnosis(monkeypatch):
    docker = FakeDocker(image_responses())
    smoke = make(docker, keep=True)
    monkeypatch.setattr(smoke, "start_postgres", lambda: (_ for _ in ()).throw(SmokeFailure("x")))
    with pytest.raises(SmokeFailure):
        smoke.execute()
    assert not any("down" in argv for argv in docker.argvs())


def test_the_steps_run_in_order_and_cleanup_comes_last(monkeypatch):
    docker = FakeDocker()
    smoke = make(docker)
    order = []
    for name in ("prerequisites", "check_image", "start_postgres", "refuse_unmigrated_schema", "migrate",
                 "refuse_privileged_connections", "check_data_volume", "check_isolation", "stop_worker",
                 "check_resolution", "check_leaks", "cleanup"):
        monkeypatch.setattr(smoke, name, lambda *args, _name=name: order.append(_name))
    monkeypatch.setattr(smoke, "start_worker", lambda: order.append("start_worker") or "principal")
    monkeypatch.setattr(smoke, "demonstrate", lambda principal: order.append(f"demonstrate:{principal}") or {})
    smoke.execute()
    # `check_resolution` vient APRES `stop_worker`: le resolveur est une commande ponctuelle, et le
    # travail d'effacement qu'elle met en file doit rester `queued` (004.4.5 E6)
    assert order == ["prerequisites", "check_image", "start_postgres", "refuse_unmigrated_schema", "migrate",
                     "refuse_privileged_connections", "start_worker", "check_data_volume",
                     "demonstrate:principal", "check_isolation", "stop_worker", "check_resolution",
                     "check_leaks", "cleanup"]


def test_the_image_is_built_only_when_asked():
    docker = FakeDocker(image_responses())
    make(docker, build=True).check_image()
    assert ["docker", "build", "--tag", "mervio:test", str(ROOT)] in docker.argvs()
    docker = FakeDocker(image_responses())
    make(docker).check_image()
    assert not any(argv[:2] == ["docker", "build"] for argv in docker.argvs())
    probes = [argv for argv in docker.argvs() if argv[:2] == ["docker", "run"]]
    assert probes and all(argv[2:5] == ["--rm", "--network", "none"] for argv in probes)


@pytest.mark.parametrize("config, facts", [
    ({**GOOD_CONFIG, "User": ""}, None),
    ({**GOOD_CONFIG, "User": "0:0"}, None),
    ({**GOOD_CONFIG, "ExposedPorts": {"8000/tcp": {}}}, None),
    ({**GOOD_CONFIG, "Cmd": ["sh"]}, None),
    ({**GOOD_CONFIG, "Healthcheck": None}, None),
    ({**GOOD_CONFIG, "Env": ["MERVIO_DATABASE_URL=postgresql://x"]}, None),
    ({**GOOD_CONFIG, "Env": ["POSTGRES_PASSWORD=x"]}, None),
    (None, {**GOOD_FACTS, "uid": 0}),
    (None, {**GOOD_FACTS, "python": "3.12.0"}),
    (None, {**GOOD_FACTS, "absent": ["pip"]}),
    (None, {**GOOD_FACTS, "paths": ["/var/lib/mervio/tests"]}),
])
def test_an_unsafe_image_is_refused(config, facts):
    with pytest.raises(SmokeFailure):
        make(FakeDocker(image_responses(config, facts))).check_image()


#: 004.4.2: le worker SANS cle maitre d'identite est refuse a la configuration, avant toute connexion
NO_KEY_REFUSAL = ('{"event": "worker.config_invalid", "problems": [{"variable": "MERVIO_IDENTITY_MASTER_KEY", '
                  '"rule": "obligatoire"}]}\n')


def negative_responses(worker_code=2, admin_code=3, no_key_code=2):
    return [
        (contains("worker", "MERVIO_IDENTITY_MASTER_KEY"), worker_code, "", '{"event": "worker.identity_refused"}\n'),
        (contains("worker"), no_key_code, "", NO_KEY_REFUSAL),
        (contains("psql"), 0, "0\n", ""),
        (contains("admin"), admin_code, '{"ok": false, "error": {"code": "database_unavailable", "message": "x"}}\n',
         ""),
    ]


def test_negative_connections_pass_their_url_through_the_environment_only():
    docker = FakeDocker(negative_responses())
    smoke = make(docker)
    smoke.refuse_privileged_connections()
    runs = [call for call in docker.calls if call["argv"][:2] == ["docker", "run"]]
    assert len(runs) == 3
    for call in runs:
        argv = call["argv"]
        assert argv[argv.index("-e") + 1] == "MERVIO_DATABASE_URL", "nom seul: la valeur vient de l'environnement"
        assert argv[argv.index("--network") + 1] == f"{smoke.project}_backend"
        assert "postgresql://" not in " ".join(argv)
        assert call["env"]["MERVIO_DATABASE_URL"].startswith("postgresql://")
    superuser, no_key, wrong = runs
    assert superuser["argv"].count("-e") == 3 and "MERVIO_IDENTITY_MASTER_KEY" in superuser["argv"]
    assert "MERVIO_IDENTITY_MASTER_KEY" not in no_key["argv"]  # l'absence de cle est le cas teste
    assert smoke.passwords["MERVIO_IDENTITY_MASTER_KEY"] not in " ".join(superuser["argv"])  # nom seul
    assert superuser["env"]["MERVIO_DATABASE_URL"].startswith(f"postgresql://{smoke_module.SUPERUSER}:")
    assert no_key["env"]["MERVIO_DATABASE_URL"].startswith(f"postgresql://{smoke_module.WORKER_ROLE}:")
    assert wrong["env"]["MERVIO_DATABASE_URL"].startswith(f"postgresql://{smoke_module.APP_ROLE}:")
    assert smoke.redact(wrong["env"]["MERVIO_DATABASE_URL"]) != wrong["env"]["MERVIO_DATABASE_URL"]


def test_the_generated_secrets_include_a_valid_identity_master_key():
    import re
    generated = smoke_module.generate_secrets()
    assert set(generated) == set(smoke_module.PASSWORD_VARIABLES) | {"MERVIO_IDENTITY_MASTER_KEY"}
    assert re.fullmatch(r"[0-9a-f]{64}", generated["MERVIO_IDENTITY_MASTER_KEY"])
    assert smoke_module.generate_secrets()["MERVIO_IDENTITY_MASTER_KEY"] != generated["MERVIO_IDENTITY_MASTER_KEY"]


@pytest.mark.parametrize("worker_code, admin_code, no_key_code", [(0, 3, 2), (1, 3, 2), (2, 0, 2), (2, 1, 2),
                                                                  (2, 3, 0), (2, 3, 1)])
def test_negative_connections_must_fail_with_the_expected_code(worker_code, admin_code, no_key_code):
    docker = FakeDocker(negative_responses(worker_code, admin_code, no_key_code))
    with pytest.raises(SmokeFailure):
        make(docker).refuse_privileged_connections()


def test_sql_checks_run_inside_the_postgres_container_without_credentials():
    docker = FakeDocker([(contains("psql"), 0, "a|b\n\nc|d\n", "")])
    smoke = make(docker)
    assert smoke.sql("SELECT 1", role="someone") == [["a", "b"], ["c", "d"]]
    argv = docker.argvs()[-1]
    assert argv[argv.index("exec"):argv.index("exec") + 4] == ["exec", "-T", "postgres", "psql"]
    assert argv[argv.index("-U") + 1] == "someone" and argv[argv.index("-d") + 1] == smoke_module.DATABASE
    assert "-h" not in argv and "PGPASSWORD" not in " ".join(argv)
    with pytest.raises(SmokeFailure):
        smoke.scalar("SELECT 1")


def test_a_job_that_never_finishes_is_a_failure(monkeypatch):
    smoke = make(FakeDocker(), poll_seconds=0)
    monkeypatch.setattr(smoke, "admin", lambda *argv, **kw: {"job": {"status": "running"}})
    with pytest.raises(SmokeFailure, match="toujours running"):
        smoke.wait_job("org", "job", timeout=0.05)


def test_the_transcript_check_catches_a_published_password():
    smoke = make(FakeDocker())
    smoke.transcript.append("ok")
    smoke.check_transcript("worker logs")
    smoke.transcript.append("oops " + smoke.passwords["MERVIO_WORKER_DB_PASSWORD"])
    with pytest.raises(SmokeFailure, match="password#3"):
        smoke.check_transcript()


def worker_start_responses(principal="p-id", ready_after=2):
    calls = {"health": 0}

    def health(argv):
        if "healthcheck" not in argv:
            return False
        calls["health"] += 1
        return calls["health"] < ready_after
    logs = "\n".join(json.dumps(e) for e in [
        event(event="worker.database_connected", principal_id=principal,
              principal_name=smoke_module.WORKER_ROLE),
        event(event="worker.ready")])
    return calls, [
        (contains("ps", "--quiet"), 0, "cid\n", ""),
        (contains("{{.State.Health.Status}}"), 0, "healthy\n", ""),
        (contains("{{.Config.User}}"), 0, "10001:10001\n", ""),
        (contains("{{.HostConfig.ReadonlyRootfs}}"), 0, "true\n", ""),
        (contains("{{json .NetworkSettings.Ports}}"), 0, "{}\n", ""),
        (health, 1, "", ""),
        (contains("healthcheck"), 0, '{"healthy": true, "state": "ready"}\n', ""),
        (contains("python", "-c"), 0, "10001\n", ""),
        (contains("psql"), 0, f"{principal}\n", ""),
        (contains("logs"), 0, logs, ""),
    ]


def test_the_worker_start_waits_for_readiness_then_checks_its_identity():
    calls, responses = worker_start_responses(ready_after=3)
    smoke = make(FakeDocker(responses), poll_seconds=0)
    assert smoke.start_worker() == "p-id"
    assert calls["health"] == 3


def test_a_worker_announcing_another_principal_is_refused():
    _, responses = worker_start_responses()
    responses[8] = (contains("psql"), 0, "other-id\n", "")
    with pytest.raises(SmokeFailure, match="principal"):
        make(FakeDocker(responses), poll_seconds=0).start_worker()


def test_only_uuids_are_interpolated_in_control_queries():
    assert smoke_module.identifier("6F9619FF-8B86-D011-B42D-00C04FC964FF") == "6f9619ff-8b86-d011-b42d-00c04fc964ff"
    for bad in ("x' OR '1'='1", "", None, "1; DROP TABLE reports"):
        with pytest.raises(SmokeFailure):
            smoke_module.identifier(bad)



def test_the_negative_worker_mounts_its_health_tmpfs_for_the_image_user():
    docker = FakeDocker(negative_responses())
    make(docker).refuse_privileged_connections()
    workers = [argv for argv in docker.argvs() if argv[:2] == ["docker", "run"] and argv[-1] == "worker"]
    assert len(workers) == 2  # superutilisateur, puis sans cle d'identite (004.4.2)
    for worker in workers:
        assert worker[worker.index("--tmpfs") + 1] == "/run/mervio:uid=10001,gid=10001,mode=0700"


def test_the_negative_workers_get_the_object_store_the_compose_worker_gets():
    """004.4.4: sans cette racine, ils meurent en `worker.config_invalid` AVANT le controle vise.

    C'est l'echec du job `docker` de CI #13: le contrat du magasin est obligatoire des que le
    worker traite des imports, et `JOB_TYPES` le contient par defaut. Ces conteneurs ad-hoc ne
    passent pas par le compose: la variable doit donc leur etre donnee explicitement.
    """
    docker = FakeDocker(negative_responses())
    make(docker).refuse_privileged_connections()
    workers = [argv for argv in docker.argvs() if argv[:2] == ["docker", "run"] and argv[-1] == "worker"]
    assert len(workers) == 2  # superutilisateur, puis sans cle d'identite (004.4.2)
    for worker in workers:
        assert f"MERVIO_OBJECT_STORE_ROOT={smoke_module.OBJECT_STORE_ROOT}" in worker


def test_a_failure_prints_the_redacted_docker_health_verdicts():
    holder = {}

    class Unhealthy(FakeDocker):
        def __call__(self, argv, **options):
            result = super().__call__(argv, **options)
            secret = holder["smoke"].passwords["MERVIO_WORKER_DB_PASSWORD"]
            if "{{json .State.Health}}" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"Status": "unhealthy", "Log": [
                    {"ExitCode": 1, "Output": f"unhealthy check=liveness state=- reason=missing {secret}"}]}), "")
            if "up" in argv and "worker" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "container worker is unhealthy")
            return result

    docker = Unhealthy([(contains("ps", "--quiet"), 0, "cid\n", "")])
    printed = []
    smoke = smoke_module.Smoke(image="mervio:test", build=False, keep=False, runner=docker, log=printed.append,
                               port=55999)
    holder["smoke"] = smoke
    for name in ("prerequisites", "check_image", "start_postgres", "refuse_unmigrated_schema", "migrate",
                 "refuse_privileged_connections"):
        setattr(smoke, name, lambda: None)
    with pytest.raises(SmokeFailure, match="unhealthy"):
        smoke.execute()
    report = "\n".join(printed)
    assert "reason=missing" in report
    assert smoke.passwords["MERVIO_WORKER_DB_PASSWORD"] not in report
    assert docker.argvs()[-1][-5:] == ["down", "--volumes", "--remove-orphans", "--timeout", "60"]


def test_the_negative_worker_environment_really_satisfies_the_worker_contract():
    """L'argv ne suffit pas a prouver: on rejoue `WorkerSettings.from_env` sur ce que voit le conteneur.

    C'est l'invariant qui avait rompu en CI #13. Les tests d'argv ci-dessus verifient une FORME; ici
    le produit lui-meme juge l'environnement, donc un futur reglage obligatoire casserait ce test
    plutot que le job `docker` de la CI.
    """
    from mervio.settings import SettingsError, WorkerSettings

    docker = FakeDocker(negative_responses())
    smoke = make(docker)
    smoke.refuse_privileged_connections()
    call = next(c for c in docker.calls if c["argv"][:2] == ["docker", "run"] and c["argv"][-1] == "worker")

    # ce que le conteneur voit: `-e NOM` (valeur heritee du client) ou `-e NOM=VALEUR`, plus l'ENV
    # de l'image (Dockerfile), qui ne porte que le fichier de sante.
    environ = {"MERVIO_WORKER_HEALTH_FILE": "/run/mervio/worker-health.json"}
    argv = call["argv"]
    for index, token in enumerate(argv):
        if token == "-e":
            name, separator, value = argv[index + 1].partition("=")
            environ[name] = value if separator else call["env"][name]
    assert environ["MERVIO_OBJECT_STORE_ROOT"] == smoke_module.OBJECT_STORE_ROOT

    settings = WorkerSettings.from_env(environ)
    assert "import" in settings.job_types and settings.object_store is not None

    without = {name: value for name, value in environ.items() if name != "MERVIO_OBJECT_STORE_ROOT"}
    with pytest.raises(SettingsError) as refusal:  # sinon le refus precede le controle d'identite
        WorkerSettings.from_env(without)
    assert [name for name, _ in refusal.value.problems] == ["MERVIO_OBJECT_STORE_ROOT"]
