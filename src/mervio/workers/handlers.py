"""Gestionnaires de travaux: import, analyse, purge.

Un gestionnaire par type, enregistre dans un registre. Aucun `if job_type == ...`
dans le worker.

Aucun gestionnaire ne calcule quoi que ce soit: l'import passe par
`import_csv_snapshot` et l'analyse par `analyze_snapshot`, tous deux inchanges
depuis 004.1. Le moteur deterministe reste la seule source de verite; aucun LLM
n'intervient dans un KPI.

Idempotence: elle vient des cas d'usage de 004.1, pas du worker.
- import: `inputs_sha256` -> un instantane scelle au plus par empreinte;
- analyse: `analysis_runs_completed_uniq` -> un rapport au plus par execution.
Un travail rejoue apres un crash renvoie donc `reused` et le meme identifiant.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional
from uuid import UUID

from ..config import AnalyticsConfig
from ..observability.logging import EventLogger
from ..persistence import audit, jobs
from ..persistence.audit import Action, ActorType, Outcome, ResourceType
from ..persistence.jobs import JobRecord, JobType
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
        audit.record(conn, organization_id=self.session.organization_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     correlation_id=self.correlation_id, outcome=outcome, actor_type=ActorType.WORKER,
                     actor_id=self.session.user_id, store_id=self.job.store_id, metadata=metadata)


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

def import_handler(context: JobContext) -> Dict[str, Any]:
    """Travail d'import: fichiers CSV -> instantane scelle, via le chemin 004.1."""
    from ..application.persisted_analysis import SnapshotImportRequest, import_csv_snapshot

    store_id = context.uuid("store_id")
    connection_id = context.uuid("connection_id")
    sources = context.payload.get("sources")
    if not isinstance(sources, Mapping) or not sources:
        raise PermanentJobError("payload_incomplete", "charge utile sans source")
    unknown = set(sources) - {"shopify_orders", "shopify_products", "stripe", "google_ads"}
    if unknown:
        raise PermanentJobError("payload_invalid", "source inconnue: " + ", ".join(sorted(unknown)))

    request = SnapshotImportRequest(
        synthetic=bool(context.payload.get("synthetic", False)),
        synthetic_manifest=context.payload.get("synthetic_manifest"),
        **{kind: str(path) for kind, path in sources.items()},
    )
    with context.session.transaction(Permission.IMPORT_DATA) as conn:
        context.audit(conn, Action.IMPORT_STARTED, ResourceType.JOB, context.job.id, outcome=Outcome.STARTED,
                      metadata={"sources": sorted(sources), "attempt": context.job.attempts})
    context.log.info("import.started", sources=sorted(sources), attempt=context.job.attempts)

    started = time.perf_counter()
    result = import_csv_snapshot(context.session, store_id=store_id, connection_id=connection_id, request=request)
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
    analysis = analyze_snapshot(context.session, store_id=store_id, snapshot_id=snapshot_id, config=config,
                                today=as_of, label=label)
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
        deleted_jobs[status.value] = jobs.purge_terminal_jobs(
            context.session, before=cutoff, statuses=[status], limit=policy.batch_limit)
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


def default_registry() -> HandlerRegistry:
    """Registre par defaut: un gestionnaire par type declare dans le schema."""
    return (HandlerRegistry()
            .register(JobType.IMPORT, import_handler)
            .register(JobType.ANALYSIS, analysis_handler)
            .register(JobType.PURGE, purge_handler))
