"""AN-16: aucun seuil de regle metier code en dur hors de `AnalyticsConfig`.

Le docstring de `src/mervio/config.py` affirme depuis l'origine:

    << Aucun seuil n'est code en dur ailleurs dans le moteur: tout ce qui influence un score,
       une anomalie ou une severite passe par AnalyticsConfig >>

C'etait faux. L'inventaire a `7580ad4` denombre les seuils de regle metier restes dans
`analytics/`, et DEUX champs de configuration qui existaient sans jamais etre lus:

    AnalyticsConfig.quality                       (QualityConfig, code mort)
    AnalyticsConfig.refund_reconciliation_tolerance (code mort)

Le second n'avait pas ete releve par l'audit: `pipeline._reconcile_refunds` comparait a `0.01`
code en dur alors que le champ portant exactement cette valeur existait a cote.

PERIMETRE (D-083 = A). Deux seuils de `root_cause._confidence` et les six constantes de confiance
d'`insights.py` NE sont PAS traites ici: ils DISPARAISSENT avec AN-14 (confiance qualitative) et
AN-02 (decomposition a deux couches). Les migrer puis les supprimer serait du travail jete.

EXCLU explicitement: `profitability._ROUNDING = 0.02` est une precision de FORMAT, pas une regle
metier. Le rendre parametrable n'aurait aucun sens: on ne configure pas la facon d'arrondir un
centime.

INVARIANT CENTRAL (A-16): aux valeurs par defaut, le rapport est identique OCTET POUR OCTET a
celui d'avant la centralisation. C'est a la fois l'exigence et la preuve: si un seuil avait ete
deplace avec une valeur differente, cette empreinte changerait.
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from mervio.analytics.pipeline import SourcePaths, run_analysis
from mervio.config import AnalyticsConfig, HealthConfig
from mervio.errors import ConfigurationError
from mervio.identity import CustomerIdentity

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
#: empreinte du rapport `data/sample` MESUREE A 7580ad4, avant toute centralisation de seuil.
#: Elle ne doit pas changer: AN-16 deplace des seuils, il n'en modifie aucune valeur.
DIGEST_BEFORE_AN16 = "9ae20682408336b9e85207b77f652f68f43c7148a87d5400d13fca365911cdf4"
IDENTITY = CustomerIdentity(bytes.fromhex("ab" * 32))
TODAY = date(2026, 9, 14)


def _paths() -> SourcePaths:
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes: lancer scripts/generate_sample_data.py")
    return SourcePaths(
        shopify_orders=str(SAMPLE / "shopify_orders.csv"),
        shopify_products=str(SAMPLE / "shopify_products.csv"),
        stripe=str(SAMPLE / "stripe_transactions.csv"),
        google_ads=str(SAMPLE / "google_ads.csv"),
    )


def _report(config: AnalyticsConfig | None = None) -> dict:
    kwargs = {"config": config} if config is not None else {}
    report = run_analysis(_paths(), today=TODAY, identity=IDENTITY, **kwargs)
    report["_meta"].pop("generated_at", None)
    return report


def _digest(report: dict) -> str:
    blob = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# -- A-16: l'empreinte par defaut ne bouge pas ------------------------------
def test_the_default_report_is_byte_identical_to_the_pre_centralisation_one():
    """Preuve de non-regression d'AN-16, et sa raison d'etre.

    Si ce test echoue, un seuil a ete deplace AVEC une valeur differente: ce n'est plus une
    centralisation, c'est un changement de comportement.
    """
    assert _digest(_report()) == DIGEST_BEFORE_AN16


def test_the_explicit_default_config_behaves_like_no_config_at_all():
    assert _digest(_report(AnalyticsConfig())) == DIGEST_BEFORE_AN16


# -- chaque seuil deplace doit etre REELLEMENT actif ------------------------
# Avant AN-16, ces seuils sont codes en dur: changer la configuration ne change RIEN et chacun
# de ces tests echoue.
def test_the_low_product_margin_threshold_is_configurable():
    """`insights.py` comparait a 0.20 en dur (seuil nomme par la roadmap AN-16)."""
    strict = AnalyticsConfig(insights=replace(AnalyticsConfig().insights,
                                              low_product_margin=0.95))
    types = {i["type"] for i in _report(strict)["warnings"] + _report(strict)["critical_issues"]}
    assert "low_margin_product" in types, "seuil a 0.95: tout produit devient a faible marge"
    lenient = AnalyticsConfig(insights=replace(AnalyticsConfig().insights,
                                               low_product_margin=-1.0))
    report = _report(lenient)
    types = {i["type"] for i in report["warnings"] + report["critical_issues"]}
    assert "low_margin_product" not in types, "seuil negatif: aucun produit ne peut etre dessous"


def test_the_customer_concentration_threshold_has_a_single_owner():
    """`insights.py` comparait a 0.40 en dur, DOUBLON de `HealthConfig.concentration_warning`.

    Deux proprietaires pour un seuil, c'est une divergence qui attend. Le test verifie qu'un seul
    reglage suffit a piloter les deux consommateurs.
    """
    config = AnalyticsConfig(health=replace(HealthConfig(), concentration_warning=0.0))
    report = _report(config)
    types = {i["type"] for i in report["warnings"] + report["opportunities"]}
    assert "customer_concentration" in types, "seuil a 0: la concentration est toujours signalee"


def test_the_gross_margin_reliability_coverage_is_configurable():
    """`kpi.py` comparait a 0.95 en dur, alors que `QualityConfig.reliable_coverage` portait
    exactement cette valeur sans jamais etre lu.

    Sur `data/sample` la couverture du cout produit vaut 78 %: la marge brute est donc
    `incomplete` par defaut. Le seuil est exerce dans le sens qui CHANGE cet etat.
    """
    base = AnalyticsConfig()
    assert base.quality.reliable_coverage == 0.95, "valeur par defaut preservee"
    assert _report()["kpis"]["gross_margin"]["data_quality"] == "incomplete", "etat par defaut"
    lenient = AnalyticsConfig(quality=replace(base.quality, reliable_coverage=0.10))
    assert _report(lenient)["kpis"]["gross_margin"]["data_quality"] == "reliable", (
        "couverture requise abaissee a 10 %: 78 % devient fiable"
    )


def test_the_complete_cogs_coverage_is_configurable():
    """`profitability.py` comparait a 0.999 en dur, a DEUX endroits."""
    base = AnalyticsConfig()
    assert _report()["profitability"]["components"]["cogs"]["available"] is False, (
        "etat par defaut: 78 % de couverture, COGS total non calculable"
    )
    lenient = AnalyticsConfig(profitability=replace(base.profitability,
                                                    complete_cogs_coverage=0.0))
    cogs = _report(lenient)["profitability"]["components"]["cogs"]
    assert cogs["available"] is True and cogs["value"] is not None, (
        "couverture requise nulle: le COGS devient calculable"
    )


def test_the_minimum_product_coverage_is_configurable():
    """`health.py` portait `MIN_PRODUCT_COVERAGE = 0.80` comme constante de module."""
    base = AnalyticsConfig()
    strict = AnalyticsConfig(health=replace(base.health, min_product_coverage=1.0 + 1e-9))
    dimensions = {d["key"]: d for d in _report(strict)["business_health_score"]["dimensions"]}
    assert dimensions["product_health"]["available"] is False, (
        "couverture produit inatteignable: la dimension doit etre EXCLUE, pas approximee"
    )


def test_the_refund_reconciliation_tolerance_is_configurable():
    """`pipeline._reconcile_refunds` comparait a 0.01 en dur alors que
    `AnalyticsConfig.refund_reconciliation_tolerance` portait exactement cette valeur.

    Code mort NON releve par l'audit A-Z.
    """
    base = AnalyticsConfig()
    assert base.refund_reconciliation_tolerance == 0.01, "valeur par defaut preservee"
    strict = AnalyticsConfig(refund_reconciliation_tolerance=-1.0)
    kinds = {i["kind"] for i in _report(strict)["data_quality"]["issues"]}
    assert "refund_mismatch" in kinds, "tolerance negative: tout ecart non nul est signale"


#: bareme -> dimension du score qu'il gouverne
_SCALE_DIMENSION = {
    "revenue_growth_thresholds": "revenue_growth",
    "profitability_thresholds": "profitability",
    "roas_thresholds": "marketing_efficiency",
    "repeat_thresholds": "customer_health",
    "product_margin_thresholds": "product_health",
}


@pytest.mark.parametrize("scale,dimension", sorted(_SCALE_DIMENSION.items()))
def test_each_health_threshold_scale_is_configurable(scale, dimension):
    """Les cinq baremes du Business Health Score etaient des constantes de module.

    Sur `data/sample`, deux dimensions sont EXCLUES (`profitability` et `product_health`: couts
    incomplets). Leur score ne peut donc rien prouver. On mesure a la place le champ `thresholds`
    du rapport, que `health.py` renseigne dans TOUTES ses branches, disponible ou non: c'est la
    preuve que la valeur de configuration atteint le code. Quand le score existe, on exige en
    plus qu'il bouge.
    """
    flat = ((0.0, 7.0), (1.0, 7.0))
    config = AnalyticsConfig(health=replace(AnalyticsConfig().health, **{scale: flat}))
    before = {d["key"]: d for d in _report()["business_health_score"]["dimensions"]}
    after = {d["key"]: d for d in _report(config)["business_health_score"]["dimensions"]}

    assert after[dimension]["thresholds"] == str(flat), (
        f"{scale} n'atteint pas la dimension {dimension}: bareme non consomme"
    )
    assert before[dimension]["thresholds"] != after[dimension]["thresholds"]
    if before[dimension]["score"] is not None:
        assert before[dimension]["score"] != after[dimension]["score"], (
            f"{dimension} est calculable: son score doit suivre le bareme"
        )


# -- validation: un bareme non monotone est un refus ------------------------
def test_a_non_monotonic_health_scale_is_refused():
    """Les baremes vivaient dans une `dataclass(frozen=True)` SANS aucune validation.

    `interpolate` suppose des valeurs de seuil croissantes; un bareme decroissant produirait des
    scores silencieusement faux au lieu d'une erreur.
    """
    with pytest.raises(ConfigurationError):
        AnalyticsConfig(health=replace(HealthConfig(),
                                       roas_thresholds=((2.0, 0.0), (1.0, 50.0))))


def test_an_empty_health_scale_is_refused():
    with pytest.raises(ConfigurationError):
        AnalyticsConfig(health=replace(HealthConfig(), roas_thresholds=()))


def test_the_default_health_scales_are_monotonic():
    """Garde: les valeurs par defaut doivent satisfaire la regle qu'on vient d'imposer."""
    health = HealthConfig()
    for name in ("revenue_growth_thresholds", "profitability_thresholds", "roas_thresholds",
                 "repeat_thresholds", "product_margin_thresholds"):
        scale = getattr(health, name)
        values = [point[0] for point in scale]
        assert values == sorted(values), f"{name} n'est pas croissant: {values}"
        assert len(set(values)) == len(values), f"{name} a des seuils dupliques: {values}"


# -- garde statique: plus de litteral de regle metier aux sites migres ------
#: (module, lignes a blanchir de tout litteral flottant de regle metier)
#:
#: `root_cause.py` et les constantes de confiance d'`insights.py` sont ABSENTS: ils relevent
#: d'AN-14 / AN-02, qui les supprimeront. Les y inclure maintenant ferait echouer ce test pour
#: une raison etrangere a AN-16.
_MIGRATED = ("src/mervio/analytics/health.py", "src/mervio/analytics/kpi.py",
             "src/mervio/analytics/profitability.py", "src/mervio/analytics/pipeline.py")
#: seuls flottants tolerables: 0 et 1 (bornes arithmetiques), 100 (echelle de score),
#: et l'arrondi de format exclu du perimetre par D-083
_ALLOWED_FLOATS = {0.0, 1.0, 100.0, 0.02}


@pytest.mark.parametrize("relative", _MIGRATED)
def test_no_business_rule_float_literal_remains_in_the_migrated_modules(relative):
    source = (ROOT / relative).read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            if node.value not in _ALLOWED_FLOATS:
                offenders.append(f"{relative}:{node.lineno} -> {node.value}")
    assert not offenders, (
        "seuils de regle metier encore codes en dur:\n" + "\n".join(offenders)
    )


def test_the_config_docstring_promise_is_kept_for_the_migrated_modules():
    """`config.py` promet qu'aucun seuil n'est code en dur ailleurs. Ce test mesure la promesse
    sur le perimetre d'AN-16, et echouera si un seuil y est reintroduit."""
    for relative in _MIGRATED:
        source = (ROOT / relative).read_text(encoding="utf-8")
        floats = {node.value for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Constant) and isinstance(node.value, float)}
        assert floats <= _ALLOWED_FLOATS, f"{relative}: {sorted(floats - _ALLOWED_FLOATS)}"
