"""Configuration typee et validee du worker (Mission 004.3).

Une seule source: les variables d'environnement `MERVIO_*`, lues et validees UNE fois
au demarrage. Toutes les erreurs sont rassemblees dans une seule `SettingsError`, qui
nomme les variables fautives et la regle violee, jamais leur valeur.

Aucun secret n'est code en dur, ni ecrit dans un log: l'URL de base est portee par
`Secret` (masque dans `repr`, `str` et `public()`), et `describe_database_url` n'en
publie que l'hote, le port, la base et le role.

Bibliotheque standard uniquement: ce module n'importe ni pilote de base ni persistance
(test de frontieres).

    Variable                                  Defaut        Regle
    MERVIO_ENV                                development   development | test | staging | production
    MERVIO_DATABASE_URL (ou ..._FILE)         -             OBLIGATOIRE; postgresql://role@hote[:port]/base
    MERVIO_WORKER_NAME                        nom d'hote    [A-Za-z0-9._-], 1 a 40 caracteres
    MERVIO_WORKER_JOB_TYPES                   tous          liste parmi import, analysis, purge
    MERVIO_WORKER_POLL_INTERVAL_SECONDS       1             0.05 a 60
    MERVIO_WORKER_POLL_MAX_SECONDS            5             >= intervalle, <= 60
    MERVIO_WORKER_DISPATCH_BATCH              50            1 a 1000
    MERVIO_JOB_LEASE_SECONDS                  300           30 a 86400
    MERVIO_JOB_LEASE_RENEW_SECONDS            bail / 3      1 a bail / 3
    MERVIO_WORKER_RECOVERY_INTERVAL_SECONDS   30            1 a 3600
    MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS      30            0 a 3600
    MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS     60            0 a 600
    MERVIO_JOB_BACKOFF_BASE_SECONDS           30            1 a 3600
    MERVIO_JOB_BACKOFF_CAP_SECONDS            3600          >= base, <= 86400
    MERVIO_WORKER_HEALTH_FILE                 aucun         chemin absolu
    MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS      60            >= 3 x max(poll max, 5), <= 3600
    MERVIO_WORKER_DEGRADED_GRACE_SECONDS      120           1 a 86400
    MERVIO_LOG_LEVEL                          INFO          DEBUG | INFO | WARNING | ERROR
    MERVIO_LOG_FORMAT                         json          json | text (text refuse en production)
    MERVIO_IDENTITY_MASTER_KEY (ou ..._FILE)  -             hexadecimal, 32 octets au moins; OBLIGATOIRE si le
                                                            worker traite des imports (cle maitre d'identite, D-053)

Le nom du principal de service n'est pas configurable: la base le derive du role de
connexion (revision 0007).
"""
from __future__ import annotations

import logging
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import unquote, urlsplit

from .errors import ConfigurationError
from .identity import IdentityKeyError, parse_hex_key

ENVIRONMENTS = ("development", "test", "staging", "production")
JOB_TYPES = ("import", "analysis", "purge")
LOG_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
LOG_FORMATS = ("json", "text")
#: Rythme du rafraichissement de sante pendant un travail (fil de bail, 004.3.5).
BUSY_HEARTBEAT_SECONDS = 5
_WORKER_NAME = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_URL_SCHEMES = ("postgresql", "postgres")


class SettingsError(ConfigurationError):
    """Configuration invalide. `problems`: (variable, regle), jamais de valeur."""

    def __init__(self, problems: List[Tuple[str, str]]) -> None:
        self.problems = list(problems)
        super().__init__("configuration invalide: " + "; ".join(f"{name}: {rule}" for name, rule in self.problems))


class Secret:
    """Valeur secrete: jamais affichee, jamais comparee par sa representation."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('[redacted]')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))

    def __reduce__(self):  # pragma: no cover - interdit la serialisation par pickle
        raise TypeError("un secret ne se serialise pas")


def describe_database_url(url: str) -> Dict[str, Any]:
    """Ce qu'on peut publier d'une URL de base: jamais le mot de passe ni les options."""
    parts = urlsplit(url)
    return {"host": parts.hostname or "", "port": parts.port or 5432,
            "database": unquote(parts.path.lstrip("/")), "role": unquote(parts.username or "")}


@dataclass(frozen=True)
class WorkerSettings:
    environment: str
    database_url: Secret = field(repr=False)
    worker_name: str
    job_types: Tuple[str, ...]
    poll_interval_seconds: float
    poll_max_seconds: float
    dispatch_batch: int
    lease_seconds: int
    lease_renew_seconds: int
    recovery_interval_seconds: int
    shutdown_grace_seconds: int
    startup_timeout_seconds: int
    backoff_base_seconds: int
    backoff_cap_seconds: int
    health_file: Optional[Path]
    health_max_age_seconds: int
    degraded_grace_seconds: int
    log_level: int
    log_format: str
    #: cle maitre d'identite (hexadecimal); jamais affichee, jamais en base (D-053)
    identity_master_key: Optional[Secret] = field(default=None, repr=False)

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None, *,
                 hostname: Callable[[], str] = socket.gethostname) -> "WorkerSettings":
        return _Reader(os.environ if environ is None else environ, hostname).settings()

    @property
    def production(self) -> bool:
        return self.environment == "production"

    def public(self) -> Dict[str, Any]:
        """Resume publiable (log `worker.config`): aucune valeur secrete, aucun chemin."""
        return {
            "environment": self.environment,
            "database": describe_database_url(self.database_url.reveal()),
            "worker_name": self.worker_name,
            "job_types": list(self.job_types),
            "poll_interval_seconds": self.poll_interval_seconds,
            "poll_max_seconds": self.poll_max_seconds,
            "dispatch_batch": self.dispatch_batch,
            "lease_seconds": self.lease_seconds,
            "lease_renew_seconds": self.lease_renew_seconds,
            "recovery_interval_seconds": self.recovery_interval_seconds,
            "shutdown_grace_seconds": self.shutdown_grace_seconds,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "backoff_base_seconds": self.backoff_base_seconds,
            "backoff_cap_seconds": self.backoff_cap_seconds,
            "health_check": self.health_file is not None,
            "health_max_age_seconds": self.health_max_age_seconds,
            "degraded_grace_seconds": self.degraded_grace_seconds,
            "log_level": logging.getLevelName(self.log_level),
            "log_format": self.log_format,
            "identity_master_configured": self.identity_master_key is not None,
        }


@dataclass(frozen=True)
class HealthCheckSettings:
    """Ce que lit `mervio worker healthcheck`: le fichier et ses tolerances, rien d'autre.

    Memes variables et memes regles que `WorkerSettings`, mais sans URL de base: un controle
    de sante ne doit ni exiger ni lire le secret de connexion.
    """

    health_file: Optional[Path]
    health_max_age_seconds: int
    degraded_grace_seconds: int

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "HealthCheckSettings":
        return _Reader(os.environ if environ is None else environ, socket.gethostname).health_check()


@dataclass(frozen=True)
class AdminSettings:
    """Ce que lit `mervio admin` (004.3.7): la connexion applicative, rien d'autre.

    Meme variable et memes regles que le worker (`MERVIO_DATABASE_URL` ou `..._FILE`). Le role
    connecte est un role applicatif ordinaire: `Database` refuse un superutilisateur ou BYPASSRLS.
    """

    database_url: Secret = field(repr=False)

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "AdminSettings":
        return _Reader(os.environ if environ is None else environ, socket.gethostname).admin()


class _Reader:
    """Lit et valide; rassemble TOUTES les erreurs avant d'echouer."""

    def __init__(self, environ: Mapping[str, str], hostname: Callable[[], str]) -> None:
        self.environ = environ
        self.hostname = hostname
        self.problems: List[Tuple[str, str]] = []

    # -- primitives ------------------------------------------------------------------------
    def raw(self, name: str) -> Optional[str]:
        value = self.environ.get(name)
        if value is None:
            return None
        value = value.strip()
        return value or None

    def fail(self, name: str, rule: str) -> None:
        self.problems.append((name, rule))

    def choice(self, name: str, default: str, allowed, *, normalize=str.lower) -> str:
        value = self.raw(name)
        if value is None:
            return default
        value = normalize(value)
        if value not in allowed:
            self.fail(name, "valeurs possibles: " + ", ".join(allowed))
            return default
        return value

    def number(self, name: str, default, minimum, maximum, *, kind=int):
        value = self.raw(name)
        if value is None:
            return default
        try:
            parsed = kind(value)
        except ValueError:
            self.fail(name, "entier attendu" if kind is int else "nombre attendu")
            return default
        if parsed != parsed or not minimum <= parsed <= maximum:  # NaN compris
            self.fail(name, f"entre {minimum} et {maximum}")
            return default
        return parsed

    # -- valeurs composees -----------------------------------------------------------------
    def database_url(self) -> Secret:
        name = "MERVIO_DATABASE_URL"
        direct, file_name = self.raw(name), self.raw(name + "_FILE")
        if direct and file_name:
            self.fail(name, f"definir {name} ou {name}_FILE, pas les deux")
            return Secret("")
        if file_name:
            path = Path(file_name)
            if not path.is_absolute():
                self.fail(name + "_FILE", "chemin absolu attendu")
                return Secret("")
            try:
                direct = path.read_text(encoding="utf-8").strip()
            except OSError:
                self.fail(name + "_FILE", "fichier illisible")
                return Secret("")
            name = name + "_FILE"
        if not direct:
            self.fail("MERVIO_DATABASE_URL", "obligatoire (aucune base par defaut)")
            return Secret("")
        try:
            parts = urlsplit(direct)
            parts.port  # noqa: B018 - valide le port
        except ValueError:
            self.fail(name, "URL invalide")
            return Secret("")
        if parts.scheme not in _URL_SCHEMES:
            self.fail(name, "URL postgresql:// attendue")
        elif not parts.hostname:
            self.fail(name, "hote obligatoire")
        elif not parts.username:
            self.fail(name, "role de connexion obligatoire")
        elif not parts.path.strip("/"):
            self.fail(name, "nom de base obligatoire")
        return Secret(direct)

    def identity_master_key(self, *, required: bool) -> Optional[Secret]:
        """Cle maitre d'identite (D-053): hexadecimal, au moins 32 octets. Jamais la valeur dans une erreur."""
        name = "MERVIO_IDENTITY_MASTER_KEY"
        direct, file_name = self.raw(name), self.raw(name + "_FILE")
        if direct and file_name:
            self.fail(name, f"definir {name} ou {name}_FILE, pas les deux")
            return None
        if file_name:
            path = Path(file_name)
            if not path.is_absolute():
                self.fail(name + "_FILE", "chemin absolu attendu")
                return None
            try:
                direct = path.read_text(encoding="utf-8").strip()
            except OSError:
                self.fail(name + "_FILE", "fichier illisible")
                return None
            name = name + "_FILE"
        if not direct:
            if required:
                self.fail("MERVIO_IDENTITY_MASTER_KEY", "obligatoire pour les travaux import (cle maitre d'identite)")
            return None
        try:
            parse_hex_key(direct)
        except IdentityKeyError:
            self.fail(name, "au moins 64 caracteres hexadecimaux (32 octets)")
            return None
        return Secret(direct)

    def worker_name(self) -> str:
        value = self.raw("MERVIO_WORKER_NAME")
        if value is None:
            value = re.sub(r"[^A-Za-z0-9._-]", "-", self.hostname() or "worker")[:40] or "worker"
        elif not _WORKER_NAME.match(value):
            self.fail("MERVIO_WORKER_NAME", "1 a 40 caracteres parmi lettres, chiffres, . _ -")
            return "worker"
        return value

    def job_types(self) -> Tuple[str, ...]:
        value = self.raw("MERVIO_WORKER_JOB_TYPES")
        if value is None:
            return JOB_TYPES
        kinds = [item.strip().lower() for item in value.split(",") if item.strip()]
        unknown = sorted(set(kinds) - set(JOB_TYPES))
        if not kinds or unknown:
            self.fail("MERVIO_WORKER_JOB_TYPES", "liste non vide parmi " + ", ".join(JOB_TYPES))
            return JOB_TYPES
        return tuple(kind for kind in JOB_TYPES if kind in kinds)

    def health_file(self) -> Optional[Path]:
        value = self.raw("MERVIO_WORKER_HEALTH_FILE")
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            self.fail("MERVIO_WORKER_HEALTH_FILE", "chemin absolu attendu")
            return None
        return path

    # -- assemblage ------------------------------------------------------------------------
    def poll(self) -> Tuple[float, float]:
        interval = self.number("MERVIO_WORKER_POLL_INTERVAL_SECONDS", 1.0, 0.05, 60.0, kind=float)
        maximum = self.number("MERVIO_WORKER_POLL_MAX_SECONDS", max(5.0, interval), 0.05, 60.0, kind=float)
        if maximum < interval:
            self.fail("MERVIO_WORKER_POLL_MAX_SECONDS", "superieur ou egal a MERVIO_WORKER_POLL_INTERVAL_SECONDS")
        return interval, maximum

    def health(self, poll_max: float) -> Tuple[Optional[Path], int, int]:
        health_file = self.health_file()
        heartbeat = max(poll_max, BUSY_HEARTBEAT_SECONDS)
        max_age = self.number("MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS", max(60, int(3 * heartbeat) + 1), 1, 3600)
        if max_age < 3 * heartbeat:
            self.fail("MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS",
                      "au moins trois fois le plus long intervalle de rafraichissement du worker")
        degraded_grace = self.number("MERVIO_WORKER_DEGRADED_GRACE_SECONDS", 120, 1, 86400)
        return health_file, max_age, degraded_grace

    def health_check(self) -> "HealthCheckSettings":
        _, poll_max = self.poll()
        health_file, max_age, degraded_grace = self.health(poll_max)
        if self.problems:
            raise SettingsError(self.problems)
        return HealthCheckSettings(health_file=health_file, health_max_age_seconds=max_age,
                                   degraded_grace_seconds=degraded_grace)

    def admin(self) -> "AdminSettings":
        database_url = self.database_url()
        if self.problems:
            raise SettingsError(self.problems)
        return AdminSettings(database_url=database_url)

    def settings(self) -> WorkerSettings:
        environment = self.choice("MERVIO_ENV", "development", ENVIRONMENTS)
        database_url = self.database_url()
        worker_name = self.worker_name()
        job_types = self.job_types()
        identity_master_key = self.identity_master_key(required="import" in job_types)
        poll_interval, poll_max = self.poll()
        dispatch_batch = self.number("MERVIO_WORKER_DISPATCH_BATCH", 50, 1, 1000)
        lease = self.number("MERVIO_JOB_LEASE_SECONDS", 300, 30, 86400)
        renew = self.number("MERVIO_JOB_LEASE_RENEW_SECONDS", max(1, lease // 3), 1, 86400)
        if renew > lease // 3:
            self.fail("MERVIO_JOB_LEASE_RENEW_SECONDS", "au plus un tiers de MERVIO_JOB_LEASE_SECONDS")
        recovery = self.number("MERVIO_WORKER_RECOVERY_INTERVAL_SECONDS", 30, 1, 3600)
        grace = self.number("MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS", 30, 0, 3600)
        startup = self.number("MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS", 60, 0, 600)
        backoff_base = self.number("MERVIO_JOB_BACKOFF_BASE_SECONDS", 30, 1, 3600)
        backoff_cap = self.number("MERVIO_JOB_BACKOFF_CAP_SECONDS", max(3600, backoff_base), 1, 86400)
        if backoff_cap < backoff_base:
            self.fail("MERVIO_JOB_BACKOFF_CAP_SECONDS", "superieur ou egal a MERVIO_JOB_BACKOFF_BASE_SECONDS")
        health_file, health_max_age, degraded_grace = self.health(poll_max)
        log_level = LOG_LEVELS[self.choice("MERVIO_LOG_LEVEL", "INFO", tuple(LOG_LEVELS), normalize=str.upper)]
        log_format = self.choice("MERVIO_LOG_FORMAT", "json", LOG_FORMATS)
        if environment == "production" and log_format != "json":
            self.fail("MERVIO_LOG_FORMAT", "json obligatoire en production")
        if environment == "production" and log_level == logging.DEBUG:
            self.fail("MERVIO_LOG_LEVEL", "DEBUG refuse en production")
        if self.problems:
            raise SettingsError(self.problems)
        return WorkerSettings(
            environment=environment, database_url=database_url, worker_name=worker_name, job_types=job_types,
            poll_interval_seconds=poll_interval, poll_max_seconds=poll_max, dispatch_batch=dispatch_batch,
            lease_seconds=lease, lease_renew_seconds=renew, recovery_interval_seconds=recovery,
            shutdown_grace_seconds=grace, startup_timeout_seconds=startup, backoff_base_seconds=backoff_base,
            backoff_cap_seconds=backoff_cap, health_file=health_file, health_max_age_seconds=health_max_age,
            degraded_grace_seconds=degraded_grace, log_level=log_level, log_format=log_format,
            identity_master_key=identity_master_key,
        )


__all__ = ["ENVIRONMENTS", "JOB_TYPES", "LOG_FORMATS", "AdminSettings", "HealthCheckSettings", "Secret",
           "SettingsError", "WorkerSettings", "describe_database_url"]
