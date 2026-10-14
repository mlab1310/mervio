"""`ApiSettings`: ce que lit `mervio api` (Mission 004.6, tranche F).

Aucun nom de fournisseur n'est code en dur: l'emetteur et l'audience sont des reglages, et
Keycloak -- defaut ratifie par D-098b -- n'apparait nulle part dans `src/`.
"""
from __future__ import annotations

import pytest

from mervio.settings import ApiSettings, SettingsError

BASE = {"MERVIO_DATABASE_URL": "postgresql://mervio_app:x@127.0.0.1:5432/mervio",
        "MERVIO_OIDC_ISSUER": "https://idp.example/realms/mervio",
        "MERVIO_OIDC_AUDIENCE": "mervio-api"}


def test_the_minimal_configuration_is_accepted():
    settings = ApiSettings.from_env(BASE)
    assert settings.oidc_issuer == "https://idp.example/realms/mervio"
    assert settings.oidc_audience == "mervio-api"
    assert settings.jwks_cache_seconds == 300.0
    assert settings.clock_skew_seconds == 0.0, "la derive est nulle par defaut, jamais implicite"


def test_a_trailing_slash_on_the_issuer_is_normalised():
    assert ApiSettings.from_env({**BASE, "MERVIO_OIDC_ISSUER": "https://idp.example/realms/m/"}
                                ).oidc_issuer == "https://idp.example/realms/m"


@pytest.mark.parametrize("missing", ["MERVIO_OIDC_ISSUER", "MERVIO_OIDC_AUDIENCE"])
def test_the_issuer_and_the_audience_are_mandatory(missing):
    environ = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(SettingsError):
        ApiSettings.from_env(environ)


def test_a_plain_http_issuer_is_refused_outside_localhost():
    """Un emetteur joint sans TLS peut etre remplace, et le JWKS avec lui."""
    with pytest.raises(SettingsError):
        ApiSettings.from_env({**BASE, "MERVIO_OIDC_ISSUER": "http://idp.example/realms/m"})


@pytest.mark.parametrize("issuer", ["http://localhost:8080/realms/m",
                                    "http://127.0.0.1:8080/realms/m"])
def test_plain_http_is_tolerated_on_localhost_only(issuer):
    assert ApiSettings.from_env({**BASE, "MERVIO_OIDC_ISSUER": issuer}).oidc_issuer == issuer


def test_the_jwks_cache_has_a_floor():
    """Un cache a zero demanderait une relecture par requete, donc un aller-retour vers
    l'emetteur a chaque appel."""
    with pytest.raises(SettingsError):
        ApiSettings.from_env({**BASE, "MERVIO_OIDC_JWKS_CACHE_SECONDS": "0"})


def test_the_clock_skew_is_bounded():
    with pytest.raises(SettingsError):
        ApiSettings.from_env({**BASE, "MERVIO_OIDC_CLOCK_SKEW_SECONDS": "3600"})


def test_the_pool_bounds_must_be_coherent():
    with pytest.raises(SettingsError):
        ApiSettings.from_env({**BASE, "MERVIO_API_POOL_MIN": "20", "MERVIO_API_POOL_MAX": "4"})


def test_the_algorithms_are_not_part_of_the_settings():
    """Une installation ne doit pas pouvoir s'ouvrir a `none` par une variable."""
    assert not any("ALGORITHM" in name for name in vars(ApiSettings.from_env(BASE)))
    assert not hasattr(ApiSettings.from_env(BASE), "algorithms")


def test_no_provider_name_appears_in_the_logic_of_the_source():
    """D-098b: le fournisseur est une configuration de DEPLOIEMENT, pas du code.

    Le controle porte sur la LOGIQUE, pas sur la documentation: un commentaire qui explique
    quel fournisseur est le defaut ratifie est utile, et l'effacer appauvrirait le code sans
    rien rendre plus sur. L'AST est donc reconstruit SANS docstrings -- `ast.unparse` n'emet
    par ailleurs aucun commentaire -- et c'est ce code-la qui est inspecte.
    """
    import ast
    import pathlib
    import re

    # Frontieres de MOT, et non sous-chaines: `entra` apparait dans des mots francais
    # (`concentration`, `rentrant`) et un jeton court produirait un faux positif.
    vendors = ("keycloak", "auth0", "okta", "zitadel", "cognito", "entra")
    pattern = re.compile(r"\b(" + "|".join(vendors) + r")\b")
    root = pathlib.Path(__file__).resolve().parent.parent / "src"
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                continue
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
        logic = ast.unparse(ast.fix_missing_locations(tree)).lower()
        found = pattern.search(logic)
        assert found is None, f"{path.relative_to(root)} nomme {found.group(1)} dans sa logique"


def test_the_minimum_refresh_interval_has_a_default_and_bounds():
    """F.1: l'intervalle borne le flot de `kid` inventes -- 42 appels sortants pour 20
    requetes avant correctif. Defaut 30 s, bornes [1, 3600]."""
    assert ApiSettings.from_env(BASE).min_refresh_seconds == 30.0
    assert ApiSettings.from_env({**BASE, "MERVIO_OIDC_MIN_REFRESH_SECONDS": "5"}
                                ).min_refresh_seconds == 5.0
    for invalid in ("0", "0.5", "7200"):
        with pytest.raises(SettingsError):
            ApiSettings.from_env({**BASE, "MERVIO_OIDC_MIN_REFRESH_SECONDS": invalid})


def test_the_refresh_interval_cannot_exceed_the_cache_lifetime():
    """Sinon la relecture par expiration serait inatteignable: le cache expirerait sans
    pouvoir etre renouvele."""
    with pytest.raises(SettingsError):
        ApiSettings.from_env({**BASE, "MERVIO_OIDC_JWKS_CACHE_SECONDS": "30",
                              "MERVIO_OIDC_MIN_REFRESH_SECONDS": "60"})


def test_the_stale_tolerance_has_a_default_and_bounds():
    """F.2: la tolerance est DISTINCTE de la fraicheur et de la cadence de relecture."""
    settings = ApiSettings.from_env(BASE)
    assert settings.max_stale_seconds == 60.0
    assert (settings.jwks_cache_seconds, settings.min_refresh_seconds) == (300.0, 30.0)
    assert ApiSettings.from_env({**BASE, "MERVIO_OIDC_MAX_STALE_SECONDS": "0"}
                                ).max_stale_seconds == 0.0, "0 = aucune tolerance, licite"
    for invalid in ("-1", "3601"):
        with pytest.raises(SettingsError):
            ApiSettings.from_env({**BASE, "MERVIO_OIDC_MAX_STALE_SECONDS": invalid})


def test_the_three_durations_are_independent_settings():
    """Aucune des trois ne se deduit des autres: elles ne mesurent pas la meme chose."""
    settings = ApiSettings.from_env({**BASE, "MERVIO_OIDC_JWKS_CACHE_SECONDS": "120",
                                     "MERVIO_OIDC_MIN_REFRESH_SECONDS": "15",
                                     "MERVIO_OIDC_MAX_STALE_SECONDS": "900"})
    assert (settings.jwks_cache_seconds, settings.min_refresh_seconds,
            settings.max_stale_seconds) == (120.0, 15.0, 900.0)
