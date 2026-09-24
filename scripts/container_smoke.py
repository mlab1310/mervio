#!/usr/bin/env python3
"""Smoke test CONTENEURISE de Mervio (Mission 004.3.8): vrais conteneurs, vraie base.

    python scripts/container_smoke.py                  # construit l'image puis teste
    python scripts/container_smoke.py --image mervio:ci --skip-build
    python scripts/container_smoke.py --keep           # laisse la pile en place (diagnostic)

Prerequis: Docker Engine et Docker Compose v2 (`docker compose`), Python 3.11+ sur l'hote.
Bibliotheque standard uniquement: ce script n'importe pas Mervio, il pilote les commandes du
produit dans les conteneurs (`mervio worker`, `mervio admin`, `python -m mervio.persistence`).

Parcours (chaque etape doit reussir, la premiere erreur arrete tout):

    image        non-root, sans port, sans outil de test ni pip, HEALTHCHECK present
    postgres     demarre, devient healthy, bootstrap: roles non privilegies, base, CONNECT
    worker       refuse un schema non migre (code 4): il ne migre jamais lui-meme
    migrate      etape explicite, revision head atteinte
    worker       refuse un superutilisateur (code 2); demarre, healthy, pret, principal de service
    admin        refuse un mauvais mot de passe (code 3, sans le publier)
    demo         provisionnement, import, analyse, rapport persiste, audit attribue
    isolation    un humain etranger ne voit rien; RLS sous les roles applicatif et worker
    arret        SIGTERM: arret propre (code 0, `worker.stopped`), aucun travail ni bail en cours
    fuites       aucun mot de passe ni URL de connexion dans les sorties et les logs

Isolation du test: projet compose unique (`mervio-smoke-<aleatoire>`), mots de passe aleatoires
generes ici et transmis par l'environnement du processus `docker compose` seulement, port
PostgreSQL libre sur 127.0.0.1. La pile et ses volumes sont TOUJOURS supprimes a la fin (bloc
finally), sauf --keep. Toute sortie affichee est d'abord nettoyee des mots de passe.

Codes de sortie: 0 smoke test reussi, 1 echec, 2 prerequis absents ou usage invalide.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "docker-compose.yml"

DATABASE = "mervio"
SUPERUSER = "mervio_admin"
MIGRATOR_ROLE = "mervio_migrator"
APP_ROLE = "mervio_app_user"
WORKER_ROLE = "mervio_worker_svc"
DATA_DIR = "/var/lib/mervio/data/demo"
OWNER = "smoke|owner"
INTRUDER = "smoke|intruder"
IMAGE_UID = 10001
#: Meme montage que le service worker du compose (proprietaire explicite, voir docker-compose.yml).
HEALTH_TMPFS = f"/run/mervio:uid={IMAGE_UID},gid={IMAGE_UID},mode=0700"
#: Codes des commandes du produit (workers.runtime, admin.errors).
WORKER_EXIT_OK, WORKER_EXIT_CONFIG, WORKER_EXIT_SCHEMA = 0, 2, 4
ADMIN_EXIT_DATABASE, ADMIN_EXIT_NOT_FOUND = 3, 5
PASSWORD_VARIABLES = ("MERVIO_POSTGRES_PASSWORD", "MERVIO_MIGRATOR_DB_PASSWORD", "MERVIO_APP_DB_PASSWORD",
                      "MERVIO_WORKER_DB_PASSWORD")
#: cle maitre d'identite du worker (004.4.2): obligatoire des que le worker traite des imports
IDENTITY_KEY_VARIABLE = "MERVIO_IDENTITY_MASTER_KEY"
EXPECTED_ADMIN_ACTIONS = ("organization.created", "store.created", "connection.created", "service.authorized",
                          # 004.4.4: un depot par source, TOUS avant la mise en file (D-054)
                          *("object.uploaded",) * 4, "job.enqueued")
EXPECTED_WORKER_ACTIONS = ("job.claimed", "import.started", "import.succeeded", "analysis.started",
                           "analysis.succeeded", "job.succeeded")
REDACTED = "[redacted]"
CONNECTION_URL = re.compile(r"postgres(?:ql)?(?:\+\w+)?://", re.IGNORECASE)


class SmokeFailure(Exception):
    """Une verification du smoke test a echoue. Le message est deja nettoye."""


class Prerequisite(Exception):
    """Docker ou Compose absent: le smoke test ne peut pas s'executer (jamais un succes)."""


@dataclass
class Result:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str


def generate_passwords() -> Dict[str, str]:
    """Quatre secrets distincts, compatibles avec le bootstrap ([A-Za-z0-9._~-], 16 a 128)."""
    return {name: secrets.token_hex(24) for name in PASSWORD_VARIABLES}


def generate_secrets() -> Dict[str, str]:
    """Mots de passe + cle maitre d'identite du worker (004.4.2, 32 octets en hexadecimal).

    Tous sont traites en secrets: masques dans les sorties et recherches comme fuites.
    """
    return {**generate_passwords(), IDENTITY_KEY_VARIABLE: secrets.token_hex(32)}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Redactor:
    """Retire les secrets d'un texte avant tout affichage."""

    def __init__(self, secrets_: Sequence[str]) -> None:
        self.secrets = sorted({s for s in secrets_ if s}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return text


def find_leaks(text: str, secrets_: Sequence[str]) -> List[str]:
    """Codes des fuites trouvees (jamais la valeur): mot de passe ou URL de connexion."""
    leaks = [f"password#{index}" for index, secret in enumerate(secrets_) if secret and secret in text]
    if CONNECTION_URL.search(text):
        leaks.append("connection_url")
    return leaks


def identifier(value: object) -> str:
    """UUID canonique: seule forme d'identifiant interpolee dans une requete de controle."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise SmokeFailure("identifiant UUID attendu") from None


def compose_environment(base: Dict[str, str], passwords: Dict[str, str], *, image: str, port: int) -> Dict[str, str]:
    """Environnement du processus `docker compose`: aucune variable MERVIO_* heritee de l'hote."""
    environment = {key: value for key, value in base.items() if not key.startswith(("MERVIO_", "COMPOSE_"))}
    environment.update(passwords)
    environment.update({"MERVIO_IMAGE": image, "MERVIO_POSTGRES_PORT": str(port), "MERVIO_ENV": "test",
                        "MERVIO_LOG_LEVEL": "INFO"})
    return environment


def parse_admin(result: Result) -> dict:
    """Document JSON d'une commande `mervio admin --json` (derniere ligne non vide de stdout)."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise SmokeFailure(f"admin: aucune sortie JSON (code {result.returncode})")
    try:
        document = json.loads(lines[-1])
    except ValueError:
        raise SmokeFailure(f"admin: sortie non JSON (code {result.returncode})") from None
    if not isinstance(document, dict) or "ok" not in document:
        raise SmokeFailure("admin: document JSON inattendu")
    return document


def parse_json_logs(text: str) -> List[dict]:
    """Lignes JSON des logs du worker (`docker compose logs --no-log-prefix`)."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            raise SmokeFailure("worker: ligne de log JSON invalide") from None
    return events


def check_log_contract(events: List[dict]) -> None:
    """Contrat de 004.3: JSON, UTC, evenement nomme, service, aucun champ de secret."""
    if not events:
        raise SmokeFailure("worker: aucun log JSON")
    for event in events:
        for key in ("timestamp", "level", "service", "event"):
            if key not in event:
                raise SmokeFailure(f"worker: log sans champ {key}")
        stamp = str(event["timestamp"])
        if not (stamp.endswith("Z") or stamp.endswith("+00:00")):
            raise SmokeFailure("worker: horodatage non UTC")
        for key, value in event.items():
            sensitive = any(fragment in key.lower() for fragment in ("password", "secret", "token", "database_url"))
            if sensitive and value != REDACTED:
                raise SmokeFailure(f"worker: champ de log sensible non masque {key}")


def audit_problems(events: List[dict], *, owner_id: str, principal_id: str) -> List[str]:
    """Le journal est complet et chaque trace est attribuee (humain ou principal du worker)."""
    problems = []
    actions = [event.get("action") for event in events]
    if tuple(actions[:len(EXPECTED_ADMIN_ACTIONS)]) != EXPECTED_ADMIN_ACTIONS:
        problems.append("audit: premieres actions inattendues")
    problems += [f"audit: {action} absent" for action in EXPECTED_WORKER_ACTIONS if action not in actions]
    for event in events:
        pair = (event.get("actor_id"), event.get("on_behalf_of"))
        if event.get("actor_type") == "user":
            if pair != (owner_id, None):
                problems.append(f"audit: {event.get('action')} mal attribue")
        elif event.get("actor_type") != "worker" or pair != (principal_id, owner_id):
            problems.append(f"audit: {event.get('action')} mal attribue au worker")
    return problems


# =============================================================================================
# Pilote
# =============================================================================================

@dataclass
class Smoke:
    image: str
    build: bool
    keep: bool
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    log: Callable[[str], None] = print
    passwords: Dict[str, str] = field(default_factory=generate_secrets)
    project: str = field(default_factory=lambda: f"mervio-smoke-{secrets.token_hex(4)}")
    port: int = field(default_factory=free_port)
    transcript: List[str] = field(default_factory=list)
    poll_seconds: float = 2.0

    def __post_init__(self) -> None:
        self.redact = Redactor(list(self.passwords.values()))
        self.environment = compose_environment(dict(os.environ), self.passwords, image=self.image, port=self.port)

    # -- execution -----------------------------------------------------------------------------
    def run(self, argv: Sequence[str], *, check: bool = True, env: Optional[Dict[str, str]] = None,
            stdin: Optional[str] = None, timeout: float = 600) -> Result:
        completed = self.runner(list(argv), cwd=ROOT, env=env or self.environment, input=stdin,
                                capture_output=True, text=True, timeout=timeout)
        result = Result(list(argv), completed.returncode, completed.stdout or "", completed.stderr or "")
        self.transcript.append(result.stdout + result.stderr)
        if check and result.returncode != 0:
            raise SmokeFailure(self.redact(
                f"commande en echec (code {result.returncode}): {' '.join(argv)}\n"
                f"--- stdout ---\n{result.stdout[-4000:]}\n--- stderr ---\n{result.stderr[-4000:]}"))
        return result

    def compose(self, *arguments: str, **options) -> Result:
        return self.run(["docker", "compose", "--project-name", self.project, "--file", str(COMPOSE_FILE),
                         "--profile", "ops", *arguments], **options)

    def step(self, label: str) -> None:
        self.log(f"container_smoke: {label}")

    def expect(self, condition: bool, message: str) -> None:
        if not condition:
            raise SmokeFailure(self.redact(message))

    # -- outils ----------------------------------------------------------------------------------
    def sql(self, query: str, *, role: Optional[str] = None) -> List[List[str]]:
        """Requete dans le conteneur postgres (socket local). Verification seulement.

        Par defaut sous le superutilisateur du conteneur (lecture de controle, jamais le produit).
        """
        result = self.compose("exec", "-T", "postgres", "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-A", "-t",
                              "-F", "|", "-U", role or SUPERUSER, "-d", DATABASE, "-c", query)
        return [line.split("|") for line in result.stdout.splitlines() if line.strip()]

    def scalar(self, query: str, **options) -> str:
        rows = self.sql(query, **options)
        self.expect(len(rows) == 1 and len(rows[0]) == 1, f"requete scalaire inattendue: {query}")
        return rows[0][0]

    def admin(self, *argv: str, expect_code: int = 0) -> dict:
        result = self.compose("run", "--rm", "-T", "admin", *argv, "--json", check=False)
        document = parse_admin(result)
        self.expect(result.returncode == expect_code,
                    f"admin {' '.join(argv[:2])}: code {result.returncode}, {expect_code} attendu: {document}")
        return document["result"] if document["ok"] else document["error"]

    def wait_job(self, organization: str, job: str, timeout: float = 300) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            shown = self.admin("job", "show", "--as", OWNER, "--org", organization, "--job", job)["job"]
            if shown["status"] in ("succeeded", "failed", "cancelled"):
                return shown
            self.expect(time.monotonic() < deadline, f"travail {job} toujours {shown['status']}")
            time.sleep(self.poll_seconds)

    def container_id(self, service: str) -> str:
        return self.compose("ps", "--all", "--quiet", service).stdout.strip()

    def inspect(self, target: str, template: str) -> str:
        return self.run(["docker", "inspect", "--format", template, target]).stdout.strip()

    # -- parcours --------------------------------------------------------------------------------
    def prerequisites(self) -> None:
        for argv in (["docker", "version"], ["docker", "compose", "version"]):
            try:
                result = self.runner(argv, capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                raise Prerequisite(f"{' '.join(argv)} indisponible") from None
            if result.returncode != 0:
                raise Prerequisite(f"{' '.join(argv)} indisponible (code {result.returncode})")

    def check_image(self) -> None:
        self.step("image: construction" if self.build else f"image: {self.image} (deja construite)")
        if self.build:
            self.run(["docker", "build", "--tag", self.image, str(ROOT)], timeout=1800)
        config = json.loads(self.run(["docker", "image", "inspect", "--format", "{{json .Config}}",
                                      self.image]).stdout)
        self.expect(config.get("User") == f"{IMAGE_UID}:{IMAGE_UID}", "image: utilisateur non-root attendu")
        self.expect(not config.get("ExposedPorts"), "image: aucun port ne doit etre expose")
        self.expect(config.get("Entrypoint") == ["mervio"] and config.get("Cmd") == ["worker"],
                    "image: point d'entree `mervio worker` attendu")
        self.expect((config.get("Healthcheck") or {}).get("Test") == ["CMD", "mervio", "worker", "healthcheck"],
                    "image: HEALTHCHECK `mervio worker healthcheck` attendu")
        self.expect(not any(item.split("=", 1)[0].startswith(("MERVIO_DATABASE_URL", "MERVIO_MIGRATION"))
                            or "PASSWORD" in item for item in config.get("Env") or []),
                    "image: aucune connexion ni mot de passe dans l'environnement de l'image")
        probe = ("import importlib.util, json, os, sys; import mervio.cli, mervio.persistence.migrate;"
                 "print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'python': sys.version.split()[0],"
                 "'absent': [m for m in ('pytest', 'pip', 'setuptools', 'iniconfig', 'pluggy', 'pygments')"
                 " if importlib.util.find_spec(m) is None],"
                 "'paths': [p for p in ('/var/lib/mervio/tests', '/var/lib/mervio/.git', '/build', '/opt/venv/bin/pip')"
                 " if os.path.exists(p)]}))")
        facts = json.loads(self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint",
                                     "/opt/venv/bin/python", self.image, "-c", probe]).stdout)
        self.expect(facts["uid"] == IMAGE_UID and facts["gid"] == IMAGE_UID, "image: processus non-root attendu")
        self.expect(facts["python"] == "3.11.16", "image: Python 3.11.16 attendu")
        self.expect(len(facts["absent"]) == 6, f"image: outils presents dans l'environnement: {facts['absent']}")
        self.expect(facts["paths"] == [], f"image: fichiers inattendus: {facts['paths']}")
        version = self.run(["docker", "run", "--rm", "--network", "none", self.image, "--version"]).stdout
        self.expect(version.startswith("mervio "), "image: `mervio --version` attendu")

    def start_postgres(self) -> None:
        self.step("compose: fichier valide; postgres: demarrage et bootstrap")
        self.compose("config", "--quiet")
        self.compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres", timeout=300)
        self.expect(self.inspect(self.container_id("postgres"), "{{.State.Health.Status}}") == "healthy",
                    "postgres: healthy attendu")
        self.check_bootstrap()

    def check_bootstrap(self) -> None:
        """Roles et base crees par docker/postgres/10-mervio-bootstrap.sql."""
        self.expect(self.scalar("SHOW server_version_num")[:2] == "17", "postgres: version 17 attendue")
        rows = self.sql("SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
                        "rolreplication FROM pg_roles WHERE rolname IN ('mervio_app', 'mervio_worker', "
                        f"'{MIGRATOR_ROLE}', '{APP_ROLE}', '{WORKER_ROLE}') ORDER BY 1")
        roles = {row[0]: row[1:] for row in rows}
        self.expect(sorted(roles) == sorted(["mervio_app", "mervio_worker", MIGRATOR_ROLE, APP_ROLE, WORKER_ROLE]),
                    f"postgres: roles attendus absents: {sorted(roles)}")
        for name, (login, *privileges) in roles.items():
            self.expect(privileges == ["f"] * 5, f"postgres: role {name} privilegie")
            self.expect(login == ("f" if name in ("mervio_app", "mervio_worker") else "t"),
                        f"postgres: LOGIN inattendu pour {name}")
        memberships = self.sql(
            "SELECT m.rolname, g.rolname FROM pg_auth_members a JOIN pg_roles m ON m.oid = a.member "
            "JOIN pg_roles g ON g.oid = a.roleid WHERE g.rolname IN ('mervio_app', 'mervio_worker') "
            f"AND m.rolname IN ('{MIGRATOR_ROLE}', '{APP_ROLE}', '{WORKER_ROLE}') ORDER BY 1, 2")
        self.expect(memberships == [[APP_ROLE, "mervio_app"], [WORKER_ROLE, "mervio_app"],
                                    [WORKER_ROLE, "mervio_worker"]], f"postgres: appartenances {memberships}")
        owner, encoding, public_connect = self.sql(
            "SELECT pg_get_userbyid(datdba), pg_encoding_to_char(encoding), "
            f"has_database_privilege('public', oid, 'CONNECT') FROM pg_database WHERE datname = '{DATABASE}'")[0]
        self.expect((owner, encoding, public_connect) == (MIGRATOR_ROLE, "UTF8", "f"),
                    "postgres: base mal initialisee")

    def refuse_unmigrated_schema(self) -> None:
        self.step("worker: refus d'un schema non migre (aucune migration implicite)")
        result = self.compose("run", "--rm", "-T", "--no-deps", "worker", check=False, timeout=300)
        self.expect(result.returncode == WORKER_EXIT_SCHEMA, f"worker sans schema: code {result.returncode}, 4 attendu")
        self.expect(self.scalar("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == "0",
                    "worker: le schema a ete cree par le worker")

    def migrate(self) -> None:
        self.step("migrate: upgrade head (etape explicite)")
        self.compose("run", "--rm", "-T", "migrate", timeout=300)
        head = self.compose("run", "--rm", "-T", "migrate", "heads").stdout.strip().splitlines()[-1]
        current = self.compose("run", "--rm", "-T", "migrate", "current").stdout.strip().splitlines()[-1]
        self.expect(bool(head) and current == head, f"migrate: revision {current}, head {head}")
        self.check_schema(head)

    def check_schema(self, head: str) -> None:
        self.expect(self.scalar("SELECT version_num FROM alembic_version") == head, "migrate: alembic_version")
        forced = self.scalar("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                             "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version' "
                             "AND NOT (c.relrowsecurity AND c.relforcerowsecurity) AND c.relname <> 'users'")
        self.expect(forced == "0", "migrate: une table de tenant sans RLS forcee")

    def refuse_privileged_connections(self) -> None:
        self.step("worker: refus d'un superutilisateur; admin: refus d'un mauvais mot de passe")
        # `docker run -e NOM` lit la valeur dans l'environnement du client: jamais dans argv
        network = f"{self.project}_backend"
        superuser_url = (f"postgresql://{SUPERUSER}:{self.passwords['MERVIO_POSTGRES_PASSWORD']}"
                         f"@postgres:5432/{DATABASE}")
        result = self.run(["docker", "run", "--rm", "--network", network, "--read-only", "--tmpfs", HEALTH_TMPFS,
                           "-e", "MERVIO_DATABASE_URL", "-e", IDENTITY_KEY_VARIABLE, self.image, "worker"],
                          env=dict(self.environment, MERVIO_DATABASE_URL=superuser_url), check=False, timeout=300)
        self.expect(result.returncode == WORKER_EXIT_CONFIG,
                    f"worker superutilisateur: code {result.returncode}, 2 attendu")
        self.expect("worker.identity_refused" in result.stdout + result.stderr,
                    "worker superutilisateur: evenement worker.identity_refused attendu")
        self.expect(self.scalar("SELECT count(*) FROM users WHERE kind = 'service'") == "0",
                    "worker superutilisateur: un principal a ete enregistre")
        # 004.4.2: sans cle maitre d'identite, le worker (qui traite les imports) refuse de demarrer
        worker_url = (f"postgresql://{WORKER_ROLE}:{self.passwords['MERVIO_WORKER_DB_PASSWORD']}"
                      f"@postgres:5432/{DATABASE}")
        result = self.run(["docker", "run", "--rm", "--network", network, "--read-only", "--tmpfs", HEALTH_TMPFS,
                           "-e", "MERVIO_DATABASE_URL", self.image, "worker"],
                          env=dict(self.environment, MERVIO_DATABASE_URL=worker_url), check=False, timeout=300)
        self.expect(result.returncode == WORKER_EXIT_CONFIG,
                    f"worker sans cle d'identite: code {result.returncode}, 2 attendu")
        self.expect("worker.config_invalid" in result.stdout + result.stderr
                    and IDENTITY_KEY_VARIABLE in result.stdout + result.stderr,
                    "worker sans cle d'identite: refus de configuration nommant la variable attendu")
        self.expect(self.scalar("SELECT count(*) FROM users WHERE kind = 'service'") == "0",
                    "worker sans cle d'identite: un principal a ete enregistre")
        wrong = secrets.token_hex(24)
        self.redact = Redactor(self.redact.secrets + [wrong])
        result = self.run(["docker", "run", "--rm", "--network", network, "-e", "MERVIO_DATABASE_URL",
                           "--entrypoint", "mervio", self.image, "admin", "org", "list", "--as", OWNER, "--json"],
                          env=dict(self.environment,
                                   MERVIO_DATABASE_URL=f"postgresql://{APP_ROLE}:{wrong}@postgres:5432/{DATABASE}"),
                          check=False, timeout=300)
        self.expect(result.returncode == ADMIN_EXIT_DATABASE,
                    f"admin mauvais mot de passe: code {result.returncode}, 3 attendu")
        self.expect(parse_admin(result)["error"]["code"] == "database_unavailable",
                    "admin mauvais mot de passe: code d'erreur inattendu")
        self.expect(find_leaks(result.stdout + result.stderr, [wrong]) == [],
                    "admin mauvais mot de passe: secret publie")

    def start_worker(self) -> str:
        self.step("worker: demarrage, healthcheck, identite de service")
        self.compose("up", "--detach", "--wait", "--wait-timeout", "180", "worker", timeout=300)
        container = self.container_id("worker")
        self.expect(self.inspect(container, "{{.State.Health.Status}}") == "healthy", "worker: healthy attendu")
        self.expect(self.inspect(container, "{{.Config.User}}") == f"{IMAGE_UID}:{IMAGE_UID}",
                    "worker: utilisateur non-root attendu")
        self.expect(self.inspect(container, "{{.HostConfig.ReadonlyRootfs}}") == "true",
                    "worker: systeme de fichiers en lecture seule attendu")
        self.expect(self.inspect(container, "{{json .NetworkSettings.Ports}}") in ("{}", "null"),
                    "worker: aucun port publie")
        uid = self.compose("exec", "-T", "worker", "python", "-c", "import os; print(os.getuid())").stdout.strip()
        self.expect(uid == str(IMAGE_UID), "worker: processus non-root attendu")
        # `--wait` rend la main quand le worker est VIVANT (STARTING compris): la disponibilite est attendue ici
        deadline = time.monotonic() + 60
        while True:
            check = self.compose("exec", "-T", "worker", "mervio", "worker", "healthcheck", "--ready", "--json",
                                 check=False)
            output = check.stdout.strip()
            verdict = json.loads(output) if output.startswith("{") else {}
            if check.returncode == 0 or time.monotonic() > deadline:
                break
            time.sleep(self.poll_seconds)
        self.expect(check.returncode == 0 and verdict.get("healthy") is True and verdict.get("state") == "ready",
                    f"worker: pret attendu: {verdict}")
        return self.check_service_identity(self.compose("logs", "--no-color", "--no-log-prefix", "worker").stdout)

    def check_service_identity(self, worker_logs: str) -> str:
        """Principal `service:<role worker>` enregistre par la base et annonce par le worker."""
        principal = self.scalar("SELECT id FROM users WHERE kind = 'service' "
                                f"AND idp_subject = 'service:{WORKER_ROLE}'")
        logs = parse_json_logs(worker_logs)
        check_log_contract(logs)
        connected = [e for e in logs if e["event"] == "worker.database_connected"]
        self.expect(len(connected) == 1 and connected[0].get("principal_id") == principal
                    and connected[0].get("principal_name") == WORKER_ROLE,
                    "worker: principal de service non annonce")
        self.expect(any(e["event"] == "worker.ready" for e in logs), "worker: evenement worker.ready absent")
        return principal

    def check_data_volume(self) -> None:
        """Le volume partage appartient a l'utilisateur de l'image (admin y ecrit, le worker y lit)."""
        owner = self.compose("run", "--rm", "-T", "--entrypoint", "stat", "admin", "-c", "%u",
                             "/var/lib/mervio/data").stdout.strip()
        self.expect(owner == str(IMAGE_UID), f"volume de donnees: proprietaire {owner}, {IMAGE_UID} attendu")

    def demonstrate(self, principal: str) -> Dict[str, str]:
        self.step("demo: provisionnement, import, analyse, rapport, audit")
        provision = ("demo", "provision", "--owner", OWNER, "--data-dir", DATA_DIR, "--service", WORKER_ROLE)
        first = self.admin(*provision)
        for part in ("user", "organization", "store", "connection", "service_authorization", "import_job"):
            self.expect(first[part]["status"] == "created", f"demo: {part} non cree")
        again = self.admin(*provision)
        self.expect(again["import_job"]["job"]["id"] == first["import_job"]["job"]["id"], "demo: non rejouable")
        organization = identifier(first["organization"]["organization"]["id"])
        store = identifier(first["store"]["store"]["id"])

        imported = self.wait_job(organization, first["import_job"]["job"]["id"])
        self.expect(imported["status"] == "succeeded",
                    f"import: {imported['status']} {imported.get('last_error_code')}")
        self.expect(imported["result"]["row_count"] > 0, "import: aucune ligne")
        queued = self.admin("job", "enqueue-analysis", "--as", OWNER, "--org", organization, "--store", store,
                            "--from-import", imported["id"], "--as-of", first["next"]["analysis_as_of"],
                            "--grain", "week", "--idempotency-key", "smoke-analysis")
        analysed = self.wait_job(organization, queued["job"]["id"])
        self.expect(analysed["status"] == "succeeded",
                    f"analyse: {analysed['status']} {analysed.get('last_error_code')}")
        report = identifier(analysed["result"]["report_id"])
        stored = self.sql(f"SELECT organization_id, store_id, payload_sha256 FROM reports WHERE id = '{report}'")
        self.expect(stored == [[organization, store, analysed["result"]["payload_sha256"]]],
                    "rapport: non persiste ou different")

        owner_id = self.scalar(f"SELECT id FROM users WHERE idp_subject = '{OWNER}' AND kind = 'human'")
        trail = self.admin("audit", "list", "--as", OWNER, "--org", organization, "--limit", "1000")["events"]
        problems = audit_problems(trail, owner_id=owner_id, principal_id=principal)
        self.expect(problems == [], "; ".join(problems))
        state = self.admin("org", "show", "--as", OWNER, "--org", organization)
        self.expect(state["jobs"]["by_status"] == {"succeeded": 2}, f"demo: file {state['jobs']['by_status']}")
        return {"organization": organization, "store": store}

    def check_isolation(self, demo: Dict[str, str]) -> None:
        self.step("isolation: autre organisation, RLS applicative et worker")
        organization = identifier(demo["organization"])
        self.admin("user", "ensure", "--subject", INTRUDER)
        foreign = self.admin("org", "create", "--as", INTRUDER, "--name", "Smoke intruder")["organization"]["id"]
        foreign = identifier(foreign)
        for argv in (("org", "show", "--as", INTRUDER, "--org", organization),
                     ("job", "list", "--as", INTRUDER, "--org", organization),
                     ("store", "list", "--as", INTRUDER, "--org", organization),
                     ("org", "show", "--as", OWNER, "--org", foreign)):
            error = self.admin(*argv, expect_code=ADMIN_EXIT_NOT_FOUND)
            self.expect(error["code"] == "not_found", f"isolation: {error}")
        listed = self.admin("org", "list", "--as", INTRUDER)["organizations"]
        self.expect([o["id"] for o in listed] == [foreign], "isolation: organisations visibles par l'intrus")

        # SQL brut sous les roles de l'application: RLS forcee, contexte absent = rien de visible
        self.expect(self.scalar("SELECT count(*) FROM stores") == "1", "isolation: boutique de demo absente")
        self.expect(self.scalar("SELECT count(*) FROM stores", role=APP_ROLE) == "0",
                    "RLS: le role applicatif voit des boutiques sans contexte")
        self.expect(self.scalar(f"SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = '{WORKER_ROLE}'")
                    == "f", "worker: role privilegie")
        # une seule chaine = une seule transaction: le contexte local (set_config ..., true) s'applique au SELECT
        context = "SELECT set_config('app.organization_id', '{}', true); SELECT count(*) FROM stores"
        own = self.sql(context.format(organization), role=WORKER_ROLE)
        other = self.sql(context.format(foreign), role=WORKER_ROLE)
        self.expect(own[-1] == ["1"], "RLS: le worker ne voit pas l'organisation autorisee")
        self.expect(other[-1] == ["0"], "RLS: le worker voit une organisation non autorisee")
        orders = "SELECT set_config('app.organization_id', '{}', true); SELECT count(*) FROM orders"
        self.expect(self.sql(orders.format(organization), role=WORKER_ROLE)[-1] == ["0"],
                    "RLS: le worker lit des donnees metier sans delegue")
        self.expect(self.scalar("SELECT count(*) FROM orders") != "0", "isolation: aucune commande importee")

    def stop_worker(self) -> None:
        self.step("worker: SIGTERM et arret propre")
        container = self.container_id("worker")
        self.compose("stop", "worker", timeout=300)
        self.expect(self.inspect(container, "{{.State.Status}}") == "exited", "worker: non arrete")
        self.expect(self.inspect(container, "{{.State.ExitCode}}") == str(WORKER_EXIT_OK),
                    "worker: code de sortie non nul apres SIGTERM")
        self.expect(self.inspect(container, "{{.State.OOMKilled}}") == "false", "worker: tue par la memoire")
        self.check_stopped(self.compose("logs", "--no-color", "--no-log-prefix", "worker").stdout)

    def check_stopped(self, worker_logs: str) -> None:
        """Arret gracieux publie, aucun travail laisse en cours."""
        logs = parse_json_logs(worker_logs)
        check_log_contract(logs)
        stopped = [e for e in logs if e["event"] == "worker.stopped"]
        self.expect(len(stopped) == 1 and stopped[0].get("exit_code") == 0 and stopped[0].get("reason") == "stopped",
                    "worker: evenement worker.stopped propre attendu")
        self.expect(any(e["event"] == "worker.stopping" for e in logs), "worker: SIGTERM non recu")
        self.expect(self.scalar("SELECT count(*) FROM jobs WHERE status = 'running'") == "0",
                    "worker: travail laisse en cours")

    def check_leaks(self) -> None:
        self.step("fuites: sorties et logs de tous les conteneurs")
        self.check_transcript(self.compose("logs", "--no-color").stdout)

    def check_transcript(self, *extra: str) -> None:
        """Aucun mot de passe ni URL de connexion dans tout ce qui a ete publie."""
        leaks = find_leaks("\n".join(self.transcript + list(extra)), list(self.passwords.values()))
        self.expect(leaks == [], f"fuite detectee: {leaks}")

    def cleanup(self) -> None:
        if self.keep:
            self.log(f"container_smoke: pile conservee (--keep): docker compose -p {self.project} down -v")
            return
        self.step("nettoyage: conteneurs, reseaux et volumes du projet")
        self.run(["docker", "compose", "--project-name", self.project, "--file", str(COMPOSE_FILE),
                  "--profile", "ops", "down", "--volumes", "--remove-orphans", "--timeout", "60"],
                 check=False, timeout=300)

    def execute(self) -> None:
        self.prerequisites()
        self.check_image()
        try:
            self.start_postgres()
            self.refuse_unmigrated_schema()
            self.migrate()
            self.refuse_privileged_connections()
            principal = self.start_worker()
            self.check_data_volume()
            demo = self.demonstrate(principal)
            self.check_isolation(demo)
            self.stop_worker()
            self.check_leaks()
        except Exception:
            self.dump_logs()
            raise
        finally:
            self.cleanup()

    def dump_logs(self) -> None:
        try:
            logs = self.compose("logs", "--no-color", check=False).stdout
        except Exception:  # noqa: BLE001 - le diagnostic ne doit pas masquer l'echec
            return
        self.log(self.redact("--- logs des conteneurs (nettoyes) ---\n" + logs[-20000:]))
        # verdicts du HEALTHCHECK Docker (codes fixes publies par `mervio worker healthcheck`, sans secret)
        container = self.compose("ps", "--all", "--quiet", "worker", check=False).stdout.strip()
        if container:
            health = self.run(["docker", "inspect", "--format", "{{json .State.Health}}", container], check=False)
            self.log(self.redact("--- sante du worker ---\n" + health.stdout[-4000:]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test conteneurise de Mervio")
    parser.add_argument("--image", default="mervio:smoke", help="etiquette de l'image (defaut mervio:smoke)")
    parser.add_argument("--skip-build", action="store_true", help="utiliser une image deja construite")
    parser.add_argument("--keep", action="store_true", help="ne pas supprimer la pile a la fin")
    args = parser.parse_args(argv)
    smoke = Smoke(image=args.image, build=not args.skip_build, keep=args.keep)
    try:
        smoke.execute()
    except Prerequisite as exc:
        print(f"container_smoke: prerequis absent: {exc}", file=sys.stderr)
        return 2
    except SmokeFailure as exc:
        print(f"ECHEC: {smoke.redact(str(exc))}")
        return 1
    except subprocess.TimeoutExpired as exc:
        print(f"ECHEC: delai depasse: {smoke.redact(' '.join(map(str, exc.cmd)))}")
        return 1
    except Exception as exc:  # noqa: BLE001 - toute erreur inattendue est un echec, jamais un succes
        print(f"ECHEC: {type(exc).__name__}: {smoke.redact(str(exc))[:500]}")
        return 1
    print("container_smoke: smoke test reussi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
