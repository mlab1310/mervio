"""File de travaux durable: mise en file, prise, resultat, reprise, purge.

PostgreSQL EST la file (ADR-004-008). Aucun SQL ne sort de ce module: le worker
manipule des `JobRecord`.

Deux transactions courtes encadrent une execution longue:

    transaction 1 (prise)        SELECT ... FOR UPDATE SKIP LOCKED, queued -> running
    -- hors transaction --       le travail metier (import, analyse): minutes possibles
    transaction 2 (resultat)     running -> succeeded | failed | queued

Rien n'est verrouille pendant le travail metier: une analyse d'un million de
commandes ne retient ni verrou ni WAL (R-24).

Execution AU MOINS une fois, jamais "exactement une fois". Un worker mort laisse un
travail `running` dont le bail finit par expirer; un autre worker le reprend.
L'absence de double ecriture vient de l'idempotence de 004.1, jamais d'une promesse
d'unicite d'execution.

Bail et jeton d'exclusion (004.3, revision 0006):

    renew_lease            le detenteur prolonge un bail ENCORE VALIDE
    mark_* / requeue       le detenteur publie le sort de SA tentative
    recover_stale_jobs     sans jeton, seulement un bail expire a l'instant declare

Le jeton est `(attempts, locked_by)`: `attempts` change a chaque prise, donc un ancien
detenteur - meme avec le meme worker_id - ne peut plus rien ecrire. Il est verifie
deux fois: par le filtre de chaque UPDATE (le perdant d'une course voit 0 ligne) et
par le trigger `jobs_guard_transition`, qui refuse toute ecriture d'un travail en
cours sans jeton valide, y compris en SQL brut.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from uuid import UUID

from psycopg.types.json import Jsonb

from ..observability.redaction import redact_text
from .errors import JobLeaseLost, JobStateError, NotFound, PayloadRejected
from .stores import _limit, _uuid, fetch_store
from .tenancy import Permission, TenantSession


class JobType(str, Enum):
    IMPORT = "import"
    ANALYSIS = "analysis"
    PURGE = "purge"
    #: 004.4.5 (revision 0014, gestionnaire en E4): effacement d'UN client par le chemin
    #: privilegie de D-052. La charge ne porte qu'un `customer_ref` -- un HMAC deja non
    #: reversible, jamais une identite. Le worker ne resout jamais une identite (D-062).
    REDACT_CUSTOMER = "redact_customer"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})

#: Graphe des transitions, identique a celui impose par le trigger `jobs_guard_transition`.
#: `running -> queued` couvre la reprise apres echec transitoire ET la recuperation d'un bail expire.
ALLOWED_TRANSITIONS: Dict[JobStatus, frozenset] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.QUEUED}),
    JobStatus.RUNNING: frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.QUEUED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}

#: Defauts operationnels (ADR-004.2-003), surchargeables par appel.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 300
#: Plafond d'un bail, a la prise comme au renouvellement (egalement impose par la base).
MAX_LEASE_SECONDS = 86400
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600

#: Permission exigee pour METTRE EN FILE chaque type de travail. Executer un travail
#: deja en file demande `RUN_JOBS`; la purge revalide `PURGE_DATA` au moment de supprimer.
ENQUEUE_PERMISSION = {
    JobType.IMPORT: Permission.IMPORT_DATA,
    JobType.ANALYSIS: Permission.RUN_ANALYSIS,
    JobType.PURGE: Permission.PURGE_DATA,
    JobType.REDACT_CUSTOMER: Permission.REDACT_CUSTOMER,
}

#: Une charge utile localise une source, elle ne la transporte pas et ne porte aucun secret.
FORBIDDEN_PAYLOAD_KEY_FRAGMENTS = (
    "password", "passwd", "secret", "token", "credential", "authorization",
    "api_key", "apikey", "private_key", "cookie", "access_key",
)
#: 004.4.4 (D-054): le worker ne recoit JAMAIS de chemin. Ces fragments de cle sont refuses,
#: en plus de la detection de VALEUR ci-dessous. `sources` etait la cle historique des chemins.
FORBIDDEN_PATH_KEY_FRAGMENTS = ("path", "filename", "filepath", "pathname", "dirname", "directory")
#: Noms de cle refuses a l'identique (fragments trop courts pour etre cherches en sous-chaine:
#: "file" apparait dans "profile", "dir" dans "redirect").
#:
#: `files` en est volontairement ABSENT: le manifeste synthetique versionne (`synthetic_manifest`)
#: porte une entree `files` qui associe un NOM de fichier a son empreinte et a son nombre de
#: lignes -- aucune localisation. La protection de fond reste la detection de VALEUR ci-dessous,
#: qui refuse un chemin quelle que soit la cle qui le porte.
FORBIDDEN_PATH_KEY_NAMES = frozenset({"file", "dir", "sources", "source_path"})
MAX_PAYLOAD_VALUE_LENGTH = 4096
MAX_PAYLOAD_BYTES = 16384

#: Lecteur de disque Windows (`C:\...`), refuse comme un chemin absolu POSIX.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def looks_like_a_path(value: str) -> bool:
    """Une valeur de charge utile designe-t-elle un emplacement de fichier? (D-054)

    Refuse ce qu'un `open()` pourrait resoudre: chemin absolu POSIX, chemin relatif au foyer,
    lecteur Windows, chemin UNC, schema `file:`, et tout segment de remontee `..`.

    N'refuse PAS une valeur qui contient seulement un `/` (un libelle comme "Q1/2026" reste
    legitime): la frontiere reelle est que le worker ne resout plus aucune valeur en fichier,
    ce controle n'est qu'une defense en profondeur.
    """
    text = value.strip()
    if not text:
        return False
    if text.startswith(("/", "~/", "\\")) or text == "~":
        return True
    if text.lower().startswith("file:"):
        return True
    if _WINDOWS_DRIVE.match(text):
        return True
    return any(part == ".." for part in re.split(r"[\\/]", text))


@dataclass(frozen=True)
class JobRecord:
    id: UUID
    organization_id: UUID
    store_id: Optional[UUID]
    job_type: str
    status: str
    priority: int
    attempts: int
    max_attempts: int
    idempotency_key: Optional[str]
    correlation_id: UUID
    enqueued_by: Optional[UUID]
    available_at: datetime
    locked_at: Optional[datetime]
    locked_by: Optional[str]
    lease_expires_at: Optional[datetime]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    last_error_code: Optional[str]
    last_error: Optional[str]
    payload: dict
    result: Optional[dict]
    created_at: datetime
    updated_at: datetime

    @property
    def kind(self) -> JobType:
        return JobType(self.job_type)

    @property
    def state(self) -> JobStatus:
        return JobStatus(self.status)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATUSES

    @property
    def attempts_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)


_JOB_COLUMNS = ("id, organization_id, store_id, job_type, status, priority, attempts, max_attempts, "
                "idempotency_key, correlation_id, enqueued_by, available_at, locked_at, locked_by, "
                "lease_expires_at, started_at, finished_at, last_error_code, last_error, payload, result, "
                "created_at, updated_at")

#: Meme liste qualifiee: la CTE de prise met deux relations en portee (`jobs` et
#: `candidate`), donc `RETURNING` doit nommer sa table.
_JOB_COLUMNS_QUALIFIED = ", ".join("jobs." + column.strip() for column in _JOB_COLUMNS.split(","))

#: Prise du prochain travail, en UNE instruction. La CTE selectionne et verrouille
#: (`FOR UPDATE SKIP LOCKED`), l'UPDATE transite vers `running`: aucun intervalle
#: entre les deux, donc aucune prise en double et aucun worker bloque par un autre.
#: Constante de module: `explain_claim` explique EXACTEMENT ce qui s'execute.
#: Horloge evaluee UNE fois (`(SELECT COALESCE(..., now()))`, meme instant que `now()`: celui de la
#: transaction): sous RLS, `now()` (non leakproof) ne peut pas figurer dans une condition d'index;
#: comparee a un parametre d'InitPlan, `available_at` (derniere cle de `jobs_ready_idx`, revision
#: 0012) est verifiee dans l'index, sans lecture de table pour les travaux differes (D-060).
_CLAIM_SQL = (
    "WITH candidate AS ("
    "    SELECT id FROM jobs"
    "    WHERE organization_id = %s AND status = 'queued'"
    "      AND available_at <= (SELECT COALESCE(%s::timestamptz, now()))"
    "      AND job_type = ANY(%s) AND attempts < max_attempts"
    "      AND (%s::uuid IS NULL OR id = %s::uuid)"
    "    ORDER BY priority DESC, created_at ASC, id ASC"
    "    FOR UPDATE SKIP LOCKED"
    "    LIMIT 1"
    ") "
    "UPDATE jobs SET status = 'running', attempts = jobs.attempts + 1,"
    "    locked_at = COALESCE(%s::timestamptz, now()), locked_by = %s,"
    "    lease_expires_at = COALESCE(%s::timestamptz, now()) + make_interval(secs => %s),"
    "    started_at = COALESCE(%s::timestamptz, now()), updated_at = clock_timestamp() "
    "FROM candidate WHERE jobs.id = candidate.id AND jobs.organization_id = %s "
    f"RETURNING {_JOB_COLUMNS_QUALIFIED}"
)

#: Crochet appele DANS la transaction qui change l'etat, avec (connexion, travail).
#: Il sert a ecrire l'audit: l'evenement et le changement d'etat sont commis ensemble,
#: ou pas du tout. Une exception du crochet annule la transition.
TransitionHook = Callable[[Any, "JobRecord"], None]


# -- retentative ---------------------------------------------------------------------------

def backoff_seconds(attempts: int, *, base: int = BACKOFF_BASE_SECONDS, cap: int = BACKOFF_CAP_SECONDS) -> int:
    """Delai avant la prochaine tentative: base, 2x base, 4x base... plafonne.

    Deterministe (aucun alea): un test peut predire l'instant de reprise. La gigue
    ("jitter") n'apporte rien tant qu'un travail est pris par un seul worker.
    """
    if attempts < 1:
        raise ValueError("attempts commence a 1")
    if base < 1 or cap < base:
        raise ValueError("base >= 1 et cap >= base attendus")
    return min(cap, base * (2 ** (attempts - 1)))


# -- charge utile --------------------------------------------------------------------------

def _reject_paths(key: str, value: Any) -> None:
    """Refuse recursivement toute CLE et toute VALEUR de chemin (D-054), listes comprises."""
    lowered = str(key).lower()
    if lowered in FORBIDDEN_PATH_KEY_NAMES or any(f in lowered for f in FORBIDDEN_PATH_KEY_FRAGMENTS):
        raise PayloadRejected(f"cle de chemin interdite dans une charge utile: {key}")
    if isinstance(value, str) and looks_like_a_path(value):
        # la valeur elle-meme n'est jamais citee: elle pourrait porter une donnee locale
        raise PayloadRejected(f"valeur de chemin interdite dans une charge utile: {key}")
    if isinstance(value, Mapping):
        for nested_key, nested in value.items():
            _reject_paths(nested_key, nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_paths(key, item)


def validate_payload(payload: Mapping[str, Any]) -> dict:
    """Refuse une charge utile qui porterait un secret, un chemin, ou le contenu d'une source.

    Depuis 004.4.4 (D-054), un import ne transporte que des identifiants d'objets bruts: aucun
    chemin fourni par l'appelant ne peut donc survivre jusqu'au worker.
    """
    if not isinstance(payload, Mapping):
        raise PayloadRejected("la charge utile d'un travail est un objet")
    document = dict(payload)
    for key, value in document.items():
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in FORBIDDEN_PAYLOAD_KEY_FRAGMENTS):
            raise PayloadRejected(f"cle de charge utile interdite: {key}")
        if isinstance(value, str) and len(value) > MAX_PAYLOAD_VALUE_LENGTH:
            raise PayloadRejected(f"valeur trop grande pour {key}: une charge utile localise, ne transporte pas")
        _reject_paths(key, value)
        if isinstance(value, Mapping):
            validate_payload(value)
    if len(json.dumps(document, default=str).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise PayloadRejected("charge utile trop grande")
    return document


# -- mise en file --------------------------------------------------------------------------

def enqueue_job(session: TenantSession, *, job_type, payload: Mapping[str, Any],
                store_id: Optional[UUID] = None, priority: int = 0,
                max_attempts: int = DEFAULT_MAX_ATTEMPTS, idempotency_key: Optional[str] = None,
                available_at: Optional[datetime] = None, correlation_id: Optional[UUID] = None,
                hook: Optional[TransitionHook] = None) -> JobRecord:
    """Met un travail en file. Meme cle d'idempotence -> le travail deja en file est renvoye."""
    kind = JobType(job_type)
    document = validate_payload(payload)
    if not isinstance(priority, int) or not -100 <= priority <= 100:
        raise ValueError("priority entre -100 et 100")
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 20:
        raise ValueError("max_attempts entre 1 et 20")
    correlation = _uuid(correlation_id, "correlation") if correlation_id else uuid.uuid4()

    with session.transaction(ENQUEUE_PERMISSION[kind]) as conn:
        if store_id is not None:
            fetch_store(conn, session, store_id)
        row = conn.execute(
            "INSERT INTO jobs (id, organization_id, store_id, job_type, status, priority, max_attempts, "
            "idempotency_key, correlation_id, enqueued_by, payload, available_at) "
            "VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now())) "
            "ON CONFLICT (organization_id, job_type, idempotency_key) WHERE idempotency_key IS NOT NULL "
            f"DO NOTHING RETURNING {_JOB_COLUMNS}",
            (uuid.uuid4(), session.organization_id, store_id, kind.value, priority, max_attempts,
             idempotency_key, correlation, session.user_id, Jsonb(document), available_at),
        ).fetchone()
        if row is None:
            # rejeu: la cle existe deja DANS CETTE organisation (index prefixe par
            # organization_id: aucune information sur un autre tenant ne filtre ici)
            row = conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs "
                "WHERE organization_id = %s AND job_type = %s AND idempotency_key = %s",
                (session.organization_id, kind.value, idempotency_key),
            ).fetchone()
            if row is None:  # pragma: no cover - conflit sans ligne visible: impossible sous RLS de tenant
                raise JobStateError("conflit de cle d'idempotence sans travail correspondant")
            return JobRecord(*row)  # rejeu: rien de nouveau, donc rien a tracer
        record = JobRecord(*row)
        if hook is not None:
            hook(conn, record)
    return record


# -- prise ---------------------------------------------------------------------------------

def claim_next_job(session: TenantSession, *, worker_id: str, job_types: Optional[Sequence] = None,
                   lease_seconds: int = DEFAULT_LEASE_SECONDS, now: Optional[datetime] = None,
                   job_id: Optional[UUID] = None,
                   hook: Optional[TransitionHook] = None) -> Optional[JobRecord]:
    """Prend le prochain travail disponible de l'organisation, ou None.

    `FOR UPDATE SKIP LOCKED` dans une CTE, puis transition atomique vers `running`
    dans la MEME instruction: aucun intervalle entre la selection et la prise, donc
    aucun travail pris deux fois simultanement, et aucun worker bloque par un autre.
    """
    if not worker_id or len(worker_id) > 100:
        raise ValueError("worker_id non vide, 100 caracteres au plus")
    _check_lease_seconds(lease_seconds)
    kinds = [JobType(k).value for k in (job_types if job_types is not None else list(JobType))]
    target = _uuid(job_id, "job") if job_id is not None else None

    with session.transaction(Permission.RUN_JOBS) as conn:
        row = conn.execute(
            _CLAIM_SQL,
            (session.organization_id, now, kinds, target, target,
             now, worker_id, now, lease_seconds, now, session.organization_id),
        ).fetchone()
        if row is None:
            return None
        claimed = JobRecord(*row)
        if hook is not None:
            hook(conn, claimed)
    return claimed


# -- fin d'execution -----------------------------------------------------------------------

def renew_lease(session: TenantSession, job: JobRecord, *, worker_id: Optional[str] = None,
                lease_seconds: int = DEFAULT_LEASE_SECONDS, timeout_ms: Optional[int] = None,
                hook: Optional[TransitionHook] = None) -> JobRecord:
    """Prolonge le bail d'un travail en cours. Seul son detenteur y parvient.

    Le bail devient `max(bail actuel, maintenant + lease_seconds)`: jamais raccourci.
    L'horloge est TOUJOURS celle de la base: un bail deja expire n'est jamais
    renouvele, meme si personne ne l'a encore repris. `JobLeaseLost` signifie donc
    "arreter le travail": la tentative ne publiera plus rien.

    Refus (`JobLeaseLost`): travail termine, remis en file, repris par une autre
    tentative (meme worker_id), autre detenteur, bail expire. Travail d'un autre
    tenant ou inexistant: `NotFound`, indiscernables.

    `timeout_ms` borne l'attente (verrou compris): un renouvellement bloque echoue au lieu
    d'empecher le gardien de constater l'echeance de son bail (004.3).
    """
    _check_lease_seconds(lease_seconds)
    holder = worker_id if worker_id is not None else job.locked_by
    with session.transaction(Permission.RUN_JOBS) as conn:
        _limit_statement_time(conn, timeout_ms)
        _declare_lease_holder(conn, job, holder)
        row = conn.execute(
            "UPDATE jobs SET lease_expires_at = GREATEST(lease_expires_at, now() + make_interval(secs => %s)), "
            "    updated_at = clock_timestamp() "
            "WHERE organization_id = %s AND id = %s AND status = 'running' "
            "  AND locked_by = %s AND attempts = %s AND lease_expires_at > clock_timestamp() "
            f"RETURNING {_JOB_COLUMNS}",
            (lease_seconds, session.organization_id, job.id, holder, job.attempts),
        ).fetchone()
        if row is None:
            fetch_job(conn, session, job.id)  # NotFound si hors du tenant
            raise JobLeaseLost(job.id)
        renewed = JobRecord(*row)
        if hook is not None:
            hook(conn, renewed)
    return renewed


def mark_succeeded(session: TenantSession, job: JobRecord, *, result: Optional[Mapping[str, Any]] = None,
                   worker_id: Optional[str] = None, now: Optional[datetime] = None,
                   hook: Optional[TransitionHook] = None) -> JobRecord:
    """Termine un travail en succes. Seul le detenteur de CETTE tentative y parvient.

    Le detenteur est `worker_id`, sinon celui du `JobRecord` fourni; la tentative est
    `job.attempts`. Un bail expire mais non repris reste publiable: personne d'autre
    ne detient le travail, et le verrou de ligne arbitre avec une reprise concurrente.
    """
    document = validate_payload(result or {})
    holder = worker_id if worker_id is not None else job.locked_by
    with session.transaction(Permission.RUN_JOBS) as conn:
        _declare_lease_holder(conn, job, holder)
        row = conn.execute(
            "UPDATE jobs SET status = 'succeeded', finished_at = COALESCE(%s::timestamptz, now()), result = %s, "
            "    locked_at = NULL, locked_by = NULL, lease_expires_at = NULL, last_error_code = NULL, "
            "    last_error = NULL, updated_at = clock_timestamp() "
            "WHERE organization_id = %s AND id = %s AND status = 'running' "
            f"  AND locked_by = %s AND attempts = %s RETURNING {_JOB_COLUMNS}",
            (now, Jsonb(document), session.organization_id, job.id, holder, job.attempts),
        ).fetchone()
        if row is None:
            _explain_lost_transition(conn, session, job, holder)
        finished = JobRecord(*row)
        if hook is not None:
            hook(conn, finished)
    return finished


def mark_failed(session: TenantSession, job: JobRecord, *, error_code: str, error: str = "",
                retryable: bool = True, worker_id: Optional[str] = None,
                now: Optional[datetime] = None, hook: Optional[TransitionHook] = None,
                backoff_base: int = BACKOFF_BASE_SECONDS, backoff_cap: int = BACKOFF_CAP_SECONDS) -> JobRecord:
    """Echec d'une tentative.

    Reprise si l'erreur est transitoire ET qu'il reste des tentatives: le travail
    retourne en file avec un delai exponentiel. Sinon echec terminal.
    """
    code = _error_code(error_code)
    message = _safe_error(error)
    retry = bool(retryable) and job.attempts < job.max_attempts
    if retry:
        return requeue(session, job, error_code=code, error=message, worker_id=worker_id, now=now,
                       delay_seconds=backoff_seconds(job.attempts, base=backoff_base, cap=backoff_cap), hook=hook)
    holder = worker_id if worker_id is not None else job.locked_by
    with session.transaction(Permission.RUN_JOBS) as conn:
        _declare_lease_holder(conn, job, holder)
        row = conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = COALESCE(%s::timestamptz, now()), "
            "    locked_at = NULL, locked_by = NULL, lease_expires_at = NULL, last_error_code = %s, "
            "    last_error = %s, updated_at = clock_timestamp() "
            "WHERE organization_id = %s AND id = %s AND status = 'running' "
            f"  AND locked_by = %s AND attempts = %s RETURNING {_JOB_COLUMNS}",
            (now, code, message, session.organization_id, job.id, holder, job.attempts),
        ).fetchone()
        if row is None:
            _explain_lost_transition(conn, session, job, holder)
        failed = JobRecord(*row)
        if hook is not None:
            hook(conn, failed)
    return failed


def requeue(session: TenantSession, job: JobRecord, *, delay_seconds: int = 0, error_code: Optional[str] = None,
            error: str = "", worker_id: Optional[str] = None, now: Optional[datetime] = None,
            hook: Optional[TransitionHook] = None, timeout_ms: Optional[int] = None) -> JobRecord:
    """Remet un travail en cours dans la file, apres un delai. L'historique des tentatives est conserve.

    Meme regle de detention que `mark_succeeded`.
    """
    if not isinstance(delay_seconds, int) or not 0 <= delay_seconds <= 86400 * 7:
        raise ValueError("delay_seconds entre 0 et 604800")
    holder = worker_id if worker_id is not None else job.locked_by
    with session.transaction(Permission.RUN_JOBS) as conn:
        _limit_statement_time(conn, timeout_ms)
        _declare_lease_holder(conn, job, holder)
        row = conn.execute(
            "UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL, lease_expires_at = NULL, "
            "    available_at = COALESCE(%s::timestamptz, now()) + make_interval(secs => %s), "
            "    last_error_code = COALESCE(%s::text, last_error_code), last_error = COALESCE(%s::text, last_error), "
            "    updated_at = clock_timestamp() "
            "WHERE organization_id = %s AND id = %s AND status = 'running' "
            f"  AND locked_by = %s AND attempts = %s RETURNING {_JOB_COLUMNS}",
            (now, delay_seconds, _error_code(error_code) if error_code else None,
             _safe_error(error) if error else None, session.organization_id, job.id, holder, job.attempts),
        ).fetchone()
        if row is None:
            _explain_lost_transition(conn, session, job, holder)
        queued = JobRecord(*row)
        if hook is not None:
            hook(conn, queued)
    return queued


def cancel_job(session: TenantSession, job_id: UUID, *, hook: Optional[TransitionHook] = None) -> JobRecord:
    """Annule un travail encore en file. Un travail en cours ou termine n'est pas annulable."""
    with session.transaction(Permission.RUN_JOBS) as conn:
        row = conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = clock_timestamp(), updated_at = clock_timestamp() "
            f"WHERE organization_id = %s AND id = %s AND status = 'queued' RETURNING {_JOB_COLUMNS}",
            (session.organization_id, _uuid(job_id, "job")),
        ).fetchone()
        if row is None:
            current = fetch_job(conn, session, job_id)
            raise JobStateError(f"travail {current.status}: seul un travail en file s'annule")
        cancelled = JobRecord(*row)
        if hook is not None:
            hook(conn, cancelled)
    return cancelled


# -- recuperation --------------------------------------------------------------------------

def recover_stale_jobs(session: TenantSession, *, now: Optional[datetime] = None,
                       limit: int = 100, hook: Optional[TransitionHook] = None) -> List[JobRecord]:
    """Reprend les travaux dont le bail a expire (worker mort, machine perdue).

    Aucun travail n'est perdu ni duplique: la MEME ligne repasse en file, avec ses
    tentatives. Si les tentatives sont epuisees, le travail devient terminalement
    en echec plutot que de tourner en boucle.
    """
    bound = _limit(limit)
    with session.transaction(Permission.RUN_JOBS) as conn:
        # instant de reference declare a la base: le trigger n'accepte la reprise sans
        # jeton que d'un bail expire a cet instant (revision 0006)
        conn.execute("SELECT set_config('app.job_lease_recovery_at', COALESCE(%s::timestamptz, now())::text, true)",
                     (now,))
        expired = conn.execute(
            "SELECT id, attempts, max_attempts FROM jobs "
            "WHERE organization_id = %s AND status = 'running' "
            "  AND lease_expires_at < COALESCE(%s::timestamptz, now()) "
            "ORDER BY lease_expires_at ASC, id ASC LIMIT %s FOR UPDATE SKIP LOCKED",
            (session.organization_id, now, bound),
        ).fetchall()
        recovered: List[JobRecord] = []
        for job_id, attempts, max_attempts in expired:
            if attempts < max_attempts:
                row = conn.execute(
                    "UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL, "
                    "    lease_expires_at = NULL, available_at = COALESCE(%s::timestamptz, now()), "
                    "    last_error_code = 'lease_expired', updated_at = clock_timestamp() "
                    "WHERE organization_id = %s AND id = %s AND status = 'running' "
                    f"  AND lease_expires_at < COALESCE(%s::timestamptz, now()) RETURNING {_JOB_COLUMNS}",
                    (now, session.organization_id, job_id, now),
                ).fetchone()
            else:
                row = conn.execute(
                    "UPDATE jobs SET status = 'failed', finished_at = COALESCE(%s::timestamptz, now()), "
                    "    locked_at = NULL, locked_by = NULL, lease_expires_at = NULL, "
                    "    last_error_code = 'lease_expired', last_error = %s, updated_at = clock_timestamp() "
                    "WHERE organization_id = %s AND id = %s AND status = 'running' "
                    f"  AND lease_expires_at < COALESCE(%s::timestamptz, now()) RETURNING {_JOB_COLUMNS}",
                    (now, "bail expire, tentatives epuisees", session.organization_id, job_id, now),
                ).fetchone()
            if row is not None:
                record = JobRecord(*row)
                if hook is not None:
                    hook(conn, record)
                recovered.append(record)
    return recovered


# -- lecture -------------------------------------------------------------------------------

def fetch_job(conn, session: TenantSession, job_id: UUID) -> JobRecord:
    row = conn.execute(
        f"SELECT {_JOB_COLUMNS} FROM jobs WHERE organization_id = %s AND id = %s",
        (session.organization_id, _uuid(job_id, "job")),
    ).fetchone()
    if row is None:
        raise NotFound("job")
    return JobRecord(*row)


def get_job(session: TenantSession, job_id: UUID) -> JobRecord:
    with session.transaction(Permission.READ) as conn:
        return fetch_job(conn, session, job_id)


def list_jobs(session: TenantSession, *, store_id: Optional[UUID] = None, status=None, job_type=None,
              limit: int = 50) -> List[JobRecord]:
    statuses = [JobStatus(s).value for s in status] if status is not None else None
    kinds = [JobType(k).value for k in job_type] if job_type is not None else None
    with session.transaction(Permission.READ) as conn:
        if store_id is not None:
            fetch_store(conn, session, store_id)
        rows = conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE organization_id = %s "
            "  AND (%s::uuid IS NULL OR store_id = %s::uuid) "
            "  AND (%s::text[] IS NULL OR status = ANY(%s::text[])) "
            "  AND (%s::text[] IS NULL OR job_type = ANY(%s::text[])) "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (session.organization_id, store_id, store_id, statuses, statuses, kinds, kinds, _limit(limit)),
        ).fetchall()
    return [JobRecord(*r) for r in rows]


def explain_claim(session: TenantSession, *, job_types: Optional[Sequence] = None,
                  lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    """Plan d'execution de la prise, sans rien prendre (EXPLAIN sans ANALYZE n'execute pas).

    Sert aux gardes anti-regression: la prise doit rester un acces par index. La
    lecon de 004.1 vaut ici aussi, un index mal ordonne ne casse rien, il rend
    seulement la file quadratique le jour ou elle grossit.
    """
    kinds = [JobType(k).value for k in (job_types if job_types is not None else list(JobType))]
    with session.transaction(Permission.READ) as conn:
        plan = conn.execute(
            "EXPLAIN (FORMAT JSON) " + _CLAIM_SQL,
            (session.organization_id, None, kinds, None, None,
             None, "explain", None, lease_seconds, None, session.organization_id),
        ).fetchone()[0]
    return plan[0]["Plan"]


def queue_statistics(session: TenantSession, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Compteurs bruts de la file: base des metriques `jobs_*` (ADR-004.2-005)."""
    with session.transaction(Permission.READ) as conn:
        rows = conn.execute(
            "SELECT status, job_type, count(*), "
            "       coalesce(max(extract(epoch FROM COALESCE(%s::timestamptz, now()) - created_at)), 0) "
            "FROM jobs WHERE organization_id = %s GROUP BY status, job_type ORDER BY status, job_type",
            (now, session.organization_id),
        ).fetchall()
    by_status: Dict[str, int] = {}
    by_type: Dict[str, int] = {}
    oldest = 0.0
    for status, job_type, count, age in rows:
        by_status[status] = by_status.get(status, 0) + count
        by_type[job_type] = by_type.get(job_type, 0) + count
        if status == JobStatus.QUEUED.value:
            oldest = max(oldest, float(age))
    return {"by_status": by_status, "by_type": by_type,
            "jobs_total": sum(by_status.values()),
            "queue_oldest_age_ms": int(oldest * 1000)}


# -- purge ---------------------------------------------------------------------------------

def purge_terminal_jobs(session: TenantSession, *, before: datetime, statuses: Sequence,
                        limit: int = 1000) -> int:
    """Supprime des travaux TERMINES avant `before`. Renvoie le nombre supprime.

    La base refuse par elle-meme (politique RESTRICTIVE `jobs_purge_terminal_only`)
    la suppression d'un travail en file ou en cours, et de tout travail termine
    depuis moins d'une heure: une erreur de retention ne peut pas detruire la file.
    """
    if not isinstance(before, datetime):
        raise ValueError("before est un datetime")
    wanted = [JobStatus(s).value for s in statuses]
    if not wanted or set(wanted) - {s.value for s in TERMINAL_STATUSES}:
        raise ValueError("seuls des etats terminaux se purgent")
    with session.transaction(Permission.PURGE_DATA) as conn:
        deleted = conn.execute(
            "DELETE FROM jobs WHERE organization_id = %s AND id IN ("
            "    SELECT id FROM jobs WHERE organization_id = %s AND status = ANY(%s) "
            "      AND finished_at IS NOT NULL AND finished_at < %s "
            "    ORDER BY finished_at ASC, id ASC LIMIT %s)",
            (session.organization_id, session.organization_id, wanted, before, _limit(limit)),
        ).rowcount
    return int(deleted)


# -- aides internes -------------------------------------------------------------------------

def _check_lease_seconds(lease_seconds: int) -> None:
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) \
            or not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError(f"lease_seconds entre 1 et {MAX_LEASE_SECONDS}")


def _limit_statement_time(conn, timeout_ms: Optional[int]) -> None:
    if timeout_ms is None:
        return
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 3_600_000:
        raise ValueError("timeout_ms entre 1 et 3600000")
    conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout_ms),))


def lease_token(attempts: int, holder: str) -> str:
    """Jeton d'exclusion attendu par le trigger: `<attempts>:<locked_by>`."""
    return f"{int(attempts)}:{holder}"


def _declare_lease_holder(conn, job: JobRecord, holder: Optional[str]) -> None:
    """Declare a la base, pour CETTE transaction seulement, la tentative dont on se reclame."""
    token = lease_token(job.attempts, holder) if holder else ""
    conn.execute("SELECT set_config('app.job_lease_token', %s, true)", (token,))


def _explain_lost_transition(conn, session: TenantSession, job: JobRecord, holder: Optional[str]) -> None:
    """Aucune ligne mise a jour: dire pourquoi sans jamais reveler un autre tenant."""
    current = fetch_job(conn, session, job.id)
    if (current.status != JobStatus.RUNNING.value or current.locked_by != holder
            or current.attempts != job.attempts):
        raise JobLeaseLost(job.id)
    raise JobStateError(f"travail {current.status}: transition refusee")  # pragma: no cover - defense


def _error_code(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    text = str(code).strip()
    if not text or len(text) > 100:
        raise ValueError("code d'erreur non vide, 100 caracteres au plus")
    return text


def _safe_error(message: str) -> str:
    """Message d'erreur publiable: tronque, sans saut de ligne, sans chemin absolu, sans secret.

    Un message qui EST un chemin est masque en entier; dans un texte, les identifiants
    d'URL, jetons, chemins absolus et e-mails sont masques (`redact_text`, 004.3). La file
    n'est pas un journal d'exceptions (ADR-004.2-006).
    """
    text = " ".join(str(message or "").split())
    if text.startswith("/"):
        return "[redacted]"
    return redact_text(text)[:2000]
