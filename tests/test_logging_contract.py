"""Contrat de journalisation du processus (Mission 004.3). Sans base.

Complete `test_observability_logging.py` (004.2): configuration unique, format JSON
exact, correlation entre fils et dans le moteur reel, erreurs publiees par leur type,
sortie texte de la CLI inchangee.
"""
from __future__ import annotations

import io
import json
import logging
import re
import secrets
import subprocess
import sys
import threading
from datetime import date, datetime
from pathlib import Path

import pytest

from mervio.logging_config import get_logger
from mervio.observability.logging import (
    EVENTS, TEXT_FORMAT, JsonFormatter, bind_context, configure, correlation_scope, current_correlation_id,
    get_event_logger,
)
from mervio.settings import WorkerSettings

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
SECRET = "Zq9-XyZ-s3cr3t-VALUE"
REQUIRED_KEYS = {"timestamp", "level", "service", "logger", "event"}


@pytest.fixture
def restore_logging():
    root = logging.getLogger("mervio")
    state = (list(root.handlers), root.propagate, root.level)
    yield root
    root.handlers, root.propagate, root.level = state[0], state[1], state[2]


@pytest.fixture
def output(restore_logging):
    buffer = io.StringIO()
    configure(format="json", level=logging.DEBUG, service="worker", environment="test", stream=buffer)
    return buffer


def documents(buffer):
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# -- format JSON ----------------------------------------------------------------------------

def test_every_line_carries_the_required_fields(output):
    get_event_logger("worker").info("worker.ready", worker_id="host/1/abcd1234")
    get_logger("ingestion.shopify").warning("shopify orders: %s lignes rejetees", 3)
    for document in documents(output):
        assert REQUIRED_KEYS <= set(document)
        assert document["service"] == "worker" and document["environment"] == "test"
    assert [d["event"] for d in documents(output)] == ["worker.ready", "log"]


def test_the_timestamp_is_utc_iso8601_with_milliseconds(output):
    get_event_logger("worker").info("worker.starting")
    stamp = documents(output)[0]["timestamp"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00", stamp)
    assert datetime.fromisoformat(stamp).utcoffset().total_seconds() == 0


def test_a_line_is_one_object_with_sorted_keys_and_unicode(output):
    get_event_logger("worker").info("worker.config", note="donnees resumees: ete", zeta=1, alpha="é")
    raw = output.getvalue().strip()
    assert "\n" not in raw
    document = json.loads(raw)
    assert list(document) == sorted(document)
    assert document["alpha"] == "é"


def test_non_json_values_are_published_as_text(output):
    get_event_logger("worker").info("job.claimed", day=date(2026, 9, 16), ratio=0.5, flags={"a", "b"})
    document = documents(output)[0]
    assert document["day"] == "2026-09-16" and document["ratio"] == 0.5
    assert sorted(document["flags"]) == ["a", "b"]


def test_the_level_threshold_is_applied(restore_logging):
    buffer = io.StringIO()
    configure(format="json", level=logging.WARNING, stream=buffer)
    log = get_event_logger("worker")
    log.info("job.claimed")
    log.debug("job.lease_renewed")
    log.warning("job.lease_lost")
    assert [d["event"] for d in documents(buffer)] == ["job.lease_lost"]


def test_an_unknown_format_is_refused(restore_logging):
    with pytest.raises(ValueError):
        configure(format="xml")


def test_switching_formats_never_duplicates_lines(restore_logging):
    buffer = io.StringIO()
    for fmt in ("json", "text", "json", "text", "json"):
        configure(format=fmt, stream=buffer)
    names = [h.get_name() for h in restore_logging.handlers if h.get_name() in ("mervio-json", "mervio-text")]
    assert names == ["mervio-json"]
    get_event_logger("worker").info("worker.ready")
    assert len(documents(buffer)) == 1


def test_json_output_is_written_once_and_never_propagated(restore_logging):
    configure(format="json", stream=io.StringIO())
    assert restore_logging.propagate is False


def test_the_event_catalogue_is_consistent():
    assert {"worker.starting", "worker.ready", "worker.stopped", "job.claimed", "job.lease_renewed",
            "job.lease_lost", "job.recovered", "job.result_unrecorded"} <= EVENTS
    for name in EVENTS:
        assert re.fullmatch(r"[a-z]+\.[a-z_]+", name), name


def test_the_worker_configuration_can_be_logged_safely(output):
    master_key = secrets.token_hex(32)  # cle maitre d'identite factice (004.4.2)
    settings = WorkerSettings.from_env({"MERVIO_DATABASE_URL": f"postgresql://w:{SECRET}@db/mervio",
                                        "MERVIO_WORKER_HEALTH_FILE": "/var/run/mervio/health.json",
                                        "MERVIO_IDENTITY_MASTER_KEY": master_key})
    get_event_logger("worker").info("worker.config", **settings.public())
    raw = output.getvalue()
    assert SECRET not in raw and "/var/run" not in raw and master_key not in raw
    assert documents(output)[0]["database"]["host"] == "db"
    assert documents(output)[0]["identity_master_configured"] is True


# -- absence de secret ----------------------------------------------------------------------

def test_no_secret_reaches_the_json_output(output):
    log = get_event_logger("worker")
    log.error("worker.database_unavailable", detail=f"postgresql://w:{SECRET}@db/x", dsn=f"password={SECRET}")
    log.info("job.failed", reason=f"Bearer {SECRET}", nested={"url": f"https://u:{SECRET}@h/x"})
    get_logger("reporting.writers").info("analyse x: 3 fichiers ecrits dans %s", "/Users/alice/Clients/acme")
    get_logger("application.service").warning("client alice@example.com: token=%s", SECRET)
    raw = output.getvalue()
    for leak in (SECRET, "/Users/alice", "alice@example.com", "acme"):
        assert leak not in raw, leak


def test_an_exception_publishes_its_type_never_its_message_nor_traceback(output):
    log = get_event_logger("worker")
    try:
        raise ConnectionError(f"connexion postgresql://w:{SECRET}@db refusee depuis /Users/alice")
    except ConnectionError as error:
        log.error("worker.database_unavailable", exc_info=True, error=error)
        get_logger("persistence").exception("echec de connexion")
    raw = output.getvalue()
    assert SECRET not in raw and "/Users/alice" not in raw
    assert "Traceback" not in raw and "refusee" not in raw
    first, second = documents(output)
    assert first["error_type"] == "ConnectionError" and first["error"] == "ConnectionError"
    assert second["error_type"] == "ConnectionError" and second["message"] == "echec de connexion"


def test_a_broken_format_string_still_produces_a_line():
    record = logging.LogRecord("mervio.worker", logging.INFO, __file__, 1, "valeur %s et %s", ("une",), None)
    document = json.loads(JsonFormatter(service="worker").format(record))
    assert document["message"] == "valeur %s et %s"


def test_the_redacting_text_format_is_cleaned_too(restore_logging):
    buffer = io.StringIO()
    configure(format="text", stream=buffer)
    try:
        raise OSError(f"password={SECRET}")
    except OSError:
        get_logger("worker").exception("lecture de /Users/alice/x.csv, token=%s", SECRET)
    line = buffer.getvalue()
    assert SECRET not in line and "/Users/alice" not in line and "Traceback" not in line
    assert line.startswith("ERROR mervio.worker: lecture de [redacted-path]")
    assert "[error_type=OSError]" in line


# -- correlation ----------------------------------------------------------------------------

def test_a_bound_thread_inherits_the_correlation(output):
    seen = {}

    def work():
        seen["inner"] = current_correlation_id()
        get_event_logger("worker").debug("job.lease_renewed")

    with correlation_scope("11111111-1111-1111-1111-111111111111"):
        bound = threading.Thread(target=bind_context(work))
        bound.start()
        bound.join()
    assert seen["inner"] == "11111111-1111-1111-1111-111111111111"
    assert documents(output)[0]["correlation_id"] == "11111111-1111-1111-1111-111111111111"


def test_an_unbound_thread_starts_without_correlation():
    seen = {}
    with correlation_scope("outer"):
        thread = threading.Thread(target=lambda: seen.setdefault("inner", current_correlation_id()))
        thread.start()
        thread.join()
    assert seen["inner"] is None


def test_a_scope_opened_in_a_bound_thread_does_not_leak_back():
    with correlation_scope("parent"):
        def work():
            with correlation_scope("child"):
                pass
            return current_correlation_id()

        result = {}
        bound = bind_context(work)  # copie du contexte du PARENT
        thread = threading.Thread(target=lambda: result.setdefault("value", bound()))
        thread.start()
        thread.join()
        assert result["value"] == "parent"
        assert current_correlation_id() == "parent"


def test_the_real_engine_joins_the_correlation_without_being_changed(output, tmp_path):
    """Analyse complete du moteur inchange, puis ecriture des livrables: meme correlation partout."""
    from mervio.application.service import AnalysisRequest, analyze_dataset
    from mervio.reporting.writers import write_outputs
    request = AnalysisRequest(shopify_orders=str(SAMPLE / "shopify_orders.csv"),
                              shopify_products=str(SAMPLE / "shopify_products.csv"),
                              stripe=str(SAMPLE / "stripe_transactions.csv"),
                              google_ads=str(SAMPLE / "google_ads.csv"), today=date(2026, 9, 14))
    with correlation_scope() as correlation_id:
        result = analyze_dataset(request)
        write_outputs(result, tmp_path / "out")
    lines = documents(output)
    assert len(lines) >= 5
    assert {line["correlation_id"] for line in lines} == {correlation_id}
    assert {line["logger"] for line in lines} >= {"mervio.ingestion.shopify", "mervio.reporting.writers"}
    raw = output.getvalue()
    assert str(tmp_path) not in raw and str(SAMPLE) not in raw
    assert "@example.com" not in raw


# -- CLI: sortie texte historique -----------------------------------------------------------

def test_the_cli_text_output_is_unchanged():
    code = ("import logging; from mervio.logging_config import configure_logging, get_logger; "
            "configure_logging(logging.INFO); configure_logging(logging.DEBUG); "
            "log = get_logger('reporting.writers'); log.debug('invisible'); "
            "log.info('analyse %s: 3 fichiers ecrits dans %s', 'a1', '/Users/alice/out')")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            env={"PYTHONPATH": str(ROOT / "src")}, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stderr == "INFO mervio.reporting.writers: analyse a1: 3 fichiers ecrits dans /Users/alice/out\n"
    assert TEXT_FORMAT == "%(levelname)s %(name)s: %(message)s"
