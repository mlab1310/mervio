"""Politique de cache PERIME: coherente, bornee, et sans papillonnement (F.2).

LE DEFAUT CORRIGE
F.1 servait le document rassis UNIQUEMENT dans la fenetre de refroidissement et levait dans la
branche de tentative: le verdict dependait donc de l'instant d'arrivee de la requete. Mesure
sur `6085de3`, `cache_seconds=60`, `min_refresh_seconds=30`, emetteur en panne, 16 instants de
t=60 a t=210 par pas de 10: **10 acceptations et 6 refus, en ALTERNANCE**.

F.2 fait prendre la MEME decision aux deux branches (`_tolerated`), donc le verdict ne depend
plus que de l'AGE du dernier chargement REUSSI. Ce module le prouve, et il reproduit le
scenario exact ci-dessus.

TROIS DUREES, TROIS ROLES
    cache_seconds        fraicheur, depuis le dernier chargement REUSSI
    max_stale_seconds    tolerance EN PLUS, pendant laquelle un cache valide sert encore
    min_refresh_seconds  cadence des tentatives reseau -- n'accepte jamais une cle a elle seule
"""
from __future__ import annotations

import threading

import pytest

from mervio.api.auth import HttpJwksSource, TokenRejected, TokenVerifier

from .jwt_support import AUDIENCE, ISSUER, Key, token

KEYS_URI = "https://idp.test.invalid/realms/mervio/protocol/openid-connect/certs"


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def at(self, moment: float) -> None:
        self.now = moment

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Issuer:
    """Emetteur injecte: compte les appels, et peut tomber ou revenir a la demande."""

    def __init__(self, keys, *, down: bool = False, delay: float = 0.0) -> None:
        self.keys = list(keys)
        self.down = down
        self.delay = delay
        self.calls: list = []
        self._lock = threading.Lock()

    def __call__(self, url: str, *, timeout: float):
        with self._lock:
            self.calls.append(url)
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.down:
            raise TokenRejected("issuer_unreachable")
        if url.endswith("openid-configuration"):
            return {"jwks_uri": KEYS_URI}
        return {"keys": [k.jwk() for k in self.keys]}

    @property
    def key_fetches(self) -> int:
        return sum(1 for c in self.calls if not c.endswith("openid-configuration"))


def build(issuer, clock, **kwargs):
    defaults = dict(cache_seconds=60.0, min_refresh_seconds=30.0, max_stale_seconds=60.0)
    defaults.update(kwargs)
    source = HttpJwksSource(ISSUER, clock=clock, fetch=issuer, **defaults)
    return source, TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=source)


def verdict(verifier, key):
    try:
        verifier.verify(token(key))
        return "accepte"
    except TokenRejected as exc:
        return exc.reason


# -- le scenario EXACT de F.1, reproduit ---------------------------------------------------

def test_the_exact_flapping_scenario_of_f1_is_now_monotone():
    """16 instants, memes reglages, meme panne. AVANT: 10 acceptes / 6 refuses en alternance."""
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(key))                      # cache rempli a t=0
    emetteur.down = True

    verdicts = []
    for moment in range(60, 211, 10):
        clock.at(float(moment))
        verdicts.append(verdict(verifier, key))

    accepted = [v == "accepte" for v in verdicts]
    assert sum(accepted) == 6 and len(accepted) - sum(accepted) == 10
    # La propriete qui compte: MONOTONE. Aucun refus suivi d'une acceptation.
    assert not any(accepted[i] is False and accepted[i + 1] is True
                   for i in range(len(accepted) - 1)), verdicts
    # Et la fenetre est bien `cache_seconds + max_stale_seconds` = 120 s.
    assert verdicts[:6] == ["accepte"] * 6
    assert set(verdicts[6:]) == {"jwks_too_stale"}


def test_the_verdict_is_stable_at_every_instant_of_the_tolerance_window():
    """Pas d'echantillonnage grossier: chaque seconde de la fenetre rend le meme verdict."""
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(key))
    emetteur.down = True

    for moment in range(60, 120):
        clock.at(float(moment))
        assert verdict(verifier, key) == "accepte", moment
    for moment in range(120, 180):
        clock.at(float(moment))
        assert verdict(verifier, key) == "jwks_too_stale", moment


# -- les instants de reference -------------------------------------------------------------

def test_a_failed_attempt_never_postpones_the_expiry_of_the_keys():
    """Sinon une panne prolongee repousserait indefiniment l'expiration -- exactement ce
    qu'une revocation doit empecher."""
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(key))
    loaded_at = source._fetched_at
    emetteur.down = True

    for moment in (60, 90, 110):                     # plusieurs tentatives echouees
        clock.at(float(moment))
        verdict(verifier, key)
    assert source._fetched_at == loaded_at, "l'horodatage du dernier chargement REUSSI ne bouge pas"
    clock.at(120.0)
    assert verdict(verifier, key) == "jwks_too_stale"


def test_a_successful_refresh_resets_the_freshness_timestamp():
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(key))
    clock.at(61.0)
    verifier.verify(token(key))                      # cache expire, relecture REUSSIE
    assert source._fetched_at == 61.0
    assert source.state()["fresh"] is True
    clock.at(100.0)
    emetteur.down = True
    assert verdict(verifier, key) == "accepte", "frais depuis 61, donc encore frais a 100"


def test_zero_tolerance_refuses_as_soon_as_the_cache_expires():
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock, max_stale_seconds=0.0)
    verifier.verify(token(key))
    emetteur.down = True
    clock.at(59.0)
    assert verdict(verifier, key) == "accepte"
    clock.at(60.0)
    assert verdict(verifier, key) == "jwks_too_stale"


# -- cache initialement vide ---------------------------------------------------------------

def test_an_empty_cache_never_accepts_a_token_when_the_issuer_is_down():
    """Aucune tolerance ne s'applique a un cache qui n'a jamais ete valide."""
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key], down=True), Clock()
    source, verifier = build(emetteur, clock)
    for moment in (0, 10, 40, 100, 1000):
        clock.at(float(moment))
        assert verdict(verifier, key) == "issuer_unreachable", moment
    assert source.state()["has_keys"] is False


# -- `kid` connu contre `kid` inconnu pendant une panne ------------------------------------

def test_during_an_outage_a_known_key_is_tolerated_and_an_unknown_one_is_not():
    """La tolerance porte sur la DISPONIBILITE de cles deja validees, pas sur leur contenu."""
    connue, inconnue = Key("k-connue", "RS256"), Key("k-absente", "RS256")
    emetteur, clock = Issuer([connue]), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(connue))
    emetteur.down = True
    clock.at(70.0)                                   # dans la tolerance

    assert verdict(verifier, connue) == "accepte"
    assert verdict(verifier, inconnue) == "key_id_unknown", \
        "un cache qui ne contient pas la cle ne peut rien tolerer"


# -- la tolerance ne contourne AUCUNE verification -----------------------------------------

@pytest.mark.parametrize("name, build_token", [
    ("signature invalide", lambda k: token(Key(k.key_id, "RS256"))),
    ("mauvais iss", lambda k: token(k, issuer="https://autre.invalid/r")),
    ("mauvaise aud", lambda k: token(k, audience="une-autre-api")),
    ("expire", lambda k: token(k, now=-7200, lifetime=60)),
    ("claim absent", lambda k: token(k, drop=("sub",))),
])
def test_a_stale_cache_never_turns_an_invalid_token_into_a_success(name, build_token):
    """DEUX horloges, et il ne faut pas les confondre: celle injectee gouverne le
    VIEILLISSEMENT DU CACHE, pas les revendications temporelles du jeton, que `pyjwt` valide
    sur l'heure reelle. Les jetons sont donc forges avec l'heure reelle."""
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(key))
    emetteur.down = True
    clock.at(70.0)                                   # cache perime, dans la tolerance

    assert verdict(verifier, key) == "accepte", "le cache rassis sert bien un jeton VALIDE"
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(build_token(key))
    # Le refus doit venir de la VERIFICATION, pas de l'etat du cache: sinon on ne saurait pas
    # si c'est la tolerance qui a sauve le jeton ou le controle qui l'a refuse.
    assert excinfo.value.reason in ("signature_invalid", "issuer_invalid", "audience_invalid",
                                    "token_expired", "claim_absent", "subject_invalid"), \
        f"{name}: refus attendu par la verification, obtenu {excinfo.value.reason}"


# -- concurrence ---------------------------------------------------------------------------

def _concurrently(callable_, *, threads: int = 8):
    barrier = threading.Barrier(threads)
    outcomes, errors = [], []

    def worker():
        try:
            barrier.wait(timeout=10)
            outcomes.append(callable_())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)
    assert not errors, errors
    return outcomes


def test_a_concurrent_failure_gives_every_request_the_same_verdict():
    """L'invariant de F.2: la requete qui DECLENCHE l'echec recoit le meme verdict que les
    suivantes. C'etait exactement le defaut de F.1."""
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key], delay=0.05), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(key))
    emetteur.down = True
    clock.at(70.0)                                   # perime, dans la tolerance
    before = len(emetteur.calls)

    outcomes = _concurrently(lambda: verdict(verifier, key))
    assert set(outcomes) == {"accepte"}, outcomes
    assert len(emetteur.calls) - before == 1, "single-flight conserve: UNE tentative pour 8 fils"


def test_a_concurrent_failure_beyond_the_tolerance_refuses_everyone_identically():
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key], delay=0.05), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(key))
    emetteur.down = True
    clock.at(200.0)                                  # au-dela de la tolerance

    outcomes = _concurrently(lambda: verdict(verifier, key))
    assert set(outcomes) == {"jwks_too_stale"}, outcomes


def test_concurrent_cold_start_against_a_down_issuer_refuses_everyone():
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key], down=True, delay=0.05), Clock()
    source, verifier = build(emetteur, clock)
    outcomes = _concurrently(lambda: verdict(verifier, key))
    assert set(outcomes) == {"issuer_unreachable"}, outcomes
    assert len(emetteur.calls) == 1, "une seule tentative malgre 8 fils"
    assert not source._fetch_lock.locked() and not source._state_lock.locked()


# -- reprise de l'emetteur -----------------------------------------------------------------

def test_the_issuer_coming_back_restores_a_fresh_cache():
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(key))
    emetteur.down = True
    clock.at(200.0)
    assert verdict(verifier, key) == "jwks_too_stale"

    emetteur.down = False
    clock.at(240.0)                                  # au-dela du refroidissement
    assert verdict(verifier, key) == "accepte"
    etat = source.state()
    assert etat["fresh"] is True and etat["last_failure"] is None
    assert not source._fetch_lock.locked()


def test_a_successful_recovery_drops_a_retired_key():
    ancienne, nouvelle = Key("k-ancienne", "RS256"), Key("k-nouvelle", "RS256")
    emetteur, clock = Issuer([ancienne]), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(ancienne))

    emetteur.keys = [nouvelle]                       # l'ancienne est RETIREE
    clock.at(61.0)
    assert verdict(verifier, nouvelle) == "accepte"
    assert verdict(verifier, ancienne) == "key_id_unknown", "la cle retiree ne sert plus"


# -- la panne n'est pas MASQUEE ------------------------------------------------------------

def test_a_tolerated_outage_is_still_named_in_the_state():
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock)
    verifier.verify(token(key))
    emetteur.down = True
    clock.at(70.0)
    assert verdict(verifier, key) == "accepte"

    etat = source.state()
    assert etat["last_failure"] == "issuer_unreachable", "servie, mais NOMMEE"
    assert etat["fresh"] is False
    assert etat["age_seconds"] == 70.0
    assert "key" not in str(etat).lower() or "has_keys" in etat   # aucun secret


def test_the_state_carries_the_three_durations_and_no_secret():
    key = Key("k1", "RS256")
    source, verifier = build(Issuer([key]), Clock())
    verifier.verify(token(key))
    etat = source.state()
    assert etat["cache_seconds"] == 60.0
    assert etat["max_stale_seconds"] == 60.0
    assert etat["min_refresh_seconds"] == 30.0
    assert set(etat) == {"has_keys", "age_seconds", "fresh", "cache_seconds",
                         "max_stale_seconds", "min_refresh_seconds", "last_failure"}


# -- la tolerance ne couvre QUE la disponibilite -------------------------------------------

@pytest.mark.parametrize("document, expected", [
    ({"jwks_uri": "https://attaquant.invalid/certs"}, "jwks_uri_outside_issuer"),
    ({}, "discovery_without_jwks_uri"),
])
def test_an_integrity_failure_is_never_tolerated_even_with_a_valid_cache(document, expected):
    """Un document de decouverte altere n'est PAS un hoquet.

    Le tolerer ferait chercher une panne reseau devant un probleme de confiance -- et un
    `jwks_uri` qui change d'hote peut aussi etre une migration legitime, qui demande un humain.
    Compromis assume: refus de service plutot que degradation silencieuse.
    """
    key = Key("k1", "RS256")
    clock = Clock()
    phase = {"tampered": False}

    def emetteur(url, *, timeout):
        if url.endswith("openid-configuration"):
            return document if phase["tampered"] else {"jwks_uri": KEYS_URI}
        return {"keys": [key.jwk()]}

    source = HttpJwksSource(ISSUER, clock=clock, fetch=emetteur, cache_seconds=60.0,
                            min_refresh_seconds=30.0, max_stale_seconds=60.0)
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=source)
    verifier.verify(token(key))                      # cache VALIDE rempli

    phase["tampered"] = True
    source._jwks_uri = None                          # force une nouvelle decouverte
    clock.at(61.0)                                   # cache perime, DANS la tolerance
    assert verdict(verifier, key) == expected, "l'echec d'integrite remonte tel quel"
    assert source.state()["last_failure"] == expected


def test_an_availability_failure_is_tolerated_and_an_integrity_one_is_not():
    """Les deux cotes de la frontiere, dans un seul test, pour que la regle soit lisible."""
    from mervio.api.auth import TOLERABLE_FAILURES
    assert "issuer_unreachable" in TOLERABLE_FAILURES
    assert "jwks_uri_outside_issuer" not in TOLERABLE_FAILURES
    assert "insecure_discovery_url" not in TOLERABLE_FAILURES
    assert "discovery_without_jwks_uri" not in TOLERABLE_FAILURES
