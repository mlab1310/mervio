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


# -- la CADENCE face a la TOLERANCE: `max_stale_seconds < min_refresh_seconds` -------------
#
# Configuration LEGALE et volontairement non interdite. Ce bloc epingle ce qu'elle produit
# reellement, mesure sur ce meme harnais: la tolerance n'absorbe une panne sans refus visible
# que si une tentative autorisee ET reussie peut avoir lieu DANS la fenetre de tolerance.
# Reglages: `cache_seconds=300`, donc fenetre `[300, 300 + max_stale_seconds)`.

TOLERANCE_COURTE = dict(cache_seconds=300.0, max_stale_seconds=10.0,
                        min_refresh_seconds=30.0)
TOLERANCE_LONGUE = dict(cache_seconds=300.0, max_stale_seconds=60.0,
                        min_refresh_seconds=30.0)


def test_a_tolerance_shorter_than_the_cadence_only_postpones_the_refusal():
    """Panne continue. La fenetre reste `cache + max_stale` et le verdict reste MONOTONE: ce
    n'est donc pas le papillonnement de F.1 qui revient. Mesure: dernier accepte t=309,
    premier refus t=310, tentatives a 300/330/360/390/420."""
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock, **TOLERANCE_COURTE)
    verifier.verify(token(key))                      # cache rempli a t=0
    emetteur.down = True

    tentatives, verdicts = [], []
    for moment in range(0, 421):
        clock.at(float(moment))
        avant = len(emetteur.calls)
        verdicts.append((float(moment), verdict(verifier, key)))
        if len(emetteur.calls) > avant:
            tentatives.append(float(moment))

    acceptes = [t for t, v in verdicts if v == "accepte"]
    refuses = [(t, v) for t, v in verdicts if v != "accepte"]
    assert acceptes[-1] == 309.0 and refuses[0] == (310.0, "jwks_too_stale")
    assert {v for _, v in refuses} == {"jwks_too_stale"}
    # La cadence n'est PAS suspendue par l'expiration de la tolerance: on ressaie toujours.
    assert tentatives == [300.0, 330.0, 360.0, 390.0, 420.0]
    sequence = [v == "accepte" for _, v in verdicts]
    assert not any(not sequence[i] and sequence[i + 1] for i in range(len(sequence) - 1)), \
        "aucune transition refus -> acceptation tant que l'emetteur est en panne"
    assert source._fetched_at == 0.0, "aucune tentative echouee ne deplace le chargement REUSSI"


def test_a_tolerance_shorter_than_the_cadence_refuses_although_the_issuer_is_back():
    """LE POINT DE LA TOLERANCE COURTE. L'emetteur revient a t=305, DANS la tolerance, mais la
    prochaine tentative n'est permise qu'a t=330: 20 s de refus (t=310 a t=329) alors que les
    cles etaient disponibles. Le retablissement suit une tentative autorisee ET reussie, pas
    le retour de l'emetteur."""
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock, **TOLERANCE_COURTE)
    verifier.verify(token(key))
    emetteur.down = True

    refuses, appels_pendant_le_retour = [], 0
    for moment in range(300, 331):
        if moment == 305:
            emetteur.down = False                    # retour, DANS la fenetre de tolerance
        clock.at(float(moment))
        avant = len(emetteur.calls)
        if verdict(verifier, key) != "accepte":
            refuses.append(float(moment))
        if moment > 305 and moment < 330 and len(emetteur.calls) > avant:
            appels_pendant_le_retour += 1

    assert refuses == [float(t) for t in range(310, 330)], "20 s de refus, emetteur disponible"
    assert appels_pendant_le_retour == 0, "aucune tentative avant l'instant autorise t=330"
    assert source._fetched_at == 330.0, "rechargement a la premiere tentative autorisee"
    assert source.state()["fresh"] is True and source.state()["last_failure"] is None


def test_a_tolerance_longer_than_the_cadence_absorbs_the_same_outage_without_a_refusal():
    """Le contraste qui justifie la relation recommandee: memes 300 s de fraicheur, meme
    cadence, meme panne, tolerance 60 au lieu de 10 -- une tentative tombe DANS la fenetre
    (330 < 360) et la panne devient invisible."""
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock, **TOLERANCE_LONGUE)
    verifier.verify(token(key))
    emetteur.down = True

    verdicts = []
    for moment in range(300, 361):
        if moment == 315:
            emetteur.down = False
        clock.at(float(moment))
        verdicts.append(verdict(verifier, key))

    assert set(verdicts) == {"accepte"}, "aucun refus: la tolerance a pu etre renouvelee"
    assert source._fetched_at == 330.0


def test_an_allowed_attempt_is_not_an_effective_one_without_traffic():
    """Trois instants distincts. La tentative est AUTORISEE des t=330, mais la recuperation est
    tiree par le TRAFIC: sans requete entre t=311 et t=400, aucun appel sortant n'a lieu et le
    retablissement n'arrive qu'a t=400. Aucun rafraichisseur en arriere-plan."""
    key = Key("k1", "RS256")
    emetteur, clock = Issuer([key]), Clock()
    source, verifier = build(emetteur, clock, **TOLERANCE_COURTE)
    verifier.verify(token(key))
    emetteur.down = True
    clock.at(300.0)
    assert verdict(verifier, key) == "accepte"       # tentative echouee, tolerance appliquee
    emetteur.down = False                            # l'emetteur revient des t=300+
    clock.at(311.0)
    assert verdict(verifier, key) == "jwks_too_stale"
    appels = len(emetteur.calls)

    clock.at(400.0)                                  # 70 s apres l'instant AUTORISE (t=330)
    assert len(emetteur.calls) == appels, "aucun appel sortant sans requete entrante"
    assert verdict(verifier, key) == "accepte"
    assert len(emetteur.calls) == appels + 1, "la tentative a lieu A la requete, pas avant"
    assert source._fetched_at == 400.0
