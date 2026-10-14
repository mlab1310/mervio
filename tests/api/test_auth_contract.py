"""Le contrat de jeton de D-098a, controle par controle (Mission 004.6, tranche F).

UNE SIGNATURE VALIDE NE SUFFIT PAS. Elle prouve l'origine des octets, et rien d'autre: ni
l'emetteur attendu, ni le destinataire, ni la fenetre temporelle. Chaque regle a donc son
propre test et son propre code de refus, de sorte qu'un echec DISE lequel des controles a cede.

AUCUN RESEAU, AUCUN FOURNISSEUR. Les cles sont generees a l'execution et le JWKS vit en
memoire (`jwt_support`). Rien ici ne nomme Keycloak.
"""
from __future__ import annotations

import time

import pytest

from mervio.api.auth import (ALLOWED_ALGORITHMS, REQUIRED_CLAIMS, MAX_KEYS, TokenRejected,
                             TokenVerifier)

from .jwt_support import (AUDIENCE, ISSUER, Key, MemoryJwks, RawJwks, hmac_token, token,
                          unsigned_token)


@pytest.fixture
def rsa_key():
    return Key("k-rsa", "RS256")


@pytest.fixture
def source(rsa_key):
    return MemoryJwks([rsa_key])


@pytest.fixture
def verifier(source):
    return TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=source)


def reason(excinfo) -> str:
    return excinfo.value.reason


# -- 0. ce qui est accepte ----------------------------------------------------------------

@pytest.mark.parametrize("algorithm", ALLOWED_ALGORITHMS)
def test_a_well_formed_token_of_an_allowed_algorithm_is_accepted(algorithm):
    key = Key("k1", algorithm)
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=MemoryJwks([key]))
    verified = verifier.verify(token(key, subject="idp|alice"))
    assert (verified.subject, verified.issuer, verified.key_id) == ("idp|alice", ISSUER, "k1")
    assert verified.expires_at > int(time.time())


def test_the_verified_token_does_not_carry_the_token_itself(verifier, rsa_key):
    """Un objet qui transporterait le jeton finirait par apparaitre dans une trace."""
    raw = token(rsa_key)
    verified = verifier.verify(raw)
    assert raw not in repr(verified)
    assert not any(raw in str(v) for v in vars(verified).values())


# -- 1. forme du jeton --------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("", "token_absent"), ("   ", "token_absent"), (None, "token_absent"),
    ("pas-un-jeton", "token_malformed"), ("a.b", "token_malformed"),
    ("a.b.c.d", "token_malformed"),
])
def test_a_malformed_token_is_refused_before_any_cryptography(verifier, value, expected):
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(value)
    assert reason(excinfo) == expected


# -- 2. algorithmes ------------------------------------------------------------------------

def test_the_none_algorithm_is_refused(verifier):
    """`alg: none` n'est pas une signature. Le refus precede toute verification."""
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(unsigned_token())
    assert reason(excinfo) == "algorithm_forbidden"


def test_an_hmac_algorithm_is_refused(verifier):
    """Une cle symetrique partagee avec l'emetteur n'est pas une signature verifiable."""
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(hmac_token())
    assert reason(excinfo) == "algorithm_forbidden"


def test_the_allowed_algorithms_are_a_constant_not_a_setting():
    """Une installation ne doit pas pouvoir s'ouvrir a `none` par une variable."""
    assert ALLOWED_ALGORITHMS == ("RS256", "ES256")
    import inspect

    import mervio.api.auth as module
    assert "ALLOWED_ALGORITHMS" not in inspect.signature(module.TokenVerifier.__init__).parameters


@pytest.mark.parametrize("declared", ["RS512", "PS256"])
def test_a_key_declared_for_another_algorithm_of_the_same_family_is_refused(rsa_key, declared):
    """Sans ce controle, une cle destinee a `RS512` validerait un jeton `RS256`.

    Mesure a l'appui: `pyjwt` ACCEPTE un JWK RSA declarant `RS512` ou `PS256` -- c'est la meme
    famille de cle. Le controle d'egalite entre l'`alg` ANNONCE par le jeton et celui declare
    par la cle est donc atteignable, et il gagne sa place.
    """
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE,
                             source=RawJwks({"keys": [rsa_key.jwk(declare_alg=declared)]}))
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key))
    assert reason(excinfo) == "algorithm_key_mismatch"


def test_a_cross_family_key_declaration_is_refused_even_earlier(rsa_key):
    """Et le cas croise famille (cle RSA declarant `ES256`): `pyjwt` le refuse DES l'analyse
    du JWK, donc avant notre controle d'egalite.

    Depuis F.1, ce refus est classe `jwks_without_usable_key` et non `jwks_malformed`: la
    FORME du document est bonne, c'est son CONTENU qui est illisible. Le refus arrive plus
    tot, pas moins fort, et il nomme desormais la bonne cause.
    """
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE,
                             source=RawJwks({"keys": [rsa_key.jwk(declare_alg="ES256")]}))
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key))
    assert reason(excinfo) == "jwks_without_usable_key"


def test_a_key_without_a_declared_algorithm_is_accepted(rsa_key):
    """Un JWK sans `alg` est licite: le controle d'egalite ne doit pas inventer un refus."""
    jwk = {k: v for k, v in rsa_key.jwk().items() if k != "alg"}
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=RawJwks({"keys": [jwk]}))
    assert verifier.verify(token(rsa_key)).key_id == "k-rsa"


# -- 3. signature --------------------------------------------------------------------------

def test_a_token_signed_by_another_key_is_refused(rsa_key):
    """Meme `kid`, autre cle: la signature ne doit pas passer."""
    impostor = Key("k-rsa", "RS256")
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=MemoryJwks([rsa_key]))
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(impostor))
    assert reason(excinfo) == "signature_invalid"


def test_a_tampered_payload_is_refused(verifier, rsa_key):
    raw = token(rsa_key)
    header, claims, signature = raw.split(".")
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(f"{header}.{claims[:-4]}aaaa.{signature}")
    assert reason(excinfo) in ("signature_invalid", "token_malformed", "token_invalid")


# -- 4. kid et rotation --------------------------------------------------------------------

def test_a_token_without_a_key_id_is_refused(verifier, rsa_key):
    """Sans `kid`, choisir une cle serait deviner -- et l'attaquant choisirait pour nous."""
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, key_id=None))
    assert reason(excinfo) == "key_id_absent"


def test_an_unknown_key_id_is_refused_after_exactly_one_refresh(verifier, source, rsa_key):
    """UNE relecture, puis refus: sinon un flot de `kid` inventes amplifie vers l'emetteur."""
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, key_id="k-inconnu"))
    assert reason(excinfo) == "key_id_unknown"
    assert source.refreshes == 1


def test_a_rotated_key_is_picked_up_without_a_restart(source):
    """Rotation: une cle nouvelle doit etre vue a la premiere relecture."""
    rotated = Key("k-nouvelle", "RS256")
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=source)
    source.set_keys([rotated])
    assert verifier.verify(token(rotated)).key_id == "k-nouvelle"
    assert source.refreshes == 1, "la cle nouvelle est trouvee par UNE relecture"


def test_a_retired_key_keeps_working_until_the_cache_is_refreshed(source, rsa_key):
    """Exposition BORNEE, enoncee plutot que niee.

    Un cache JWKS implique qu'une cle retiree par l'emetteur reste acceptee jusqu'a la
    prochaine relecture -- au plus `jwks_cache_seconds`. Ce test fixe ce comportement dans les
    deux sens pour qu'un changement ne passe pas inapercu: la cle retiree fonctionne encore
    pendant la fenetre, et plus du tout apres la relecture. Reduire la fenetre est un reglage
    (`MERVIO_OIDC_JWKS_CACHE_SECONDS`); la supprimer demanderait une relecture par requete,
    donc un aller-retour vers l'emetteur a chaque appel.
    """
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=source)
    assert verifier.verify(token(rsa_key)).key_id == "k-rsa"

    source.set_keys([Key("k-nouvelle", "RS256")])
    assert verifier.verify(token(rsa_key)).key_id == "k-rsa", "pendant la fenetre de cache"
    assert source.refreshes == 0, "une cle encore en cache ne declenche aucune relecture"

    source.refresh()
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key))
    assert reason(excinfo) == "key_id_unknown"


@pytest.mark.parametrize("document, expected", [
    # FORME fausse: `keys` absent, ou pas une liste.
    ({}, "jwks_malformed"),
    ({"keys": "pas-une-liste"}, "jwks_malformed"),
    # Forme bonne, CONTENU inutilisable (F.1, objectif 3): c'est une erreur de l'emetteur, et
    # elle ne doit pas etre deguisee en `kid` inconnu ni confondue avec un document mal forme.
    ({"keys": []}, "jwks_without_usable_key"),
    ({"keys": [{"kty": "inconnu"}]}, "jwks_without_usable_key"),
])
def test_a_jwks_that_cannot_serve_a_key_is_a_refusal_not_a_crash(document, expected):
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=RawJwks(document))
    key = Key("k1", "RS256")
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(key))
    assert reason(excinfo) == expected


def test_an_oversized_jwks_is_refused():
    """Un emetteur compromis ne doit pas faire grandir notre memoire sans limite."""
    keys = [Key(f"k{i}", "RS256").jwk() for i in range(MAX_KEYS + 1)]
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=RawJwks({"keys": keys}))
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(Key("k0", "RS256")))
    assert reason(excinfo) == "jwks_too_many_keys"


# -- 5. emetteur et audience ---------------------------------------------------------------

def test_a_token_from_another_issuer_is_refused(verifier, rsa_key):
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, issuer="https://autre.test.invalid/realms/mervio"))
    assert reason(excinfo) == "issuer_invalid"


def test_a_token_for_another_audience_is_refused(verifier, rsa_key):
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, audience="une-autre-api"))
    assert reason(excinfo) == "audience_invalid"


# -- 6. fenetre temporelle -----------------------------------------------------------------

def test_an_expired_token_is_refused(verifier, rsa_key):
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, now=int(time.time()) - 3600, lifetime=60))
    assert reason(excinfo) == "token_expired"


def test_a_token_not_yet_valid_is_refused(verifier, rsa_key):
    moment = int(time.time())
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, not_before=moment + 600))
    assert reason(excinfo) == "token_not_yet_valid"


def test_a_token_issued_in_the_future_is_refused(verifier, rsa_key):
    moment = int(time.time())
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, issued_at=moment + 600, not_before=moment + 600))
    assert reason(excinfo) in ("token_not_yet_valid", "token_invalid")


def test_the_declared_clock_skew_is_honoured(rsa_key, source):
    """La derive est DECLAREE, jamais implicite: sans tolerance, ce jeton est refuse."""
    moment = int(time.time())
    strict = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=source)
    tolerant = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, source=source, leeway_seconds=120)
    raw = token(rsa_key, now=moment - 130, lifetime=60)
    with pytest.raises(TokenRejected):
        strict.verify(raw)
    assert tolerant.verify(raw).subject == "idp|alice"


# -- 7. revendications obligatoires --------------------------------------------------------

@pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
def test_every_required_claim_is_required(verifier, rsa_key, claim):
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, drop=(claim,)))
    assert reason(excinfo) in ("claim_absent", "issuer_invalid", "audience_invalid",
                               "subject_invalid")


# -- 8. le sujet ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject", ["", "   "])
def test_an_empty_subject_is_refused(verifier, rsa_key, subject):
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, subject=subject))
    assert reason(excinfo) in ("claim_absent", "subject_invalid")


@pytest.mark.parametrize("subject", ["service:mervio_worker", "service:quelconque"])
def test_a_service_subject_is_refused(verifier, rsa_key, subject):
    """Un principal de service ne s'authentifie JAMAIS par HTTP: ce serait une escalade vers
    l'identite du worker depuis l'exterieur."""
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, subject=subject))
    assert reason(excinfo) == "subject_reserved"


def test_an_oversized_subject_is_refused_before_touching_the_database(verifier, rsa_key):
    """Meme borne que `users.idp_subject`: le refus doit arriver avant d'y toucher."""
    with pytest.raises(TokenRejected) as excinfo:
        verifier.verify(token(rsa_key, subject="x" * 256))
    assert reason(excinfo) == "subject_too_long"


# -- 9. le verificateur refuse une configuration incomplete --------------------------------

@pytest.mark.parametrize("issuer, audience", [("", AUDIENCE), (ISSUER, ""), ("", "")])
def test_the_verifier_refuses_to_exist_without_an_issuer_and_an_audience(source, issuer, audience):
    with pytest.raises(ValueError):
        TokenVerifier(issuer=issuer, audience=audience, source=source)
