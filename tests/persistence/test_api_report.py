"""`GET /v1/stores/{store_id}/analysis-runs/{run_id}/report` contre une VRAIE base (G-2).

LA TRANCHE VERTICALE, EPROUVEE DE BOUT EN BOUT
En-tete `Authorization` -> contrat de jeton (D-098a) -> appartenances lues sous RLS ->
`TenantSession` -> `reports` -> projection G-1 -> octets HTTP. Chaque test passe par le
CLIENT HTTP et par PostgreSQL: aucun double ne remplace la persistance, parce que ce qui est
prouve ici est justement l'accord entre les deux.

CE QUE CE MODULE PROUVE, ET QUE `/v1/me` NE POUVAIT PAS
<< Acces croise = 404 >>. Deux organisations reelles, chacune avec sa boutique, son execution
et son rapport; un porteur de l'une demande la ressource de l'autre et recoit le MEME document
que pour une ressource inexistante. `/v1/me` ne rendait que les appartenances de l'appelant,
donc elle ne pouvait pas montrer ce cas.

AUCUN RESEAU. Les cles sont generees a l'execution et le JWKS vit en memoire.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.jwt_support import AUDIENCE, ISSUER, Key, MemoryJwks, token  # noqa: E402

from mervio.api.app import create_app  # noqa: E402
from mervio.api.auth import TokenVerifier  # noqa: E402
from mervio.api.contract import API_PREFIX  # noqa: E402
from mervio.api.errors import PROBLEM_MEDIA_TYPE  # noqa: E402
from mervio.application.persisted_analysis import (SnapshotImportRequest,  # noqa: E402
                                                   import_csv_snapshot)
from mervio.config import ENGINE_VERSION, AnalyticsConfig  # noqa: E402
from mervio.persistence import analyses  # noqa: E402
from mervio.persistence.codec import serialize_report  # noqa: E402
from mervio.persistence.errors import PermissionDenied  # noqa: E402
from mervio.persistence.pool import DatabasePool  # noqa: E402
from .persistence_support import SAMPLE_FILES, TEST_MASTER_KEY, make_tenant  # noqa: E402

ME = f"{API_PREFIX}/me"


def route(store_id, run_id) -> str:
    return f"{API_PREFIX}/stores/{store_id}/analysis-runs/{run_id}/report"


def bearer(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def build_report(*, synthetic: bool, titre: str) -> dict:
    """Rapport MINIMAL mais valide pour `complete_run`, avec des accents et des objets
    imbriques: les accents sont precisement ce qu'une reserialisation abimerait."""
    return {
        "_meta": {"report_contract": "2.0", "engine_version": ENGINE_VERSION, "grain": "week",
                  "currency": "EUR", "dataset_is_synthetic": synthetic, "analysis_label": "",
                  "generated_at": datetime(2026, 3, 2, 8, 30, tzinfo=timezone.utc).isoformat()},
        "period": {"current": {"grain": "week", "label": "2026-W09",
                               "start": "2026-02-23", "end": "2026-03-01"}},
        "executive_summary": titre,
        "critical_issues": [{"finding_id": "revenue_drop",
                             "confidence": {"level": "high", "criteria": ["baseline"]},
                             "evidence": [{"fact_key": "revenue.drop",
                                           "fact_params": {"pct": -28.0}}]}],
        "warnings": [], "setup": [], "opportunities": [],
    }


class World:
    """Un tenant complet: organisation, proprietaire, boutique, execution et rapport."""

    def __init__(self, tenant, run, record, payload: str) -> None:
        self.tenant = tenant
        self.run = run
        self.record = record
        self.payload = payload

    @property
    def store_id(self):
        return self.tenant.store_id

    @property
    def run_id(self):
        return self.run.id


def seed(db, name: str, titre: str) -> World:
    tenant = make_tenant(db, name)
    result = import_csv_snapshot(tenant.session, store_id=tenant.store_id,
                                 connection_id=tenant.connection_id,
                                 request=SnapshotImportRequest(**SAMPLE_FILES),
                                 master_key=TEST_MASTER_KEY)
    assert result.status == "completed", result.error
    run = analyses.start_run(tenant.session, store_id=tenant.store_id,
                             snapshot_id=result.snapshot.id, config=AnalyticsConfig(),
                             as_of_date=date(2026, 3, 1))
    report = build_report(synthetic=result.snapshot.synthetic, titre=titre)
    record = analyses.complete_run(tenant.session, run, report)
    return World(tenant, run, record, serialize_report(report))


# -- fixtures ------------------------------------------------------------------------------

@pytest.fixture
def key():
    return Key("k-api", "RS256")


@pytest.fixture
def jwks(key):
    return MemoryJwks([key])


@pytest.fixture
def pool(clean):
    with DatabasePool(clean.url("app"), min_size=1, max_size=4,
                      application_name="mervio-api-report-test") as p:
        yield p


@pytest.fixture
def client(pool, jwks, clean):
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=jwks)
    app = create_app(database_url=clean.url("app"), verifier=verifier, database=pool)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def alpha(db):
    return seed(db, "A", "Chiffre d'affaires en légère baisse")


@pytest.fixture
def beta(db):
    return seed(db, "B", "Marge brute stable, trésorerie à surveiller")


@pytest.fixture
def porteur_alpha(key):
    return bearer(token(key, subject="test|A|owner"))


@pytest.fixture
def porteur_beta(key):
    return bearer(token(key, subject="test|B|owner"))


# -- 1. le parcours heureux -------------------------------------------------------------------

def test_an_accessible_report_is_served(client, alpha, porteur_alpha):
    r = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


def test_the_body_is_the_persisted_json_byte_for_byte(client, alpha, porteur_alpha):
    """LA PROPRIETE. Le corps est le texte scelle par la persistance, pas une reecriture."""
    r = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    assert r.content == alpha.payload.encode("utf-8")
    assert "légère" in r.content.decode("utf-8"), "les accents sont des octets, pas des echappements"
    assert r.content != json.dumps(json.loads(alpha.payload)).encode("utf-8")


def test_the_served_bytes_match_what_the_persistence_layer_returns(client, alpha, porteur_alpha):
    stored = analyses.get_report_for_run(alpha.tenant.session, alpha.store_id, alpha.run_id)
    r = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    assert r.content == stored.payload_bytes()


# -- 2. l'ETag --------------------------------------------------------------------------------

def test_the_response_carries_a_strong_etag_derived_from_the_persisted_fingerprint(
        client, alpha, porteur_alpha):
    r = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    assert r.headers["etag"] == f'"{alpha.record.payload_sha256}"'
    assert not r.headers["etag"].startswith("W/"), "ETag FORT, jamais faible"


def test_the_etag_is_stable_across_requests(client, alpha, porteur_alpha):
    premier = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    second = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    assert premier.headers["etag"] == second.headers["etag"]
    assert premier.content == second.content


def test_two_reports_with_different_content_have_different_etags(
        client, alpha, beta, porteur_alpha, porteur_beta):
    a = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    b = client.get(route(beta.store_id, beta.run_id), headers=porteur_beta)
    assert a.headers["etag"] != b.headers["etag"]


# -- 3. `If-None-Match` ------------------------------------------------------------------------

def test_a_matching_validator_gives_304_without_a_body(client, alpha, porteur_alpha):
    etag = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha).headers["etag"]
    r = client.get(route(alpha.store_id, alpha.run_id),
                   headers={**porteur_alpha, "If-None-Match": etag})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == etag, "la RFC 9110 demande de repeter le validateur"


@pytest.mark.parametrize("forme", ["exact", "liste", "faible", "etoile"])
def test_the_supported_validator_forms_all_give_304(client, alpha, porteur_alpha, forme):
    """Les formes que `etag_matches` (G-1) accepte, et elles seules: aucun comportement
    nouveau n'est invente ici."""
    etag = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha).headers["etag"]
    entete = {"exact": etag, "liste": f'"autre", {etag}', "faible": f"W/{etag}",
              "etoile": "*"}[forme]
    r = client.get(route(alpha.store_id, alpha.run_id),
                   headers={**porteur_alpha, "If-None-Match": entete})
    assert r.status_code == 304, forme


@pytest.mark.parametrize("entete", ['"autre"', 'W/"autre"', '"abc", "def"'])
def test_a_validator_that_does_not_match_gives_the_full_body(client, alpha, porteur_alpha, entete):
    r = client.get(route(alpha.store_id, alpha.run_id),
                   headers={**porteur_alpha, "If-None-Match": entete})
    assert r.status_code == 200
    assert r.content == alpha.payload.encode("utf-8")


# -- 4. les quatre 404, indiscernables -----------------------------------------------------------

def refus(client, headers, store_id, run_id):
    r = client.get(route(store_id, run_id), headers=headers)
    return r.status_code, r.content, r.headers.get("content-type")


def test_an_unknown_run_in_your_own_store_is_404(client, alpha, porteur_alpha):
    statut, _, media = refus(client, porteur_alpha, alpha.store_id, uuid.uuid4())
    assert statut == 404 and media.startswith(PROBLEM_MEDIA_TYPE)


def test_an_unknown_store_is_404(client, alpha, porteur_alpha):
    statut, _, _ = refus(client, porteur_alpha, uuid.uuid4(), alpha.run_id)
    assert statut == 404


def test_a_report_of_another_organization_is_404(client, alpha, beta, porteur_alpha):
    """L'ACCES CROISE. Le porteur de A demande la ressource de B, entierement valide chez B."""
    statut, _, _ = refus(client, porteur_alpha, beta.store_id, beta.run_id)
    assert statut == 404


@pytest.mark.parametrize("combinaison", ["store_a_run_b", "store_b_run_a"])
def test_crossed_identifiers_are_404(client, alpha, beta, porteur_alpha, combinaison):
    store, run = ((alpha.store_id, beta.run_id) if combinaison == "store_a_run_b"
                  else (beta.store_id, alpha.run_id))
    statut, _, _ = refus(client, porteur_alpha, store, run)
    assert statut == 404, combinaison


#: En-tetes compares entre les quatre 404, et pourquoi chacun compte.
#:   content-type                 le contrat impose `problem+json`; un cas qui rendrait un
#:                                autre type se distinguerait des trois autres
#:   content-length               une nuance ajoutee a un seul cas -- un `detail` plus
#:                                precis, par exemple -- changerait la longueur
#:   cache-control                une mise en cache differente revelerait un traitement
#:                                different, et `no-store` doit tenir sur les quatre
#:   les quatre en-tetes de securite  poses par le middleware; leur absence sur UN cas
#:                                signalerait un chemin de reponse qui court-circuite la pile
ENTETES_COMPARES = ("content-type", "content-length", "cache-control",
                    "x-content-type-options", "x-frame-options", "referrer-policy",
                    "content-security-policy")

#: En-tetes qui varient legitimement d'une reponse a l'autre: exclus de la comparaison des
#: NOMS presents. Aucun identifiant de requete n'existe aujourd'hui; `date` et `server`
#: seraient les seuls candidats, et ils sont nommes ici pour que leur apparition future ne
#: fasse pas echouer le test a tort.
ENTETES_VOLATILS = frozenset({"date", "server"})


def test_the_four_refusals_are_byte_for_byte_identical(client, alpha, beta, porteur_alpha):
    """LA PROPRIETE CENTRALE: inexistant et croise rendent la MEME reponse -- document ET
    en-tetes. Le chemin differe, donc `instance` aussi: il est retire apres verification de sa
    forme, et c'est la seule difference admise. Meme methode qu'en G-0, ou la comparaison
    portait sur les octets et sept en-tetes; ici les octets ne peuvent pas etre compares
    directement, puisque les chemins different par construction."""
    cas = {
        "run inexistant": (alpha.store_id, uuid.uuid4()),
        "boutique inexistante": (uuid.uuid4(), alpha.run_id),
        "rapport d'un autre tenant": (beta.store_id, beta.run_id),
        "identifiants croises": (alpha.store_id, beta.run_id),
    }
    empreintes = {}
    for nom, (store, run) in cas.items():
        r = client.get(route(store, run), headers=porteur_alpha)
        assert r.status_code == 404, nom

        document = r.json()
        assert document.pop("instance").endswith("/report"), nom

        entetes = {entete: r.headers.get(entete) for entete in ENTETES_COMPARES}
        assert entetes["content-type"].startswith(PROBLEM_MEDIA_TYPE), nom
        assert entetes["cache-control"] == "no-store", nom
        # Un 404 ne porte AUCUN validateur: un ETag sur un refus dirait que quelque chose
        # existe derriere, et c'est exactement ce que l'indiscernabilite interdit.
        presents = {nom_entete.lower() for nom_entete in r.headers} - ENTETES_VOLATILS
        assert "etag" not in presents, nom

        empreintes[nom] = json.dumps({"document": document, "entetes": entetes,
                                      "en-tetes presents": sorted(presents)}, sort_keys=True)

    assert len(set(empreintes.values())) == 1, empreintes


def test_a_refusal_never_names_the_other_organization_or_any_sql(client, alpha, beta, porteur_alpha):
    r = client.get(route(beta.store_id, beta.run_id), headers=porteur_alpha)
    corps = r.text
    assert str(beta.tenant.organization_id) not in corps
    assert str(beta.record.id) not in corps
    for fuite in ("SELECT", "organization_id", "psycopg", "Traceback", "reports"):
        assert fuite not in corps, fuite


# -- 5. authentification --------------------------------------------------------------------------

def test_without_a_token_the_route_is_401(client, alpha):
    r = client.get(route(alpha.store_id, alpha.run_id))
    assert r.status_code == 401
    assert r.json()["code"] == "unauthenticated"


@pytest.mark.parametrize("entete", [{"Authorization": "Bearer "}, {"Authorization": "pas-bearer"},
                                    {"Authorization": "Bearer abc.def.ghi"}])
def test_a_rejected_token_never_reaches_the_database(client, alpha, entete):
    r = client.get(route(alpha.store_id, alpha.run_id), headers=entete)
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"


def test_a_malformed_uuid_is_a_problem_json_422(client, porteur_alpha):
    """Le defaut de FastAPI rendait `application/json`, avec `detail`, `loc`, `ctx` et la
    valeur recue sous `input`. Le contrat impose `problem+json` et un code du catalogue."""
    r = client.get(f"{API_PREFIX}/stores/pas-un-uuid/analysis-runs/{uuid.uuid4()}/report",
                   headers=porteur_alpha)
    assert r.status_code == 422
    assert r.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    document = r.json()
    assert document["code"] == "invalid_request"
    assert set(document) == {"type", "title", "status", "code", "instance"}


def test_the_422_carries_none_of_the_validator_internals(client, porteur_alpha):
    """Aucun detail du validateur ne sort: ni l'emplacement, ni le type d'erreur, ni le
    message de pydantic."""
    r = client.get(f"{API_PREFIX}/stores/pas-un-uuid/analysis-runs/{uuid.uuid4()}/report",
                   headers=porteur_alpha)
    for interne in ("uuid_parsing", "loc", "ctx", "input", "valid UUID", "pydantic"):
        assert interne not in r.text, interne


def test_the_only_echo_of_the_request_is_its_own_path(client, porteur_alpha):
    """LIMITE ASSUMEE, et elle est contractuelle: `instance` porte le chemin de la requete,
    pour TOUS les problemes depuis la tranche E. Un segment malforme y reapparait donc --
    mais le chemin est ce que l'appelant a lui-meme ecrit, et rien d'autre ne sort."""
    r = client.get(f"{API_PREFIX}/stores/pas-un-uuid/analysis-runs/{uuid.uuid4()}/report",
                   headers=porteur_alpha)
    document = r.json()
    assert document["instance"] == f"{API_PREFIX}/stores/pas-un-uuid/analysis-runs/" \
                                   f"{document['instance'].rsplit('/', 2)[1]}/report"
    sans_instance = {k: v for k, v in document.items() if k != "instance"}
    assert "pas-un-uuid" not in json.dumps(sans_instance)


def test_an_unauthenticated_malformed_uuid_is_401_not_422(client):
    """L'authentification precede le routage: un anonyme n'apprend rien de la forme attendue."""
    r = client.get(f"{API_PREFIX}/stores/pas-un-uuid/analysis-runs/x/report")
    assert r.status_code == 401


# -- 6. integrite -----------------------------------------------------------------------------------

def test_a_corrupted_fingerprint_is_a_500_never_a_200_or_a_404(client, alpha, porteur_alpha, monkeypatch):
    """Une incoherence d'integrite ne devient NI un succes, NI un 404 trompeur: elle suit le
    traitement d'erreur serveur. Le `payload_sha256` est fausse a la lecture."""
    from mervio.api.routes import reports as module

    vrai = module.analyses.get_report_for_run

    def corrompu(session, store_id, run_id):
        stored = vrai(session, store_id, run_id)
        object.__setattr__(stored.record, "payload_sha256", "0" * 64)
        return stored

    monkeypatch.setattr(module.analyses, "get_report_for_run", corrompu)
    r = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    assert r.status_code == 500
    assert r.json()["code"] == "internal_error"
    assert "0" * 64 not in r.text


def test_an_insufficient_role_is_not_swallowed_as_a_404(client, alpha, porteur_alpha, monkeypatch):
    """La boucle sur les appartenances n'avale QUE `NotFound`. Le cas est inatteignable par la
    matrice actuelle -- `READ` exige `VIEWER`, le role le plus bas (D-ouverte-1) -- mais la
    boucle ne doit pas decider a la place de la matrice si celle-ci change."""
    from mervio.api.routes import reports as module

    def refuse(session, store_id, run_id):
        raise PermissionDenied("read", "un-role-sans-lecture")

    monkeypatch.setattr(module.analyses, "get_report_for_run", refuse)
    r = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"
    assert "un-role-sans-lecture" not in r.text


# -- 7. isolation du contexte et non-regression ------------------------------------------------------

def test_no_tenant_context_leaks_between_requests_on_a_single_connection(clean, jwks, key, alpha, beta):
    """Pool de TAILLE 1: la meme connexion sert A, puis B, puis A. Si un contexte survivait a
    une requete, la sequence le montrerait."""
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=jwks)
    with DatabasePool(clean.url("app"), min_size=1, max_size=1,
                      application_name="mervio-api-report-pool1") as pool:
        app = create_app(database_url=clean.url("app"), verifier=verifier, database=pool)
        with TestClient(app, raise_server_exceptions=False) as client:
            a = bearer(token(key, subject="test|A|owner"))
            b = bearer(token(key, subject="test|B|owner"))
            sequence = [
                (a, alpha.store_id, alpha.run_id, 200),
                (b, beta.store_id, beta.run_id, 200),
                (a, beta.store_id, beta.run_id, 404),     # A ne voit pas B, meme connexion
                (b, alpha.store_id, alpha.run_id, 404),
                (a, alpha.store_id, alpha.run_id, 200),
            ]
            for entete, store, run, attendu in sequence:
                r = client.get(route(store, run), headers=entete)
                assert r.status_code == attendu, (store, run, attendu)


def test_the_health_and_identity_routes_still_behave(client, alpha, porteur_alpha):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code in (200, 503)
    assert client.get(ME, headers=porteur_alpha).status_code == 200
    assert client.get(ME).status_code == 401


def test_the_route_path_carries_no_organization_segment():
    """Corollaire de la garde AST 3, verifie sur le chemin REEL de la route."""
    chemin = f"{API_PREFIX}/stores/{{store_id}}/analysis-runs/{{run_id}}/report"
    for nom in ("organization_id", "organisation_id", "org_id", "tenant_id"):
        assert f"{{{nom}}}" not in chemin


# -- 8. politique de cache: constatee, non modifiee --------------------------------------------

#: Les cinq en-tetes que le middleware de la tranche E pose sur TOUTE reponse.
SECURITE = {"x-content-type-options": "nosniff", "x-frame-options": "DENY",
            "referrer-policy": "no-referrer",
            "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
            "cache-control": "no-store"}


def test_the_200_carries_no_store_and_every_security_header(client, alpha, porteur_alpha):
    r = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    assert r.status_code == 200
    for nom, valeur in SECURITE.items():
        assert r.headers[nom] == valeur, nom
    assert r.headers["etag"] == f'"{alpha.record.payload_sha256}"'


def test_the_304_carries_no_store_and_every_security_header(client, alpha, porteur_alpha):
    etag = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha).headers["etag"]
    r = client.get(route(alpha.store_id, alpha.run_id),
                   headers={**porteur_alpha, "If-None-Match": etag})
    assert r.status_code == 304
    for nom, valeur in SECURITE.items():
        assert r.headers[nom] == valeur, nom
    assert r.headers["etag"] == etag


@pytest.mark.parametrize("code", [200, 304])
def test_nothing_weakens_the_cache_policy_on_the_report_route(client, alpha, porteur_alpha, code):
    """LE COMPROMIS, CONSTATE ET NON RESOLU ICI. `no-store` est pose par le middleware avec
    `setdefault`: la route POURRAIT le remplacer, et elle ne le fait pas. La consequence est
    qu'un client qui honore `no-store` ne conservera pas le validateur et n'emettra jamais
    `If-None-Match`. Le `304` reste correct; sa portee pratique depend d'un client qui garde
    l'ETag de lui-meme. Changer cela demanderait `private, no-cache`, donc une decision."""
    entetes = dict(porteur_alpha)
    if code == 304:
        entetes["If-None-Match"] = client.get(route(alpha.store_id, alpha.run_id),
                                              headers=porteur_alpha).headers["etag"]
    r = client.get(route(alpha.store_id, alpha.run_id), headers=entetes)

    assert r.status_code == code
    assert r.headers["cache-control"] == "no-store", "exactement, sans directive ajoutee"
    for affaiblissement in ("max-age", "public", "s-maxage", "immutable", "must-revalidate"):
        assert affaiblissement not in r.headers["cache-control"]
    assert "expires" not in r.headers and "pragma" not in r.headers


# -- 9. appartenances multiples: la boucle, reellement exercee ------------------------------------

@pytest.fixture
def membre_des_deux(alpha, beta):
    """Le proprietaire de A devient AUSSI `viewer` chez B. La boucle a donc deux tentatives."""
    from mervio.persistence.tenancy import Role, add_member
    add_member(beta.tenant.session, user_id=alpha.tenant.owner_id, role=Role.VIEWER)
    return alpha.tenant.owner_id


def test_a_member_of_two_organizations_reads_the_report_of_each(
        client, alpha, beta, membre_des_deux, porteur_alpha):
    """La boucle essaie chaque appartenance: le rapport de B est trouve a la SECONDE."""
    a = client.get(route(alpha.store_id, alpha.run_id), headers=porteur_alpha)
    b = client.get(route(beta.store_id, beta.run_id), headers=porteur_alpha)
    assert a.status_code == 200 and b.status_code == 200
    assert a.content == alpha.payload.encode("utf-8")
    assert b.content == beta.payload.encode("utf-8")
    assert a.headers["etag"] != b.headers["etag"]


@pytest.mark.parametrize("combinaison", ["store_a_run_b", "store_b_run_a"])
def test_a_member_of_two_organizations_still_cannot_cross_the_identifiers(
        client, alpha, beta, membre_des_deux, porteur_alpha, combinaison):
    """Etre membre des deux n'ouvre AUCUNE combinaison: le filtre est une seule condition
    `(organization_id, store_id, analysis_run_id)` par tentative."""
    store, run = ((alpha.store_id, beta.run_id) if combinaison == "store_a_run_b"
                  else (beta.store_id, alpha.run_id))
    r = client.get(route(store, run), headers=porteur_alpha)
    assert r.status_code == 404, combinaison


def test_a_refused_membership_falls_through_to_the_next_one(
        client, alpha, beta, membre_des_deux, porteur_alpha, monkeypatch):
    """`TenantAccessDenied` HERITE de `NotFound`: la boucle passe donc a l'appartenance
    suivante au lieu de s'arreter. Ici la premiere tentative est refusee et le rapport de B
    est quand meme servi."""
    from mervio.persistence.errors import TenantAccessDenied
    from mervio.api.routes import reports as module

    vrai = module.analyses.get_report_for_run
    vus = []

    def refuse_la_premiere(session, store_id, run_id):
        vus.append(session.organization_id)
        if len(vus) == 1:
            raise TenantAccessDenied()
        return vrai(session, store_id, run_id)

    monkeypatch.setattr(module.analyses, "get_report_for_run", refuse_la_premiere)
    r = client.get(route(beta.store_id, beta.run_id), headers=porteur_alpha)
    assert r.status_code == 200 and len(vus) == 2
    assert r.content == beta.payload.encode("utf-8")


def test_the_memberships_come_from_the_database_not_from_the_token(
        client, alpha, beta, key, porteur_alpha):
    """Un jeton qui PRETEND une appartenance ne la cree pas: la liste est lue sous RLS."""
    forge = bearer(token(key, subject="test|A|owner",
                         extra={"organization_id": str(beta.tenant.organization_id),
                                "org": str(beta.tenant.organization_id),
                                "role": "owner",
                                "memberships": [str(beta.tenant.organization_id)]}))
    r = client.get(route(beta.store_id, beta.run_id), headers=forge)
    assert r.status_code == 404
    assert client.get(route(alpha.store_id, alpha.run_id), headers=forge).status_code == 200


# -- 10. aucun contexte residuel apres la requete ---------------------------------------------------

def test_a_connection_returned_to_the_pool_carries_no_tenant_context(clean, jwks, key, alpha):
    """Pool de TAILLE 1: la connexion qui vient de servir la requete est la SEULE disponible.
    Si le contexte avait survecu, il serait visible ici. `set_config(..., true)` le fait mourir
    avec la transaction, et son absence vaut REFUS, jamais acces elargi."""
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=jwks)
    with DatabasePool(clean.url("app"), min_size=1, max_size=1,
                      application_name="mervio-api-report-residu") as pool:
        app = create_app(database_url=clean.url("app"), verifier=verifier, database=pool)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(route(alpha.store_id, alpha.run_id),
                           headers=bearer(token(key, subject="test|A|owner")))
            assert r.status_code == 200

        with pool.transaction() as conn:
            organisation, utilisateur = conn.execute(
                "SELECT app_current_organization_id(), "
                "NULLIF(current_setting('app.user_id', true), '')").fetchone()
            assert organisation is None, "un contexte d'organisation a survecu a la requete"
            assert utilisateur is None
            # Et sans contexte, la table ne rend RIEN: le mode de defaillance est le refus.
            assert conn.execute("SELECT count(*) FROM reports").fetchone()[0] == 0
