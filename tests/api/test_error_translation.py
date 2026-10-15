"""Traduction des erreurs de persistance a la frontiere HTTP (Mission 004.6, tranche G-0).

LE DEFAUT CORRIGE, ET SA MESURE
Avant G-0, `api/` n'importait aucune erreur de `persistence/errors.py` et l'application ne
posait que deux gestionnaires: `ApiError` et `Exception`. Mesure sur `0dbb8a4`, sur
l'application reelle fabriquee par `create_app`:
    NotFound            -> 500 internal_error
    TenantAccessDenied  -> 500 internal_error
    PermissionDenied    -> 500 internal_error
Un acces croise aurait donc ete un 500. La propriete centrale de l'isolation HTTP --
<< tenant croise et inexistant sont indiscernables >> -- etait inatteignable, et la matrice de
G-2 l'aurait ete aussi.

CE QUE CE MODULE PROUVE
Deux traductions, et DEUX SEULEMENT. Que les deux chemins de 404 rendent le MEME document,
octet pour octet, sur le MEME chemin. Que rien de l'exception ne sort. Et que toutes les
autres erreurs de persistance restent des 500.

AUCUNE BASE, AUCUN RESEAU. Les routes-sondes levent les exceptions directement: ce qui est
teste est la TRADUCTION, pas la persistance, qui a ses propres suites.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mervio.api.app import create_app
from mervio.api.errors import FORBIDDEN, NOT_FOUND, PROBLEM_MEDIA_TYPE
from mervio.persistence.errors import (ImportRejected, JobStateError, MoneyPrecisionError,
                                       NotFound, PermissionDenied, PersistenceError,
                                       SchemaNotReady, ServiceIdentityError,
                                       SnapshotIntegrityError, TenantAccessDenied)

#: Chemin UNIQUE des sondes. `instance` porte le chemin de la requete, donc deux sondes sur
#: deux chemins differents rendraient deux documents differents et la comparaison octet pour
#: octet ne prouverait rien.
SONDE = "/essai/sonde"


@pytest.fixture
def leve():
    """Ce que la sonde doit lever. Mutable, pour que tous les cas partagent UN chemin."""
    return {"exception": None}


@pytest.fixture
def client(leve):
    app = create_app()                               # squelette: aucune route metier

    @app.get(SONDE)
    def _sonde():
        raise leve["exception"]

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def reponse(client, leve, exception):
    leve["exception"] = exception
    return client.get(SONDE)


# -- les deux traductions ------------------------------------------------------------------

def test_a_missing_resource_becomes_a_404_problem(client, leve):
    r = reponse(client, leve, NotFound("report"))
    assert r.status_code == 404
    assert r.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    document = r.json()
    assert document["code"] == "not_found"
    assert document["type"] == NOT_FOUND.type
    assert document["status"] == 404
    assert document["instance"] == SONDE


def test_an_insufficient_role_becomes_a_403_problem(client, leve):
    r = reponse(client, leve, PermissionDenied("read", "viewer"))
    assert r.status_code == 403
    assert r.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    document = r.json()
    assert document["code"] == "forbidden"
    assert document["type"] == FORBIDDEN.type
    assert document["status"] == 403


# -- l'indiscernabilite, prouvee octet pour octet ------------------------------------------

def test_a_cross_tenant_access_is_the_same_404_as_a_missing_resource(client, leve):
    """LA PROPRIETE CENTRALE. Meme chemin, donc meme `instance`: ce qui reste compare est bien
    la traduction, et rien d'autre."""
    absent = reponse(client, leve, NotFound("report"))
    croise = reponse(client, leve, TenantAccessDenied())

    assert absent.status_code == croise.status_code == 404
    assert absent.content == croise.content, "les deux documents doivent etre IDENTIQUES"


@pytest.mark.parametrize("entete", ["content-type", "content-length", "cache-control",
                                    "x-content-type-options", "x-frame-options",
                                    "referrer-policy", "content-security-policy"])
def test_the_two_paths_to_a_404_carry_identical_headers(client, leve, entete):
    """Un corps identique ne suffit pas: un en-tete different distinguerait les deux cas."""
    absent = reponse(client, leve, NotFound("report"))
    croise = reponse(client, leve, TenantAccessDenied())
    assert absent.headers.get(entete) == croise.headers.get(entete), entete


def test_the_indiscernability_comes_from_the_hierarchy_not_from_a_rule():
    """`TenantAccessDenied` n'est nommee par AUCUN code de `api/` -- l'en-tete de `app.py`
    l'explique, mais aucune instruction ne la mentionne. Elle herite de `NotFound` et la
    recherche de gestionnaire remonte la hierarchie: on ne peut donc pas cesser de les
    confondre par inadvertance. Verifie par l'AST, comme les trois gardes structurelles."""
    import ast
    import pathlib

    api = pathlib.Path(__file__).resolve().parents[2] / "src" / "mervio" / "api"
    for module in sorted(api.rglob("*.py")):
        arbre = ast.parse(module.read_text(encoding="utf-8"))
        nomme = set()
        for noeud in ast.walk(arbre):
            if isinstance(noeud, ast.Name):
                nomme.add(noeud.id)
            elif isinstance(noeud, ast.Attribute):
                nomme.add(noeud.attr)
            elif isinstance(noeud, (ast.Import, ast.ImportFrom)):
                nomme |= {alias.name for alias in noeud.names}
        assert "TenantAccessDenied" not in nomme, f"{module.name} nomme TenantAccessDenied"
    assert issubclass(TenantAccessDenied, NotFound)


# -- rien de l'exception ne sort -----------------------------------------------------------

def test_the_404_never_names_the_kind_of_resource(client, leve):
    r = reponse(client, leve, NotFound("un-type-tres-reconnaissable"))
    assert "un-type-tres-reconnaissable" not in r.text


def test_the_403_never_names_the_permission_or_the_role(client, leve):
    r = reponse(client, leve, PermissionDenied("purge_tenant", "analyst"))
    assert "purge_tenant" not in r.text and "analyst" not in r.text


@pytest.mark.parametrize("exception", [NotFound("report"), TenantAccessDenied(),
                                       PermissionDenied("read", "viewer")])
def test_a_translated_refusal_carries_no_message_and_no_extra_field(client, leve, exception):
    """Les clefs sont celles du contrat RFC 9457, et aucune autre: pas de `kind`, pas de
    `role`, pas de trace."""
    r = reponse(client, leve, exception)
    assert set(r.json()) == {"type", "title", "status", "code", "detail", "instance"}
    assert str(exception) not in r.text


# -- la liste est FERMEE: tout le reste demeure un 500 -------------------------------------

@pytest.mark.parametrize("exception", [
    MoneyPrecisionError("montant non representable"),
    SchemaNotReady("revision manquante"),
    ServiceIdentityError("mauvais principal"),
    ImportRejected("currency_mismatch", "devise incompatible"),
    JobStateError("transition impossible"),
    SnapshotIntegrityError("instantane inutilisable"),
    PersistenceError("erreur de base"),
], ids=lambda e: type(e).__name__)
def test_every_other_persistence_error_stays_an_internal_500(client, leve, exception):
    """Un defaut INTERNE ne decrit pas une requete client fautive. Le traduire en 4xx ferait
    chercher le probleme du mauvais cote."""
    r = reponse(client, leve, exception)
    assert r.status_code == 500
    assert r.json()["code"] == "internal_error"
    assert str(exception) not in r.text


def test_a_plain_exception_stays_an_internal_500(client, leve):
    r = reponse(client, leve, RuntimeError("defaut interne"))
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
    assert "defaut interne" not in r.text


def test_the_api_translates_exactly_two_persistence_errors(client, leve):
    """Structurel: la table de traduction de l'application ne contient que ces deux entrees."""
    app = client.app
    traduites = {exc for exc in app.exception_handlers
                 if isinstance(exc, type) and issubclass(exc, PersistenceError)}
    assert traduites == {NotFound, PermissionDenied}


# -- les en-tetes de securite survivent a la traduction ------------------------------------

@pytest.mark.parametrize("exception", [NotFound("report"), PermissionDenied("read", "viewer")])
def test_a_translated_refusal_still_carries_the_security_headers(client, leve, exception):
    r = reponse(client, leve, exception)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


# -- limite connue, et elle est TESTEE -----------------------------------------------------

def test_a_persistence_error_raised_by_a_middleware_is_not_translated():
    """Les gestionnaires vivent DANS la pile, pas au-dessus: une erreur levee par un
    middleware ne les atteint pas. C'est le comportement d'avant G-0, conserve volontairement
    -- `_require_identity` resout une identite, il ne lit pas une ressource -- et ecrit ici
    pour qu'il soit constate plutot que decouvert."""
    from fastapi import Request

    app = create_app()

    @app.middleware("http")
    async def _leve(request: Request, call_next):
        raise NotFound("report")

    @app.get(SONDE)
    def _sonde():
        return {"jamais": "atteint"}

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get(SONDE)
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
