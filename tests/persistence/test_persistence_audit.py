"""Journal d'audit en ajout seul (Mission 004.2): contenu, immuabilite, isolation, retention.

Ce que ces tests etablissent:
- INSERT reussit, UPDATE est refuse, DELETE est refuse avant le plancher;
- les deux refus tiennent en SQL brut, pas seulement dans le code applicatif;
- une organisation n'ecrit ni ne lit le journal d'une autre;
- aucun secret ni chemin absolu ne survit a l'ecriture.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from mervio.persistence import audit
from mervio.persistence.audit import Action, ActorType, Outcome, ResourceType
from mervio.persistence.errors import NotFound, PermissionDenied
from mervio.persistence.tenancy import Role, TenantContext, TenantSession, add_member, ensure_user

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def write(tenant, *, action=Action.JOB_ENQUEUED, resource_type=ResourceType.JOB, resource_id=None,
          correlation_id=None, **kwargs):
    return audit.record_event(tenant.session, action=action, resource_type=resource_type,
                              resource_id=resource_id or uuid.uuid4(),
                              correlation_id=correlation_id or uuid.uuid4(), **kwargs)


def admin_session(database, tenant, label="audit") -> TenantSession:
    user_id = ensure_user(database, f"test|{label}|admin")
    add_member(tenant.session, user_id=user_id, role=Role.ADMIN)
    return TenantSession(database, TenantContext(tenant.organization_id, user_id))


def set_tenant(conn, organization_id, user_id):
    conn.execute("SELECT set_config('app.organization_id', %s, false), set_config('app.user_id', %s, false)",
                 (str(organization_id), str(user_id)))


# -- contenu --------------------------------------------------------------------------------

def test_an_event_answers_the_seven_questions(tenant_a):
    resource, correlation = uuid.uuid4(), uuid.uuid4()
    event = write(tenant_a, action=Action.JOB_SUCCEEDED, resource_type=ResourceType.JOB, resource_id=resource,
                  correlation_id=correlation, actor_type=ActorType.WORKER, actor_id=tenant_a.owner_id,
                  store_id=tenant_a.store_id, outcome=Outcome.SUCCEEDED, metadata={"duration_ms": 12})
    assert event.actor_type == "worker" and event.actor_id == tenant_a.owner_id  # QUI
    assert event.action == "job.succeeded"                                        # QUOI
    assert (event.resource_type, event.resource_id) == ("job", resource)          # QUELLE RESSOURCE
    assert (event.organization_id, event.store_id) == (tenant_a.organization_id, tenant_a.store_id)
    assert event.created_at is not None                                           # QUAND
    assert event.correlation_id == correlation                                    # QUELLE CORRELATION
    assert event.outcome == "succeeded"                                           # QUEL RESULTAT
    assert event.metadata == {"duration_ms": 12}


def test_a_user_event_always_names_its_actor(tenant_a):
    with pytest.raises(psycopg.errors.CheckViolation):
        write(tenant_a, actor_type=ActorType.USER, actor_id=None)


def test_a_system_event_may_have_no_actor(tenant_a):
    assert write(tenant_a, action=Action.JOB_RECOVERED, actor_type=ActorType.SYSTEM).actor_id is None


def test_an_unknown_action_is_refused_before_the_database(tenant_a):
    with pytest.raises(ValueError):
        write(tenant_a, action="job.exploded")


def test_an_unknown_resource_type_is_refused(tenant_a):
    with pytest.raises(ValueError):
        write(tenant_a, resource_type="invoice")


def test_every_declared_action_can_be_written(tenant_a):
    for action in Action:
        resource = ResourceType.ORGANIZATION if action.value.startswith("purge") else ResourceType.JOB
        assert write(tenant_a, action=action, resource_type=resource) is not None


def test_metadata_defaults_to_an_empty_object(tenant_a):
    assert write(tenant_a).metadata == {}


# -- redaction ------------------------------------------------------------------------------

def test_a_secret_never_enters_the_audit(tenant_a):
    event = write(tenant_a, metadata={"api_key": "sk-live-1234", "job_type": "import"})
    assert event.metadata == {"api_key": "[redacted]", "job_type": "import"}


def test_an_absolute_path_never_enters_the_audit(tenant_a):
    assert write(tenant_a, metadata={"source": "/Users/someone/orders.csv"}).metadata["source"] == "[redacted]"


def test_a_long_value_is_truncated_before_storage(tenant_a):
    assert len(write(tenant_a, metadata={"detail": "x" * 9000}).metadata["detail"]) < 9000


# -- immuabilite ----------------------------------------------------------------------------

def test_the_repository_exposes_no_update_and_no_ordinary_delete():
    exported = {name for name in vars(audit) if not name.startswith("_")}
    assert not {name for name in exported if "update" in name or name in ("delete", "delete_event")}
    assert {"record", "record_event", "list_events", "purge_expired_events"} <= exported


def test_the_application_role_cannot_update_the_audit(owner):
    assert owner.execute("SELECT has_table_privilege('mervio_app', 'audit_events', 'INSERT')").fetchone()[0]
    assert owner.execute("SELECT has_table_privilege('mervio_app', 'audit_events', 'SELECT')").fetchone()[0]
    assert not owner.execute("SELECT has_table_privilege('mervio_app', 'audit_events', 'UPDATE')").fetchone()[0]
    assert not owner.execute("SELECT has_table_privilege('mervio_app', 'audit_events', 'TRUNCATE')").fetchone()[0]


def test_raw_sql_insert_succeeds_then_update_is_rejected(app_conn, tenant_a):
    event = write(tenant_a)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("UPDATE audit_events SET action = 'job.failed' WHERE id = %s", (event.id,))


def test_even_the_table_owner_cannot_update_the_audit(owner, tenant_a):
    """Trigger, et non simple droit: le proprietaire du schema est couvert lui aussi."""
    event = write(tenant_a)
    set_tenant(owner, tenant_a.organization_id, tenant_a.owner_id)
    with pytest.raises(psycopg.errors.RestrictViolation):
        owner.execute("UPDATE audit_events SET outcome = 'succeeded' WHERE id = %s", (event.id,))


def test_raw_sql_delete_of_a_recent_event_is_rejected(app_conn, tenant_a):
    event = write(tenant_a)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    assert app_conn.execute("DELETE FROM audit_events WHERE id = %s", (event.id,)).rowcount == 0
    assert audit.count_events(tenant_a.session) == 1


def test_even_the_table_owner_cannot_delete_a_recent_event(owner, tenant_a):
    event = write(tenant_a)
    set_tenant(owner, tenant_a.organization_id, tenant_a.owner_id)
    assert owner.execute("DELETE FROM audit_events WHERE id = %s", (event.id,)).rowcount == 0


# -- lecture --------------------------------------------------------------------------------

def test_reading_the_audit_requires_an_administrator(db, tenant_a):
    write(tenant_a)
    analyst_id = ensure_user(db, "test|audit|analyst")
    add_member(tenant_a.session, user_id=analyst_id, role=Role.ANALYST)
    analyst = TenantSession(db, TenantContext(tenant_a.organization_id, analyst_id))
    with pytest.raises(PermissionDenied):
        audit.list_events(analyst)
    assert len(audit.list_events(admin_session(db, tenant_a))) == 1


def test_events_are_read_in_chronological_order(db, tenant_a):
    for action in (Action.JOB_ENQUEUED, Action.JOB_CLAIMED, Action.JOB_SUCCEEDED):
        write(tenant_a, action=action)
    assert [event.action for event in audit.list_events(admin_session(db, tenant_a))] == [
        "job.enqueued", "job.claimed", "job.succeeded"]


def test_one_correlation_id_gathers_a_whole_execution(db, tenant_a):
    correlation = uuid.uuid4()
    job_id = uuid.uuid4()
    for action in (Action.JOB_ENQUEUED, Action.JOB_CLAIMED, Action.IMPORT_STARTED, Action.IMPORT_SUCCEEDED,
                   Action.JOB_SUCCEEDED):
        write(tenant_a, action=action, resource_id=job_id, correlation_id=correlation)
    write(tenant_a, action=Action.JOB_ENQUEUED)  # une autre execution
    gathered = audit.list_events(admin_session(db, tenant_a), correlation_id=correlation)
    assert [event.action for event in gathered] == [
        "job.enqueued", "job.claimed", "import.started", "import.succeeded", "job.succeeded"]


def test_the_history_of_one_resource_is_readable(db, tenant_a):
    job_id = uuid.uuid4()
    write(tenant_a, action=Action.JOB_ENQUEUED, resource_id=job_id)
    write(tenant_a, action=Action.JOB_ENQUEUED, resource_id=uuid.uuid4())
    events = audit.list_events(admin_session(db, tenant_a), resource_type=ResourceType.JOB, resource_id=job_id)
    assert [event.resource_id for event in events] == [job_id]


def test_events_are_filtered_by_action_and_store(db, tenant_a):
    write(tenant_a, action=Action.JOB_FAILED, store_id=tenant_a.store_id)
    write(tenant_a, action=Action.JOB_SUCCEEDED)
    session = admin_session(db, tenant_a)
    assert len(audit.list_events(session, action=[Action.JOB_FAILED])) == 1
    assert len(audit.list_events(session, store_id=tenant_a.store_id)) == 1


# -- isolation ------------------------------------------------------------------------------

def test_a_tenant_never_reads_another_tenant_audit(db, tenant_a, tenant_b):
    write(tenant_b)
    session = admin_session(db, tenant_a)
    assert audit.list_events(session) == []
    assert audit.count_events(session) == 0


def test_a_tenant_never_writes_an_audit_event_for_another(app_conn, tenant_a, tenant_b):
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(
            "INSERT INTO audit_events (id, organization_id, actor_type, action, resource_type, resource_id, "
            "correlation_id, outcome) VALUES (%s, %s, 'system', 'job.enqueued', 'job', %s, %s, 'succeeded')",
            (uuid.uuid4(), tenant_b.organization_id, uuid.uuid4(), uuid.uuid4()))


def test_raw_sql_sees_no_foreign_event(app_conn, tenant_a, tenant_b):
    write(tenant_b)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    assert app_conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0


def test_a_tenant_cannot_delete_a_foreign_event(app_conn, tenant_a, tenant_b):
    event = write(tenant_b)
    set_tenant(app_conn, tenant_a.organization_id, tenant_a.owner_id)
    assert app_conn.execute("DELETE FROM audit_events WHERE id = %s", (event.id,)).rowcount == 0


def test_an_event_cannot_reference_another_tenant_store(tenant_a, tenant_b):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        write(tenant_a, store_id=tenant_b.store_id)


def test_a_non_member_reads_nothing(db, tenant_a, tenant_b):
    write(tenant_a)
    stranger = TenantSession(db, TenantContext(tenant_a.organization_id, tenant_b.owner_id))
    with pytest.raises(NotFound):
        audit.list_events(stranger)


# -- retention ------------------------------------------------------------------------------

def test_purging_the_audit_requires_the_owner(db, tenant_a):
    with pytest.raises(PermissionDenied):
        audit.purge_expired_events(admin_session(db, tenant_a), before=NOW)


def test_a_recent_event_survives_the_purge(tenant_a):
    write(tenant_a)
    assert audit.purge_expired_events(tenant_a.session, before=datetime.now(timezone.utc)) == 0
    assert audit.count_events(tenant_a.session) == 1


def test_an_old_event_is_purged(owner, tenant_a):
    event = write(tenant_a)
    _age(owner, tenant_a, event.id, days=400)
    assert audit.purge_expired_events(tenant_a.session,
                                      before=datetime.now(timezone.utc) - timedelta(days=365)) == 1
    assert audit.count_events(tenant_a.session) == 0


def test_the_database_floor_protects_an_event_younger_than_thirty_days(owner, tenant_a):
    """Meme avec une retention absurde, la base refuse d'effacer une trace recente."""
    event = write(tenant_a)
    _age(owner, tenant_a, event.id, days=20)
    assert audit.purge_expired_events(tenant_a.session, before=datetime.now(timezone.utc)) == 0
    assert audit.count_events(tenant_a.session) == 1


def test_the_purge_stops_at_the_current_tenant(owner, tenant_a, tenant_b):
    mine, theirs = write(tenant_a), write(tenant_b)
    _age(owner, tenant_a, mine.id, days=400)
    _age(owner, tenant_b, theirs.id, days=400)
    assert audit.purge_expired_events(tenant_a.session,
                                      before=datetime.now(timezone.utc) - timedelta(days=365)) == 1
    assert audit.count_events(tenant_b.session) == 1


def _age(owner_conn, tenant, event_id, *, days: int) -> None:
    """Vieillit une ligne d'audit pour tester la retention sans attendre un an.

    Ce detour est reserve au test: il exige a la fois la propriete de la table (pour
    desactiver le trigger) et le contexte du tenant (RLS FORCEE s'applique au
    proprietaire). Aucun chemin applicatif ne permet cela.
    """
    set_tenant(owner_conn, tenant.organization_id, tenant.owner_id)
    owner_conn.execute("ALTER TABLE audit_events DISABLE TRIGGER audit_events_forbid_update")
    try:
        updated = owner_conn.execute(
            "UPDATE audit_events SET created_at = now() - make_interval(days => %s) WHERE id = %s",
            (days, event_id)).rowcount
        assert updated == 1, "la ligne d'audit a vieillir est invisible: contexte de tenant absent"
    finally:
        owner_conn.execute("ALTER TABLE audit_events ENABLE TRIGGER audit_events_forbid_update")
