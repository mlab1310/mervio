"""`GET /v1/me` -- premiere surface HTTP authentifiee, contre une VRAIE base (tranche F).

CE MODULE PROUVE
Le parcours complet: en-tete `Authorization` -> contrat de jeton (D-098a) -> `app_ensure_user`
-> `memberships` sous RLS -> `TenantSession` par organisation. Et que tout refus est un
`problem+json` 401 INDIFFERENCIE.

CE MODULE NE PROUVE PAS
L'isolation entre organisations. `/v1/me` ne rend que les appartenances de l'appelant, donc
elle ne peut pas montrer ce qui arrive quand on demande la ressource d'un AUTRE tenant. Cette
preuve est une matrice par route -- succes, 401, role insuffisant, acces croise = 404 -- et
c'est la tranche G qui la porte. Ce qui est demontre ici est enonce test par test.

AUCUN RESEAU. Les cles sont generees a l'execution, le JWKS vit en memoire.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.jwt_support import (AUDIENCE, ISSUER, Key, MemoryJwks, hmac_token,  # noqa: E402
                             token, unsigned_token)

from mervio.api.app import PUBLIC_PATHS, create_app  # noqa: E402
from mervio.api.auth import TokenVerifier  # noqa: E402
from mervio.api.contract import API_PREFIX  # noqa: E402
from mervio.api.errors import PROBLEM_MEDIA_TYPE  # noqa: E402
from mervio.persistence import tenancy  # noqa: E402
from mervio.persistence.pool import DatabasePool  # noqa: E402
from mervio.persistence.tenancy import Role  # noqa: E402

ME = f"{API_PREFIX}/me"


@pytest.fixture
def key():
    return Key("k-api", "RS256")


@pytest.fixture
def jwks(key):
    return MemoryJwks([key])


@pytest.fixture
def pool(clean):
    with DatabasePool(clean.url("app"), min_size=1, max_size=4,
                      application_name="mervio-api-test") as p:
        yield p


@pytest.fixture
def client(pool, jwks, clean):
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=jwks)
    app = create_app(database_url=clean.url("app"), verifier=verifier, database=pool)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def bearer(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


# -- 1. identite valide --------------------------------------------------------------------

def test_a_valid_token_provisions_the_user_and_returns_an_empty_membership_list(client, key, pool):
    """Provisionnement a la volee: D-098a ecrit `sub` -> `ensure_user`, un insert-or-get. Un
    jeton valide de l'emetteur configure CREE donc l'utilisateur. La barriere est `iss` + `aud`,
    verifies avant d'arriver a la base."""
    response = client.get(ME, headers=bearer(token(key, subject="idp|nouvelle")))
    assert response.status_code == 200
    document = response.json()
    assert document["organizations"] == []
    assert document["user_id"] == str(tenancy.ensure_user(pool, "idp|nouvelle"))


def test_the_same_subject_resolves_to_the_same_user_twice(client, key):
    first = client.get(ME, headers=bearer(token(key, subject="idp|stable"))).json()
    second = client.get(ME, headers=bearer(token(key, subject="idp|stable"))).json()
    assert first["user_id"] == second["user_id"]


def test_memberships_and_roles_come_from_the_database(client, key, pool):
    """Les appartenances sont LUES, jamais deduites d'un parametre: la politique
    `memberships_own_rows_readable` ne rend que ses propres lignes."""
    user = tenancy.ensure_user(pool, "idp|proprietaire")
    organization = tenancy.create_organization(pool, owner_user_id=user, name="Boulangerie")

    document = client.get(ME, headers=bearer(token(key, subject="idp|proprietaire"))).json()
    assert document["user_id"] == str(user)
    assert document["organizations"] == [
        {"organization_id": str(organization), "role": Role.OWNER.value, "name": "Boulangerie"}]


def test_a_user_never_sees_another_users_memberships(client, key, pool):
    """Ce que CE test prouve: `/v1/me` ne rend que SES appartenances. Ce qu'il ne prouve PAS:
    qu'une route tenant-scopee refuse une ressource d'un autre tenant -- c'est la tranche G."""
    alice = tenancy.ensure_user(pool, "idp|alice")
    bob = tenancy.ensure_user(pool, "idp|bob")
    tenancy.create_organization(pool, owner_user_id=alice, name="Chez Alice")
    tenancy.create_organization(pool, owner_user_id=bob, name="Chez Bob")

    seen = client.get(ME, headers=bearer(token(key, subject="idp|alice"))).json()
    assert [o["name"] for o in seen["organizations"]] == ["Chez Alice"]
    assert seen["user_id"] == str(alice)
    assert str(bob) not in response_text(seen)


def response_text(document) -> str:
    import json
    return json.dumps(document)


# -- 2. la matrice de refus ----------------------------------------------------------------

def test_without_a_token_the_route_is_401(client):
    response = client.get(ME)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.parametrize("header", [
    {"Authorization": ""}, {"Authorization": "Bearer"}, {"Authorization": "Bearer    "},
    {"Authorization": "Basic abc"}, {"Authorization": "abc"},
])
def test_a_malformed_authorization_header_is_401(client, header):
    assert client.get(ME, headers=header).status_code == 401


def test_a_token_in_a_query_parameter_is_not_accepted(client, key):
    """Un jeton dans une URL finit dans les journaux de tout intermediaire."""
    assert client.get(f"{ME}?access_token={token(key)}").status_code == 401


def test_a_token_in_a_cookie_is_not_accepted(client, key):
    """Un jeton en cookie demanderait une defense CSRF que cette API n'a pas."""
    client.cookies.set("access_token", token(key))
    assert client.get(ME).status_code == 401


@pytest.mark.parametrize("name, build", [
    ("signature invalide", lambda k: token(Key(k.key_id, "RS256"))),
    ("alg none", lambda k: unsigned_token()),
    ("alg HMAC", lambda k: hmac_token()),
    ("kid absent", lambda k: token(k, key_id=None)),
    ("kid inconnu", lambda k: token(k, key_id="k-fantome")),
    ("mauvais iss", lambda k: token(k, issuer="https://autre.invalid/realms/x")),
    ("mauvaise aud", lambda k: token(k, audience="une-autre-api")),
    ("expire", lambda k: token(k, now=int(time.time()) - 7200, lifetime=60)),
    ("pas encore valide", lambda k: token(k, not_before=int(time.time()) + 3600)),
    ("sujet absent", lambda k: token(k, drop=("sub",))),
    ("sujet vide", lambda k: token(k, subject="")),
    ("sujet de service", lambda k: token(k, subject="service:mervio_worker")),
    ("sujet trop long", lambda k: token(k, subject="x" * 256)),
    ("malforme", lambda k: "pas.un.jeton"),
])
def test_every_rejection_is_the_same_undifferentiated_401(client, key, name, build):
    """Distinguer << mauvaise audience >> de << signature invalide >> aiderait un attaquant a
    calibrer sa tentative suivante. Les quatorze refus rendent le MEME document."""
    response = client.get(ME, headers=bearer(build(key)))
    assert response.status_code == 401, name
    assert response.json()["code"] == "unauthenticated", name


def test_the_rejections_are_byte_for_byte_identical(client, key):
    """Pas seulement le meme code: le meme corps. Une nuance serait un canal lateral."""
    bodies = {
        client.get(ME, headers=bearer(token(key, audience="autre"))).text,
        client.get(ME, headers=bearer(token(key, issuer="https://autre.invalid/r"))).text,
        client.get(ME, headers=bearer(token(Key(key.key_id, "RS256")))).text,
        client.get(ME).text,
    }
    assert len(bodies) == 1, bodies


def test_an_unknown_subject_is_indistinguishable_from_a_known_one(client, key, pool):
    """<< Ne revele pas si un sujet existe en base >>: avec le provisionnement a la volee, la
    reponse a la MEME forme dans les deux cas, et aucun champ ne dit lequel."""
    tenancy.ensure_user(pool, "idp|connu")
    known = client.get(ME, headers=bearer(token(key, subject="idp|connu")))
    unknown = client.get(ME, headers=bearer(token(key, subject="idp|jamais-vu")))
    assert known.status_code == unknown.status_code == 200
    assert set(known.json()) == set(unknown.json())
    assert known.json()["organizations"] == unknown.json()["organizations"] == []


# -- 3. aucune fuite -----------------------------------------------------------------------

def test_no_rejection_echoes_the_token_or_the_issuer(client, key):
    raw = token(key, audience="une-autre-api")
    response = client.get(ME, headers=bearer(raw))
    for secret in (raw, raw.split(".")[1], ISSUER, "idp.test.invalid"):
        assert secret not in response.text


def test_the_identity_document_never_carries_the_idp_subject(client, key):
    """Le porteur connait deja son sujet -- il est dans son jeton. Un document qui le
    transporte finit par apparaitre dans une trace."""
    response = client.get(ME, headers=bearer(token(key, subject="idp|alice-secrete")))
    assert response.status_code == 200
    assert "alice-secrete" not in response.text
    assert "idp_subject" not in response.text


def test_no_token_or_subject_reaches_the_logs(client, key, caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        raw = token(key, subject="idp|alice-secrete")
        client.get(ME, headers=bearer(raw))
        client.get(ME, headers=bearer(token(key, audience="autre")))
    captured = "\n".join(r.getMessage() for r in caplog.records)
    assert raw not in captured
    assert "alice-secrete" not in captured


# -- 4. les limites de perimetre -----------------------------------------------------------

def test_only_health_is_unauthenticated(client, pool, clean, jwks):
    """Liste FERMEE. Une route ajoutee demain est protegee par DEFAUT, parce que la protection
    est un middleware et non une dependance par route."""
    assert PUBLIC_PATHS == ("/healthz", "/readyz")
    for path in PUBLIC_PATHS:
        assert client.get(path).status_code in (200, 503)
    for path in (ME, f"{API_PREFIX}/stores", f"{API_PREFIX}/inconnu", "/"):
        assert client.get(path).status_code == 401, path


def test_a_skeleton_without_a_verifier_serves_no_business_route(clean):
    """Sans seam d'authentification, AUCUNE route metier n'est enregistree: un squelette sans
    identite ne doit pas pouvoir servir une route protegee par inadvertance."""
    app = create_app(database_url=clean.url("app"))
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get(ME).status_code == 404
        assert c.get(f"{API_PREFIX}/openapi.json").json()["paths"] == {}


def test_the_openapi_document_now_declares_exactly_one_route(client):
    document = client.get(f"{API_PREFIX}/openapi.json").json()
    assert list(document["paths"]) == [ME]
    assert list(document["paths"][ME]) == ["get"]
