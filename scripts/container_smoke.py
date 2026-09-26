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
#: Meme racine que le service worker du compose (004.4.4): obligatoire des que le worker traite des
#: imports, donc AUSSI pour les conteneurs ad-hoc de cette verification. Sans elle, ils meurent en
#: `worker.config_invalid` avant le controle qu'on veut eprouver. Le volume n'est pas monte ici: le
#: pilote ne touche au systeme de fichiers qu'au premier objet, jamais a la construction.
OBJECT_STORE_ROOT = "/var/lib/mervio/objects"
OBJECT_STORE_VARIABLE = f"MERVIO_OBJECT_STORE_ROOT={OBJECT_STORE_ROOT}"
#: Types de travaux des workers ad-hoc NEGATIFS (`refuse_privileged_connections`). `import` est
#: CONSERVE parce que c'est lui qui rend la cle maitre et le magasin d'objets OBLIGATOIRES: c'est
#: exactement ce que ces tests eprouvent, et les retirer dissoudrait leur premisse. En revanche
#: `redact_customer` est deliberement ABSENT: ces conteneurs n'ont pas le volume d'objets et tournent
#: avec une racine en LECTURE SEULE, donc le preflight de D-064 refuserait -- a juste titre -- pour
#: incapacite de destruction AVANT d'atteindre le refus d'identite ou de configuration teste ici.
#: Ce n'est pas un contournement du preflight: c'est la separation des scenarios negatifs.
NEGATIVE_JOB_TYPES = "MERVIO_WORKER_JOB_TYPES=import,analysis"
#: Codes des commandes du produit (workers.runtime, admin.errors).
WORKER_EXIT_OK, WORKER_EXIT_CONFIG, WORKER_EXIT_SCHEMA = 0, 2, 4
ADMIN_EXIT_DATABASE, ADMIN_EXIT_NOT_FOUND = 3, 5
ADMIN_EXIT_USAGE = 2
#: Forme canonique d'une reference client (004.4.5, D-062): le resolveur n'en produit pas d'autre.
CUSTOMER_REF = re.compile(r"^c1:[0-9a-f]{32}$")
#: Jeton de tombstone (D-056): aleatoire, jamais derive du HMAC du client.
TOMBSTONE = re.compile(r"^redacted:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
#: Types de source PORTEURS d'identite, detruits par un effacement client (D-056). Les autres non.
IDENTITY_BEARING = ("shopify_orders", "stripe")
#: Identites du generateur synthetique: structurelles, donc reproductibles sans lire un fichier.
#: Domaine reserve (`.invalid`): ces adresses ne peuvent designer personne de reel.
DEMO_IDENTITY = "c{index:07d}@customers.synthetic.invalid"
#: 1026 clients distincts pour 1500 commandes: une poignee de candidats suffit largement.
DEMO_CUSTOMER_CANDIDATES = 12
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

    def tail(self, result: Result, limit: int = 1200) -> str:
        """Sortie d'un conteneur ad-hoc, pour le message d'un refus INATTENDU.

        `docker run` n'apparait pas dans `docker compose logs`, et son flux est capture dans
        `Result`: sans cela, l'echec d'une de ces assertions ne dit PAS ce que le conteneur a
        publie. C'est ce qui a rendu le diagnostic de la CI 36258539901 indirect. Le texte est
        nettoye par `expect`, comme tout message de refus.
        """
        published = (result.stdout + result.stderr).strip()
        return f"\n--- sortie du conteneur ---\n{published[-limit:]}" if published else ""

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
        # `docker run -e NOM` lit la valeur dans l'environnement du client: aucun SECRET dans argv.
        # Une racine de magasin n'en est pas un, elle y figure en clair comme le montage du tmpfs.
        network = f"{self.project}_backend"
        superuser_url = (f"postgresql://{SUPERUSER}:{self.passwords['MERVIO_POSTGRES_PASSWORD']}"
                         f"@postgres:5432/{DATABASE}")
        result = self.run(["docker", "run", "--rm", "--network", network, "--read-only", "--tmpfs", HEALTH_TMPFS,
                           "-e", "MERVIO_DATABASE_URL", "-e", IDENTITY_KEY_VARIABLE,
                           "-e", OBJECT_STORE_VARIABLE, "-e", NEGATIVE_JOB_TYPES, self.image, "worker"],
                          env=dict(self.environment, MERVIO_DATABASE_URL=superuser_url), check=False, timeout=300)
        self.expect(result.returncode == WORKER_EXIT_CONFIG,
                    f"worker superutilisateur: code {result.returncode}, 2 attendu{self.tail(result)}")
        self.expect("worker.identity_refused" in result.stdout + result.stderr,
                    f"worker superutilisateur: evenement worker.identity_refused attendu{self.tail(result)}")
        self.expect(self.scalar("SELECT count(*) FROM users WHERE kind = 'service'") == "0",
                    "worker superutilisateur: un principal a ete enregistre")
        # 004.4.2: sans cle maitre d'identite, le worker (qui traite les imports) refuse de demarrer
        worker_url = (f"postgresql://{WORKER_ROLE}:{self.passwords['MERVIO_WORKER_DB_PASSWORD']}"
                      f"@postgres:5432/{DATABASE}")
        result = self.run(["docker", "run", "--rm", "--network", network, "--read-only", "--tmpfs", HEALTH_TMPFS,
                           "-e", "MERVIO_DATABASE_URL", "-e", OBJECT_STORE_VARIABLE,
                           "-e", NEGATIVE_JOB_TYPES, self.image, "worker"],
                          env=dict(self.environment, MERVIO_DATABASE_URL=worker_url), check=False, timeout=300)
        self.expect(result.returncode == WORKER_EXIT_CONFIG,
                    f"worker sans cle d'identite: code {result.returncode}, 2 attendu{self.tail(result)}")
        self.expect("worker.config_invalid" in result.stdout + result.stderr
                    and IDENTITY_KEY_VARIABLE in result.stdout + result.stderr,
                    f"worker sans cle d'identite: refus de configuration nommant la variable "
                    f"attendu{self.tail(result)}")
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

    def check_resolution(self, demo: Dict[str, str]) -> None:
        """Frontiere de D-062 dans le vrai runtime (004.4.5 E6): qui peut resoudre, et qui ne peut pas.

        Le resolveur est une commande PONCTUELLE, lancee dans un conteneur jetable du service
        `worker` (c'est lui, et lui seul, qui porte la cle maitre). Cette etape porte uniquement sur
        la FRONTIERE: qui peut resoudre, qui ne peut pas, et ce que la resolution n'ecrit pas.
        L'effacement lui-meme -- mise en file, execution par le VRAI worker, destruction physique des
        octets -- est la gate de D-064 et appartient a `check_erasure`.
        """
        self.step("resolution: le worker resout, admin ne peut pas (D-062)")
        organization = identifier(demo["organization"])
        identity = "smoke-resolve@example.invalid"
        # etat AVANT toute resolution: c'est lui qui prouve qu'elle n'ecrit rien
        salts = self.scalar("SELECT count(*) FROM organization_identity_keys")
        events = self.scalar("SELECT count(*) FROM audit_events")

        # 1. le worker resout: la derniere ligne de stdout est la reference, et rien d'autre
        first = self.resolve(organization, identity)
        self.expect(CUSTOMER_REF.fullmatch(first), f"resolution: sortie inattendue {first!r}")

        # 2. deterministe: meme identite, meme organisation, meme reference
        self.expect(self.resolve(organization, identity) == first, "resolution: non deterministe")
        # ... et l'identite n'est pas la reference: la casse ne change rien, l'identite ne sort pas
        self.expect(self.resolve(organization, identity.upper()) == first,
                    "resolution: la normalisation ne suit pas celle de l'import")
        # 3. la resolution n'a RIEN ecrit: aucun sel cree, aucun evenement d'audit (E' non adoptee)
        self.expect(self.scalar("SELECT count(*) FROM organization_identity_keys") == salts,
                    "resolution: une ligne de sel a ete creee ou supprimee")
        self.expect(self.scalar("SELECT count(*) FROM audit_events") == events,
                    "resolution: un evenement d'audit de resolution a ete ecrit")

        # 4. stdin, jamais argv: l'option n'existe nulle part
        helped = self.compose("run", "--rm", "-T", "--entrypoint", "mervio", "worker",
                              "worker", "resolve-customer-ref", "--help")
        self.expect("--email" not in helped.stdout and "STDIN" in helped.stdout,
                    "resolution: l'aide n'impose pas stdin, ou propose une identite en argument")

        # 5. `admin` n'a PAS la cle maitre (et `migrate` non plus), le worker SI
        self.expect(self.holds_identity_key("worker"), "frontiere: le worker n'a pas la cle maitre")
        for service in ("admin", "migrate"):
            self.expect(not self.holds_identity_key(service),
                        f"frontiere: {service} recoit la cle maitre d'identite")

        # ... et `admin` n'accepte AUCUNE identite comme reference
        refused = self.admin("job", "enqueue-redact", "--as", OWNER, "--org", organization,
                             "--customer-ref", identity, expect_code=ADMIN_EXIT_USAGE)
        self.expect(refused["code"] == "invalid_input", f"admin: une identite acceptee comme reference: {refused}")

        # La mise en file et l'EXECUTION appartiennent a `check_erasure`: le worker tourne desormais
        # pendant cette etape, un travail mis en file ici serait pris immediatement.

    # -- gate de D-064: effacement client REELLEMENT execute dans le conteneur ------------------

    def check_erasure(self, demo: Dict[str, str]) -> None:
        """LA gate de D-064: le VRAI worker execute `redact_customer` et les octets DISPARAISSENT.

        Jusqu'a D-064, ce parcours n'etait jamais execute ici: le smoke mettait un travail en file
        et n'attendait pas. C'est exactement ce qui a laisse passer la contradiction entre le
        montage `:ro` du worker (D-054) et l'obligation de detruire les octets (D-056) -- une CI
        verte ne disait rien du chemin d'effacement.

        Ce controle prouve la DESTRUCTION PHYSIQUE, pas seulement le passage de la ligne a `purged`:
        les octets sont constates presents avant, puis absents apres, DANS le volume partage.
        """
        self.step("effacement: le vrai worker detruit les octets porteurs d'identite (D-064)")
        organization = identifier(demo["organization"])

        # a. une identite REELLE et deterministe du jeu de demonstration, resolue par le worker
        identity, reference, orders = self.demo_customer(organization)

        # b. etat AVANT: les objets porteurs d'identite ont des octets sur le volume
        before = self.raw_objects()
        present = self.stored_object_keys()
        identity_bearing = {kind: row for kind, row in before.items() if kind in IDENTITY_BEARING}
        other = {kind: row for kind, row in before.items() if kind not in IDENTITY_BEARING}
        self.expect(len(identity_bearing) >= 1, f"aucun objet porteur d'identite a effacer: {before}")
        for kind, row in before.items():
            self.expect(row["state"] == "available", f"{kind}: etat initial {row['state']}")
            self.expect(row["object_key"] in present, f"{kind}: aucun octet avant l'effacement")
        tombstones_before = self.scalar("SELECT count(*) FROM customer_redactions")

        # c. mise en file: la charge ne transporte qu'un HMAC, jamais l'identite
        job = self.enqueue_erasure(organization, reference, identity, key="smoke-redact")

        # d. le VRAI worker prend et execute le travail
        finished = self.wait_job(organization, job)
        self.expect(finished["status"] == "succeeded",
                    f"effacement: {finished['status']} {finished.get('last_error_code')}")
        # Les compteurs `raw_objects_*` du resultat NE SONT PAS lisibles ici, et c'est VOULU:
        # `render_job` passe le resultat dans `scrub`, qui masque toute cle contenant "raw" pour
        # qu'aucune donnee brute ne ressorte par la CLI. On ne contourne pas ce filtre -- la preuve
        # de destruction est prise a la source: l'etat des lignes et le CONTENU DU VOLUME, ci-dessous.
        # `orders_tombstoned`, lui, n'est pas masque et corrobore le nombre de commandes touchees.
        self.expect(finished["result"]["orders_tombstoned"] == orders,
                    f"commandes tombstonees: {finished['result']}, {orders} attendues")

        # e. PREUVE PHYSIQUE: les octets porteurs d'identite ont quitte le volume
        after = self.stored_object_keys()
        for kind, row in identity_bearing.items():
            self.expect(row["object_key"] not in after,
                        f"{kind}: les octets sont TOUJOURS sur le volume apres l'effacement")
        for kind, row in other.items():
            self.expect(row["object_key"] in after,
                        f"{kind}: un objet SANS identite a ete detruit (D-056 l'interdit)")

        # f. preuve en base: etats, tombstone, preuve, metadonnees conservees
        rows = self.raw_objects()
        for kind, row in rows.items():
            if kind in IDENTITY_BEARING:
                self.expect(row["state"] == "purged", f"{kind}: etat {row['state']}, purged attendu")
                self.expect(row["purged_at"] not in ("", None), f"{kind}: purged_at absent")
                # la ligne SURVIT avec ses metadonnees non sensibles (D-056)
                self.expect(row["sha256"] and row["byte_size"] not in ("", None),
                            f"{kind}: metadonnees non sensibles perdues")
            else:
                self.expect(row["state"] == "available", f"{kind}: etat {row['state']} inattendu")
        self.expect(len(rows) == len(before), "une ligne raw_objects a disparu: elles sont conservees")

        tombstoned = self.sql("SELECT DISTINCT customer_ref FROM orders WHERE customer_ref LIKE 'redacted:%'")
        self.expect(len(tombstoned) == 1 and TOMBSTONE.fullmatch(tombstoned[0][0]),
                    f"tombstone absent ou hors forme: {tombstoned}")
        self.expect(self.scalar(f"SELECT count(*) FROM orders WHERE customer_ref = '{reference}'") == "0",
                    "la reference effacee subsiste dans les lignes canoniques")
        self.expect(self.scalar(f"SELECT count(*) FROM orders WHERE customer_ref = '{tombstoned[0][0]}'")
                    == str(orders), "le nombre de commandes tombstonees ne correspond pas")
        proof = self.sql("SELECT customer_ref, redacted_ref FROM customer_redactions")
        self.expect(proof == [[reference, tombstoned[0][0]]], f"preuve d'effacement inattendue: {proof}")
        self.expect(int(self.scalar("SELECT count(*) FROM customer_redactions"))
                    == int(tombstones_before) + 1, "preuve non ecrite, ou ecrite en double")

        # g. audit: l'effacement est trace, sans identite
        actions = [row[0] for row in self.sql(
            "SELECT action FROM audit_events WHERE resource_type IN ('job', 'raw_object') "
            "ORDER BY created_at")]
        self.expect(any("redact" in action or "purge" in action for action in actions),
                    f"aucun evenement d'audit d'effacement: {actions}")

        # h. AUCUNE PII nulle part: ni charge utile, ni erreur, ni audit, ni ligne canonique
        self.check_no_identity(identity, reference)

        # i. isolation: l'organisation etrangere de `check_isolation` n'est pas touchee
        self.expect(self.scalar("SELECT count(*) FROM raw_objects WHERE state = 'purged'")
                    == str(len(identity_bearing)),
                    "des objets d'une autre organisation ont ete detruits")

        # j. rejeu: rejouer le MEME effacement ne resurrecte rien et ne double aucune preuve
        self.check_erasure_replay(organization, reference, identity, tombstoned[0][0], after)

    def check_erasure_replay(self, organization: str, reference: str, identity: str,
                             tombstone: str, stored: set) -> None:
        """Rejouer l'effacement est SANS EFFET: ni resurrection, ni seconde preuve, ni echec.

        La fenetre exacte "octets detruits, ligne encore `purging`" n'est pas reproductible de
        maniere deterministe ici -- il faudrait interrompre le worker au milieu d'une transaction.
        Elle est prouvee en processus par
        tests/persistence/test_customer_erasure_handler.py::test_a_retry_after_the_bytes_vanished_still_finalizes_the_row.
        Ce qui est prouve ICI, dans le vrai conteneur, est la propriete que l'operateur constate:
        un rejeu aboutit, ne restaure aucun octet et ne cree aucune seconde preuve.
        """
        job = self.enqueue_erasure(organization, reference, identity, key="smoke-redact-replay")
        finished = self.wait_job(organization, job)
        self.expect(finished["status"] == "succeeded",
                    f"rejeu de l'effacement: {finished['status']} {finished.get('last_error_code')}")
        # meme raison qu'au-dessus: le compteur est masque par `scrub`. Ce qui prouve qu'aucune
        # destruction supplementaire n'a eu lieu, c'est que le volume est IDENTIQUE octet pour octet
        # et que les lignes sont restees `purged` -- deux constats pris a la source.
        self.expect(self.stored_object_keys() == stored, "le rejeu a fait revivre ou detruit des octets")
        replayed = self.raw_objects()
        for kind, row in replayed.items():
            expected = "purged" if kind in IDENTITY_BEARING else "available"
            self.expect(row["state"] == expected, f"{kind}: etat {row['state']} apres le rejeu")
            if kind in IDENTITY_BEARING:
                self.expect(row["purged_at"] not in ("", None), f"{kind}: purged_at perdu au rejeu")
        self.expect(self.scalar("SELECT count(*) FROM customer_redactions") == "1",
                    "le rejeu a ecrit une seconde preuve")
        self.expect(self.scalar(f"SELECT count(*) FROM orders WHERE customer_ref = '{reference}'") == "0",
                    "le rejeu a fait revivre la reference effacee")
        self.expect(self.scalar("SELECT count(*) FROM orders WHERE customer_ref LIKE 'redacted:%'")
                    != "0" and self.scalar(
                        f"SELECT count(*) FROM orders WHERE customer_ref = '{tombstone}'") != "0",
                    "le tombstone a change au rejeu")

    # -- gate de D-064: le preflight face a un montage REELLEMENT en lecture seule ---------------

    def check_delete_preflight(self) -> None:
        """Montage `:ro` REEL -> EROFS reel -> le worker refuse de demarrer (D-064).

        Ce n'est pas une permission 0500 simulee: le volume est monte en lecture seule par le noyau,
        exactement comme il l'etait pour le worker avant D-064. Le refus doit arriver AVANT toute
        connexion, et c'est verifiable: aucun evenement de base n'est publie, et aucun principal de
        service n'apparait pour ce conteneur.

        L'URL de base est syntaxiquement valide mais injoignable et SANS mot de passe: si le
        preflight ne refusait pas, l'echec suivant serait une panne de base (code 3), ce qui
        distingue nettement les deux refus -- et aucun secret n'entre ni dans argv ni dans
        l'environnement de ce conteneur.
        """
        self.step("preflight: un magasin d'objets en lecture seule refuse de servir l'effacement")
        volume = f"{self.project}_mervio_objects"
        untouched = self.stored_object_keys()
        unreachable = f"postgresql://{WORKER_ROLE}@127.0.0.1:1/{DATABASE}"
        result = self.run(["docker", "run", "--rm", "--read-only", "--tmpfs", HEALTH_TMPFS,
                           "--volume", f"{volume}:{OBJECT_STORE_ROOT}:ro",
                           "-e", "MERVIO_DATABASE_URL", "-e", IDENTITY_KEY_VARIABLE,
                           "-e", OBJECT_STORE_VARIABLE, "-e", "MERVIO_WORKER_JOB_TYPES=redact_customer",
                           self.image, "worker"],
                          env=dict(self.environment, MERVIO_DATABASE_URL=unreachable),
                          check=False, timeout=300)
        published = result.stdout + result.stderr
        self.expect(result.returncode == WORKER_EXIT_CONFIG,
                    f"preflight: code {result.returncode}, {WORKER_EXIT_CONFIG} attendu (EROFS reel)")
        self.expect("worker.object_store_incapable" in published,
                    f"preflight: evenement worker.object_store_incapable attendu: {published[-600:]}")
        self.expect("incapable" in published, "preflight: la capacite constatee n'est pas publiee")
        # le refus PRECEDE la base: aucune tentative de connexion n'est publiee
        for forbidden in ("worker.database_connected", "worker.database_unavailable", "worker.ready"):
            self.expect(forbidden not in published, f"preflight: {forbidden} publie avant le refus")
        # ni chemin de racine, ni valeur: le refus ne nomme que la regle et le pilote
        self.expect(OBJECT_STORE_ROOT not in published,
                    "preflight: la racine du magasin est publiee dans le refus")
        # aucun effet destructif: le volume est EXACTEMENT celui d'avant le conteneur refuse
        self.expect(self.stored_object_keys() == untouched,
                    "preflight: le contenu du volume a change alors que le worker a refuse de demarrer")

        # ... et un worker qui ne sert PAS l'effacement demarre malgre le montage en lecture seule
        result = self.run(["docker", "run", "--rm", "--read-only", "--tmpfs", HEALTH_TMPFS,
                           "--volume", f"{volume}:{OBJECT_STORE_ROOT}:ro",
                           "-e", "MERVIO_DATABASE_URL", "-e", IDENTITY_KEY_VARIABLE,
                           "-e", OBJECT_STORE_VARIABLE, "-e", "MERVIO_WORKER_JOB_TYPES=import,analysis",
                           self.image, "worker"],
                          env=dict(self.environment, MERVIO_DATABASE_URL=unreachable),
                          check=False, timeout=300)
        self.expect("worker.object_store_incapable" not in result.stdout + result.stderr,
                    "preflight: un worker sans effacement ne doit pas etre soumis au controle")

    # -- outils de la gate ----------------------------------------------------------------------

    def demo_customer(self, organization: str):
        """Un client REEL et deterministe du jeu de demonstration, resolu par le worker.

        Les identites du generateur synthetique sont structurelles (`c<7 chiffres>@<domaine
        reserve>`), donc reproductibles sans lire aucun fichier. La correspondance est VERIFIEE en
        base avant l'effacement: on n'efface pas une reference qui ne designe personne, sinon
        l'assertion de tombstone ne prouverait rien.
        """
        for index in range(1, DEMO_CUSTOMER_CANDIDATES + 1):
            identity = DEMO_IDENTITY.format(index=index)
            reference = self.resolve(organization, identity)
            self.expect(CUSTOMER_REF.fullmatch(reference), f"resolution: sortie inattendue {reference!r}")
            orders = int(self.scalar(
                f"SELECT count(*) FROM orders WHERE customer_ref = '{reference}'"))
            if orders:
                return identity, reference, orders
        raise SmokeFailure("aucune identite de demonstration ne correspond a une commande importee")

    def raw_objects(self) -> Dict[str, dict]:
        """Toutes les lignes `raw_objects`, par type de source.

        Lue en superutilisateur du conteneur, donc SANS filtre d'organisation: c'est voulu, cela
        permet d'affirmer qu'AUCUNE autre organisation n'a vu ses objets touches.
        """
        rows = self.sql("SELECT source_kind, object_key, state, coalesce(purged_at::text, ''), "
                        "sha256, byte_size FROM raw_objects ORDER BY source_kind")
        return {row[0]: {"object_key": row[1], "state": row[2], "purged_at": row[3],
                         "sha256": row[4], "byte_size": row[5]} for row in rows}

    def stored_object_keys(self) -> set:
        """Cles REELLEMENT presentes sur le volume, lues dans un conteneur jetable.

        C'est la preuve PHYSIQUE: la base peut dire `purged`, seul le volume dit si les octets sont
        partis. Les fichiers vides comptent comme presents -- une troncature n'est pas une
        destruction (raison pour laquelle D-064 a ecarte l'option B1).
        """
        listing = self.compose("run", "--rm", "-T", "--entrypoint", "sh", "worker", "-c",
                               f"find {OBJECT_STORE_ROOT} -type f -print 2>/dev/null | "
                               f"sed 's|^{OBJECT_STORE_ROOT}/||' || true")
        return {line.strip() for line in listing.stdout.splitlines()
                if line.strip().startswith("org/")}

    def enqueue_erasure(self, organization: str, reference: str, identity: str, *, key: str) -> str:
        """Met en file l'effacement et verifie que la charge ne porte QU'UN HMAC."""
        enqueued = self.admin("job", "enqueue-redact", "--as", OWNER, "--org", organization,
                              "--customer-ref", reference, "--idempotency-key", key)
        job = identifier(enqueued["job"]["id"])
        payload = self.scalar(f"SELECT payload::text FROM jobs WHERE id = '{job}'")
        self.expect(payload == '{"customer_ref": "%s"}' % reference, f"charge utile inattendue: {payload}")
        self.expect(identity not in payload, "charge utile: l'identite du client y figure")
        return job

    def check_no_identity(self, identity: str, reference: str) -> None:
        """L'identite du client ne doit apparaitre NULLE PART: ni en base, ni en log, ni en audit."""
        local = identity.split("@")[0]
        for column, table in (("payload::text", "jobs"), ("coalesce(result::text, '')", "jobs"),
                              ("coalesce(last_error, '')", "jobs"),
                              ("coalesce(metadata::text, '')", "audit_events"),
                              ("customer_ref", "orders"), ("object_key", "raw_objects")):
            found = self.scalar(f"SELECT count(*) FROM {table} WHERE {column} LIKE '%{local}%'")
            self.expect(found == "0", f"identite du client trouvee dans {table}.{column}")
        logs = self.compose("logs", "--no-color", "--no-log-prefix", "worker").stdout
        self.expect(identity not in logs and local not in logs, "identite du client dans les logs du worker")
        self.expect(reference not in logs, "reference client dans les logs du worker")

    def holds_identity_key(self, service: str) -> bool:
        """La cle maitre est-elle dans l'environnement de ce service? (D-062)

        Le test se fait DANS le conteneur et ne publie qu'un mot: ni la valeur, ni le reste de
        l'environnement. Un `env` complet ferait entrer `MERVIO_DATABASE_URL`, mot de passe inclus,
        dans le journal du smoke -- c'est-a-dire creerait la fuite qu'il est cense chercher.
        """
        probe = f'test -n "${{{IDENTITY_KEY_VARIABLE}:-}}" && echo PRESENT || echo ABSENT'
        result = self.compose("run", "--rm", "-T", "--entrypoint", "sh", service, "-c", probe)
        verdict = [line.strip() for line in result.stdout.splitlines() if line.strip() in ("PRESENT", "ABSENT")]
        self.expect(len(verdict) == 1, f"frontiere: sonde d'environnement muette pour {service}")
        return verdict[0] == "PRESENT"

    def resolve(self, organization: str, identity: str) -> str:
        """`worker resolve-customer-ref`: l'identite entre par STDIN, la reference sort sur STDOUT.

        Derniere ligne non vide de stdout, comme `parse_admin`: `docker compose run` publie ses
        propres messages de cycle de vie, qui ne sont pas la sortie du produit. Ce qui est verifie
        ici est donc ce que le produit ecrit -- une reference, aucun refus, et jamais l'identite.
        """
        result = self.compose("run", "--rm", "-T", "--entrypoint", "mervio", "worker",
                              "worker", "resolve-customer-ref", "--org", organization, "--as", OWNER,
                              stdin=identity + "\n")
        published = result.stdout + result.stderr
        self.expect("resolution refusee" not in published,
                    f"resolution: refusee alors qu'elle devait aboutir: {result.stderr[-500:]}")
        # l'identite entre par stdin et ne doit ressortir NULLE PART (D-062)
        for form in (identity, identity.lower(), identity.upper()):
            self.expect(form not in published, "resolution: l'identite fournie apparait dans une sortie")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        self.expect(bool(lines), f"resolution: aucune sortie (code {result.returncode})")
        return lines[-1]

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
            self.check_resolution(demo)
            self.check_erasure(demo)
            self.stop_worker()
            self.check_delete_preflight()
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
