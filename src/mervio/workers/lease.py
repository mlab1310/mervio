"""Gardien de bail d'une tentative (Mission 004.3).

Pendant qu'un gestionnaire travaille sur la connexion PRINCIPALE du worker, un fil
dedie renouvelle le bail sur sa PROPRE connexion (une `Database` n'est jamais partagee
entre fils). Il ne reinvente rien: il appelle `jobs.renew_lease`, dont la base garantit
le jeton d'exclusion `(attempts, locked_by)` (revision 0006).

    renouvellement reussi   nouvelle echeance locale = debut de la tentative + bail
    JobLeaseLost            la tentative est perimee (reprise, terminee, autre detenteur)
    erreur transitoire      nouvelle tentative rapide, connexion du gardien reinitialisee
    echeance locale passee  bail considere perdu, meme si la base n'a rien repris

Un bail perdu est DEFINITIF pour la tentative: `lost` passe a vrai, `on_lost` est appele
(le worker annule l'instruction en cours sur sa connexion principale), `checkpoint()`
leve `JobLeaseLost`, et le worker ne publie plus rien - ni succes, ni echec, ni remise en
file. La reprise du travail appartient a un autre worker, apres expiration du bail.

L'echeance locale est volontairement pessimiste: elle part de l'instant ou la demande
de renouvellement a ete ENVOYEE, alors que la base compte depuis sa reception.

Execution AU MOINS une fois: ce gardien empeche un ancien worker de PUBLIER, il
n'empeche pas qu'un travail soit execute deux fois.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from ..observability.logging import EventLogger, bind_context, get_event_logger
from ..persistence.errors import JobLeaseLost
from ..persistence.jobs import JobRecord
from ..persistence.retry import database_error_code

#: Rafraichissement de la sante pendant un travail (voir settings.BUSY_HEARTBEAT_SECONDS).
DEFAULT_HEARTBEAT_SECONDS = 5.0

LOST_REASONS = ("lease_lost", "lease_expired", "worker_shutdown")


def _noop(*args, **kwargs) -> None:
    return None


class LeaseKeeper:
    """Renouvelle le bail d'UNE tentative dans un fil dedie."""

    def __init__(self, job: JobRecord, *, renew: Callable[[JobRecord], JobRecord], lease_seconds: float,
                 renew_seconds: float, release: Optional[Callable[[JobRecord, str], JobRecord]] = None,
                 reset: Callable[[], None] = _noop, on_lost: Callable[[str], None] = _noop,
                 heartbeat: Callable[[], None] = _noop, heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
                 started_at: Optional[float] = None, clock: Callable[[], float] = time.monotonic,
                 log: Optional[EventLogger] = None, join_timeout: float = 30.0) -> None:
        if renew_seconds <= 0 or lease_seconds <= 0 or renew_seconds >= lease_seconds:
            raise ValueError("0 < renew_seconds < lease_seconds attendu")
        self._job = job
        self._renew = renew
        self._release = release
        self._reset = reset
        self._on_lost = on_lost
        self._heartbeat = heartbeat
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._join_timeout = join_timeout
        self.lease_seconds = float(lease_seconds)
        self.renew_seconds = float(renew_seconds)
        self.log = (log or get_event_logger("worker")).bind(job_id=str(job.id), attempt=job.attempts)
        start = clock() if started_at is None else started_at
        self._deadline = start + self.lease_seconds
        self._next_renewal = start + self.renew_seconds
        self._retry_seconds = min(1.0, self.renew_seconds / 2)
        self._lock = threading.Lock()          # serialise renouvellement, liberation et perte
        self._stop = threading.Event()
        self._lost = threading.Event()
        self.loss_reason: Optional[str] = None
        self.renewals = 0
        self.failures = 0
        self._thread: Optional[threading.Thread] = None

    # -- etat --------------------------------------------------------------------------------
    @property
    def job(self) -> JobRecord:
        return self._job

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    @property
    def deadline(self) -> float:
        return self._deadline

    def remaining(self) -> float:
        return self._deadline - self._clock()

    def checkpoint(self) -> None:
        """Point d'arret coopératif des gestionnaires: leve si la tentative est perdue."""
        if self._lost.is_set():
            raise JobLeaseLost(self._job.id)

    # -- cycle de vie ------------------------------------------------------------------------
    def start(self) -> "LeaseKeeper":
        if self._thread is not None:
            raise RuntimeError("gardien deja demarre")
        self._thread = threading.Thread(target=bind_context(self._run), daemon=True,
                                        name=f"mervio-lease-{str(self._job.id)[:8]}")
        self._thread.start()
        return self

    def stop(self) -> bool:
        """Arrete le fil. Un renouvellement en cours se termine; aucun autre ne commence."""
        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(self._join_timeout)
        stopped = not self._thread.is_alive()
        if not stopped:
            self.log.error("job.lease_keeper_stuck")
        return stopped

    def release(self, error_code: str = "worker_shutdown") -> bool:
        """Remet la tentative en file (arret force) et la declare perdue. Vrai si liberee."""
        if self._release is None:
            return False
        with self._lock:
            if self._lost.is_set():
                return False
            try:
                self._job = self._release(self._job, error_code)
            except JobLeaseLost:
                self._lose_locked("lease_lost")
                return False
            except Exception as exc:  # noqa: BLE001 - la liberation est un effort, le bail expirera sinon
                self.log.error("job.release_failed", error_type=type(exc).__name__,
                               error_code=database_error_code(exc))
                self._lose_locked("worker_shutdown")
                return False
            self._lose_locked("worker_shutdown")
            return True

    # -- fil ---------------------------------------------------------------------------------
    def _run(self) -> None:
        while True:
            now = self._clock()
            wake = min(self._next_renewal, self._deadline)
            if self._stop.wait(max(0.0, min(wake - now, self._heartbeat_seconds))):
                return
            self._safe(self._heartbeat)
            with self._lock:
                if self._stop.is_set() or self._lost.is_set():
                    return
                now = self._clock()
                if now >= self._deadline:
                    self._lose_locked("lease_expired")
                    return
                if now < self._next_renewal:
                    continue
                self._renew_once(now)
                if self._lost.is_set():
                    return

    def _renew_once(self, sent_at: float) -> None:
        try:
            renewed = self._renew(self._job)
        except JobLeaseLost:
            self._lose_locked("lease_lost")
            return
        except Exception as exc:  # noqa: BLE001 - toute panne du gardien est retentee jusqu'a l'echeance
            self.failures += 1
            self.log.warning("job.lease_renew_failed", error_type=type(exc).__name__,
                             error_code=database_error_code(exc), failures=self.failures,
                             remaining_ms=int(max(0.0, self._deadline - self._clock()) * 1000))
            self._safe(self._reset)
            self._next_renewal = self._clock() + self._retry_seconds
            return
        self._job = renewed
        self.renewals += 1
        self._deadline = sent_at + self.lease_seconds
        self._next_renewal = sent_at + self.renew_seconds
        self.log.debug("job.lease_renewed", renewals=self.renewals,
                       lease_expires_at=renewed.lease_expires_at.isoformat() if renewed.lease_expires_at else None)

    def _lose_locked(self, reason: str) -> None:
        if self._lost.is_set():
            return
        self.loss_reason = reason
        self._lost.set()
        self.log.warning("job.lease_lost", reason=reason, renewals=self.renewals)
        self._safe(self._on_lost, reason)

    def _safe(self, function: Callable, *args) -> None:
        try:
            function(*args)
        except Exception as exc:  # noqa: BLE001 - un rappel defaillant n'arrete pas le gardien
            self.log.error("job.lease_callback_failed", error_type=type(exc).__name__)


__all__ = ["DEFAULT_HEARTBEAT_SECONDS", "LOST_REASONS", "LeaseKeeper"]
