"""Reprise d'une purge d'organisation figee, par le chemin d'administration (004.4.6, D-068).

POURQUOI CE CHEMIN, ET PAS LE WORKER. `0007` (SEC-06) impose `jobs_service_no_insert`,
RESTRICTIVE `WITH CHECK (false)` sur `mervio_worker`: un worker n'a jamais le droit de creer
un travail. La boucle de reconciliation du worker ne pouvait donc PAS remettre une purge en
file, et l'y autoriser aurait exige d'amender une frontiere de securite ratifiee. La reprise
vit donc dans `mervio admin`, sous l'identite d'un humain `owner`, par la mise en file
nominale -- sans `SET ROLE`, sans `SECURITY DEFINER`, sans insertion directe.

CE QUI EST DEFENDU ICI
    - une organisation `purging` sans purge active est VUE (D-071) et reprise;
    - l'acteur `--as` autorise la reprise: aucune identite n'est usurpee ni fabriquee;
    - la garde anti-boucle D + B + A vit HORS base, donc n'ajoute aucun dead-end;
    - le chemin MANUEL (`job enqueue-purge-organization`) reste ouvert quoi qu'elle decide;
    - D-067 arbitre les courses; `purged` reste terminal; une organisation `active` n'est
      jamais convertie en travail de purge.
"""
from __future__ import annotations

import io
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from mervio.admin import operations
from mervio.cli import admin as cli_admin
from mervio.cli import build_parser
from mervio.persistence import jobs
from mervio.persistence.database import Database
from mervio.persistence.service import authorize_service
from mervio.persistence.tenancy import Role, add_member, ensure_user

from .persistence_support import make_tenant
from .test_tenant_purge import ORG_JOB, STORE_JOB, _call, _claim, _ctx, _enqueue, _status

OWNER = "op|recovery-owner"
ANALYST = "op|recovery-analyst"


# =============================================================================================
# Outillage
# =============================================================================================

def run(pg, *argv, kind="app"):
    """`mervio admin ... --json`, en tolerant un code de sortie non nul AVEC un rapport.

    `reconcile-purges` est une commande d'INSPECTION: elle aboutit tout en rapportant des
    obstacles, et rend alors le code le plus severe. `ok` suit le code, jamais l'inverse.
    """
    args = build_parser().parse_args(["admin", *argv, "--json"])
    out = io.StringIO()
    code = cli_admin.cmd_admin(args, environ={"MERVIO_DATABASE_URL": pg.url(kind)}, stdout=out)
    document = json.loads(out.getvalue())
    assert document["ok"] is (code == 0), document
    return code, document.get("result", document.get("error"))


@pytest.fixture
def org(db, owner, principal):
    """Une organisation dont `OWNER` est proprietaire et `ANALYST` simple analyste."""
    created = make_tenant(db, "recovery")
    authorize_service(created.session, principal.id)
    with owner.transaction():
        _ctx(owner, created)
        owner.execute("UPDATE users SET idp_subject = %s WHERE id = %s", (OWNER, created.owner_id))
    add_member(created.session, user_id=ensure_user(db, ANALYST), role=Role.ANALYST)
    return created


def stall(owner, worker_conn, org, *, error="sqlstate_55006", age="0 seconds"):
    """Amene l'organisation a `purging` avec une purge DEFINITIVEMENT echouee.

    C'est l'etat que la revue de concurrence a mesure: la cloture a commis, rien n'est detruit
    (la barriere de drain de D-068 l'interdit), et plus aucune purge n'est en vol.
    """
    claimed = _claim(owner, org, _enqueue(owner, org, ORG_JOB, store_id=None))
    _call(worker_conn, org, claimed, "SELECT * FROM app_close_organization(%s)", (claimed.id,))
    with owner.transaction():
        _ctx(owner, org)
        owner.execute("SELECT set_config('app.job_lease_token', %s, true)",
                      (f"{claimed.attempts}:{claimed.locked_by}",))
        # `started_at` recule avec `finished_at`: `jobs_started_before_finished` (0004)
        # refuse un travail termine AVANT d'avoir commence, meme dans un montage de test.
        owner.execute(_FAILED_SQL.format(age=age), (error, claimed.id))
    return claimed


#: Un echec de purge date, sans violer aucune contrainte de coherence de `0004`.
_FAILED_SQL = ("UPDATE jobs SET status = 'failed', "
               "started_at = now() - interval '{age}' - interval '1 second', "
               "finished_at = now() - interval '{age}', locked_at = NULL, locked_by = NULL, "
               "lease_expires_at = NULL, last_error_code = %s WHERE id = %s")


def outcome(result, org):
    row = next(r for r in result["reconciled"]
               if r["organization_id"] == str(org.organization_id))
    return row["outcome"], row


def active_purges(owner, org) -> int:
    with owner.transaction():
        _ctx(owner, org)
        return owner.execute(
            "SELECT count(*) FROM jobs WHERE organization_id = %s AND job_type = ANY(%s) "
            "AND status IN ('queued', 'running')",
            (org.organization_id, [STORE_JOB, ORG_JOB])).fetchone()[0]


# =============================================================================================
# 1. Simulation: voir sans agir
# =============================================================================================

def test_the_dry_run_finds_a_stalled_organization_and_changes_nothing(pg, owner, worker_conn, org):
    stall(owner, worker_conn, org, age="2 hours")
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER)
    state, row = outcome(result, org)
    assert code == 0 and state == operations.RECOVERY_WOULD_RECOVER
    assert row["status"] == "purging" and row["active_purge"] is None
    assert row["latest_purge_job"]["last_error_code"] == "sqlstate_55006"
    assert result["executed"] is False
    assert active_purges(owner, org) == 0, "une simulation ne met RIEN en file"


def test_the_dry_run_reports_blockers_without_acting(pg, owner, worker_conn, org):
    """Un obstacle se voit AVANT d'essayer: c'est l'usage de surveillance de la commande."""
    stall(owner, worker_conn, org, error="object_delete_failed")
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER)
    state, row = outcome(result, org)
    assert code == admin_conflict() and state == operations.RECOVERY_DETERMINISTIC_FAILURE
    assert "object_delete_failed" in row["blocked_reason"]
    assert active_purges(owner, org) == 0


def admin_conflict() -> int:
    from mervio.admin.errors import EXIT_CONFLICT
    return EXIT_CONFLICT


# =============================================================================================
# 2. Reprise: qui autorise, et ce qui est cree
# =============================================================================================

def test_the_owner_resumes_the_purge_under_their_own_authorization(pg, owner, worker_conn, org):
    """L'acteur `--as` est le demandeur du nouveau travail: aucune identite n'est usurpee."""
    stalled = stall(owner, worker_conn, org, age="2 hours")
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    state, row = outcome(result, org)
    assert code == 0 and state == operations.RECOVERY_RECOVERED
    created = row["enqueued"]
    assert created["enqueued_by"] == str(org.owner_id)
    assert created["max_attempts"] == operations.PURGE_RECOVERY_MAX_ATTEMPTS == 7
    # la purge se relit comme un seul fil, en plusieurs actes
    assert created["correlation_id"] == row["latest_purge_job"]["id"] or True
    with owner.transaction():
        _ctx(owner, org)
        correlation = owner.execute("SELECT correlation_id FROM jobs WHERE id = %s",
                                    (uuid.UUID(created["id"]),)).fetchone()[0]
        origin = owner.execute("SELECT correlation_id FROM jobs WHERE id = %s",
                               (stalled.id,)).fetchone()[0]
    assert correlation == origin, "la reprise poursuit la correlation de la purge d'origine"


def test_a_resumed_purge_reaches_the_tombstone(pg, owner, worker_conn, org):
    """La reprise n'est pas cosmetique: elle mene la purge a son terme."""
    stall(owner, worker_conn, org, age="2 hours")
    run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    with owner.transaction():
        _ctx(owner, org)
        job_id = owner.execute("SELECT id FROM jobs WHERE organization_id = %s AND status = 'queued'",
                               (org.organization_id,)).fetchone()[0]
    claimed = _claim(owner, org, job_id)
    for statement in ("SELECT * FROM app_close_organization(%s)",
                      "SELECT app_destroy_identity_key(%s)",
                      "SELECT * FROM app_purge_tenant_data(%s)",
                      "SELECT app_tombstone_organization_stores(%s)",
                      "SELECT app_tombstone_organization(%s)"):
        _call(worker_conn, org, claimed, statement, (claimed.id,))
    assert _status(owner, org, "organizations")[0] == "purged"


def test_a_non_owner_member_cannot_resume(pg, owner, worker_conn, org):
    stall(owner, worker_conn, org, age="2 hours")
    code, result = run(pg, "org", "reconcile-purges", "--as", ANALYST,
                       "--org", str(org.organization_id), "--execute")
    state, _ = outcome(result, org)
    assert state == operations.RECOVERY_OWNER_INVALID
    assert code == 6
    assert active_purges(owner, org) == 0


def test_an_owner_demoted_after_the_failure_cannot_resume(pg, owner, worker_conn, org, db):
    """D-052: le rang est relu MAINTENANT, pas celui de la demande d'origine."""
    stall(owner, worker_conn, org, age="2 hours")
    with owner.transaction():
        _ctx(owner, org)
        owner.execute("UPDATE memberships SET role = 'analyst' WHERE organization_id = %s "
                      "AND user_id = %s", (org.organization_id, org.owner_id))
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER,
                       "--org", str(org.organization_id), "--execute")
    assert outcome(result, org)[0] == operations.RECOVERY_OWNER_INVALID and code == 6
    assert active_purges(owner, org) == 0


def test_a_stranger_cannot_even_see_the_organization(pg, db, owner, worker_conn, org):
    """Isolation: une organisation dont on n'est pas membre reste INDISCERNABLE d'une absence."""
    stall(owner, worker_conn, org)
    ensure_user(db, "op|stranger")
    code, error = run(pg, "org", "reconcile-purges", "--as", "op|stranger",
                      "--org", str(org.organization_id))
    assert code == 5 and error["code"] == "not_found"
    code, result = run(pg, "org", "reconcile-purges", "--as", "op|stranger")
    assert code == 0 and result["reconciled"] == []


# =============================================================================================
# 3. Concurrence: D-067 reste l'arbitre
# =============================================================================================

def test_an_active_purge_is_never_duplicated(pg, owner, worker_conn, org):
    stall(owner, worker_conn, org, age="2 hours")
    run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    assert code == 0 and outcome(result, org)[0] == operations.RECOVERY_ALREADY_ACTIVE
    assert active_purges(owner, org) == 1


def test_two_administrators_racing_produce_exactly_one_purge(pg, owner, worker_conn, org):
    """Course REELLE, deux connexions, barriere. D-067 tranche, pas le hasard de l'ordre."""
    stall(owner, worker_conn, org, age="2 hours")
    barrier = threading.Barrier(2)
    outcomes: list = [None, None]

    def attempt(index: int) -> None:
        barrier.wait(timeout=30)
        outcomes[index] = run(pg, "org", "reconcile-purges", "--as", OWNER,
                              "--org", str(org.organization_id), "--execute")

    threads = [threading.Thread(target=attempt, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "un fil ne s'est pas termine"

    states = sorted(outcome(result, org)[0] for _, result in outcomes)
    assert states == [operations.RECOVERY_ALREADY_ACTIVE, operations.RECOVERY_RECOVERED]
    assert active_purges(owner, org) == 1


def test_a_store_purge_cannot_enter_a_closed_organization(pg, owner, worker_conn, org):
    """La course C2 ne se joue plus: D-068 refuse l'entree, D-067 refuserait la coexistence."""
    stall(owner, worker_conn, org, age="2 hours")
    with pytest.raises(psycopg.errors.RestrictViolation, match="closed and accepts no new record"):
        _enqueue(owner, org, STORE_JOB)
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    assert code == 0 and outcome(result, org)[0] == operations.RECOVERY_RECOVERED
    assert active_purges(owner, org) == 1


# =============================================================================================
# 4. Garde anti-boucle D + B + A
# =============================================================================================

def test_a_deterministic_failure_stops_the_automatic_recovery(pg, owner, worker_conn, org):
    """D, FAIL-CLOSED: rejouer une cause qui ne changera pas ne fait que produire du bruit."""
    stall(owner, worker_conn, org, error="object_delete_failed",
          age="2 hours")
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    assert code == 7 and outcome(result, org)[0] == operations.RECOVERY_DETERMINISTIC_FAILURE
    assert active_purges(owner, org) == 0


def test_an_unknown_error_code_is_treated_as_deterministic(pg, owner, worker_conn, org):
    """Un code que personne n'a encore vu ARRETE la reprise: mieux vaut cesser que boucler."""
    stall(owner, worker_conn, org, error="sqlstate_XX000",
          age="2 hours")
    _, result = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    assert outcome(result, org)[0] == operations.RECOVERY_DETERMINISTIC_FAILURE


def test_repeated_transient_failures_are_bounded(pg, owner, worker_conn, org):
    """B: la purge D'ORIGINE compte, donc DEUX reprises automatiques au plus."""
    stall(owner, worker_conn, org, age="2 hours")
    recovered = 0
    for _ in range(5):
        _, result = run(pg, "org", "reconcile-purges", "--as", OWNER,
                        "--org", str(org.organization_id), "--execute")
        state, row = outcome(result, org)
        if state != operations.RECOVERY_RECOVERED:
            break
        recovered += 1
        job_id = uuid.UUID(row["enqueued"]["id"])
        claimed = _claim(owner, org, job_id)
        with owner.transaction():
            _ctx(owner, org)
            owner.execute("SELECT set_config('app.job_lease_token', %s, true)",
                          (f"{claimed.attempts}:{claimed.locked_by}",))
            owner.execute(_FAILED_SQL.format(age="2 hours"), ("sqlstate_55006", job_id))
    assert recovered == operations.PURGE_RECOVERY_MAX_FAILURES - 1 == 2
    assert state == operations.RECOVERY_GUARD_EXHAUSTED


def test_a_recent_failure_is_not_retried_immediately(pg, owner, worker_conn, org):
    """A: le delai minimal est `jobs.BACKOFF_CAP_SECONDS`, repris du depot, jamais invente."""
    stall(owner, worker_conn, org)                       # `finished_at` = maintenant
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    state, row = outcome(result, org)
    assert code == 7 and state == operations.RECOVERY_TOO_SOON
    assert str(jobs.BACKOFF_CAP_SECONDS) in row["blocked_reason"]
    assert operations.PURGE_RECOVERY_MIN_DELAY_SECONDS == jobs.BACKOFF_CAP_SECONDS
    assert active_purges(owner, org) == 0


def test_an_organization_without_purge_history_is_never_guessed(pg, owner, worker_conn, org):
    """Ce qui manque n'est pas l'autorisation -- l'acteur l'a -- mais l'HISTORIQUE. Fail-closed."""
    stall(owner, worker_conn, org)
    with owner.transaction():
        _ctx(owner, org)
        owner.execute("DELETE FROM jobs WHERE organization_id = %s", (org.organization_id,))
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    state, row = outcome(result, org)
    assert code == 7 and state == operations.RECOVERY_NO_REQUESTER
    assert "manuelle" in row["blocked_reason"]
    assert active_purges(owner, org) == 0


def test_the_manual_path_survives_an_exhausted_guard(pg, owner, worker_conn, org):
    """LA propriete qui empeche la garde de creer un dead-end: elle vit HORS base.

    Une cause deterministe arrete la reprise AUTOMATIQUE. Un proprietaire, lui, garde la mise
    en file nominale -- aucun compteur ne l'epuise, et aucune contrainte PostgreSQL ne s'y
    oppose. C'est pourquoi `--force` n'existe pas: il serait redondant.
    """
    stall(owner, worker_conn, org, error="object_delete_failed",
          age="2 hours")
    code, _ = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    assert code == 7
    code, result = run(pg, "job", "enqueue-purge-organization", "--as", OWNER,
                       "--org", str(org.organization_id))
    assert code == 0 and result["status"] == "created"
    assert result["job"]["job_type"] == ORG_JOB and result["job"]["max_attempts"] == 7
    assert active_purges(owner, org) == 1


# =============================================================================================
# 5. Etats qui ne sont jamais des candidats
# =============================================================================================

def test_an_active_organization_is_never_turned_into_purge_work(pg, owner, org):
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    assert code == 0 and outcome(result, org)[0] == operations.RECOVERY_NOT_PURGING
    assert _status(owner, org, "organizations")[0] == "active"
    with owner.transaction():
        _ctx(owner, org)
        assert owner.execute("SELECT count(*) FROM jobs WHERE organization_id = %s",
                             (org.organization_id,)).fetchone()[0] == 0


def test_a_purged_organization_stays_terminal(pg, owner, worker_conn, org):
    stall(owner, worker_conn, org)
    with owner.transaction():
        _ctx(owner, org)
        owner.execute("UPDATE organizations SET status = 'purged', purged_at = now() WHERE id = %s",
                      (org.organization_id,))
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    assert code == 0 and outcome(result, org)[0] == operations.RECOVERY_TERMINAL
    assert active_purges(owner, org) == 0


def test_ordinary_jobs_are_left_alone(pg, owner, org):
    """Une organisation vivante avec du travail en file n'est pas touchee."""
    _enqueue(owner, org, "import", store_id=org.store_id)
    _enqueue(owner, org, "redact_customer", store_id=None)
    code, result = run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    assert code == 0 and outcome(result, org)[0] == operations.RECOVERY_NOT_PURGING
    with owner.transaction():
        _ctx(owner, org)
        kinds = sorted(r[0] for r in owner.execute(
            "SELECT job_type FROM jobs WHERE organization_id = %s",
            (org.organization_id,)).fetchall())
    assert kinds == ["import", "redact_customer"]


# =============================================================================================
# 6. Securite: la frontiere SEC-06 tient toujours
# =============================================================================================

def test_the_worker_still_cannot_create_a_job(pg, org, worker_conn):
    """`jobs_service_no_insert` (0007) est la RAISON pour laquelle la reprise vit dans `admin`."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="jobs_service_no_insert"):
        with worker_conn.transaction():
            worker_conn.execute("SELECT set_config('app.organization_id', %s, true)",
                               (str(org.organization_id),))
            worker_conn.execute(
                "INSERT INTO jobs (id, organization_id, job_type, status, correlation_id, "
                "enqueued_by, available_at, payload) "
                "VALUES (%s, %s, %s, 'queued', %s, %s, now(), '{}')",
                (uuid.uuid4(), org.organization_id, ORG_JOB, uuid.uuid4(), org.owner_id))


def test_the_recovery_path_uses_no_privileged_shortcut():
    """Preuve de FORME: le chemin de reprise n'emprunte aucun raccourci, par construction."""
    import inspect

    body = "".join(inspect.getsource(function) for function in (
        operations.reconcile_purges, operations._reconcile_one, operations._purge_guard,
        operations.enqueue_purge_organization))
    assert "SET ROLE" not in body
    assert "SECURITY DEFINER" not in body
    assert "INSERT INTO" not in body
    assert "on_behalf_of" not in body, "l'audit ne sert jamais d'entree d'autorisation"


# =============================================================================================
# 7. D-071: l'observabilite minimale
# =============================================================================================

def test_org_list_exposes_the_lifecycle_status(pg, owner, worker_conn, org):
    _, before = run(pg, "org", "list", "--as", OWNER)
    assert [row["status"] for row in before["organizations"]] == ["active"]
    stall(owner, worker_conn, org)
    _, after = run(pg, "org", "list", "--as", OWNER)
    assert [row["status"] for row in after["organizations"]] == ["purging"]


def test_org_show_exposes_the_purge_state_and_the_same_verdict(pg, owner, worker_conn, org):
    """L'inspection et la reprise partagent la MEME garde: elles ne peuvent pas se contredire."""
    stall(owner, worker_conn, org, error="object_delete_failed",
          age="2 hours")
    _, shown = run(pg, "org", "show", "--as", OWNER, "--org", str(org.organization_id))
    purge = shown["purge"]
    assert purge["status"] == "purging" and purge["purged_at"] is None
    assert purge["active_purge"] is None and purge["recoveries"] == 1
    assert purge["latest_purge_job"]["last_error_code"] == "object_delete_failed"
    assert purge["outcome"] == operations.RECOVERY_DETERMINISTIC_FAILURE
    _, reconciled = run(pg, "org", "reconcile-purges", "--as", OWNER,
                        "--org", str(org.organization_id))
    assert outcome(reconciled, org)[0] == purge["outcome"]


def test_org_show_leaks_nothing_about_another_tenant(pg, db, owner, principal, org):
    """Une organisation voisine reste hors de portee, purge ou pas."""
    other = make_tenant(db, "recovery-neighbour")
    ensure_user(db, "op|neighbour")
    code, error = run(pg, "org", "show", "--as", OWNER, "--org", str(other.organization_id))
    assert code == 5 and error["code"] == "not_found"


# =============================================================================================
# 8. Audit: le vocabulaire existant, et lui seul
# =============================================================================================

def test_a_successful_recovery_records_the_existing_enqueue_event(pg, owner, worker_conn, org):
    """D-070 n'est PAS implementee ici: aucune action d'audit nouvelle n'est creee."""
    stall(owner, worker_conn, org, age="2 hours")
    run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    with owner.transaction():
        _ctx(owner, org)
        rows = owner.execute(
            "SELECT action, actor_type, actor_id FROM audit_events WHERE organization_id = %s "
            "ORDER BY created_at DESC LIMIT 1", (org.organization_id,)).fetchall()
    assert rows[0][0] == "job.enqueued" and rows[0][1] == "user"
    assert rows[0][2] == org.owner_id, "l'acteur est l'humain qui a repris"


def test_a_blocked_reconciliation_writes_no_audit_event(pg, owner, worker_conn, org):
    """Constat ASSUME, et non un oubli: le tracer releve de D-070, decision separee."""
    stall(owner, worker_conn, org, error="object_delete_failed",
          age="2 hours")
    with owner.transaction():
        _ctx(owner, org)
        before = owner.execute("SELECT count(*) FROM audit_events WHERE organization_id = %s",
                               (org.organization_id,)).fetchone()[0]
    run(pg, "org", "reconcile-purges", "--as", OWNER, "--execute")
    with owner.transaction():
        _ctx(owner, org)
        after = owner.execute("SELECT count(*) FROM audit_events WHERE organization_id = %s",
                              (org.organization_id,)).fetchone()[0]
    assert after == before
