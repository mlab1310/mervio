"""`HttpJwksSource`: appels sortants BORNES et recuperation UNIQUE (F.1).

CE MODULE MESURE, IL N'AFFIRME PAS. Chaque test compte les appels sortants reels via un
transport injecte, et les tests de concurrence utilisent une BARRIERE -- pas un enchainement
sequentiel qui prouverait seulement que le code ne plante pas.

Mesures de reference AVANT correctif, relevees sur le code livre par 6fe71fd:
    20 requetes a `kid` inconnu  -> 42 appels HTTP (2,1 par requete refusee)
    8 requetes simultanees a froid -> 8 recuperations
"""
from __future__ import annotations

import threading

import pytest

from mervio.api.auth import (DEFAULT_MIN_REFRESH_SECONDS, HttpJwksSource, TokenRejected,
                             TokenVerifier)

from .jwt_support import AUDIENCE, ISSUER, Key, token

DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
KEYS_URI = "https://idp.test.invalid/realms/mervio/protocol/openid-connect/certs"


class Transport:
    """Transport injecte qui COMPTE chaque appel sortant et peut echouer a la demande."""

    def __init__(self, keys=None, *, fail=False, delay: float = 0.0) -> None:
        self.calls: list = []
        self.keys = list(keys or [])
        self.fail = fail
        self.delay = delay
        self._started = threading.Event()

    def __call__(self, url: str, *, timeout: float):
        self.calls.append(url)
        self._started.set()
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.fail:
            raise TokenRejected("issuer_unreachable")
        if url.endswith("openid-configuration"):
            return {"jwks_uri": KEYS_URI}
        return {"keys": [k.jwk() for k in self.keys]}

    @property
    def discoveries(self) -> int:
        return sum(1 for c in self.calls if c.endswith("openid-configuration"))

    @property
    def key_fetches(self) -> int:
        return sum(1 for c in self.calls if not c.endswith("openid-configuration"))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def source(transport, clock, **kwargs) -> HttpJwksSource:
    return HttpJwksSource(ISSUER, clock=clock, fetch=transport, **kwargs)


# -- acceptation 1: plusieurs `kid` inconnus dans une fenetre courte -----------------------

def test_many_unknown_key_ids_in_one_window_make_a_bounded_number_of_calls():
    """AVANT: 20 requetes -> 42 appels. Le test mesure, il n'affirme pas."""
    key = Key("k-reelle", "RS256")
    transport, clock = Transport([key]), Clock()
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE,
                             source=source(transport, clock, min_refresh_seconds=30.0))

    refusals = set()
    for i in range(20):
        with pytest.raises(TokenRejected) as excinfo:
            verifier.verify(token(key, key_id=f"k-invente-{i}"))
        refusals.add(excinfo.value.reason)

    assert refusals == {"key_id_unknown"}
    assert len(transport.calls) == 2, f"attendu 2 (decouverte + cles), mesure {len(transport.calls)}"
    assert transport.discoveries == 1, "la decouverte ne se refait PAS a chaque relecture"
    assert transport.key_fetches == 1


def test_the_discovery_is_done_once_and_its_validated_uri_is_reused():
    """Une relecture coute UN appel, pas deux: le `jwks_uri` valide est mis en cache."""
    key = Key("k1", "RS256")
    transport, clock = Transport([key]), Clock()
    jwks = source(transport, clock, cache_seconds=60.0, min_refresh_seconds=1.0)

    jwks.jwks()
    assert (transport.discoveries, transport.key_fetches) == (1, 1)
    for _ in range(4):
        clock.advance(61.0)
        jwks.jwks()
    assert transport.discoveries == 1, "la decouverte reste unique"
    assert transport.key_fetches == 5


# -- acceptation 2: apres expiration de la fenetre, une relecture redevient possible -------

def test_after_the_window_a_refresh_becomes_possible_again():
    key = Key("k-reelle", "RS256")
    transport, clock = Transport([key]), Clock()
    jwks = source(transport, clock, min_refresh_seconds=30.0)
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=jwks)

    with pytest.raises(TokenRejected):
        verifier.verify(token(key, key_id="k-absente"))
    assert len(transport.calls) == 2

    with pytest.raises(TokenRejected):          # dans la fenetre: aucun appel de plus
        verifier.verify(token(key, key_id="k-absente"))
    assert len(transport.calls) == 2

    clock.advance(31.0)
    with pytest.raises(TokenRejected):          # fenetre passee: une relecture, UN appel
        verifier.verify(token(key, key_id="k-absente"))
    assert len(transport.calls) == 3
    assert transport.discoveries == 1


# -- acceptation 3: une rotation legitime est reconnue dans le delai prevu -----------------

def test_a_legitimate_rotation_is_recognised_within_the_window():
    ancienne, nouvelle = Key("k-ancienne", "RS256"), Key("k-nouvelle", "RS256")
    transport, clock = Transport([ancienne]), Clock()
    jwks = source(transport, clock, cache_seconds=300.0, min_refresh_seconds=30.0)
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=jwks)

    assert verifier.verify(token(ancienne)).key_id == "k-ancienne"
    transport.keys = [ancienne, nouvelle]       # l'emetteur publie la nouvelle

    # Dans la fenetre: refusee, et AUCUN appel sortant -- c'est le prix de la borne.
    before = len(transport.calls)
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(nouvelle))
    assert excinfo.value.reason == "key_id_unknown"
    assert len(transport.calls) == before

    # Fenetre passee: reconnue, sans redemarrage et sans attendre l'expiration du cache.
    clock.advance(DEFAULT_MIN_REFRESH_SECONDS + 1)
    assert verifier.verify(token(nouvelle)).key_id == "k-nouvelle"
    assert verifier.verify(token(ancienne)).key_id == "k-ancienne", "l'ancienne vit encore"


# -- acceptation 4: huit requetes concurrentes a froid, UNE recuperation -------------------

def test_eight_concurrent_cold_requests_make_exactly_one_retrieval():
    """AVANT: 8 requetes simultanees -> 8 recuperations. Barriere, pas enchainement."""
    key = Key("k1", "RS256")
    transport, clock = Transport([key], delay=0.05), Clock()
    jwks = source(transport, clock)

    barrier = threading.Barrier(8)
    documents, errors = [], []

    def worker():
        try:
            barrier.wait(timeout=10)
            documents.append(jwks.jwks())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(documents) == 8
    assert transport.key_fetches == 1, f"attendu 1 recuperation, mesure {transport.key_fetches}"
    assert transport.discoveries == 1
    assert all(d is documents[0] for d in documents), "les perdants reutilisent le document"


# -- acceptation 5: echec reseau et JWKS vide ---------------------------------------------

def test_a_network_failure_refuses_safely_and_does_not_loop():
    transport, clock = Transport(fail=True), Clock()
    jwks = source(transport, clock, min_refresh_seconds=30.0)

    for _ in range(10):
        with pytest.raises(TokenRejected) as excinfo:
            jwks.jwks()
        assert excinfo.value.reason == "issuer_unreachable"
    assert len(transport.calls) == 1, "une seule tentative: un echec coute une fenetre"


def test_a_retry_is_possible_after_the_window_and_the_lock_is_never_stuck():
    key = Key("k1", "RS256")
    transport, clock = Transport(fail=True), Clock()
    jwks = source(transport, clock, min_refresh_seconds=30.0)

    with pytest.raises(TokenRejected):
        jwks.jwks()
    clock.advance(31.0)
    transport.fail = False
    transport.keys = [key]
    document = jwks.jwks()
    assert [k["kid"] for k in document["keys"]] == ["k1"]
    assert not jwks._fetch_lock.locked(), "aucun verrou laisse pris apres un echec"
    assert not jwks._state_lock.locked()


def test_an_empty_jwks_is_classified_as_having_no_usable_key():
    """Objectif 3: un document JSON VALIDE sans cle utilisable est une erreur de l'EMETTEUR.

    Le deguiser en `kid` inconnu ferait chercher le probleme du mauvais cote.
    """
    key = Key("k1", "RS256")
    transport, clock = Transport([]), Clock()
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=source(transport, clock))
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(key))
    assert excinfo.value.reason == "jwks_without_usable_key"


def test_a_jwks_with_only_unreadable_keys_has_the_same_classification():
    key = Key("k1", "RS256")
    clock = Clock()

    class Unreadable(Transport):
        def __call__(self, url, *, timeout):
            self.calls.append(url)
            if url.endswith("openid-configuration"):
                return {"jwks_uri": KEYS_URI}
            return {"keys": [{"kty": "inconnu", "kid": "k1"}]}

    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE,
                             source=source(Unreadable(), clock))
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(key))
    assert excinfo.value.reason == "jwks_without_usable_key"


@pytest.mark.parametrize("document, expected", [
    ({}, "jwks_malformed"),
    ({"keys": "pas-une-liste"}, "jwks_malformed"),
])
def test_a_malformed_document_keeps_its_own_classification(document, expected):
    """La FORME fausse reste distincte du CONTENU inutilisable."""
    key = Key("k1", "RS256")
    clock = Clock()

    class Shaped(Transport):
        def __call__(self, url, *, timeout):
            self.calls.append(url)
            return {"jwks_uri": KEYS_URI} if url.endswith("openid-configuration") else document

    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=source(Shaped(), clock))
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(key))
    assert excinfo.value.reason == expected


# -- acceptation 6: cache expire et relecture concurrente ---------------------------------

def test_a_concurrent_refresh_of_an_expired_cache_is_not_redundant():
    key = Key("k1", "RS256")
    transport, clock = Transport([key], delay=0.05), Clock()
    jwks = source(transport, clock, cache_seconds=60.0)

    jwks.jwks()
    assert transport.key_fetches == 1
    clock.advance(61.0)                      # le cache expire

    barrier = threading.Barrier(8)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=10)
            jwks.jwks()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert transport.key_fetches == 2, f"une seule relecture de plus, mesure {transport.key_fetches}"
    assert transport.discoveries == 1


def test_the_warm_path_does_not_serialise_behind_the_retrieval_lock():
    """Une fois le cache rempli, les verifications ne passent PAS par le verrou de
    recuperation. Preuve deterministe: on tient ce verrou et le chemin chaud repond quand meme."""
    key = Key("k1", "RS256")
    transport, clock = Transport([key]), Clock()
    jwks = source(transport, clock, cache_seconds=600.0)
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=jwks)
    verifier.verify(token(key))                      # remplit le cache

    done = threading.Event()

    def warm():
        verifier.verify(token(key))
        done.set()

    with jwks._fetch_lock:                           # verrou de recuperation TENU
        thread = threading.Thread(target=warm)
        thread.start()
        assert done.wait(timeout=5), "le chemin chaud attend le verrou de recuperation"
        thread.join(timeout=5)
    assert transport.key_fetches == 1


# -- validations conservees ----------------------------------------------------------------

def test_a_jwks_uri_outside_the_issuer_host_is_refused():
    clock = Clock()

    class Elsewhere(Transport):
        def __call__(self, url, *, timeout):
            self.calls.append(url)
            return {"jwks_uri": "https://attaquant.invalid/certs"}

    with pytest.raises(TokenRejected) as excinfo:
        source(Elsewhere(), clock).jwks()
    assert excinfo.value.reason == "jwks_uri_outside_issuer"


def test_a_jwks_uri_on_plain_http_outside_localhost_is_refused():
    clock = Clock()

    class Insecure(Transport):
        def __call__(self, url, *, timeout):
            self.calls.append(url)
            return {"jwks_uri": "http://idp.test.invalid/certs"}

    with pytest.raises(TokenRejected) as excinfo:
        HttpJwksSource("http://idp.test.invalid", clock=clock, fetch=Insecure()).jwks()
    assert excinfo.value.reason == "insecure_discovery_url"


def test_a_discovery_document_without_a_jwks_uri_is_refused():
    clock = Clock()

    class Empty(Transport):
        def __call__(self, url, *, timeout):
            self.calls.append(url)
            return {}

    with pytest.raises(TokenRejected) as excinfo:
        source(Empty(), clock).jwks()
    assert excinfo.value.reason == "discovery_without_jwks_uri"
