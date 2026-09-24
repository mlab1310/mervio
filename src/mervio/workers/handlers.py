"""Gestionnaires de travaux: import, analyse, purge.

Un gestionnaire par type, enregistre dans un registre. Aucun `if job_type == ...`
dans le worker.

Aucun gestionnaire ne calcule quoi que ce soit: l'import passe par
`import_csv_snapshot` et l'analyse par `analyze_snapshot`, tous deux inchanges
depuis 004.1. Le moteur deterministe reste la seule source de verite; aucun LLM
n'intervient dans un KPI.

Bail (004.3): chaque gestionnaire appelle `context.checkpoint()` avant ses ecritures;
si la tentative a perdu son bail, l'appel leve `JobLeaseLost` et plus rien n'est ecrit.

Idempotence: elle vient des cas d'usage de 004.1, pas du worker.
- import: `inputs_sha256` -> un instantane scelle au plus par empreinte;
- analyse: `analysis_runs_completed_uniq` -> un rapport au plus par execution.
Un travail rejoue apres un crash renvoie donc `reused` et le meme identifiant.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional
from uuid import UUID

from ..config import AnalyticsConfig
from ..identity import MasterKey
from ..observability.logging import EventLogger
from ..storage import CHUNK_SIZE, ObjectNotFound
from ..persistence import audit, jobs, raw_objects
from ..persistence.audit import Action, ActorType, Outcome, ResourceType
from ..persistence.codec import file_sha256
from ..persistence.jobs import JobRecord, JobType
from ..persistence.raw_objects import SOURCE_KINDS
from ..persistence.tenancy import Permission, TenantSession
from .errors import PermanentJobError, RetryableJobError
from .retention import RetentionPolicy

#: Codes de refus d'import qui ne se reproduiront pas a l'identique au prochain essai.
_RETRYABLE_IMPORT_CODES = frozenset({"source_changed_during_import"})


@dataclass(frozen=True)
class JobContext:
    """Tout ce dont un gestionnaire a besoin, et rien de plus."""

    session: TenantSession
    job: JobRecord
    correlation_id: str
    log: EventLogger
    #: horloge injectable: un test fixe l'instant sans attendre
    now: Optional[datetime] = None
    #: point d'arret cooperatif: leve `JobLeaseLost` si la tentative a perdu son bail (004.3)
    checkpoint: Callable[[], None] = field(default=lambda: None)
    #: acteur technique de l'audit (principal de service); defaut: l'utilisateur de la session
    actor_id: Optional[UUID] = None
    #: acteur metier (le demandeur) pour le compte duquel le service agit
    on_behalf_of: Optional[UUID] = None

    @property
    def payload(self) -> dict:
        return self.job.payload or {}

    def clock(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    def require(self, key: str) -> Any:
        if key not in self.payload or self.payload[key] in (None, ""):
            raise PermanentJobError("payload_incomplete", f"charge utile sans {key}")
        return self.payload[key]

    def uuid(self, key: str) -> UUID:
        try:
            return UUID(str(self.require(key)))
        except (ValueError, AttributeError, TypeError):
            raise PermanentJobError("payload_invalid", f"identifiant invalide pour {key}") from None

    def audit(self, conn, action, resource_type, resource_id, *, outcome=Outcome.SUCCEEDED,
              metadata: Optional[Mapping[str, Any]] = None) -> None:
        self.checkpoint()  # jamais de trace d'une tentative qui a perdu son bail
        audit.record(conn, organization_id=self.session.organization_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     correlation_id=self.correlation_id, outcome=outcome, actor_type=ActorType.WORKER,
                     actor_id=self.actor_id if self.actor_id is not None else self.session.user_id,
                     store_id=self.job.store_id, metadata=metadata, on_behalf_of=self.on_behalf_of)


Handler = Callable[[JobContext], Dict[str, Any]]


@dataclass
class HandlerRegistry:
    """Table de correspondance type de travail -> gestionnaire."""

    _handlers: Dict[JobType, Handler] = field(default_factory=dict)

    def register(self, job_type, handler: Handler) -> "HandlerRegistry":
        kind = JobType(job_type)
        if kind in self._handlers:
            raise ValueError(f"gestionnaire deja enregistre pour {kind.value}")
        self._handlers[kind] = handler
        return self

    def resolve(self, job_type) -> Handler:
        kind = JobType(job_type)
        handler = self._handlers.get(kind)
        if handler is None:
            raise PermanentJobError("no_handler", f"aucun gestionnaire pour {kind.value}")
        return handler

    def types(self):
        return tuple(sorted(self._handlers, key=lambda k: k.value))

    def copy(self) -> "HandlerRegistry":
        return HandlerRegistry(dict(self._handlers))


# -- import ---------------------------------------------------------------------------------

def make_import_handler(identity_master: Optional[MasterKey],
                        object_store: Optional[object] = None) -> Handler:
    """Gestionnaire d'import lie a la cle maitre d'identite et au magasin d'objets du processus.

    La cle n'entre jamais dans la charge utile, le contexte, l'audit ni les logs. Sans cle, tout
    import echoue en `identity_key_unavailable` (permanent): aucune reference client n'est calculee
    avec une cle de substitution. Le processus worker refuse d'ailleurs de demarrer sans elle
    ni sans magasin s'il traite des imports (settings).
    """
    def handler(context: JobContext) -> Dict[str, Any]:
        return import_handler(context, identity_master, object_store)
    return handler


def _materialize(context: JobContext, object_store, recorded, directory: Path) -> Path:
    """Ecrit les octets de l'objet dans un fichier TEMPORAIRE detenu par le worker (D-061, Q1).

    Le chemin obtenu n'est PAS un chemin d'appelant: il est fabrique ici, apres resolution sous
    RLS, et n'apparait ni dans la charge utile, ni dans l'audit, ni dans les logs, ni dans une
    erreur publiee. Memoire bornee: copie par blocs.

    Integrite N1 (D-061): les octets materialises doivent etre ceux que LA BASE declare. Une
    divergence est definitive -- `raw_objects.sha256` est fige a l'insertion (aucun UPDATE
    accorde) et les octets d'un objet ne changent pas -- et AUCUN parseur n'est appele.
    """
    target = directory / recorded.source_kind
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as sink, object_store.open(recorded.object_key) as source:
            shutil.copyfileobj(source, sink, CHUNK_SIZE)
    except ObjectNotFound:
        raise PermanentJobError("object_missing", "objet brut absent du magasin") from None
    digest, size = file_sha256(target)
    if digest != recorded.sha256 or size != recorded.byte_size:
        raise PermanentJobError("object_checksum_mismatch",
                                "les octets de l'objet ne correspondent pas a ceux declares")
    return target


def import_handler(context: JobContext, identity_master: Optional[MasterKey] = None,
                   object_store: Optional[object] = None) -> Dict[str, Any]:
    """Travail d'import: objets bruts -> instantane scelle, via le chemin 004.1 INCHANGE.

    Ordre garanti (F-02, non regresse): la cle d'identite est resolue AVANT toute lecture
    d'octet. Un refus d'identite ne laisse donc ni `import.started`, ni objet ouvert, ni
    fichier temporaire.
    """
    from ..application.persisted_analysis import SnapshotImportRequest, import_csv_snapshot
    from ..persistence.errors import IdentityKeyUnavailable
    from ..persistence.identity_keys import ensure_identity_key

    if identity_master is None:  # refuse avant tout: aucun import n'a commence
        raise PermanentJobError("identity_key_unavailable", "cle maitre d'identite non configuree")
    if object_store is None:
        raise PermanentJobError("object_store_unavailable", "magasin d'objets non configure")
    store_id = context.uuid("store_id")
    connection_id = context.uuid("connection_id")
    requested = context.payload.get("raw_objects")
    if not isinstance(requested, Mapping) or not requested:
        raise PermanentJobError("payload_incomplete", "charge utile sans objet brut")
    unknown = set(requested) - set(SOURCE_KINDS)
    if unknown:
        raise PermanentJobError("payload_invalid", "source inconnue: " + ", ".join(sorted(unknown)))

    # F-02: identite AVANT le moindre octet. Un refus laisse le magasin intact.
    try:
        ensure_identity_key(context.session, identity_master)
    except IdentityKeyUnavailable as exc:
        raise PermanentJobError(exc.code, f"cle d'identite indisponible ({exc.reason})") from None

    def started_after_identity() -> None:
        # 004.4.2 (F-02): appele par import_csv_snapshot APRES l'autorisation et la cle d'identite.
        with context.session.transaction(Permission.IMPORT_DATA) as conn:
            context.audit(conn, Action.IMPORT_STARTED, ResourceType.JOB, context.job.id, outcome=Outcome.STARTED,
                          metadata={"sources": sorted(requested), "attempt": context.job.attempts})
        context.log.info("import.started", sources=sorted(requested), attempt=context.job.attempts)

    started = time.perf_counter()
    context.checkpoint()
    # repertoire temporaire du worker: 0700, supprime en `finally`, y compris sur bail perdu
    directory = Path(tempfile.mkdtemp(prefix="mervio-objects-"))
    try:
        with context.session.transaction(Permission.READ) as conn:
            # resolution SOUS RLS: un objet d'un autre tenant est INVISIBLE, donc introuvable
            resolved = {kind: raw_objects.fetch_object(conn, UUID(str(value)))
                        for kind, value in sorted(requested.items())}
        files = {}
        for kind, recorded in resolved.items():
            if recorded.source_kind != kind:
                raise PermanentJobError("payload_invalid", f"objet de type {recorded.source_kind} pour {kind}")
            if not recorded.available:
                raise PermanentJobError("object_unavailable", "objet brut indisponible")
            files[kind] = str(_materialize(context, object_store, recorded, directory))
            context.checkpoint()

        request = SnapshotImportRequest(
            synthetic=bool(context.payload.get("synthetic", False)),
            synthetic_manifest=context.payload.get("synthetic_manifest"), **files,
        )
        try:
            result = import_csv_snapshot(context.session, store_id=store_id, connection_id=connection_id,
                                         request=request, master_key=identity_master,
                                         on_started=started_after_identity)
        except IdentityKeyUnavailable as exc:
            raise PermanentJobError(exc.code, f"cle d'identite indisponible ({exc.reason})") from None
    finally:
        shutil.rmtree(directory, ignore_errors=True)  # jamais de residu, meme sur echec
    context.checkpoint()
    duration_ms = int((time.perf_counter() - started) * 1000)

    if result.status == "rejected":
        code = result.error_code or "import_rejected"
        with context.session.transaction(Permission.IMPORT_DATA) as conn:
            context.audit(conn, Action.IMPORT_FAILED, ResourceType.SNAPSHOT, result.snapshot.id,
                          outcome=Outcome.FAILED, metadata={"error_code": code, "duration_ms": duration_ms})
        context.log.warning("import.failed", snapshot_id=str(result.snapshot.id), error_code=code,
                            duration_ms=duration_ms)
        failure = RetryableJobError if code in _RETRYABLE_IMPORT_CODES else PermanentJobError
        raise failure(code, f"import refuse: {code}")

    snapshot = result.snapshot
    with context.session.transaction(Permission.IMPORT_DATA) as conn:
        context.audit(conn, Action.IMPORT_SUCCEEDED, ResourceType.SNAPSHOT, snapshot.id,
                      metadata={"status": result.status, "row_count": snapshot.row_count,
                                "duration_ms": duration_ms})
    context.log.info("import.succeeded", snapshot_id=str(snapshot.id), status=result.status,
                     row_count=snapshot.row_count, duration_ms=duration_ms)
    return {"status": result.status, "snapshot_id": str(snapshot.id), "row_count": snapshot.row_count,
            "inputs_sha256": snapshot.inputs_sha256, "duration_ms": duration_ms}


# -- analyse --------------------------------------------------------------------------------

def analysis_handler(context: JobContext) -> Dict[str, Any]:
    """Travail d'analyse: instantane -> moteur deterministe inchange -> rapport persiste."""
    from ..application.persisted_analysis import analyze_snapshot

    store_id = context.uuid("store_id")
    snapshot_id = context.uuid("snapshot_id")
    config = _config_from_payload(context.payload)
    as_of = _date_from_payload(context.payload.get("as_of_date"))
    label = str(context.payload.get("label") or "")

    with context.session.transaction(Permission.RUN_ANALYSIS) as conn:
        context.audit(conn, Action.ANALYSIS_STARTED, ResourceType.SNAPSHOT, snapshot_id, outcome=Outcome.STARTED,
                      metadata={"grain": config.grain, "attempt": context.job.attempts})
    context.log.info("analysis.started", snapshot_id=str(snapshot_id), grain=config.grain,
                     attempt=context.job.attempts)

    started = time.perf_counter()
    context.checkpoint()
    analysis = analyze_snapshot(context.session, store_id=store_id, snapshot_id=snapshot_id, config=config,
                                today=as_of, label=label)
    context.checkpoint()
    duration_ms = int((time.perf_counter() - started) * 1000)

    if analysis.status == "failed":
        code = analysis.run.failure_code or "analysis_failed"
        with context.session.transaction(Permission.RUN_ANALYSIS) as conn:
            context.audit(conn, Action.ANALYSIS_FAILED, ResourceType.ANALYSIS_RUN, analysis.run.id,
                          outcome=Outcome.FAILED, metadata={"error_code": code, "duration_ms": duration_ms})
        context.log.warning("analysis.failed", analysis_run_id=str(analysis.run.id), error_code=code,
                            duration_ms=duration_ms)
        raise PermanentJobError(code, f"analyse en echec: {code}")

    report = analysis.report
    with context.session.transaction(Permission.RUN_ANALYSIS) as conn:
        context.audit(conn, Action.ANALYSIS_SUCCEEDED, ResourceType.REPORT, report.id,
                      metadata={"status": analysis.status, "analysis_run_id": str(analysis.run.id),
                                "payload_sha256": report.payload_sha256, "duration_ms": duration_ms})
    context.log.info("analysis.succeeded", analysis_run_id=str(analysis.run.id), report_id=str(report.id),
                     status=analysis.status, duration_ms=duration_ms)
    return {"status": analysis.status, "analysis_run_id": str(analysis.run.id), "report_id": str(report.id),
            "payload_sha256": report.payload_sha256, "payload_bytes": report.payload_bytes,
            "duration_ms": duration_ms}


def _config_from_payload(payload: Mapping[str, Any]) -> AnalyticsConfig:
    grain = payload.get("grain")
    if grain is None:
        return AnalyticsConfig()
    if grain not in ("week", "month"):
        raise PermanentJobError("payload_invalid", "grain non supporte")
    return AnalyticsConfig(grain=grain)


def _date_from_payload(value) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise PermanentJobError("payload_invalid", "as_of_date: date ISO attendue") from None


# -- purge ----------------------------------------------------------------------------------

def purge_handler(context: JobContext) -> Dict[str, Any]:
    """Travail de purge: supprime des travaux TERMINES puis des evenements expires.

    Ordre impose: les travaux d'abord, l'audit ensuite. L'audit d'un travail lui
    survit donc toujours. Aucun instantane, aucune execution, aucun rapport n'est
    touche: le role applicatif n'a pas le droit de les supprimer.
    """
    try:
        policy = RetentionPolicy.from_document(context.payload.get("retention") or {})
    except (ValueError, TypeError) as exc:
        raise PermanentJobError("payload_invalid", str(exc)) from None
    now = context.clock()

    with context.session.transaction(Permission.PURGE_DATA) as conn:
        context.audit(conn, Action.PURGE_STARTED, ResourceType.ORGANIZATION, context.session.organization_id,
                      outcome=Outcome.STARTED, metadata={"retention": policy.to_document()})
    context.log.info("purge.started", retention=policy.to_document())

    started = time.perf_counter()
    deleted_jobs: Dict[str, int] = {}
    for status, cutoff in policy.job_cutoffs(now).items():
        context.checkpoint()
        deleted_jobs[status.value] = jobs.purge_terminal_jobs(
            context.session, before=cutoff, statuses=[status], limit=policy.batch_limit)
    context.checkpoint()
    deleted_audit = audit.purge_expired_events(context.session, before=policy.audit_cutoff(now),
                                               limit=policy.batch_limit)
    duration_ms = int((time.perf_counter() - started) * 1000)

    counts = {"jobs": deleted_jobs, "audit_events": deleted_audit, "duration_ms": duration_ms}
    with context.session.transaction(Permission.PURGE_DATA) as conn:
        context.audit(conn, Action.PURGE_COMPLETED, ResourceType.ORGANIZATION, context.session.organization_id,
                      metadata={"deleted_jobs": deleted_jobs, "deleted_audit_events": deleted_audit,
                                "duration_ms": duration_ms})
    context.log.info("purge.completed", deleted_jobs=sum(deleted_jobs.values()),
                     deleted_audit_events=deleted_audit, duration_ms=duration_ms)
    return counts


def default_registry(*, identity_master: Optional[MasterKey] = None,
                     object_store: Optional[object] = None) -> HandlerRegistry:
    """Registre par defaut: un gestionnaire par type declare dans le schema.

    `identity_master`: cle maitre d'identite du processus, liee au gestionnaire d'import.
    `object_store`: magasin d'objets bruts, d'ou le worker lit les octets (004.4.4).
    """
    return (HandlerRegistry()
            .register(JobType.IMPORT, make_import_handler(identity_master, object_store))
            .register(JobType.ANALYSIS, analysis_handler)
            .register(JobType.PURGE, purge_handler))
