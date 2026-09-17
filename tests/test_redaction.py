"""Redaction des valeurs publiees (Mission 004.3). Sans base."""
from __future__ import annotations

import pytest

from mervio.observability.logging import MAX_VALUE_LENGTH, scrub, scrub_text
from mervio.observability.redaction import (
    FORBIDDEN_KEY_FRAGMENTS, REDACTED, REDACTED_EMAIL, REDACTED_PATH, describe_exception, forbidden_key,
    redact_text,
)

SECRET = "Zq9-XyZ-s3cr3t-VALUE"

#: Les faux jetons sont assembles a l'execution: aucun ne figure tel quel dans le depot
#: (l'analyse de secrets de la CI les signalerait).
LEAKS = [
    # (texte, fragment qui ne doit plus apparaitre, marque attendue)
    (f"echec postgresql://mervio_worker:{SECRET}@db:5432/mervio", SECRET, REDACTED),
    (f"postgres://u:{SECRET}@h/d?sslmode=require", SECRET, REDACTED),
    (f"https://user:{SECRET}@api.example.com/v1", SECRET, REDACTED),
    (f"host=db port=5432 password={SECRET} dbname=x", SECRET, REDACTED),
    (f"PASSWORD: '{SECRET}'", SECRET, REDACTED),
    (f'{{"token": "{SECRET}", "retries": 3}}', SECRET, REDACTED),
    (f"api_key={SECRET}&page=2", SECRET, REDACTED),
    (f"client_secret={SECRET}", SECRET, REDACTED),
    (f"X-Shopify-Access-Token: {SECRET}", SECRET, REDACTED),
    (f"Authorization: Bearer {SECRET}", SECRET, REDACTED),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA==", REDACTED),
    ("jwt " + "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ" + "zdWIiOiIxMjM0NTYifQ." + "SflKxwRJSMeKKF2QT4fwpM",
     "SflKxwRJSMeKKF2QT4fwpM", REDACTED),
    ("stripe " + "sk_" + "live_" + "51H8ZqL2eZvKYlo2C0abcdef", "51H8ZqL2eZvKYlo2C0abcdef", REDACTED),
    ("openai " + "sk-" + "proj4f8a9b0c1d2e3f4a5b6c", "proj4f8a9b0c1d2e3f4a5b6c", REDACTED),
    ("anthropic " + "sk-" + "ant-api03-AbCdEfGhIjKlMnOp", "AbCdEfGhIjKlMnOp", REDACTED),
    ("github " + "gh" + "p_" + "16C7e42F292c6912E7710c838347Ae178B4a", "16C7e42F292c6912E7710c838347Ae178B4a",
     REDACTED),
    ("aws " + "AKIA" + "IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE", REDACTED),
    ("shopify " + "shp" + "at_" + "0123456789abcdef" * 2, "0123456789abcdef" * 2, REDACTED),
    ("slack " + "xo" + "xb-" + "123456789012-abcdefABCDEF", "123456789012-abcdefABCDEF", REDACTED),
    ("google " + "AI" + "za" + "SyA1234567890abcdefghijklmnopqrstu", "SyA1234567890abcdefghijklmnopqrstu", REDACTED),
    ("3 fichiers ecrits dans /Users/alice/Clients/acme/analysis", "/Users/alice", REDACTED_PATH),
    ("lecture impossible: /srv/uploads/org-1/orders.csv (errno 2)", "orders.csv", REDACTED_PATH),
    ("voir ~/exports/acme_orders.csv", "acme_orders", REDACTED_PATH),
    ("fichier C:\\Users\\bob\\export.csv introuvable", "bob", REDACTED_PATH),
    ("partage \\\\nas\\clients\\acme.csv", "acme", REDACTED_PATH),
    ("client alice.martin+shop@example.co.uk a commande", "alice.martin", REDACTED_EMAIL),
]


@pytest.mark.parametrize("text, leaked, marker", LEAKS)
def test_secrets_paths_and_emails_are_masked_in_free_text(text, leaked, marker):
    cleaned = redact_text(text)
    assert leaked not in cleaned, cleaned
    assert marker in cleaned, cleaned


KEPT = [
    "inputs_sha256=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "job 7c9e6679-7425-40de-944b-e07fc1f90ae7 attempt 2/3",
    "worker host-1/4242/ab12cd34",
    "voir https://example.com/v1/stores/42",
    "sqlstate_40P01 apres 12.5 ms",
    "tokens=1200 prompt_tokens=800",
    "duree 12:30, 3 remboursements",
    "import refuse: source_changed_during_import",
    "postgresql://mervio_worker@db:5432/mervio",
]


@pytest.mark.parametrize("text", KEPT)
def test_useful_diagnostics_are_kept(text):
    assert redact_text(text) == text


def test_the_url_keeps_scheme_user_and_host():
    assert redact_text(f"postgresql://mervio:{SECRET}@db.internal:5432/x") == \
        "postgresql://mervio:[redacted]@db.internal:5432/x"


def test_redaction_is_idempotent():
    for text, _, _ in LEAKS:
        once = redact_text(text)
        assert redact_text(once) == once


@pytest.mark.parametrize("key", ["password", "DB_PASSWORD", "database_url", "dsn", "conninfo", "access_key",
                                 "session_key", "x-api-key".replace("-", "_"), "shopify_token", "customer_email"])
def test_sensitive_keys_are_recognized(key):
    assert forbidden_key(key)


def test_the_key_list_covers_connection_strings():
    for fragment in ("dsn", "database_url", "conninfo"):
        assert fragment in FORBIDDEN_KEY_FRAGMENTS


def test_scrub_masks_secret_values_under_innocent_keys():
    cleaned = scrub({"detail": f"connexion postgresql://u:{SECRET}@h/d refusee", "attempt": 2,
                     "nested": {"note": f"password={SECRET}"}, "items": [f"Bearer {SECRET}"]})
    assert SECRET not in str(cleaned)
    assert cleaned["attempt"] == 2


def test_scrub_publishes_only_the_type_of_an_exception():
    error = PermissionError(f"acces refuse a /Users/alice/{SECRET}.csv")
    assert scrub({"cause": error}) == {"cause": "PermissionError"}
    assert describe_exception(error) == "PermissionError"


def test_truncation_happens_after_redaction():
    value = f"password={SECRET} " + "x" * 500
    cleaned = scrub({"detail": value})["detail"]
    assert SECRET not in cleaned and len(cleaned) == MAX_VALUE_LENGTH + 3


def test_scrub_text_cleans_without_truncating():
    text = "x" * 500 + f" token={SECRET}"
    cleaned = scrub_text(text)
    assert SECRET not in cleaned and len(cleaned) > MAX_VALUE_LENGTH
