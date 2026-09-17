"""Erreurs et metadonnees publiees en base: jamais de secret (Mission 004.3).

`last_error` et les metadonnees d'audit seront lues par l'API et l'interface: elles
passent par la meme redaction que les logs.
"""
from __future__ import annotations

import uuid

import pytest

from mervio.persistence import audit, jobs
from mervio.persistence.audit import Action, ResourceType
from mervio.persistence.jobs import JobStatus, JobType

SECRET = "Zq9-XyZ-s3cr3t-VALUE"


def credential_url(user: str, rest: str) -> str:
    """URL avec mot de passe, construite a l'execution (aucun identifiant ecrit dans le depot)."""
    return "postgresql://" + user + ":" + SECRET + "@" + rest


def failed_with(tenant, message, *, retryable=False):
    jobs.enqueue_job(tenant.session, job_type=JobType.ANALYSIS, payload={})
    job = jobs.claim_next_job(tenant.session, worker_id="w")
    return jobs.mark_failed(tenant.session, job, error_code="boom", error=message, retryable=retryable,
                            worker_id="w")


@pytest.mark.parametrize("message, leak", [
    ("connexion " + credential_url("mervio", "db:5432/x") + " refusee", SECRET),
    (f"host=db password={SECRET}", SECRET),
    (f"401 Authorization: Bearer {SECRET}", SECRET),
    ("lecture de /srv/uploads/acme/orders.csv impossible", "acme"),
    ("client alice@example.com introuvable", "alice@example.com"),
])
@pytest.mark.parametrize("retryable", [False, True])
def test_a_stored_error_never_keeps_a_secret(tenant_a, message, leak, retryable):
    stored = failed_with(tenant_a, message, retryable=retryable)
    assert stored.status == (JobStatus.QUEUED if retryable else JobStatus.FAILED).value
    assert leak not in stored.last_error
    assert stored.last_error_code == "boom"
    assert leak not in jobs.get_job(tenant_a.session, stored.id).last_error


def test_a_stored_error_keeps_its_diagnostic_value(tenant_a):
    stored = failed_with(tenant_a, "sqlstate 40P01: deadlock detected on relation jobs")
    assert stored.last_error == "sqlstate 40P01: deadlock detected on relation jobs"


def test_audit_metadata_never_keeps_a_secret(tenant_a):
    event = audit.record_event(
        tenant_a.session, action=Action.JOB_FAILED, resource_type=ResourceType.JOB, resource_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        metadata={"detail": credential_url("u", "h/d"), "hint": f"token={SECRET}", "attempt": 2})
    stored = audit.list_events(tenant_a.session, resource_id=event.resource_id)[0]
    assert SECRET not in str(stored.metadata)
    assert stored.metadata["attempt"] == 2
