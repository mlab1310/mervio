"""Squelette de l'API: sante, `problem+json`, en-tetes, versionnage (Mission 004.6, tranche E).

Aucune route metier n'existe encore, et c'est le point: ce module verifie la FRONTIERE avant
qu'elle ne porte quoi que ce soit.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mervio.api.app import SECURITY_HEADERS, create_app
from mervio.api.contract import API_CONTRACT, API_PREFIX
from mervio.api.errors import (CATALOGUE, PROBLEM_MEDIA_TYPE, ApiError, FORBIDDEN, NOT_FOUND,
                               PROBLEM_TYPE_PREFIX)
from mervio.persistence import migrate


@pytest.fixture
def client():
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


# -- sante ------------------------------------------------------------------------------------

def test_healthz_answers_without_any_dependency(client):
    """Vivant ne depend de RIEN. Sans cela, un orchestrateur redemarrerait un processus sain
    parce que sa base est momentanement absente."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "alive", "api_contract": API_CONTRACT}


def test_readyz_refuses_when_no_database_is_configured(client):
    response = client.get("/readyz")
    assert response.status_code == 503
    document = response.json()
    assert document["ready"] is False
    assert document["reason"] == "database_not_configured"
    assert document["migration"]["expected"] == migrate.head_revision()


def test_readyz_refuses_an_unreachable_database_without_naming_it():
    """Le refus ne porte ni hote, ni role, ni chemin: un code stable, et le journal fait le reste."""
    app = create_app(database_url="postgresql://nobody@127.0.0.1:1/absent")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    document = response.json()
    assert document["reason"] == "database_unreachable"
    assert "nobody" not in response.text and "127.0.0.1" not in response.text


# -- problem+json -----------------------------------------------------------------------------

def test_an_api_error_becomes_a_problem_json_document(client):
    client.app.get("/_probe_forbidden")(lambda: (_ for _ in ()).throw(ApiError(FORBIDDEN)))
    response = client.get("/_probe_forbidden")
    assert response.status_code == 403
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json() == {"type": f"{PROBLEM_TYPE_PREFIX}forbidden", "title": FORBIDDEN.title,
                               "status": 403, "code": "forbidden", "detail": FORBIDDEN.detail,
                               "instance": "/_probe_forbidden"}


def test_an_unexpected_failure_never_echoes_the_request(client):
    secret = "jeton-de-la-requete"

    def boom():
        raise RuntimeError(secret)

    client.app.get("/_probe_boom")(boom)
    response = client.get(f"/_probe_boom?trace={secret}")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert secret not in response.text, "une erreur interne ne decrit pas la requete qui l'a revelee"


def test_the_catalogue_of_codes_is_closed_and_unique():
    """Une route DESIGNE un code, elle ne le forge pas. Les codes sont donc uniques et nommes."""
    codes = [problem.code for problem in CATALOGUE]
    assert len(codes) == len(set(codes))
    assert {p.status for p in CATALOGUE} == {401, 403, 404, 422, 500, 503}


def test_a_cross_tenant_refusal_is_indistinguishable_from_an_absent_resource():
    """ADR-004-003 porte en HTTP: le MEME document pour les deux cas, sans nuance."""
    assert NOT_FOUND.status == 404
    assert "indiscernables" in (NOT_FOUND.detail or "")


# -- en-tetes et versionnage ------------------------------------------------------------------

def test_every_response_carries_the_security_headers(client):
    response = client.get("/healthz")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_the_api_contract_is_pinned_and_independent_of_the_report_contract():
    """D-100: trois choses distinctes. `API_CONTRACT` ne suit pas `REPORT_CONTRACT`."""
    from mervio.config import REPORT_CONTRACT
    assert API_CONTRACT == "1.0"
    assert API_PREFIX == "/v1"
    assert API_CONTRACT != REPORT_CONTRACT, (
        "les deux versions doivent pouvoir differer: un rapport 1.x persiste reste servi")


def test_the_openapi_document_announces_the_api_contract(client):
    document = client.get(f"{API_PREFIX}/openapi.json").json()
    assert document["openapi"].startswith("3.1")
    assert document["info"]["version"] == API_CONTRACT
    assert document["paths"] == {}, "la tranche E ne livre aucune route metier"
