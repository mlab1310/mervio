"""Logs JSON correles (Mission 004.2). Sans base de donnees.

Ce que ces tests garantissent:
- une ligne = un objet JSON valide, avec les champs attendus;
- l'identifiant de correlation suit l'execution, y compris dans les modules du
  moteur qui ignorent l'existence des jobs;
- aucun secret, aucune PII, aucun chemin absolu ne sort dans un log.
"""
from __future__ import annotations

import io
import json
import logging
import threading

import pytest

from mervio.logging_config import get_logger
from mervio.observability.logging import (
    FORBIDDEN_KEY_FRAGMENTS, MAX_VALUE_LENGTH, REDACTED, EventLogger, JsonFormatter,
    configure_json_logging, correlation_scope, current_correlation_id, get_event_logger,
    new_correlation_id, scrub,
)


@pytest.fixture
def stream(monkeypatch):
    """Sortie JSON isolee: les gestionnaires du logger `mervio` sont restaures apres le test."""
    root = logging.getLogger("mervio")
    previous, propagate, level = list(root.handlers), root.propagate, root.level
    buffer = io.StringIO()
    configure_json_logging(stream=buffer, service="test", level=logging.DEBUG)
    yield buffer
    root.handlers = previous
    root.propagate, root.level = propagate, level


def lines(buffer) -> list:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# -- forme ----------------------------------------------------------------------------------

def test_every_line_is_one_json_object(stream):
    log = get_event_logger("worker")
    log.info("job.claimed", job_id="j1")
    log.info("job.succeeded", job_id="j1")
    raw = stream.getvalue().splitlines()
    assert len(raw) == 2
    assert all(isinstance(json.loads(line), dict) for line in raw)


def test_a_log_carries_the_documented_fields(stream):
    with correlation_scope("11111111-1111-1111-1111-111111111111"):
        get_event_logger("worker").info(
            "job.succeeded", job_id="j1", organization_id="o1", store_id="s1", job_type="import",
            duration_ms=1234)
    document = lines(stream)[0]
    assert document["event"] == "job.succeeded"
    assert document["level"] == "INFO"
    assert document["service"] == "test"
    assert document["correlation_id"] == "11111111-1111-1111-1111-111111111111"
    assert (document["job_id"], document["organization_id"], document["store_id"]) == ("j1", "o1", "s1")
    assert (document["job_type"], document["duration_ms"]) == ("import", 1234)
    assert document["timestamp"].endswith("+00:00")


def test_a_failure_log_carries_attempt_and_retryability(stream):
    get_event_logger("worker").error("job.failed", job_id="j1", job_type="analysis", attempt=2,
                                     max_attempts=3, retryable=True, error_code="deadlock", duration_ms=12)
    document = lines(stream)[0]
    assert document["level"] == "ERROR"
    assert (document["attempt"], document["max_attempts"], document["retryable"]) == (2, 3, True)
    assert document["error_code"] == "deadlock"


def test_an_exception_publishes_its_type_never_its_message(stream):
    log = get_event_logger("worker")
    try:
        raise ValueError("valeur client 42 rue des Lilas")
    except ValueError:
        log.error("job.failed", exc_info=True, job_id="j1")
    document = lines(stream)[0]
    assert document["error_type"] == "ValueError"
    assert "Lilas" not in json.dumps(document)


def test_the_sort_order_of_keys_is_stable(stream):
    get_event_logger("worker").info("job.claimed", b=2, a=1)
    keys = list(json.loads(stream.getvalue().splitlines()[0]))
    assert keys == sorted(keys)


# -- correlation ----------------------------------------------------------------------------

def test_a_correlation_id_is_a_fresh_uuid():
    first, second = new_correlation_id(), new_correlation_id()
    assert first != second
    assert len(first) == 36


def test_the_scope_restores_the_previous_correlation_id():
    assert current_correlation_id() is None
    with correlation_scope("a" * 8):
        assert current_correlation_id() == "a" * 8
        with correlation_scope("b" * 8):
            assert current_correlation_id() == "b" * 8
        assert current_correlation_id() == "a" * 8
    assert current_correlation_id() is None


def test_the_scope_is_restored_even_after_an_exception():
    with pytest.raises(RuntimeError):
        with correlation_scope("c" * 8):
            raise RuntimeError("panne")
    assert current_correlation_id() is None


def test_an_existing_engine_module_inherits_the_correlation_id(stream):
    """Le moteur ignore les jobs: c'est le formateur qui recolle la trace."""
    with correlation_scope("22222222-2222-2222-2222-222222222222"):
        get_logger("application.persisted_analysis").info("instantane %s scelle", "abc")
    document = lines(stream)[0]
    assert document["correlation_id"] == "22222222-2222-2222-2222-222222222222"
    assert document["message"] == "instantane abc scelle"
    assert document["logger"] == "mervio.application.persisted_analysis"


def test_a_whole_execution_shares_one_correlation_id(stream):
    log = get_event_logger("worker")
    with correlation_scope() as correlation_id:
        for event in ("job.claimed", "import.started", "import.succeeded", "job.succeeded"):
            log.info(event, job_id="j1")
    documents = lines(stream)
    assert [d["event"] for d in documents] == ["job.claimed", "import.started", "import.succeeded", "job.succeeded"]
    assert {d["correlation_id"] for d in documents} == {correlation_id}


def test_correlation_ids_do_not_leak_between_threads(stream):
    seen = {}

    def work(name):
        with correlation_scope(name * 8):
            seen[name] = current_correlation_id()

    threads = [threading.Thread(target=work, args=(letter,)) for letter in "xyz"]
    with correlation_scope("outer12345"):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert current_correlation_id() == "outer12345"
    assert seen == {letter: letter * 8 for letter in "xyz"}


# -- redaction ------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["password", "api_token", "shop_secret", "Authorization", "customer_email",
                                 "phone_number", "file_path", "raw_body", "llm_prompt", "private_key"])
def test_a_sensitive_key_never_reaches_the_output(stream, key):
    get_event_logger("worker").info("job.failed", **{key: "valeur-tres-sensible"})
    assert "valeur-tres-sensible" not in stream.getvalue()
    assert lines(stream)[0][key] == REDACTED


def test_an_absolute_path_is_masked(stream):
    get_event_logger("worker").info("import.started", source="/Users/someone/exports/orders.csv")
    assert lines(stream)[0]["source"] == REDACTED


def test_a_long_value_is_truncated(stream):
    get_event_logger("worker").info("job.failed", detail="x" * 5000)
    assert len(lines(stream)[0]["detail"]) == MAX_VALUE_LENGTH + 3


def test_a_nested_secret_is_masked():
    assert scrub({"payload": {"connection": {"api_key": "k"}}}) == {
        "payload": {"connection": {"api_key": REDACTED}}}


def test_a_deeply_nested_structure_is_cut():
    assert scrub({"a": {"b": {"c": {"d": {"e": 1}}}}})["a"]["b"]["c"]["d"] == REDACTED


def test_a_none_value_is_dropped():
    assert scrub({"store_id": None, "job_id": "j"}) == {"job_id": "j"}


def test_the_message_itself_is_scrubbed(stream):
    get_logger("worker").info("/Users/someone/secret.csv")
    assert lines(stream)[0]["message"] == REDACTED


def test_the_forbidden_list_covers_the_documented_categories():
    for fragment in ("password", "secret", "token", "email", "credential", "authorization", "path", "raw"):
        assert fragment in FORBIDDEN_KEY_FRAGMENTS


# -- outillage ------------------------------------------------------------------------------

def test_bound_fields_are_attached_to_every_event(stream):
    log = get_event_logger("worker").bind(worker_id="w1", organization_id="o1")
    log.info("job.claimed", job_id="j1")
    log.info("job.succeeded", job_id="j1")
    assert all(d["worker_id"] == "w1" and d["organization_id"] == "o1" for d in lines(stream))


def test_binding_does_not_mutate_the_original_logger(stream):
    base = get_event_logger("worker")
    base.bind(worker_id="w1")
    base.info("job.claimed")
    assert "worker_id" not in lines(stream)[0]


def test_a_field_named_like_a_log_record_attribute_does_not_break_logging(stream):
    get_event_logger("worker").info("job.claimed", name="x", module="y", job_id="j1")
    assert lines(stream)[0]["job_id"] == "j1"


def test_configuring_twice_does_not_duplicate_lines(stream):
    configure_json_logging(stream=stream, service="test")
    get_event_logger("worker").info("job.claimed")
    assert len(stream.getvalue().splitlines()) == 1


def test_the_formatter_is_usable_on_its_own():
    record = logging.LogRecord("mervio.worker", logging.INFO, __file__, 1, "texte", (), None)
    document = json.loads(JsonFormatter(service="worker").format(record))
    assert document["service"] == "worker" and document["event"] == "log"


def test_an_event_logger_reuses_the_mervio_namespace():
    assert EventLogger("worker")._logger.name == "mervio.worker"
    assert EventLogger("mervio.worker")._logger.name == "mervio.worker"
