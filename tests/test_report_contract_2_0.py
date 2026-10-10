"""Report Contract 2.0: les RED des decisions de contrat ratifiees (004.5-E0).

Aucune implementation ici. Chaque test echoue a f3717bd pour la raison nommee dans sa
docstring, et devra passer quand le contrat 2.0 sera ecrit. Les goldens restent rouges et
inchanges: aucun de ces tests ne les lit.

PERIMETRE: K-1 (outil de validation), K-2 (version de contrat), K-4 (ordre total),
K-5 (propriete de `_meta`), K-6 (`thresholds` structure), K-11 (version du contexte LLM).
"""
from __future__ import annotations

import importlib.util
import re
from datetime import date
from pathlib import Path

import pytest

from conftest import ad, make_dataset, make_order
from mervio.analytics.pipeline import SourcePaths, analyze_loaded_dataset, run_analysis
from mervio.application.service import AnalysisRequest, analyze_dataset
from mervio.domain.models import DailyAdPerformance, Product
from mervio.llm import build_llm_context

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
TODAY = date(2026, 9, 14)
DATA_DAY, ANALYSIS_DAY = date(2026, 9, 13), date(2026, 9, 21)

#: valeur ratifiee (K-2)
REPORT_CONTRACT = "2.0"
#: version ratifiee du contexte LLM (K-11); les deux autres ne changent pas
LLM_CONTRACT_VERSION = "2.0"
UNCHANGED_LLM_VERSIONS = {"PROMPT_VERSION": "1.0", "RESPONSE_SCHEMA_VERSION": "1.0"}


def _sample_paths() -> SourcePaths:
    return SourcePaths(shopify_orders=str(SAMPLE / "shopify_orders.csv"),
                       shopify_products=str(SAMPLE / "shopify_products.csv"),
                       stripe=str(SAMPLE / "stripe_transactions.csv"),
                       google_ads=str(SAMPLE / "google_ads.csv"))


@pytest.fixture(scope="module")
def report() -> dict:
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    return run_analysis(_sample_paths(), today=TODAY)


# == K-1 -- jsonschema, dependance DEV/TEST uniquement ===================================

def test_k1_jsonschema_is_available_to_the_test_environment():
    """K-1: le validateur est une dependance de TEST. Il doit etre installable et installe.

    RED a f3717bd: `jsonschema` n'est ni dans `pyproject.toml [dev]`, ni dans
    `requirements.txt`, ni dans `requirements.lock`, ni dans l'environnement.

    `find_spec` plutot qu'un `import`: un import manquant ferait echouer la COLLECTE du
    module entier, et un `importorskip` ferait echouer la session sous
    `MERVIO_REQUIRE_DATABASE_TESTS=1` (garde anti-faux-vert).
    """
    assert importlib.util.find_spec("jsonschema") is not None, (
        "jsonschema absent de l'environnement de test")


def test_k1_jsonschema_supports_draft_2020_12():
    """Le contrat 2.0 est ecrit en Draft 2020-12: le validateur doit le connaitre."""
    if importlib.util.find_spec("jsonschema") is None:
        pytest.fail("jsonschema absent: version 2020-12 non verifiable")
    import jsonschema
    assert hasattr(jsonschema.validators, "Draft202012Validator")


def test_k1_jsonschema_is_declared_in_the_test_dependencies_only():
    """DEV/TEST uniquement: jamais une dependance runtime.

    `requirements.txt` est la source lisible, `pyproject.toml [dev]` l'extra, et
    `requirements.lock` la fermeture transitive exacte (test_requirements_lock.py). Les trois
    doivent le declarer. `requirements-runtime.lock` ne doit PAS le contenir: le coeur
    analytique reste sans dependance runtime (D-004).
    """
    declared = {name: (ROOT / name).read_text(encoding="utf-8")
                for name in ("pyproject.toml", "requirements.txt", "requirements.lock")}
    missing = [name for name, text in declared.items() if "jsonschema" not in text]
    assert not missing, f"jsonschema non declare dans: {missing}"
    runtime = (ROOT / "requirements-runtime.lock").read_text(encoding="utf-8")
    assert "jsonschema" not in runtime, "jsonschema ne doit pas etre une dependance runtime"


# == K-2 -- `_meta.report_contract` ======================================================

def test_k2_the_report_declares_its_contract_version(report):
    """K-2: un lecteur doit pouvoir distinguer 1.x de 2.0 depuis le rapport lui-meme.

    RED a f3717bd: la cle n'existe pas. D-058 avait explicitement DIFFERE ce champ a 004.5
    (<< pas de version de contrat de rapport ajoutee: le contrat 2.0 reste en 004.5 >>).
    """
    assert report["_meta"]["report_contract"] == REPORT_CONTRACT


def test_k2_the_contract_version_is_stable_across_entry_points():
    """Le meme moteur, deux points d'entree: la meme version de contrat."""
    engine = run_analysis(_sample_paths(), today=TODAY)
    application = analyze_dataset(AnalysisRequest(
        shopify_orders=str(SAMPLE / "shopify_orders.csv"),
        shopify_products=str(SAMPLE / "shopify_products.csv"),
        stripe=str(SAMPLE / "stripe_transactions.csv"),
        google_ads=str(SAMPLE / "google_ads.csv"),
        today=TODAY, label="contract-2.0")).report
    assert engine["_meta"]["report_contract"] == application["_meta"]["report_contract"]


# == K-5 -- le moteur possede la STRUCTURE de `_meta` ====================================

def _application_report(*, synthetic: bool, label: str = "k5") -> dict:
    return analyze_dataset(AnalysisRequest(
        shopify_orders=str(SAMPLE / "shopify_orders.csv"),
        shopify_products=str(SAMPLE / "shopify_products.csv"),
        today=TODAY, label=label, synthetic=synthetic)).report


def test_k5_both_entry_points_publish_the_same_meta_shape():
    """K-5: `assemble_report()` produit un `_meta` COMPLET; l'application remplit des
    valeurs, elle n'ajoute pas de cles.

    RED a f3717bd: `application/service.annotate_report` ajoute `analysis_label` (toujours)
    et `dataset_is_synthetic` (si synthetique). `run_analysis` publie 8 cles, `analyze_dataset`
    en publie 10: deux formes pour un seul contrat.
    """
    engine = set(run_analysis(SourcePaths(shopify_orders=str(SAMPLE / "shopify_orders.csv")),
                              today=TODAY)["_meta"])
    application = set(_application_report(synthetic=False)["_meta"])
    assert engine == application, (
        f"cles ajoutees hors du moteur: {sorted(application - engine)}; "
        f"cles perdues: {sorted(engine - application)}")


def test_k5_the_meta_shape_does_not_depend_on_the_synthetic_flag():
    """<< Meme forme >> vaut aussi A L'INTERIEUR d'un point d'entree.

    RED a f3717bd: `dataset_is_synthetic` n'est ajoute que lorsque le jeu EST synthetique,
    donc deux analyses du meme chemin n'ont pas la meme forme. Si le moteur possede la
    structure, la cle existe toujours et c'est sa VALEUR qui varie.
    """
    real = _application_report(synthetic=False)["_meta"]
    synthetic = _application_report(synthetic=True)["_meta"]
    assert set(real) == set(synthetic)
    assert real["dataset_is_synthetic"] is False
    assert synthetic["dataset_is_synthetic"] is True


def test_k5_the_application_layer_adds_no_contractual_key():
    """Garde statique: `annotate_report` ne doit plus CREER de cle de `_meta`.

    Elle peut en renseigner la valeur. La difference est le coeur de K-5: une cle creee hors
    du moteur n'a pas de proprietaire, et c'est ce qui a produit deux formes.
    """
    source = (ROOT / "src/mervio/application/service.py").read_text(encoding="utf-8")
    created = re.findall(r'report\["_meta"\]\["([a-z_]+)"\]\s*=', source)
    engine = set(run_analysis(SourcePaths(shopify_orders=str(SAMPLE / "shopify_orders.csv")),
                              today=TODAY)["_meta"])
    orphans = [key for key in created if key not in engine]
    assert not orphans, f"cles de `_meta` creees hors du moteur: {orphans}"


# == K-11 -- version du contrat de contexte LLM ==========================================

def test_k11_the_llm_context_declares_the_2_0_contract(report):
    """K-11: le contexte a gagne `message_key` sur cinq familles (AN-19) sans changer de
    version, et `llm/prompt.py` REFUSE un contexte dont la version ne correspond pas.

    RED a f3717bd: `contract_version` vaut encore "1.0".
    """
    assert build_llm_context(report)["contract_version"] == LLM_CONTRACT_VERSION


def test_k11_the_prompt_and_response_versions_do_not_move():
    """Seul le contrat de CONTEXTE change. Le prompt et le schema de reponse sont hors
    perimetre 004.5, et les durcir serait un changement non demande."""
    from mervio.llm import contract
    for name, expected in UNCHANGED_LLM_VERSIONS.items():
        assert getattr(contract, name) == expected, name


def test_k11_the_context_version_matches_the_declared_constant(report):
    """Le contexte ne doit pas annoncer une version differente de la constante du module:
    `prompt.build_prompt` compare les deux et leve si elles divergent."""
    from mervio.llm.contract import CONTRACT_VERSION
    assert CONTRACT_VERSION == LLM_CONTRACT_VERSION
    assert build_llm_context(report)["contract_version"] == CONTRACT_VERSION


# == K-4 -- ordre TOTAL de `products[]` et `marketing.campaigns[]` =======================

TIE_SKUS = ("ZZ-second", "AA-first")      # insertion volontairement dans le mauvais ordre
TIE_CAMPAIGNS = ("c-zz", "c-aa")


def _tied_products_dataset(order: tuple) -> dict:
    """Deux SKU au revenu EXACTEMENT egal, plus un troisieme pour que le tri ait une pente."""
    orders = [make_order("#big", DATA_DAY, "c1:" + "0" * 32, [("BIG", 2, 500.0)])]
    for index, sku in enumerate(order):
        orders.append(make_order(f"#{index}", DATA_DAY, f"c1:{index:02d}" + "0" * 30,
                                 [(sku, 1, 100.0)]))
    products = {sku: Product(sku, sku, sku, unit_cogs=10.0) for sku in order}
    products["BIG"] = Product("BIG", "BIG", "BIG", unit_cogs=10.0)
    dataset = make_dataset(orders=orders, products=products)
    dataset.quality.set_field("customer_identity", covered=3, total=3)
    return analyze_loaded_dataset(dataset, today=ANALYSIS_DAY)


def _tied_campaigns_dataset(order: tuple) -> dict:
    """Deux campagnes a la depense EXACTEMENT egale."""
    orders = [make_order("#1", DATA_DAY, "c1:" + "0" * 32, [("A", 1, 100.0)])]
    ads = [DailyAdPerformance(day=DATA_DAY, campaign_id=name, spend=50.0, impressions=300,
                              clicks=10, conversions=1.0, conversion_value=100.0)
           for name in order]
    dataset = make_dataset(orders=orders, ads=ads)
    dataset.campaigns.clear()
    for name in order:
        from mervio.domain.models import Campaign
        dataset.campaigns[name] = Campaign(name, f"Campagne {name}")
    return analyze_loaded_dataset(dataset, today=ANALYSIS_DAY)


def test_k4_products_are_totally_ordered_by_revenue_then_sku():
    """K-4: `kpi.product_performance` trie sur `e["revenue"]` SEUL (kpi.py:277).

    A egalite de revenu, l'ordre publie suit l'ordre d'insertion du dictionnaire, donc
    l'ordre d'apparition des articles dans l'export -- la faute meme de F-01, dans un tableau
    que F-01 n'a jamais couvert. `data/sample` n'a aucune egalite, donc rien ne l'a jamais
    montre.
    """
    published = [p["sku"] for p in _tied_products_dataset(TIE_SKUS)["products"]]
    tied = [sku for sku in published if sku in TIE_SKUS]
    assert tied == sorted(TIE_SKUS), f"ordre non total a egalite de revenu: {published}"


def test_k4_the_product_order_does_not_depend_on_insertion_order():
    """Contre-epreuve: le MEME jeu, les deux SKU inseres dans l'ordre inverse, doit publier
    le meme ordre. Sans cle totale, les deux sorties differeent."""
    first = [p["sku"] for p in _tied_products_dataset(TIE_SKUS)["products"]]
    second = [p["sku"] for p in _tied_products_dataset(tuple(reversed(TIE_SKUS)))["products"]]
    assert first == second, f"{first} != {second}"


def test_k4_campaigns_are_totally_ordered_by_spend_then_campaign_id():
    """Meme defaut dans `kpi.campaign_performance`, qui trie sur `e["spend"]` seul
    (kpi.py:300). L'identifiant metier du tableau est `campaign_id`."""
    rows = _tied_campaigns_dataset(TIE_CAMPAIGNS)["marketing"]["campaigns"]
    published = [c["campaign_id"] for c in rows]
    tied = [cid for cid in published if cid in TIE_CAMPAIGNS]
    assert tied == sorted(TIE_CAMPAIGNS), f"ordre non total a egalite de depense: {published}"


def test_k4_the_campaign_order_does_not_depend_on_insertion_order():
    first = [c["campaign_id"] for c in
             _tied_campaigns_dataset(TIE_CAMPAIGNS)["marketing"]["campaigns"]]
    second = [c["campaign_id"] for c in
              _tied_campaigns_dataset(tuple(reversed(TIE_CAMPAIGNS)))["marketing"]["campaigns"]]
    assert first == second, f"{first} != {second}"


def test_k4_the_existing_total_orders_are_untouched(report):
    """Garde: les clefs totales deja etablies par F-01 ne doivent pas bouger."""
    contributors = report["root_causes"][0]["campaign_contributors"]
    assert [c["campaign_id"] for c in contributors] == [
        c["campaign_id"] for c in sorted(contributors,
                                         key=lambda c: (c["conversions_delta"], c["campaign_id"]))]


# == K-6 -- `thresholds` devient une structure ==========================================

#: seule dimension SANS bareme: `HealthConfig` n'en definit aucun pour elle, et sa note est une
#: application lineaire de la part de champs fiables. Inventer une echelle ici lui donnerait un
#: second proprietaire (AN-16). Son `formula` porte la regle.
NO_SCALE = {"data_quality"}


def test_k6_the_health_thresholds_are_a_json_structure(report):
    """K-6: `DimensionScore.thresholds` etait annote `str` (health.py:54) et rempli par
    `str(cfg.revenue_growth_thresholds)` -- un `repr()` Python publie comme contrat:

        "((-0.25, 0.0), (-0.1, 40.0), (0.0, 70.0), (0.1, 100.0))"

    C'etait le dernier vestige de valeur structuree en texte apres AN-15. Forme cible:
    `[[seuil, score], ...]`, et `null` pour une dimension sans bareme.
    """
    offenders = [d["key"] for d in report["business_health_score"]["dimensions"]
                 if not isinstance(d["thresholds"], list) and d["key"] not in NO_SCALE]
    assert not offenders, f"`thresholds` encore une chaine pour: {offenders}"


def test_k6_a_dimension_without_a_scale_publishes_null_not_prose(report):
    """`data_quality` publiait la chaine libre "lineaire 0-100" dans un champ de bareme.

    Elle n'a pas d'echelle: la valeur est `null`, et la regle reste lisible dans `formula`.
    Une seule dimension est dans ce cas, et le test l'epingle pour qu'une seconde ne s'y
    glisse pas en silence.
    """
    without = {d["key"] for d in report["business_health_score"]["dimensions"]
               if d["thresholds"] is None}
    assert without == NO_SCALE, without
    for dimension in report["business_health_score"]["dimensions"]:
        if dimension["key"] in NO_SCALE:
            assert dimension["thresholds"] is None
            assert dimension["formula"], "la regle doit rester lisible quelque part"


def test_k6_each_threshold_band_is_a_pair_of_numbers(report):
    checked = 0
    for dimension in report["business_health_score"]["dimensions"]:
        bands = dimension["thresholds"]
        if dimension["key"] in NO_SCALE:
            continue
        assert isinstance(bands, list) and bands, dimension["key"]
        for band in bands:
            assert isinstance(band, list) and len(band) == 2, f"{dimension['key']}: {band!r}"
            assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in band)
        checked += 1
    assert checked >= 5, f"seulement {checked} baremes verifies"


def test_k6_the_thresholds_are_monotonic_and_match_the_configuration(report):
    """La structure doit rester LISIBLE comme la configuration qui la produit: seuils
    croissants, scores croissants. `HealthConfig` valide deja cette monotonie (AN-16)."""
    for dimension in report["business_health_score"]["dimensions"]:
        bands = dimension["thresholds"]
        if dimension["key"] in NO_SCALE:
            continue
        # pas de `continue` general ici: sans cette assertion le test passerait a vide tant
        # que `thresholds` est une chaine, et ne prouverait rien.
        assert isinstance(bands, list), f"{dimension['key']}: {bands!r}"
        xs = [band[0] for band in bands]
        ys = [band[1] for band in bands]
        assert xs == sorted(xs), f"{dimension['key']}: seuils non croissants {xs}"
        assert ys == sorted(ys), f"{dimension['key']}: scores non croissants {ys}"


def test_k6_no_structured_value_is_published_as_a_python_repr(report):
    """Garde generale: plus aucune chaine du rapport ne doit ressembler a un `repr()` de
    tuple ou de liste Python. C'est la regle, `thresholds` n'en etait que le cas restant."""
    import json
    repr_like = re.compile(r"^[\[(]\s*[\[(]?-?\d")
    offenders = []
    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items(): walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node): walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and repr_like.match(node):
            offenders.append(f"{path} = {node[:48]}")
    walk(json.loads(json.dumps(report)))
    assert not offenders, "valeur structuree publiee en texte:\n" + "\n".join(offenders)
