"""Configuration typee et validee du worker (Mission 004.3). Sans base.

Chaque refus nomme la variable et la regle, jamais la valeur: une URL de base mal
formee contient souvent un mot de passe.
"""
from __future__ import annotations

import ast
import logging
import secrets
from pathlib import Path

import pytest

from mervio.errors import ConfigurationError
from mervio.settings import (
    ENVIRONMENTS, JOB_TYPES, Secret, SettingsError, WorkerSettings, describe_database_url,
)

PASSWORD = "Sup3r-S3cret-Pa55"
URL = f"postgresql://mervio_worker:{PASSWORD}@db.internal:6543/mervio"
#: cle maitre d'identite FACTICE, tiree a l'execution (jamais de cle litterale dans le depot)
MASTER_KEY = secrets.token_hex(32)


def load(**variables):
    environ = {"MERVIO_DATABASE_URL": URL, "MERVIO_IDENTITY_MASTER_KEY": MASTER_KEY}
    environ.update({k: v for k, v in variables.items() if v is not None})
    for key in [k for k, v in variables.items() if v is None]:
        environ.pop(key, None)
    return WorkerSettings.from_env(environ, hostname=lambda: "worker-host.local")


def refused(expected_variables, **variables):
    with pytest.raises(SettingsError) as error:
        load(**variables)
    names = {name for name, _ in error.value.problems}
    assert set(expected_variables) <= names, error.value.problems
    text = str(error.value)
    vocabulary = set(ENVIRONMENTS) | {"json", "text"}
    for value in variables.values():
        if value and len(str(value)) > 3 and str(value).lower() not in vocabulary:
            assert str(value) not in text, "une valeur de configuration a fuit dans l'erreur"
    assert PASSWORD not in text
    return error.value


# -- valeurs par defaut ---------------------------------------------------------------------

def test_only_the_database_url_is_required():
    settings = load()
    assert settings.environment == "development"
    assert settings.database_url.reveal() == URL
    assert settings.worker_name == "worker-host.local"
    assert settings.job_types == JOB_TYPES
    assert (settings.poll_interval_seconds, settings.poll_max_seconds) == (1.0, 5.0)
    assert settings.dispatch_batch == 50
    assert (settings.lease_seconds, settings.lease_renew_seconds) == (300, 100)
    assert (settings.recovery_interval_seconds, settings.shutdown_grace_seconds) == (30, 30)
    assert settings.startup_timeout_seconds == 60
    assert (settings.backoff_base_seconds, settings.backoff_cap_seconds) == (30, 3600)
    assert settings.health_file is None
    assert (settings.health_max_age_seconds, settings.degraded_grace_seconds) == (60, 120)
    assert (settings.log_level, settings.log_format) == (logging.INFO, "json")
    assert not settings.production


def test_the_backoff_defaults_are_those_of_the_job_queue():
    from mervio.persistence import jobs
    settings = load()
    assert (settings.backoff_base_seconds, settings.backoff_cap_seconds) == (
        jobs.BACKOFF_BASE_SECONDS, jobs.BACKOFF_CAP_SECONDS)
    assert settings.lease_seconds == jobs.DEFAULT_LEASE_SECONDS


def test_the_job_types_match_the_queue():
    from mervio.persistence.jobs import JobType
    assert JOB_TYPES == tuple(kind.value for kind in JobType)


def test_a_full_valid_configuration_is_read():
    settings = load(MERVIO_ENV="production", MERVIO_WORKER_NAME="worker-01", MERVIO_WORKER_JOB_TYPES="purge, import",
                    MERVIO_WORKER_POLL_INTERVAL_SECONDS="0.5", MERVIO_WORKER_POLL_MAX_SECONDS="10",
                    MERVIO_WORKER_DISPATCH_BATCH="20", MERVIO_JOB_LEASE_SECONDS="900",
                    MERVIO_JOB_LEASE_RENEW_SECONDS="120", MERVIO_WORKER_RECOVERY_INTERVAL_SECONDS="15",
                    MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS="45", MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS="5",
                    MERVIO_JOB_BACKOFF_BASE_SECONDS="10", MERVIO_JOB_BACKOFF_CAP_SECONDS="600",
                    MERVIO_WORKER_HEALTH_FILE="/tmp/mervio-health.json", MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS="45",
                    MERVIO_WORKER_DEGRADED_GRACE_SECONDS="300", MERVIO_LOG_LEVEL="warning",
                    MERVIO_LOG_FORMAT="JSON")
    assert settings.production
    assert settings.job_types == ("import", "purge")
    assert (settings.poll_interval_seconds, settings.poll_max_seconds) == (0.5, 10.0)
    assert (settings.lease_seconds, settings.lease_renew_seconds) == (900, 120)
    assert settings.health_file == Path("/tmp/mervio-health.json")
    assert (settings.log_level, settings.log_format) == (logging.WARNING, "json")


def test_blank_values_mean_default():
    assert load(MERVIO_ENV="  ", MERVIO_LOG_LEVEL="").environment == "development"


# -- base de donnees et secret --------------------------------------------------------------

def test_a_missing_database_url_is_refused():
    error = refused({"MERVIO_DATABASE_URL"}, MERVIO_DATABASE_URL=None)
    assert "obligatoire" in str(error)


@pytest.mark.parametrize("url", [
    f"mysql://mervio:{PASSWORD}@db/mervio",
    f"postgresql://mervio:{PASSWORD}@/mervio",
    f"postgresql://:{PASSWORD}@db/mervio",
    f"postgresql://mervio:{PASSWORD}@db/",
    f"postgresql://mervio:{PASSWORD}@db:port/mervio",
    f"postgresql://mervio:{PASSWORD}@db:99999/mervio",
    f"host=db password={PASSWORD}",
])
def test_an_invalid_database_url_is_refused_without_echoing_it(url):
    refused({"MERVIO_DATABASE_URL"}, MERVIO_DATABASE_URL=url)


def test_the_url_can_come_from_a_secret_file(tmp_path):
    secret = tmp_path / "database_url"
    secret.write_text(URL + "\n", encoding="utf-8")
    settings = load(MERVIO_DATABASE_URL=None, MERVIO_DATABASE_URL_FILE=str(secret))
    assert settings.database_url.reveal() == URL


def test_the_secret_file_rules(tmp_path):
    refused({"MERVIO_DATABASE_URL"}, MERVIO_DATABASE_URL_FILE=str(tmp_path / "x"))
    refused({"MERVIO_DATABASE_URL_FILE"}, MERVIO_DATABASE_URL=None, MERVIO_DATABASE_URL_FILE="relative/file")
    refused({"MERVIO_DATABASE_URL_FILE"}, MERVIO_DATABASE_URL=None,
            MERVIO_DATABASE_URL_FILE=str(tmp_path / "absent"))
    invalid = tmp_path / "invalid"
    invalid.write_text(f"mysql://u:{PASSWORD}@h/d", encoding="utf-8")
    refused({"MERVIO_DATABASE_URL_FILE"}, MERVIO_DATABASE_URL=None, MERVIO_DATABASE_URL_FILE=str(invalid))


def test_the_secret_is_never_displayed():
    settings = load()
    for text in (repr(settings), str(settings), repr(settings.database_url), str(settings.database_url),
                 f"{settings.database_url}", str(settings.public())):
        assert PASSWORD not in text
        assert "S3cret" not in text
    assert settings.database_url == Secret(URL)
    assert hash(settings.database_url) == hash(Secret(URL))


def test_the_public_summary_describes_the_database_without_secret():
    public = load().public()
    assert public["database"] == {"host": "db.internal", "port": 6543, "database": "mervio", "role": "mervio_worker"}
    assert public["health_check"] is False
    assert "health_file" not in public
    assert public["log_level"] == "INFO"


def test_describe_database_url_never_returns_options_or_password():
    described = describe_database_url(f"postgres://r%40le:{PASSWORD}@h/b%20d?sslmode=require&password={PASSWORD}")
    assert described == {"host": "h", "port": 5432, "database": "b d", "role": "r@le"}


# -- regles numeriques ----------------------------------------------------------------------

@pytest.mark.parametrize("variable, value", [
    ("MERVIO_WORKER_POLL_INTERVAL_SECONDS", "0"),
    ("MERVIO_WORKER_POLL_INTERVAL_SECONDS", "61"),
    ("MERVIO_WORKER_POLL_INTERVAL_SECONDS", "nan"),
    ("MERVIO_WORKER_POLL_INTERVAL_SECONDS", "inf"),
    ("MERVIO_WORKER_POLL_INTERVAL_SECONDS", "vite"),
    ("MERVIO_WORKER_DISPATCH_BATCH", "0"),
    ("MERVIO_WORKER_DISPATCH_BATCH", "1001"),
    ("MERVIO_WORKER_DISPATCH_BATCH", "2.5"),
    ("MERVIO_JOB_LEASE_SECONDS", "29"),
    ("MERVIO_JOB_LEASE_SECONDS", "86401"),
    ("MERVIO_WORKER_RECOVERY_INTERVAL_SECONDS", "0"),
    ("MERVIO_WORKER_SHUTDOWN_GRACE_SECONDS", "-1"),
    ("MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS", "601"),
    ("MERVIO_JOB_BACKOFF_BASE_SECONDS", "0"),
    ("MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS", "3601"),
    ("MERVIO_WORKER_DEGRADED_GRACE_SECONDS", "0"),
])
def test_out_of_range_numbers_are_refused(variable, value):
    refused({variable}, **{variable: value})


def test_the_poll_maximum_cannot_be_below_the_interval():
    refused({"MERVIO_WORKER_POLL_MAX_SECONDS"}, MERVIO_WORKER_POLL_INTERVAL_SECONDS="10",
            MERVIO_WORKER_POLL_MAX_SECONDS="2")
    assert load(MERVIO_WORKER_POLL_INTERVAL_SECONDS="10").poll_max_seconds == 10.0


def test_the_lease_is_renewed_at_least_three_times_per_period():
    refused({"MERVIO_JOB_LEASE_RENEW_SECONDS"}, MERVIO_JOB_LEASE_SECONDS="60", MERVIO_JOB_LEASE_RENEW_SECONDS="21")
    assert load(MERVIO_JOB_LEASE_SECONDS="60", MERVIO_JOB_LEASE_RENEW_SECONDS="20").lease_renew_seconds == 20
    assert load(MERVIO_JOB_LEASE_SECONDS="31").lease_renew_seconds == 10


def test_the_backoff_cap_cannot_be_below_its_base():
    refused({"MERVIO_JOB_BACKOFF_CAP_SECONDS"}, MERVIO_JOB_BACKOFF_BASE_SECONDS="600",
            MERVIO_JOB_BACKOFF_CAP_SECONDS="60")


def test_the_health_age_covers_three_heartbeats():
    refused({"MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS"}, MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS="14")
    refused({"MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS"}, MERVIO_WORKER_POLL_MAX_SECONDS="30",
            MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS="60")
    assert load(MERVIO_WORKER_POLL_MAX_SECONDS="30").health_max_age_seconds == 91
    assert load(MERVIO_WORKER_HEALTH_MAX_AGE_SECONDS="15").health_max_age_seconds == 15


# -- autres regles --------------------------------------------------------------------------

@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_every_documented_environment_is_accepted(environment):
    assert load(MERVIO_ENV=environment.upper()).environment == environment


def test_an_unknown_environment_is_refused():
    refused({"MERVIO_ENV"}, MERVIO_ENV="prod-eu")


def test_production_requires_json_logs_and_no_debug():
    refused({"MERVIO_LOG_FORMAT"}, MERVIO_ENV="production", MERVIO_LOG_FORMAT="text")
    refused({"MERVIO_LOG_LEVEL"}, MERVIO_ENV="production", MERVIO_LOG_LEVEL="debug")
    assert load(MERVIO_ENV="development", MERVIO_LOG_FORMAT="text", MERVIO_LOG_LEVEL="DEBUG").log_format == "text"


def test_unknown_log_settings_are_refused():
    refused({"MERVIO_LOG_LEVEL"}, MERVIO_LOG_LEVEL="verbose")
    refused({"MERVIO_LOG_FORMAT"}, MERVIO_LOG_FORMAT="xml")


@pytest.mark.parametrize("value", ["", " , ", "import,shopify", "ANALYSIS,purge,sync"])
def test_job_types_must_be_known(value):
    if value.strip() == "":
        assert load(MERVIO_WORKER_JOB_TYPES=value).job_types == JOB_TYPES
        return
    refused({"MERVIO_WORKER_JOB_TYPES"}, MERVIO_WORKER_JOB_TYPES=value)


def test_job_types_are_deduplicated_in_a_stable_order():
    assert load(MERVIO_WORKER_JOB_TYPES="PURGE,analysis,purge").job_types == ("analysis", "purge")


@pytest.mark.parametrize("name", ["with space", "a" * 41, "slash/name", "semi;colon"])
def test_an_explicit_worker_name_must_be_safe(name):
    refused({"MERVIO_WORKER_NAME"}, MERVIO_WORKER_NAME=name)


def test_the_default_worker_name_is_a_sanitized_hostname():
    environ = {"MERVIO_DATABASE_URL": URL, "MERVIO_IDENTITY_MASTER_KEY": MASTER_KEY}
    settings = WorkerSettings.from_env(environ, hostname=lambda: "pod name/ü" + "x" * 50)
    assert settings.worker_name == ("pod-name--" + "x" * 30)
    assert WorkerSettings.from_env(environ, hostname=lambda: "").worker_name == "worker"


def test_the_health_file_must_be_absolute():
    refused({"MERVIO_WORKER_HEALTH_FILE"}, MERVIO_WORKER_HEALTH_FILE="health.json")


def test_every_problem_is_reported_at_once():
    error = refused({"MERVIO_DATABASE_URL", "MERVIO_ENV", "MERVIO_LOG_LEVEL", "MERVIO_WORKER_DISPATCH_BATCH"},
                    MERVIO_DATABASE_URL=None, MERVIO_ENV="nowhere", MERVIO_LOG_LEVEL="loud",
                    MERVIO_WORKER_DISPATCH_BATCH="many")
    assert len(error.problems) == 4
    assert isinstance(error, ConfigurationError)


def test_the_real_environment_is_read_by_default(monkeypatch):
    monkeypatch.setenv("MERVIO_DATABASE_URL", URL)
    monkeypatch.setenv("MERVIO_IDENTITY_MASTER_KEY", MASTER_KEY)
    monkeypatch.setenv("MERVIO_WORKER_DISPATCH_BATCH", "7")
    assert WorkerSettings.from_env().dispatch_batch == 7


def test_the_settings_module_has_no_database_dependency():
    source = Path(__file__).resolve().parent.parent / "src" / "mervio" / "settings.py"
    imported = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    assert not {name for name in imported if name.split(".")[0] in ("psycopg", "sqlalchemy", "alembic")}
    assert not {name for name in imported if "persistence" in name or "workers" in name}


# -- cle maitre d'identite (004.4.2, D-053) --------------------------------------------------

def test_the_identity_master_key_is_required_when_imports_are_served():
    refused({"MERVIO_IDENTITY_MASTER_KEY"}, MERVIO_IDENTITY_MASTER_KEY=None)
    with pytest.raises(SettingsError) as error:  # `import` est un mot du vocabulaire, pas une valeur secrete
        load(MERVIO_IDENTITY_MASTER_KEY=None, MERVIO_WORKER_JOB_TYPES="import")
    assert [name for name, _ in error.value.problems] == ["MERVIO_IDENTITY_MASTER_KEY"]


def test_the_identity_master_key_is_optional_without_imports():
    settings = load(MERVIO_IDENTITY_MASTER_KEY=None, MERVIO_WORKER_JOB_TYPES="analysis,purge")
    assert settings.identity_master_key is None
    assert settings.public()["identity_master_configured"] is False


@pytest.mark.parametrize("value", ["ab" * 31, "zz" * 32, "abcde" * 13, "not-hexadecimal-" * 5])
def test_a_malformed_identity_master_key_is_refused_without_its_value(value):
    refused({"MERVIO_IDENTITY_MASTER_KEY"}, MERVIO_IDENTITY_MASTER_KEY=value)


def test_the_identity_master_key_can_come_from_a_secret_file(tmp_path):
    secret_file = tmp_path / "identity-master-key"
    secret_file.write_text(MASTER_KEY + "\n", encoding="utf-8")
    settings = load(MERVIO_IDENTITY_MASTER_KEY=None, MERVIO_IDENTITY_MASTER_KEY_FILE=str(secret_file))
    assert settings.identity_master_key == Secret(MASTER_KEY)
    refused({"MERVIO_IDENTITY_MASTER_KEY"}, MERVIO_IDENTITY_MASTER_KEY_FILE=str(secret_file))  # les deux: refus
    refused({"MERVIO_IDENTITY_MASTER_KEY_FILE"}, MERVIO_IDENTITY_MASTER_KEY=None,
            MERVIO_IDENTITY_MASTER_KEY_FILE="relative/key")
    refused({"MERVIO_IDENTITY_MASTER_KEY_FILE"}, MERVIO_IDENTITY_MASTER_KEY=None,
            MERVIO_IDENTITY_MASTER_KEY_FILE=str(tmp_path / "absent"))


def test_the_identity_master_key_is_never_displayed():
    settings = load()
    assert settings.identity_master_key == Secret(MASTER_KEY)
    for text in (repr(settings), str(settings), repr(settings.identity_master_key), str(settings.public())):
        assert MASTER_KEY not in text
    assert settings.public()["identity_master_configured"] is True
