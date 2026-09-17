"""Sante et disponibilite du worker par fichier (Mission 004.3). Sans base, sans HTTP."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from mervio.observability.health import (
    HEALTH_SCHEMA_VERSION, HealthFileError, HealthReporter, HealthSnapshot, WorkerState, check_health, evaluate,
    read_health, write_health,
)

NOW = 1_800_000_000.0
MAX_AGE = 60
GRACE = 120
ALLOWED_KEYS = {"version", "state", "updated_at", "started_at", "pid", "worker_id", "jobs_processed",
                "last_database_ok_at", "degraded_since"}


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def snapshot(**changes):
    base = HealthSnapshot(version=HEALTH_SCHEMA_VERSION, state="ready", updated_at=NOW, started_at=NOW - 100,
                          pid=42, worker_id="host/42/abcd1234", jobs_processed=3, last_database_ok_at=NOW,
                          degraded_since=None)
    return replace(base, **changes)


def verdict(**changes):
    now = changes.pop("now", NOW)
    return evaluate(snapshot(**changes), now=now, max_age_seconds=MAX_AGE, degraded_grace_seconds=GRACE)


# -- verdicts -------------------------------------------------------------------------------

@pytest.mark.parametrize("state, live, ready, reason", [
    ("ready", True, True, "ok"),
    ("busy", True, True, "ok"),
    ("starting", True, False, "starting"),
    ("draining", True, False, "draining"),
    ("stopped", False, False, "stopped"),
])
def test_the_state_decides_liveness_and_readiness(state, live, ready, reason):
    result = verdict(state=state)
    assert (result.live, result.ready, result.reason, result.state) == (live, ready, reason, state)


def test_an_old_document_is_not_alive():
    assert verdict(updated_at=NOW - MAX_AGE).live
    result = verdict(updated_at=NOW - MAX_AGE - 1)
    assert (result.live, result.ready, result.reason) == (False, False, "stale")


def test_a_document_from_the_future_is_refused_beyond_the_tolerated_skew():
    assert verdict(updated_at=NOW + 4).live
    assert verdict(updated_at=NOW + 10).reason == "clock_skew"


def test_a_short_database_outage_keeps_the_worker_alive_but_not_ready():
    result = verdict(state="degraded", degraded_since=NOW - GRACE)
    assert (result.live, result.ready, result.reason) == (True, False, "degraded")


def test_a_long_database_outage_makes_the_worker_unhealthy():
    result = verdict(state="degraded", degraded_since=NOW - GRACE - 1)
    assert (result.live, result.ready, result.reason) == (False, False, "database_unavailable")


# -- lecture stricte ------------------------------------------------------------------------

def test_a_document_round_trips(tmp_path):
    path = tmp_path / "health.json"
    write_health(path, snapshot())
    assert read_health(path) == snapshot()
    assert json.loads(path.read_text()).keys() == ALLOWED_KEYS


@pytest.mark.parametrize("document, code", [
    ("{not json", "invalid"),
    ("[]", "invalid"),
    ('"ready"', "invalid"),
    (b"\xff\xfe", "invalid"),
    ("x" * 5000, "invalid"),
])
def test_an_unparsable_document_is_refused(tmp_path, document, code):
    path = tmp_path / "health.json"
    if isinstance(document, bytes):
        path.write_bytes(document)
    else:
        path.write_text(document)
    with pytest.raises(HealthFileError) as error:
        read_health(path)
    assert error.value.code == code


@pytest.mark.parametrize("change", [
    {"remove": "pid"},
    {"extra": ("organization_id", "0b527d3b-a97a-435a-87ec-6c084133722f")},
    {"set": ("pid", "42")},
    {"set": ("updated_at", True)},
    {"set": ("jobs_processed", 1.5)},
    {"set": ("state", "sleeping")},
    {"set": ("worker_id", None)},
    {"set": ("version", 2)},
])
def test_a_non_conforming_document_is_refused(tmp_path, change):
    document = snapshot().to_document()
    if "remove" in change:
        del document[change["remove"]]
    elif "extra" in change:
        document[change["extra"][0]] = change["extra"][1]
    else:
        document[change["set"][0]] = change["set"][1]
    path = tmp_path / "health.json"
    path.write_text(json.dumps(document))
    with pytest.raises(HealthFileError) as error:
        read_health(path)
    expected = "unsupported_version" if change.get("set", ("",))[0] == "version" else "invalid"
    assert error.value.code == expected


def test_missing_and_unreadable_documents_have_their_own_codes(tmp_path):
    with pytest.raises(HealthFileError) as missing:
        read_health(tmp_path / "absent.json")
    assert missing.value.code == "missing"
    with pytest.raises(HealthFileError) as unreadable:
        read_health(tmp_path)  # un repertoire
    assert unreadable.value.code == "unreadable"


def test_the_check_never_raises_and_never_reveals_the_path(tmp_path):
    secret_dir = tmp_path / "Clients-acme-S3cret"
    for path in (None, secret_dir / "absent.json", tmp_path):
        result = check_health(path, max_age_seconds=MAX_AGE, degraded_grace_seconds=GRACE, now=NOW)
        assert not result.live and not result.ready
        assert result.reason in {"not_configured", "missing", "unreadable"}
        assert "acme" not in repr(result) and str(tmp_path) not in repr(result)


def test_the_check_reads_the_real_file(tmp_path):
    path = tmp_path / "health.json"
    write_health(path, snapshot(updated_at=NOW - 5))
    assert check_health(path, max_age_seconds=MAX_AGE, degraded_grace_seconds=GRACE, now=NOW).ready
    assert check_health(path, max_age_seconds=MAX_AGE, degraded_grace_seconds=GRACE,
                        now=NOW + MAX_AGE).reason == "stale"


# -- ecriture -------------------------------------------------------------------------------

def test_writing_is_atomic_and_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "health.json"
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            try:
                read_health(path)
            except HealthFileError as error:
                if error.code != "missing":
                    bad.append(error.code)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for count in range(300):
            write_health(path, snapshot(jobs_processed=count, worker_id="w" * (count % 50 + 1)))
    finally:
        stop.set()
        thread.join()
    assert bad == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["health.json"]
    assert oct(os.stat(path).st_mode & 0o777) == oct(0o644)


def test_a_failed_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disque plein")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        write_health(tmp_path / "health.json", snapshot())
    assert list(tmp_path.iterdir()) == []


# -- rapporteur -----------------------------------------------------------------------------

def test_the_reporter_publishes_its_lifecycle(tmp_path):
    path, clock = tmp_path / "health.json", Clock()
    reporter = HealthReporter(path, worker_id="host/42/abcd1234", pid=42, clock=clock)
    assert reporter.snapshot.state == "starting" and not path.exists()
    for state in (WorkerState.READY, WorkerState.BUSY, WorkerState.DRAINING, WorkerState.STOPPED):
        clock.now += 1
        reporter.set_state(state)
        document = read_health(path)
        assert (document.state, document.updated_at) == (state.value, clock.now)
    assert read_health(path).started_at == NOW


def test_the_reporter_tracks_database_outages(tmp_path):
    path, clock = tmp_path / "health.json", Clock()
    reporter = HealthReporter(path, worker_id="w", clock=clock)
    reporter.set_state(WorkerState.READY)
    clock.now += 10
    reporter.database_unavailable()
    clock.now += 50
    reporter.database_unavailable()
    assert read_health(path).degraded_since == NOW + 10
    clock.now += 5
    reporter.database_ok()
    document = read_health(path)
    assert (document.state, document.degraded_since, document.last_database_ok_at) == ("ready", None, NOW + 65)


def test_the_reporter_counts_jobs_without_losing_updates(tmp_path):
    reporter = HealthReporter(tmp_path / "health.json", worker_id="w")
    threads = [threading.Thread(target=lambda: [reporter.job_finished() for _ in range(100)]) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert reporter.snapshot.jobs_processed == 400
    assert read_health(tmp_path / "health.json").jobs_processed == 400


def test_a_write_failure_never_stops_the_worker(tmp_path):
    reporter = HealthReporter(tmp_path / "missing-dir" / "health.json", worker_id="w")
    reporter.set_state(WorkerState.READY)
    reporter.beat()
    assert reporter.write_failures == 2
    assert reporter.snapshot.state == "ready"


def test_without_a_file_the_reporter_only_keeps_state(tmp_path):
    reporter = HealthReporter(None, worker_id="w")
    reporter.set_state(WorkerState.BUSY)
    assert reporter.snapshot.state == "busy"
    assert list(tmp_path.iterdir()) == []


def test_the_document_never_carries_tenant_or_secret_data(tmp_path):
    path = tmp_path / "health.json"
    reporter = HealthReporter(path, worker_id="host/42/abcd1234")
    reporter.set_state(WorkerState.BUSY)
    reporter.job_finished()
    document = json.loads(path.read_text())
    assert set(document) == ALLOWED_KEYS
    raw = path.read_text()
    for forbidden in ("organization", "store", "job_id", "postgres", "password", str(tmp_path)):
        assert forbidden not in raw


def test_a_long_worker_id_is_bounded():
    assert len(HealthReporter(None, worker_id="x" * 500).snapshot.worker_id) == 100


def test_the_health_module_has_no_database_or_network_dependency():
    source = Path(__file__).resolve().parent.parent / "src" / "mervio" / "observability" / "health.py"
    text = source.read_text(encoding="utf-8")
    for forbidden in ("import psycopg", "import socket", "http.server", "urllib", "persistence"):
        assert forbidden not in text
