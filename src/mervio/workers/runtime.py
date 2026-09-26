"""Processus worker: assemble les mecanismes de 004.2 a 004.3.4 sans les reimplementer.

    configuration     settings.WorkerSettings (lue et validee avant tout)
    identite          persistence.service.service_principal (derivee du role de connexion)
    travail           persistence.dispatch.Dispatcher + workers.Worker (gestionnaires 004.2)
    bail              workers.lease.LeaseKeeper sur une connexion DEDIEE (fencing de 0006)
    sante             observability.health.HealthReporter (fichier, aucun serveur HTTP)
    journalisation    observability.logging (JSON correle, redaction)

Un processus = un travail a la fois (compromis de 004.3; la montee en charge par classes
de workers et par memoire est prevue en 005.2). Deux connexions PostgreSQL, jamais
partagees entre fils: la principale (prise, gestionnaire, resultat) et celle du gardien
de bail.

Arret:
- premier SIGTERM/SIGINT -> DRAINING: plus aucune prise; le travail en cours continue
  pendant `shutdown_grace_seconds`; puis STOPPED (code 0);
- delai de grace depasse, ou second signal -> FORCED: la tentative est remise en file
  par le gardien (avec son jeton), l'instruction en cours est annulee, le worker ne
  publie plus rien; code 5. Si le gestionnaire ne rend pas la main (calcul pur), le
  processus est termine apres `forced_exit_delay`.

Codes de sortie (rendus tels quels par la commande `mervio worker`, cli/worker.py):
    0 arret propre   1 erreur interne   2 configuration ou identite refusee
    3 base injoignable au demarrage   4 schema non migre   5 arret force
"""
from __future__ import annotations

import os
import random
import signal
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..config import ENGINE_VERSION
from ..observability.health import HealthReporter
from ..observability.logging import EventLogger, configure, get_event_logger
from ..persistence.database import Database
from ..persistence.dispatch import Dispatcher
from ..persistence.errors import SchemaNotReady, UnsafeDatabaseConfiguration
from ..persistence.retry import database_error_code, retryable_database_error
from ..persistence.jobs import JobType
from ..persistence.service import ServicePrincipal, service_principal
from ..identity import MasterKey
from ..settings import BUSY_HEARTBEAT_SECONDS, WorkerSettings, describe_database_url
from ..storage import DELETE_INCAPABLE, DELETE_UNDETERMINED
from .handlers import HandlerRegistry, default_registry
from .lifecycle import HEALTH_STATE, Lifecycle, ProcessState
from .worker import JobOutcome, Worker, default_worker_id

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CONFIG = 2
EXIT_DATABASE = 3
EXIT_SCHEMA = 4
EXIT_FORCED = 5

#: Attente maximale entre deux essais quand la base est indisponible.
DATABASE_RETRY_CAP_SECONDS = 30.0


@dataclass(frozen=True)
class RunResult:
    state: ProcessState
    exit_code: int
    jobs_processed: int
    #: code publiable: stopped, forced, config, database, schema, internal
    reason: str


class _Abort(Exception):
    def __init__(self, exit_code: int, reason: str) -> None:
        self.exit_code, self.reason = exit_code, reason
        super().__init__(reason)


def configure_process_logging(settings: WorkerSettings, *, stream=None) -> None:
    """Journalisation du processus selon la configuration (JSON en production)."""
    configure(format=settings.log_format, level=settings.log_level, service="worker",
              environment=settings.environment, stream=stream)


def _hard_exit(code: int) -> None:  # pragma: no cover - termine le processus
    os._exit(code)


def _identity_master(settings: WorkerSettings) -> Optional[MasterKey]:
    """Cle maitre d'identite du processus (deja validee par settings); jamais journalisee."""
    secret = settings.identity_master_key
    return MasterKey.from_hex(secret.reveal()) if secret is not None else None


def _object_store(settings: WorkerSettings):
    """Magasin d'objets bruts du processus (004.4.4), ou None s'il n'y a pas d'import a servir."""
    if settings.object_store is None:
        return None
    from ..storage import build_object_store
    return build_object_store(settings.object_store)


class WorkerRuntime:
    """Le processus worker, de la configuration validee a l'arret."""

    def __init__(self, settings: WorkerSettings, *, registry: Optional[HandlerRegistry] = None,
                 database_factory: Callable[..., Database] = Database, health: Optional[HealthReporter] = None,
                 log: Optional[EventLogger] = None, forced_exit: Callable[[int], None] = _hard_exit,
                 forced_exit_delay: float = 5.0, jitter: Callable[[float, float], float] = random.uniform,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        #: magasin REELLEMENT configure: le preflight de D-064 l'interroge, il n'en devine rien
        self.object_store = _object_store(settings)
        self.registry = registry or default_registry(identity_master=_identity_master(settings),
                                                     object_store=self.object_store)
        self.database_factory = database_factory
        self.worker_id = default_worker_id(settings.worker_name)
        self.log = (log or get_event_logger("worker")).bind(worker_id=self.worker_id)
        self.health = health or HealthReporter(settings.health_file, worker_id=self.worker_id)
        self.lifecycle = Lifecycle(on_change=self._state_changed)
        self.forced_exit = forced_exit
        self.forced_exit_delay = forced_exit_delay
        self._jitter = jitter
        self._clock = clock
        self._stop = threading.Event()
        self._forced = threading.Event()
        #: pose quand l'arret force est entierement publie (log, etat): le processus ne sort pas avant
        self._force_published = threading.Event()
        self._control = threading.RLock()
        self._stop_requests = 0
        self._timers: list = []
        self.worker: Optional[Worker] = None
        self.principal: Optional[ServicePrincipal] = None
        self._databases: list = []
        #: statistiques observables (mesures et tests)
        self.polls = 0
        self.idle_waits: list = []

    # -- arret -------------------------------------------------------------------------------
    def request_stop(self, reason: str = "requested") -> None:
        """Premier appel: vidange. Appels suivants: arret force. Sur a appeler depuis tout fil."""
        with self._control:
            self._stop_requests += 1
            first = self._stop_requests == 1
        if not first:
            self._force("second_signal")
            return
        self.log.info("worker.stopping", reason=reason, busy=self.busy,
                      grace_seconds=self.settings.shutdown_grace_seconds)
        self.lifecycle.transition_if_allowed(ProcessState.DRAINING)
        self._stop.set()
        self._schedule(self.settings.shutdown_grace_seconds, self._grace_expired)

    def install_signal_handlers(self) -> Callable[[], None]:
        """SIGTERM et SIGINT -> `request_stop`. Renvoie une fonction de restauration.

        Le gestionnaire ne fait que deleguer a un fil court: il ne prend jamais un verrou
        que le fil principal pourrait deja tenir au moment du signal.
        """
        def handle(signum, frame) -> None:
            name = signal.Signals(signum).name
            threading.Thread(target=self.request_stop, args=(name,), daemon=True,
                             name=f"mervio-signal-{name}").start()

        previous = {signum: signal.signal(signum, handle) for signum in (signal.SIGTERM, signal.SIGINT)}

        def restore() -> None:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

        return restore

    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.busy

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _grace_expired(self) -> None:
        if self.busy:
            self._force("grace_expired")

    def _force(self, reason: str) -> None:
        with self._control:
            if self._forced.is_set():
                return
            self._forced.set()
        self._stop.set()
        released = self.worker.force_release() if self.worker is not None else False
        self.log.warning("worker.shutdown_forced", reason=reason, released=released, busy=self.busy)
        self.lifecycle.transition_if_allowed(ProcessState.FORCED)
        self._force_published.set()
        self._schedule(self.forced_exit_delay, self._hard_exit_if_stuck)

    def _hard_exit_if_stuck(self) -> None:
        if self.busy:
            self.log.error("worker.exit_forced", exit_code=EXIT_FORCED)
            self._close_databases()
            self.forced_exit(EXIT_FORCED)

    def _schedule(self, delay: float, action: Callable[[], None]) -> None:
        timer = threading.Timer(max(0.0, float(delay)), action)
        timer.daemon = True
        with self._control:
            self._timers.append(timer)
        timer.start()

    def _wait(self, seconds: float) -> bool:
        """Attente interrompue par une demande d'arret. Vrai si l'arret est demande."""
        return self._stop.wait(max(0.0, seconds))

    # -- etat et sante -----------------------------------------------------------------------
    def _state_changed(self, previous: ProcessState, current: ProcessState) -> None:
        self.health.set_state(HEALTH_STATE[current])
        self.log.debug("worker.state_changed", previous=previous.value, state=current.value)

    def _job_started(self, job) -> None:
        self.lifecycle.transition_if_allowed(ProcessState.BUSY)

    def _job_ended(self, outcome: JobOutcome) -> None:
        self.health.job_finished()
        if self.lifecycle.state is ProcessState.BUSY:
            self.lifecycle.transition_if_allowed(ProcessState.READY)

    # -- demarrage ---------------------------------------------------------------------------
    def _connect(self) -> None:
        settings = self.settings
        deadline = self._clock() + settings.startup_timeout_seconds
        delay = 0.5
        name = settings.worker_name
        while True:
            if self._stop.is_set():
                raise _Abort(EXIT_OK, "stopped")
            main = self.database_factory(settings.database_url.reveal(), application_name=f"mervio-worker:{name}",
                                         connect_timeout=5)
            lease = self.database_factory(settings.database_url.reveal(),
                                          application_name=f"mervio-worker-lease:{name}", connect_timeout=5)
            try:
                self.principal = service_principal(main)
                lease.connection()
            except SchemaNotReady:
                main.close(), lease.close()
                raise _Abort(EXIT_SCHEMA, "schema") from None
            except UnsafeDatabaseConfiguration as exc:
                main.close(), lease.close()
                self.log.error("worker.identity_refused", error_type=type(exc).__name__)
                raise _Abort(EXIT_CONFIG, "config") from None
            except Exception as exc:  # noqa: BLE001 - trie ci-dessous
                main.close(), lease.close()
                code = database_error_code(exc)
                if not retryable_database_error(exc):
                    self.log.error("worker.database_unavailable", error_type=type(exc).__name__, error_code=code,
                                   retryable=False)
                    raise _Abort(EXIT_DATABASE, "database") from None
                if self._clock() + delay > deadline:
                    self.log.error("worker.database_unavailable", error_type=type(exc).__name__, error_code=code,
                                   retryable=True, gave_up=True)
                    raise _Abort(EXIT_DATABASE, "database") from None
                self.log.warning("worker.database_unavailable", error_type=type(exc).__name__, error_code=code,
                                 retry_in_ms=int(delay * 1000))
                if self._wait(delay):
                    raise _Abort(EXIT_OK, "stopped") from None
                delay = min(delay * 2, 5.0)
                continue
            self._databases = [main, lease]
            self.log.info("worker.database_connected", principal_id=str(self.principal.id),
                          principal_name=self.principal.name, **describe_database_url(settings.database_url.reveal()))
            self.worker = Worker(
                dispatcher=Dispatcher(main, self.principal, batch=settings.dispatch_batch),
                registry=self.registry, worker_id=self.worker_id, lease_seconds=settings.lease_seconds,
                job_types=settings.job_types, log=self.log, lease_database=lease,
                renew_seconds=settings.lease_renew_seconds, heartbeat=self.health.beat,
                heartbeat_seconds=min(BUSY_HEARTBEAT_SECONDS, settings.lease_renew_seconds),
                backoff_base_seconds=settings.backoff_base_seconds,
                backoff_cap_seconds=settings.backoff_cap_seconds,
                on_job_start=self._job_started, on_job_end=self._job_ended)
            return

    def _check_delete_capability(self) -> None:
        """Fail-closed (D-064): servir `redact_customer` exige un magasin capable de DETRUIRE.

        Sans ce controle, une mauvaise configuration -- typiquement un magasin monte en lecture
        seule -- ne se manifeste qu'au MILIEU d'un effacement: la base a deja fait passer les
        objets en `purging`, puis la destruction echoue. Le refus a lieu ici, avant la moindre
        connexion, donc avant qu'aucune ligne n'ait bouge.

        Le verdict vient du magasin lui-meme (`delete_capability`), jamais d'une deduction sur le
        nom du pilote. `undetermined` est ACCEPTE et journalise: aucun controle non destructif ne
        peut prouver `s3:DeleteObject`, et l'exigence IAM etroite est reportee a 004.9 (D-064).

        Le refus ne nomme que la REGLE et le pilote: ni racine, ni chemin, ni bucket, ni endpoint.
        """
        if JobType.REDACT_CUSTOMER.value not in self.settings.job_types:
            return
        driver = self.settings.object_store.driver if self.settings.object_store else None
        rule = ("un worker qui sert redact_customer doit disposer d'un magasin d'objets "
                "capable de detruire")
        if self.object_store is None:
            self.log.error("worker.object_store_incapable", job_type=JobType.REDACT_CUSTOMER.value,
                           object_store=driver, capability=None, rule=rule, exit_code=EXIT_CONFIG)
            raise _Abort(EXIT_CONFIG, "config")
        capability = self.object_store.delete_capability()
        if capability == DELETE_INCAPABLE:
            self.log.error("worker.object_store_incapable", job_type=JobType.REDACT_CUSTOMER.value,
                           object_store=driver, capability=capability, rule=rule, exit_code=EXIT_CONFIG)
            raise _Abort(EXIT_CONFIG, "config")
        if capability == DELETE_UNDETERMINED:
            # honnete plutot que rassurant: le produit ne PEUT pas verifier la permission ici
            self.log.warning("worker.object_store_capability_undetermined",
                             job_type=JobType.REDACT_CUSTOMER.value, object_store=driver,
                             capability=capability, decision="D-064",
                             remedy="accorder s3:DeleteObject etroitement (exigence 004.9)")

    def _close_databases(self) -> None:
        for database in self._databases:
            try:
                database.close()
            except Exception:  # noqa: BLE001 - fermeture au mieux
                pass

    # -- boucle ------------------------------------------------------------------------------
    def run(self) -> RunResult:
        started = self._clock()
        self.health.set_state(HEALTH_STATE[ProcessState.STARTING])
        self.log.info("worker.starting", pid=os.getpid(), engine_version=ENGINE_VERSION)
        self.log.info("worker.config", **self.settings.public())
        exit_code, reason = EXIT_OK, "stopped"
        try:
            self._check_delete_capability()
            self._connect()
            self.health.database_ok()
            if not self._stop.is_set():
                self.lifecycle.transition(ProcessState.READY)
                self.log.info("worker.ready", principal_id=str(self.principal.id),
                              job_types=list(self.settings.job_types))
                self._loop()
        except _Abort as abort:
            exit_code, reason = abort.exit_code, abort.reason
        except Exception as exc:  # noqa: BLE001 - dernier rempart: journalise le type, sort en erreur
            self.log.error("worker.crashed", error_type=type(exc).__name__, error_code=database_error_code(exc))
            exit_code, reason = EXIT_INTERNAL, "internal"
        finally:
            with self._control:
                for timer in self._timers:
                    timer.cancel()
            self._close_databases()
        if self._forced.is_set():
            # le fil qui force libere le bail AVANT de journaliser: sans cette attente, le processus
            # pouvait sortir avant la ligne `worker.shutdown_forced`
            self._force_published.wait(self.forced_exit_delay)
            exit_code, reason = EXIT_FORCED, "forced"
            self.lifecycle.transition_if_allowed(ProcessState.FORCED)
        else:
            self.lifecycle.transition_if_allowed(ProcessState.DRAINING)
            self.lifecycle.transition_if_allowed(ProcessState.STOPPED)
        jobs_processed = self.health.snapshot.jobs_processed
        self.log.info("worker.stopped", state=self.lifecycle.state.value, exit_code=exit_code, reason=reason,
                      jobs_processed=jobs_processed, uptime_s=round(self._clock() - started, 3))
        return RunResult(self.lifecycle.state, exit_code, jobs_processed, reason)

    def _loop(self) -> None:
        settings = self.settings
        poll = settings.poll_interval_seconds
        next_recovery = self._clock()
        failure_delay = 1.0
        while not self._stop.is_set():
            try:
                if self._clock() >= next_recovery:
                    self.worker.recover_stale()
                    next_recovery = self._clock() + settings.recovery_interval_seconds
                self.polls += 1
                outcome = self.worker.run_once()
            except Exception as exc:  # noqa: BLE001 - trie ci-dessous
                if not retryable_database_error(exc):
                    raise
                self._database_unavailable(exc, failure_delay)
                if self._wait(failure_delay):
                    break
                failure_delay = min(failure_delay * 2, DATABASE_RETRY_CAP_SECONDS)
                continue
            if self.lifecycle.state is ProcessState.DEGRADED:
                self.lifecycle.transition_if_allowed(ProcessState.READY)
                self.log.info("worker.database_recovered")
            if failure_delay != 1.0 or outcome is None:
                self.health.database_ok()
            failure_delay = 1.0
            if outcome is not None:
                poll = settings.poll_interval_seconds
                continue
            wait = poll * self._jitter(0.9, 1.1)
            self.idle_waits.append(wait)
            if self._wait(wait):
                break
            poll = min(poll * 2, settings.poll_max_seconds)

    def _database_unavailable(self, exc: BaseException, delay: float) -> None:
        if self.lifecycle.transition_if_allowed(ProcessState.DEGRADED) or self.lifecycle.state is ProcessState.DEGRADED:
            self.health.database_unavailable()
        self.log.warning("worker.database_unavailable", error_type=type(exc).__name__,
                         error_code=database_error_code(exc), retry_in_ms=int(delay * 1000))
        for database in self._databases:
            database.close()


__all__ = ["EXIT_CONFIG", "EXIT_DATABASE", "EXIT_FORCED", "EXIT_INTERNAL", "EXIT_OK", "EXIT_SCHEMA", "RunResult",
           "WorkerRuntime", "configure_process_logging"]
