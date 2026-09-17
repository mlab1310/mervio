"""Aides partagees des tests d'identite de service et du dispatcher (Mission 004.3)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg

from mervio.persistence import jobs
from mervio.persistence.jobs import JobType

#: instant du passe: un travail termine a cet instant est purgeable (plancher d'une heure)
PAST = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def enqueue(tenant, *, kind=JobType.ANALYSIS, session=None, **kwargs):
    return jobs.enqueue_job(session or tenant.session, job_type=kind, payload={"k": "v"}, **kwargs)


@contextmanager
def raw(conn, organization_id=None, user_id=None, **settings):
    """Transaction SQL brute avec le contexte que la transaction declare, rien de plus."""
    with conn.transaction():
        conn.execute("SELECT set_config('app.organization_id', %s, true), set_config('app.user_id', %s, true)",
                     (str(organization_id) if organization_id else "", str(user_id) if user_id else ""))
        for name, value in settings.items():
            conn.execute("SELECT set_config(%s, %s, true)", (f"app.{name}", str(value)))
        yield conn


def count(conn, table, organization_id=None, user_id=None) -> int:
    with raw(conn, organization_id, user_id) as c:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def connect(pg, kind):
    return psycopg.connect(pg.url(kind), autocommit=True)
