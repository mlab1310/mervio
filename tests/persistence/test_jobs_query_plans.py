"""Plans d'execution de la file (Mission 004.2).

Lecon de 004.1: un index mal ordonne ne casse rien, il rend seulement l'execution
quadratique quand les donnees grossissent, et personne ne le voit avant la
production. Ces tests figent le plan attendu sur un volume suffisant pour que le
planificateur cesse de preferer un parcours complet.

Ils gardent la propriete qui compte: prendre le prochain travail coute un acces
par index, jamais un parcours de la file ni un tri.
"""
from __future__ import annotations

import json
import uuid

import pytest


VOLUME = 3000


@pytest.fixture
def filled_queue(tenant_a, owner):
    """File volumineuse: sans volume, le planificateur choisit un parcours complet a juste titre."""
    rows = [(uuid.uuid4(), tenant_a.organization_id, "analysis", "queued", index % 5, uuid.uuid4(),
             tenant_a.owner_id) for index in range(VOLUME)]
    owner.execute("SELECT set_config('app.organization_id', %s, false), set_config('app.user_id', %s, false)",
                  (str(tenant_a.organization_id), str(tenant_a.owner_id)))
    with owner.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO jobs (id, organization_id, job_type, status, priority, correlation_id, enqueued_by, "
            "payload, available_at) VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, now())", rows)
    owner.execute("ANALYZE jobs")
    return tenant_a


def plan(conn, query, parameters) -> dict:
    return conn.execute("EXPLAIN (FORMAT JSON) " + query, parameters).fetchone()[0][0]["Plan"]


def nodes(node) -> list:
    return [node] + [child for plan_child in node.get("Plans", []) for child in nodes(plan_child)]


def test_claiming_the_next_job_uses_the_ready_index_and_never_scans_the_queue(filled_queue, owner):
    tree = plan(owner,
                "SELECT id FROM jobs WHERE organization_id = %s AND status = 'queued' AND available_at <= now() "
                "AND job_type = ANY(%s) AND attempts < max_attempts "
                "ORDER BY priority DESC, created_at ASC, id ASC LIMIT 1",
                (filled_queue.organization_id, ["analysis"]))
    used = [node for node in nodes(tree) if node.get("Index Name") == "jobs_ready_idx"]
    assert used, json.dumps(tree)
    assert not [node for node in nodes(tree) if node["Node Type"] == "Seq Scan"], json.dumps(tree)


def test_claiming_the_next_job_needs_no_sort(filled_queue, owner):
    """Le tri est porte par l'index: sinon chaque prise trierait la file entiere."""
    tree = plan(owner,
                "SELECT id FROM jobs WHERE organization_id = %s AND status = 'queued' "
                "ORDER BY priority DESC, created_at ASC, id ASC LIMIT 1",
                (filled_queue.organization_id,))
    assert not [node for node in nodes(tree) if node["Node Type"] == "Sort"], json.dumps(tree)


def test_stale_recovery_uses_the_lease_index(filled_queue, owner):
    tree = plan(owner,
                "SELECT id FROM jobs WHERE organization_id = %s AND status = 'running' "
                "AND lease_expires_at < now() ORDER BY lease_expires_at ASC, id ASC LIMIT 100",
                (filled_queue.organization_id,))
    assert [node for node in nodes(tree) if node.get("Index Name") == "jobs_lease_idx"], json.dumps(tree)


def test_purge_selection_uses_the_terminal_index(filled_queue, owner):
    tree = plan(owner,
                "SELECT id FROM jobs WHERE organization_id = %s "
                "AND status = ANY(ARRAY['succeeded','failed','cancelled']) AND finished_at < now() "
                "ORDER BY finished_at ASC, id ASC LIMIT 1000",
                (filled_queue.organization_id,))
    assert [node for node in nodes(tree) if node.get("Index Name") == "jobs_terminal_idx"], json.dumps(tree)


def test_an_idempotency_lookup_is_a_single_index_access(filled_queue, owner):
    tree = plan(owner,
                "SELECT id FROM jobs WHERE organization_id = %s AND job_type = %s AND idempotency_key = %s",
                (filled_queue.organization_id, "import", "abc"))
    assert [node for node in nodes(tree) if node.get("Index Name") == "jobs_idempotency_uniq"], json.dumps(tree)


def test_the_audit_of_one_correlation_uses_its_index(tenant_a, owner):
    correlation = uuid.uuid4()
    owner.execute("SELECT set_config('app.organization_id', %s, false), set_config('app.user_id', %s, false)",
                  (str(tenant_a.organization_id), str(tenant_a.owner_id)))
    rows = [(uuid.uuid4(), tenant_a.organization_id, "worker", "job.claimed", "job", uuid.uuid4(), uuid.uuid4())
            for _ in range(VOLUME)]
    with owner.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO audit_events (id, organization_id, actor_type, action, resource_type, resource_id, "
            "correlation_id, outcome) VALUES (%s, %s, %s, %s, %s, %s, %s, 'started')", rows)
    owner.execute("ANALYZE audit_events")
    tree = plan(owner,
                "SELECT id FROM audit_events WHERE organization_id = %s AND correlation_id = %s "
                "ORDER BY created_at ASC",
                (tenant_a.organization_id, correlation))
    assert [node for node in nodes(tree) if node.get("Index Name") == "audit_events_correlation_idx"], \
        json.dumps(tree)


def test_claiming_stays_cheap_whatever_the_queue_depth(filled_queue, owner):
    """Regression anti-quadratique: le cout de la prise ne suit pas la taille de la file.

    L'index rend le premier eligible immediatement; le cout estime du LIMIT 1 est donc
    une fraction du cout d'un parcours complet de la file. Si un jour un tri ou un
    parcours sequentiel revenait, les deux couts se rejoindraient et ce test tomberait.
    """
    tree = plan(owner,
                "SELECT id FROM jobs WHERE organization_id = %s AND status = 'queued' AND available_at <= now() "
                "ORDER BY priority DESC, created_at ASC, id ASC LIMIT 1",
                (filled_queue.organization_id,))
    assert tree["Node Type"] == "Limit", json.dumps(tree)
    full_scan = max(node["Total Cost"] for node in nodes(tree))
    assert tree["Total Cost"] < full_scan / 50, json.dumps(tree)


def test_only_the_queue_index_serves_the_queued_predicate(owner):
    """Deux index partiels concurrents sur `queued` feraient hesiter le planificateur."""
    partials = owner.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'jobs' AND indexdef LIKE '%queued%'").fetchall()
    assert [row[0] for row in partials] == ["jobs_ready_idx"]
