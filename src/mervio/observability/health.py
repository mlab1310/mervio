"""Sante et disponibilite du worker par fichier (Mission 004.3). Aucun serveur HTTP.

Le worker ecrit regulierement un petit document JSON (ecriture atomique); une commande
separee (`mervio worker healthcheck`, 004.3.6) le lit et rend un verdict, sans jamais
interroger la base: un controle de sante ne doit ni charger PostgreSQL, ni echouer a
tort parce qu'une connexion de controle est refusee.

    {"version": 1, "state": "ready", "updated_at": 1789000000.0, "started_at": ...,
     "pid": 42, "worker_id": "host/42/ab12cd34", "jobs_processed": 7,
     "last_database_ok_at": ..., "degraded_since": null}

Ce que le document NE contient jamais: organisation, travail, boutique, URL de base,
chemin, message d'exception. Seulement un etat, des instants et des compteurs.

Verdict (`evaluate`):

    vivant (liveness)   le document est recent et l'etat n'est ni `stopped`, ni `degraded`
                        depuis plus que la tolerance
    pret (readiness)    vivant ET etat `ready` ou `busy`

Les raisons publiees sont des codes fixes (`stale`, `missing`, `invalid`...), jamais un
message d'erreur du systeme de fichiers (il contiendrait le chemin).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional

HEALTH_SCHEMA_VERSION = 1
#: Decalage d'horloge tolere pour un document date du futur.
CLOCK_SKEW_SECONDS = 5.0
MAX_HEALTH_FILE_BYTES = 4096


class WorkerState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    DRAINING = "draining"
    DEGRADED = "degraded"
    STOPPED = "stopped"


@dataclass(frozen=True)
class HealthSnapshot:
    version: int
    state: str
    updated_at: float
    started_at: float
    pid: int
    worker_id: str
    jobs_processed: int
    last_database_ok_at: Optional[float]
    degraded_since: Optional[float]

    def to_document(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HealthVerdict:
    live: bool
    ready: bool
    #: code fixe et publiable
    reason: str
    state: Optional[str] = None


class HealthFileError(Exception):
    """Document de sante absent, illisible ou non conforme. `code` seul est publiable."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_FIELDS = {
    "version": int, "state": str, "updated_at": (int, float), "started_at": (int, float), "pid": int,
    "worker_id": str, "jobs_processed": int, "last_database_ok_at": (int, float, type(None)),
    "degraded_since": (int, float, type(None)),
}


def write_health(path: Path, snapshot: HealthSnapshot) -> None:
    """Ecriture atomique: un lecteur voit l'ancien document ou le nouveau, jamais un melange."""
    path = Path(path)
    data = json.dumps(snapshot.to_document(), sort_keys=True).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".health-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_health(path: Path) -> HealthSnapshot:
    """Lecture stricte: tout champ manquant, inconnu ou mal type est un refus."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_HEALTH_FILE_BYTES + 1)
    except FileNotFoundError:
        raise HealthFileError("missing") from None
    except OSError:
        raise HealthFileError("unreadable") from None
    if len(data) > MAX_HEALTH_FILE_BYTES:
        raise HealthFileError("invalid")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HealthFileError("invalid") from None
    if not isinstance(document, dict) or set(document) != set(_FIELDS):
        raise HealthFileError("invalid")
    for name, kind in _FIELDS.items():
        value = document[name]
        if isinstance(value, bool) or not isinstance(value, kind):
            raise HealthFileError("invalid")
    if document["version"] != HEALTH_SCHEMA_VERSION:
        raise HealthFileError("unsupported_version")
    if document["state"] not in {state.value for state in WorkerState}:
        raise HealthFileError("invalid")
    return HealthSnapshot(**document)


def evaluate(snapshot: HealthSnapshot, *, now: float, max_age_seconds: float,
             degraded_grace_seconds: float) -> HealthVerdict:
    state = snapshot.state
    age = now - snapshot.updated_at
    if age > max_age_seconds:
        return HealthVerdict(False, False, "stale", state)
    if age < -CLOCK_SKEW_SECONDS:
        return HealthVerdict(False, False, "clock_skew", state)
    if state == WorkerState.STOPPED.value:
        return HealthVerdict(False, False, "stopped", state)
    if state == WorkerState.DEGRADED.value:
        since = snapshot.degraded_since if snapshot.degraded_since is not None else snapshot.updated_at
        if now - since > degraded_grace_seconds:
            return HealthVerdict(False, False, "database_unavailable", state)
        return HealthVerdict(True, False, "degraded", state)
    if state in (WorkerState.STARTING.value, WorkerState.DRAINING.value):
        return HealthVerdict(True, False, state, state)
    return HealthVerdict(True, True, "ok", state)


def check_health(path: Optional[Path], *, max_age_seconds: float, degraded_grace_seconds: float,
                 now: Optional[float] = None) -> HealthVerdict:
    """Verdict sans jamais lever: toute anomalie devient un code."""
    if path is None:
        return HealthVerdict(False, False, "not_configured")
    try:
        snapshot = read_health(Path(path))
    except HealthFileError as error:
        return HealthVerdict(False, False, error.code)
    return evaluate(snapshot, now=time.time() if now is None else now, max_age_seconds=max_age_seconds,
                    degraded_grace_seconds=degraded_grace_seconds)


class HealthReporter:
    """Etat de sante d'un worker, publie dans son fichier. Partageable entre fils.

    Sans fichier configure, il tient l'etat en memoire et n'ecrit rien. Une ecriture
    impossible ne fait jamais tomber le worker: elle est comptee (`write_failures`), et le
    document devenu ancien rend le worker non sain, ce qui est le signal voulu.
    """

    def __init__(self, path: Optional[Path], *, worker_id: str, pid: Optional[int] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        started = clock()
        self._snapshot = HealthSnapshot(
            version=HEALTH_SCHEMA_VERSION, state=WorkerState.STARTING.value, updated_at=started,
            started_at=started, pid=os.getpid() if pid is None else pid, worker_id=str(worker_id)[:100],
            jobs_processed=0, last_database_ok_at=None, degraded_since=None)
        self.write_failures = 0

    @property
    def snapshot(self) -> HealthSnapshot:
        with self._lock:
            return self._snapshot

    def _update(self, changes: Callable[[HealthSnapshot, float], Dict[str, Any]]) -> HealthSnapshot:
        """Lecture, calcul et ecriture sous le MEME verrou: aucune mise a jour perdue."""
        with self._lock:
            now = self._clock()
            self._snapshot = replace(self._snapshot, updated_at=now, **changes(self._snapshot, now))
            snapshot = self._snapshot
            if self.path is not None:
                try:
                    write_health(self.path, snapshot)
                except OSError:
                    self.write_failures += 1
        return snapshot

    def beat(self) -> HealthSnapshot:
        return self._update(lambda current, now: {})

    def set_state(self, state: WorkerState) -> HealthSnapshot:
        state = WorkerState(state)

        def changes(current: HealthSnapshot, now: float) -> Dict[str, Any]:
            if state is WorkerState.DEGRADED:
                return {"state": state.value,
                        "degraded_since": current.degraded_since if current.degraded_since is not None else now}
            return {"state": state.value, "degraded_since": None}

        return self._update(changes)

    def database_ok(self) -> HealthSnapshot:
        def changes(current: HealthSnapshot, now: float) -> Dict[str, Any]:
            update: Dict[str, Any] = {"last_database_ok_at": now}
            if current.state == WorkerState.DEGRADED.value:
                update.update(state=WorkerState.READY.value, degraded_since=None)
            return update

        return self._update(changes)

    def database_unavailable(self) -> HealthSnapshot:
        return self.set_state(WorkerState.DEGRADED)

    def job_finished(self) -> HealthSnapshot:
        return self._update(lambda current, now: {"jobs_processed": current.jobs_processed + 1})


__all__ = [
    "HEALTH_SCHEMA_VERSION", "HealthFileError", "HealthReporter", "HealthSnapshot", "HealthVerdict", "WorkerState",
    "check_health", "evaluate", "read_health", "write_health",
]
