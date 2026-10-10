"""Report Contract 2.0: le JSON Schema et ce qu'il verrouille (004.5 -- phase 2).

`schemas/report/2.0.json` est la definition STRUCTURELLE formelle du rapport. Il ne contient
aucune regle analytique: il decrit la forme que le moteur produit, et rien d'autre.

Ces tests couvrent les onze points du perimetre ratifie:

  A  le schema est lui-meme un schema Draft 2020-12 valide
  B  tous les rapports REELS valident -- les six scenarios de `llm_scenarios`, les dix-sept
     scenarios de verite terrain generes, et les branches que le corpus n'atteint pas seul
  C  les vingt-et-une cles de premier niveau sont exigees, sans `findings` ni `evidence`
  D  aucun objet du contrat n'est ouvert par facilite
  E  la nullabilite est celle du contrat reel, jamais un repli sur une chaine
  F  les enumerations fermees du code sont celles du schema
  G  les six conditionnels ratifies refusent les formes incoherentes
  H  `thresholds` est une structure, plus un `repr()` Python (K-6)
  I  `_meta.report_contract` vaut 2.0 (K-2)
  J  aucun identifiant client n'est publiable (AN-17)
  K  cles et parametres de message restent coherents avec le catalogue (AN-19 / D-085)

Les goldens ne sont PAS lus: aucun de ces tests ne les touche.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

import pytest

from conftest import ad, make_dataset, make_order
import llm_scenarios
from mervio.analytics import messages
from mervio.analytics.pipeline import analyze_loaded_dataset, run_analysis
from mervio.application.service import AnalysisRequest, analyze_dataset
from mervio.config import AnalyticsConfig
from mervio.synthetic import SCENARIOS, GeneratorConfig
from mervio.synthetic.evaluation import run_scenario

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "report" / "2.0.json"
SAMPLE = ROOT / "data" / "sample"

TODAY = date(2026, 9, 14)
#: lundis des deux dernieres semaines closes analysables depuis `TODAY`
LAST_WEEK, PRIOR_WEEK = date(2026, 9, 7), date(2026, 8, 31)

#: les vingt-et-une cles de premier niveau, DANS L'ORDRE ratifie (AN-18 / D-084 pour les quatre
#: sections de constats)
TOP_LEVEL = ("_meta", "business_health_score", "period", "executive_summary", "critical_issues",
             "warnings", "setup", "opportunities", "kpis", "profitability", "customers",
             "products", "marketing", "time_series", "comparisons", "anomalies", "root_causes",
             "recommendations", "data_freshness", "data_quality", "limitations")

#: profils alternes, comme `test_synthetic_scenarios`: un scenario ne doit pas valider seulement
#: sur la devise et le catalogue d'un seul profil
PROFILES = ("fashion_eu", "electronics_us")
#: volume reduit: la VALIDATION porte sur la forme, pas sur la verite terrain (que
#: `test_synthetic_scenarios` mesure deja a 20 000 commandes)
SCENARIO_ORDERS = 1200


# == outillage ===========================================================================

def load_schema() -> dict:
    """Le schema, ou un echec NOMME.

    Volontairement pas de lecture au niveau du module: un fichier absent ferait echouer la
    COLLECTE de tout le module, et un echec de collecte ne dit pas lequel des onze points du
    perimetre n'est pas tenu.
    """
    if not SCHEMA_PATH.exists():
        pytest.fail(f"schema absent: {SCHEMA_PATH.relative_to(ROOT)}")
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validator():
    from jsonschema.validators import Draft202012Validator
    return Draft202012Validator(load_schema())


def errors(report: dict) -> list:
    return sorted(validator().iter_errors(report), key=lambda e: list(e.absolute_path))


def assert_valid(report: dict, label: str = "") -> None:
    found = errors(report)
    assert not found, label + "\n" + "\n".join(
        f"  {list(e.absolute_path)}: {e.message[:160]}" for e in found[:8])


def assert_rejected(report: dict, label: str) -> None:
    assert errors(report), f"le schema accepte une forme incoherente: {label}"


def _sample_paths(**sources):
    names = {"shopify_orders": "shopify_orders.csv", "shopify_products": "shopify_products.csv",
             "stripe": "stripe_transactions.csv", "google_ads": "google_ads.csv"}
    from mervio.analytics.pipeline import SourcePaths
    return SourcePaths(**{key: str(SAMPLE / names[key]) for key in sources or names})


def _branch_reports() -> dict:
    """Rapports construits pour les BRANCHES que les scenarios n'atteignent pas.

    Le corpus de scenarios est riche mais il ne produit ni decoupage mensuel, ni decomposition
    impossible, ni fraicheur inconnue, ni devise absente: ces quatre formes font partie du
    contrat et doivent valider elles aussi.
    """
    only_last = make_dataset(orders=[make_order("#1", LAST_WEEK + timedelta(days=1),
                                                "a@example.com", [("A", 1, 100.0)])])
    two = make_dataset(
        orders=[make_order("#1", PRIOR_WEEK + timedelta(days=1), "a@example.com", [("A", 1, 100.0)]),
                make_order("#2", LAST_WEEK + timedelta(days=1), "b@example.com", [("A", 2, 100.0)])],
        ads=[ad(PRIOR_WEEK + timedelta(days=1), 20.0, 40), ad(LAST_WEEK + timedelta(days=1), 10.0, 20)])
    two.quality.set_field("customer_identity", covered=2, total=2)
    unknown_currency = make_dataset(
        orders=[make_order("#1", PRIOR_WEEK + timedelta(days=1), "a@example.com", [("A", 1, 90.0)]),
                make_order("#2", LAST_WEEK + timedelta(days=1), "b@example.com", [("A", 1, 50.0)])])
    unknown_currency.currency = "unknown"
    return {
        # decoupage MENSUEL: `comparisons` publie `mom` et non `wow`, et les labels changent de forme
        "month_grain": run_analysis(_sample_paths(), config=AnalyticsConfig(grain="month"),
                                    today=TODAY),
        # `today` absent: fraicheur `unknown`, retard non calculable
        "freshness_unknown": run_analysis(_sample_paths(), today=None),
        # analyse tres posterieure a la donnee: fraicheur `stale`
        "freshness_stale": run_analysis(_sample_paths(), today=date(2027, 3, 1)),
        # commandes seules: profitabilite indisponible, dimensions exclues, aucune campagne
        "orders_only": run_analysis(_sample_paths(shopify_orders=True), today=TODAY),
        # couche application: `dataset_is_synthetic` a true et un libelle d'analyse non vide
        "application_synthetic": analyze_dataset(AnalysisRequest(
            shopify_orders=str(SAMPLE / "shopify_orders.csv"),
            shopify_products=str(SAMPLE / "shopify_products.csv"),
            today=TODAY, label="contrat-2.0", synthetic=True)).report,
        # CA nul sur la baseline: decomposition IMPOSSIBLE, score non calculable
        "root_cause_unavailable": analyze_loaded_dataset(only_last, today=TODAY),
        # baseline d'UNE periode: le fait observe est enonce vs periode precedente
        "single_baseline_period": analyze_loaded_dataset(two, today=TODAY),
        # export sans devise: `unknown`, jamais une devise supposee
        "currency_unknown": analyze_loaded_dataset(unknown_currency, today=TODAY),
    }


@pytest.fixture(scope="module")
def engine_reports() -> dict:
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    logging.disable(logging.WARNING)
    try:
        reports = {name: build() for name, build in llm_scenarios.SCENARIOS.items()
                   if name != "minimal_report"}
        reports.update(_branch_reports())
    finally:
        logging.disable(logging.NOTSET)
    return reports


@pytest.fixture(scope="module")
def report(engine_reports) -> dict:
    return engine_reports["revenue_decline"]


# == A -- le schema lui-meme =============================================================

def test_a_the_schema_file_exists_and_is_json():
    """RED avant la phase 2: `schemas/report/2.0.json` n'existe pas."""
    schema = load_schema()
    assert isinstance(schema, dict) and schema


def test_a_the_schema_is_a_valid_draft_2020_12_schema():
    """Un schema invalide accepterait n'importe quoi en silence: on le valide d'abord."""
    from jsonschema.validators import Draft202012Validator
    Draft202012Validator.check_schema(load_schema())


def test_a_the_schema_declares_the_draft_and_an_explicit_id():
    schema = load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"], "$id explicite exige"


def test_a_every_reference_resolves_and_every_definition_is_used():
    """Une `$ref` morte ne leve qu'a la validation, et un `$defs` orphelin est du contrat mort."""
    schema = load_schema()
    refs = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                refs.add(node["$ref"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    declared = {f"#/$defs/{name}" for name in schema["$defs"]}
    assert not refs - declared, f"references non resolues: {sorted(refs - declared)}"
    assert not declared - refs, f"definitions jamais referencees: {sorted(declared - refs)}"


# == B -- tous les rapports REELS valident ===============================================

#: branches ajoutees au corpus, et la forme que chacune apporte (cf. `_branch_reports`)
BRANCH_NAMES = ("month_grain", "freshness_unknown", "freshness_stale", "orders_only",
                "application_synthetic", "root_cause_unavailable", "single_baseline_period",
                "currency_unknown")
#: scenarios de `tests/llm_scenarios.py` qui passent par le VRAI moteur
ENGINE_SCENARIOS = tuple(name for name in sorted(llm_scenarios.SCENARIOS)
                         if name != "minimal_report")


@pytest.mark.parametrize("name", ENGINE_SCENARIOS + BRANCH_NAMES)
def test_b_every_engine_report_validates(engine_reports, name):
    """RED avant la phase 2: il n'existe aucun schema contre lequel valider."""
    assert_valid(engine_reports[name], f"rapport {name} invalide")


@pytest.mark.parametrize("index, name", list(enumerate(sorted(SCENARIOS))))
def test_b_every_ground_truth_scenario_report_validates(tmp_path, index, name):
    """Les dix-sept scenarios de verite terrain, GENERES puis analyses par le vrai moteur.

    Le volume est reduit: ce test mesure la FORME, pas la verite terrain -- celle-la est deja
    mesuree a 20 000 commandes par `test_synthetic_scenarios`. Les deux profils alternent pour
    que la validation ne porte pas sur une seule devise ni un seul catalogue.
    """
    logging.disable(logging.WARNING)
    try:
        result = run_scenario(GeneratorConfig(scenario=name, orders=SCENARIO_ORDERS, days=126,
                                             seed=3, profile=PROFILES[index % 2]), tmp_path / name)
    finally:
        logging.disable(logging.NOTSET)
    assert_valid(result["report"], f"scenario {name} invalide")


def test_b_the_corpus_covers_the_branches_it_claims(engine_reports):
    """Un corpus qui ne contient pas les branches annoncees ne prouve rien.

    Sans cette garde, `test_b_*` pourrait valider vingt-trois fois la MEME forme et le declarer
    comme une couverture.
    """
    reports = list(engine_reports.values())
    assert set(engine_reports) == set(ENGINE_SCENARIOS) | set(BRANCH_NAMES)
    assert {r["_meta"]["grain"] for r in reports} == {"week", "month"}
    assert {r["data_freshness"]["status"] for r in reports} == {"fresh", "stale", "unknown"}
    assert {r["root_causes"][0]["available"] for r in reports if r["root_causes"]} == {True, False}
    assert {r["business_health_score"]["score"] is None for r in reports} == {True, False}
    assert {r["_meta"]["dataset_is_synthetic"] for r in reports} == {True, False}
    assert "unknown" in {r["_meta"]["currency"] for r in reports}
    assert {bool(r["marketing"]["campaigns"]) for r in reports} == {True, False}
    observability = {e["observability"] for r in reports for section in
                     ("critical_issues", "warnings", "setup", "opportunities")
                     for f in r[section] for e in f["evidence"]}
    assert observability == {"observed", "not_observed"}


def test_b_the_minimal_llm_fixture_is_not_a_report_of_this_contract():
    """`llm_scenarios.minimal_report` est le plus petit CONTEXTE accepte, pas un rapport.

    C'est un dict ecrit a la main pour la couche LLM: quatre cles sur vingt-et-une. Le valider
    contre le contrat 2.0 serait une erreur de perimetre, et le schema doit le refuser -- sinon
    il n'exige rien. Le test l'epingle pour que personne ne l'ajoute au corpus de `test_b_*`.
    """
    minimal = llm_scenarios.SCENARIOS["minimal_report"]()
    missing = [key for key in TOP_LEVEL if key not in minimal]
    assert len(missing) == 17, missing
    assert_rejected(minimal, "fixture minimale du contexte LLM")


# == C -- cles de premier niveau =========================================================

def test_c_the_schema_requires_the_twenty_one_top_level_keys_in_the_ratified_order():
    schema = load_schema()
    assert tuple(schema["required"]) == TOP_LEVEL
    assert tuple(schema["properties"]) == TOP_LEVEL


def test_c_no_findings_and_no_evidence_key_exists_at_the_top_level():
    """AN-18 / D-084: les constats vivent dans les quatre sections de categorie. AN-15 / D-082:
    les preuves vivent dans l'objet qu'elles soutiennent. Ni l'un ni l'autre n'a de liste
    racine, et le contrat doit l'interdire, pas seulement s'en abstenir."""
    schema = load_schema()
    assert "findings" not in schema["properties"]
    assert "evidence" not in schema["properties"]


def test_c_every_report_publishes_exactly_the_twenty_one_keys(engine_reports):
    for name, report in engine_reports.items():
        assert tuple(report) == TOP_LEVEL, name


def test_c_an_unknown_top_level_key_is_rejected(report):
    assert_rejected(dict(report, findings=[]), "cle racine `findings`")
    assert_rejected(dict(report, evidence=[]), "cle racine `evidence`")
    assert_rejected(dict(report, extra=None), "cle racine inconnue")


@pytest.mark.parametrize("key", TOP_LEVEL)
def test_c_removing_a_top_level_key_is_rejected(report, key):
    assert_rejected({k: v for k, v in report.items() if k != key}, f"cle {key} absente")


# == D -- aucun objet ouvert par facilite =================================================

#: valeurs admises dans un `<message>_params`: des SCALAIRES mesures, jamais une structure
SCALAR_TYPES = ["string", "number", "boolean", "null"]


def _object_schemas(node, path="") -> list:
    found = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append((path, node))
        for key, value in node.items():
            found += _object_schemas(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _object_schemas(value, f"{path}[{index}]")
    return found


def test_d_every_object_of_the_contract_closes_its_properties():
    """`additionalProperties: true` par facilite rendrait le contrat non verifiable.

    Deux formes sont admises: `false` pour un objet a cles connues, et un SCHEMA DE VALEUR pour
    les seuls conteneurs a cles ouvertes du contrat -- les `<message>_params`, dont les noms
    appartiennent au catalogue (`analytics/messages.py`) et dont les valeurs restent scalaires.
    """
    objects = _object_schemas(load_schema())
    assert len(objects) >= 25, f"la marche n'a trouve que {len(objects)} objets"
    offenders = []
    for path, schema in objects:
        extra = schema.get("additionalProperties", "ABSENT")
        if extra is False:
            continue
        if isinstance(extra, dict) and extra.get("type") == SCALAR_TYPES:
            continue
        offenders.append((path, extra))
    assert not offenders, f"objets ouverts: {offenders}"


def test_d_the_dynamic_containers_name_their_closed_keys():
    """Les conteneurs dynamiques du rapport ont tous une liste FERMEE cote code: elle doit
    etre dans le schema, pas supposee par le lecteur."""
    from mervio.analytics.pipeline import SERIES_METRICS
    from mervio.analytics.profitability import REQUIRED_COMPONENTS
    schema = load_schema()
    for container in ("time_series", "comparisons"):
        node = schema["properties"][container]
        assert set(node["properties"]) == set(SERIES_METRICS) == set(node["required"]), container
        assert node["additionalProperties"] is False, container
    components = schema["$defs"]["profitability"]["properties"]["components"]
    assert set(components["properties"]) == set(REQUIRED_COMPONENTS) == set(components["required"])
    assert components["additionalProperties"] is False
    row_counts = schema["$defs"]["data_quality"]["properties"]["row_counts"]
    assert set(row_counts["properties"]) == {"shopify_orders", "shopify_products", "stripe",
                                             "google_ads"}
    assert row_counts["additionalProperties"] is False
    currencies = schema["$defs"]["data_quality"]["properties"]["observed_currencies"]
    assert set(currencies["properties"]) == {"shopify", "stripe"}
    assert currencies["additionalProperties"] is False
    freshness = schema["$defs"]["data_freshness"]["properties"]["sources"]
    assert set(freshness["properties"]) == set(freshness["required"]) == {
        "shopify_orders", "google_ads", "stripe"}


def test_d_the_kpi_container_names_the_seventeen_contract_keys(report):
    """La cle du dictionnaire et le champ `key` de la metrique ne peuvent pas divergier."""
    kpis = load_schema()["properties"]["kpis"]
    assert set(kpis["properties"]) == set(report["kpis"]) == set(kpis["required"])
    assert len(kpis["required"]) == 17
    assert kpis["additionalProperties"] is False


def test_d_a_metric_published_under_the_wrong_key_is_rejected(report):
    broken = json.loads(json.dumps(report))
    broken["kpis"]["revenue"] = dict(broken["kpis"]["orders"])
    assert_rejected(broken, "metrique publiee sous une autre cle")


def test_d_an_unknown_kpi_key_is_rejected(report):
    broken = json.loads(json.dumps(report))
    broken["kpis"]["conversion_rate"] = dict(broken["kpis"]["roas"], key="roas")
    assert_rejected(broken, "cle de KPI inconnue (AN-02 a retire `conversion_rate`)")


# == E -- nullabilite ====================================================================

#: champs nullables du contrat reel, et ce que `null` y signifie. La valeur `null` doit etre
#: ACCEPTEE, et une chaine de repli REFUSEE: AN-15 a supprime les nombres dans les chaines, pas
#: l'inverse.
NULLABLE_PATHS = [
    ("profitability", "contribution_profit"),
    ("profitability", "contribution_margin"),
    ("profitability", "partial_contribution_margin"),
    ("kpis", "cac", "value"),
    ("kpis", "roas", "value"),
    ("kpis", "cogs", "value"),
    ("marketing", "cac", "value"),
    ("customers", "top_customers", 0, "revenue_share"),
    ("root_causes", 0, "change_vs_previous_period", "pct"),
    ("anomalies", 0, "delta_pct"),
]


def _set_path(report: dict, path, value):
    node = report
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _nullable_ids(path):
    return ".".join(str(part) for part in path)


@pytest.mark.parametrize("path", NULLABLE_PATHS, ids=_nullable_ids)
def test_e_a_nullable_field_accepts_null_and_refuses_a_string(report, path):
    accepted = json.loads(json.dumps(report))
    _set_path(accepted, path, None)
    assert_valid(accepted, f"{_nullable_ids(path)} = null refuse")
    refused = json.loads(json.dumps(report))
    _set_path(refused, path, "inconnu")
    assert_rejected(refused, f"{_nullable_ids(path)} = chaine de repli")


def test_e_the_future_profitability_branch_stays_open(report):
    """`contribution_margin` n'est aujourd'hui JAMAIS chiffree -- `shipping_cost` est toujours
    absente d'un export Shopify. La branche reste neanmoins au contrat: le jour ou un cout
    transporteur est branche, le profit complet devient un nombre sans changer de version."""
    future = json.loads(json.dumps(report))
    future["profitability"].update(data_available=True, contribution_profit=1234.5,
                                   contribution_margin=0.21, missing_components=[],
                                   limitations=[])
    for component in future["profitability"]["components"].values():
        if component["value"] is None:
            component.update(value=0.0, available=True)
    assert_valid(future, "profit de contribution complet refuse")


def test_e_an_unobserved_factor_keeps_its_numbers_null(report):
    """D-073: `contribution: 0.0` AFFIRMERAIT une absence d'effet. Le zero est refuse."""
    broken = json.loads(json.dumps(report))
    for factor in broken["root_causes"][0]["factors"]:
        if factor["observability"] == "not_observed":
            factor["contribution"] = 0.0
            break
    else:
        pytest.fail("aucun facteur non observe dans le rapport de reference")
    assert_rejected(broken, "facteur non observe avec une contribution nulle")


# == F -- enumerations ===================================================================

def _enum(name) -> list:
    return load_schema()["$defs"][name]["enum"]


def test_f_the_closed_vocabularies_of_the_code_are_those_of_the_schema():
    """Chaque enumeration du schema est confrontee a SA constante, pas a un echantillon.

    Un enum recopie a la main divergerait de son proprietaire a la premiere evolution: c'est la
    faute AN-16, transposee dans un fichier de contrat.
    """
    from mervio.analytics import confidence, evidence, insights
    from mervio.analytics.anomaly import Anomaly  # noqa: F401  (documente l'origine)
    from mervio.analytics.pipeline import SERIES_METRICS
    from mervio.analytics.profitability import REQUIRED_COMPONENTS
    from mervio.analytics.root_cause import LAYER_ORDER_VOLUME, LAYER_REVENUE
    from mervio.config import HealthConfig
    from mervio.domain.quality import QualityStatus

    assert _enum("finding_category") == list(insights.CATEGORIES)
    assert set(_enum("finding_severity")) == set(insights.SEVERITY_ORDER)
    assert set(_enum("data_quality_status")) == {s.value for s in QualityStatus}
    assert _enum("confidence_level") == list(confidence.CONFIDENCE_ORDER)
    assert _enum("confidence_criterion") == list(confidence.CRITERIA_ORDER)
    assert set(_enum("criterion_state")) == {confidence.SATISFIED, confidence.NOT_SATISFIED,
                                             confidence.NOT_APPLICABLE}
    assert set(_enum("evidence_observability")) == {evidence.OBSERVED, evidence.NOT_OBSERVED}
    assert set(_enum("evidence_comparison")) == set(evidence.COMPARISONS)
    assert set(_enum("evidence_unit")) == set(evidence.UNITS)
    assert set(_enum("factor_layer")) == {LAYER_REVENUE, LAYER_ORDER_VOLUME}
    assert set(_enum("health_dimension")) == set(HealthConfig().weights)
    assert set(_enum("series_metric_key")) == set(SERIES_METRICS)
    assert set(_enum("cost_component_key")) == set(REQUIRED_COMPONENTS)


def test_f_the_finding_severity_and_the_issue_severity_stay_separate():
    """Deux vocabulaires, deux echelles. Les confondre ferait passer un `warning` d'ingestion
    pour une alerte business, et il n'existe pas de signal de qualite `high`."""
    finding, issue = set(_enum("finding_severity")), set(_enum("issue_severity"))
    assert finding == {"high", "medium", "low", "info"}
    assert issue == {"info", "warning", "error"}
    assert finding != issue
    assert finding & issue == {"info"}
    assert "warning" not in finding and "high" not in issue


def test_f_the_anomaly_severity_never_admits_info():
    """`anomaly._severity` ne rend que trois valeurs. Admettre `info` decrirait une sortie que
    le detecteur ne produit pas."""
    from mervio.analytics.anomaly import _severity
    from mervio.config import AnomalyConfig
    cfg = AnomalyConfig()
    produced = {_severity(pct, z, cfg) for pct in (None, 0.0, 0.2, 0.9) for z in (None, 0.0, 2.5, 9.0)}
    assert produced <= set(_enum("anomaly_severity"))
    assert "info" not in _enum("anomaly_severity")


def test_f_the_metric_unit_enum_matches_what_the_kpi_engine_emits(report):
    units = {metric["unit"] for metric in report["kpis"].values()}
    assert units <= set(_enum("metric_unit"))
    assert "percent" not in _enum("metric_unit"), (
        "`percent` est cite par un commentaire d'annotation et n'a aucun producteur")


@pytest.mark.parametrize("path, value", [
    (("critical_issues", 0, "severity"), "critical"),
    (("critical_issues", 0, "category"), "warning"),
    (("anomalies", 0, "direction"), "sideways"),
    (("anomalies", 0, "assessment"), "bad"),
    (("anomalies", 0, "method"), "zscore"),
    (("anomalies", 0, "severity"), "info"),
    (("kpis", "revenue", "data_quality"), "green"),
    (("kpis", "revenue", "unit"), "percent"),
    (("data_quality", "issues", 0, "severity"), "high"),
    (("business_health_score", "dimensions", 0, "key"), "revenue_quality"),
    (("data_freshness", "status"), "old"),
    (("_meta", "grain"), "quarter"),
], ids=lambda value: str(value))
def test_f_an_invalid_enum_value_is_rejected(engine_reports, path, value):
    broken = json.loads(json.dumps(engine_reports["revenue_decline"]))
    _set_path(broken, path, value)
    assert_rejected(broken, f"{_nullable_ids(path)} = {value}")


@pytest.mark.parametrize("path, value", [
    (("business_health_score", "score"), "85"),
    (("_meta", "llm_used"), "false"),
    (("_meta", "dataset_is_synthetic"), 0),
    (("kpis", "revenue", "value"), "24051.10"),
    (("kpis", "revenue", "notes"), "une note"),
    (("kpis", "revenue", "sources"), "shopify_orders"),
    (("customers", "total_active"), 26.5),
    (("data_quality", "row_counts", "shopify_orders", "rows"), "466"),
    (("time_series", "revenue", "rolling_window"), 4.5),
    (("period", "current", "start"), "07/09/2026"),
    (("anomalies", 0, "baseline_periods"), ["semaine 29"]),
], ids=lambda value: str(value))
def test_f_an_invalid_type_is_rejected(engine_reports, path, value):
    broken = json.loads(json.dumps(engine_reports["revenue_decline"]))
    _set_path(broken, path, value)
    assert_rejected(broken, f"{_nullable_ids(path)} = {value!r}")


# == G -- conditionnels ==================================================================

def test_g1_a_finding_must_carry_the_parameters_its_message_requires(report):
    """Conditionnel 1: `fact_key` impose ses parametres. Un texte a trou ne doit jamais
    circuler -- `messages.render` leve, et le contrat doit refuser la meme forme."""
    for finding in report["critical_issues"]:
        if finding["fact_key"] != "finding.revenue_decline.fact":
            continue
        broken = json.loads(json.dumps(report))
        for item in broken["critical_issues"]:
            item["fact_params"].pop("delta_pct_abs", None)
        assert_rejected(broken, "fact_params sans `delta_pct_abs`")
        wrong = json.loads(json.dumps(report))
        for item in wrong["critical_issues"]:
            if item["fact_key"] == "finding.revenue_decline.fact":
                item["fact_params"]["delta_pct_abs"] = "18,7 %"
        assert_rejected(wrong, "`delta_pct_abs` publie en texte")
        return
    pytest.fail("le rapport de reference ne porte pas de constat `revenue_decline`")


def test_g1_a_message_parameter_is_never_a_structure(report):
    broken = json.loads(json.dumps(report))
    broken["critical_issues"][0]["fact_params"]["periodes"] = ["2026-W36", "2026-W37"]
    assert_rejected(broken, "parametre de message structure")


def test_g2_the_detection_criterion_publishes_only_the_thresholds_it_applied(report):
    """Conditionnel 2: B-3. `anomaly.criterion_without_zscore` ne publie PAS
    `zscore_threshold`, et `anomaly.criterion` l'exige -- sinon le rapport laisse croire qu'un
    seuil s'est applique."""
    broken = json.loads(json.dumps(report))
    anomaly = broken["anomalies"][0]
    anomaly["criterion_key"] = "anomaly.criterion_without_zscore"
    assert_rejected(broken, "critere sans z-score publiant le seuil de z-score")

    without = json.loads(json.dumps(report))
    anomaly = without["anomalies"][0]
    anomaly.update(criterion_key="anomaly.criterion_without_zscore", z_score=None)
    anomaly["criterion_params"].pop("zscore_threshold", None)
    assert_valid(without, "critere sans z-score coherent refuse")

    missing = json.loads(json.dumps(report))
    missing["anomalies"][0]["criterion_params"].pop("zscore_threshold", None)
    assert_rejected(missing, "`anomaly.criterion` sans seuil de z-score")


def test_g3_an_unobserved_factor_declares_its_blind_spot_and_nothing_else(engine_reports):
    """Conditionnel 3: D-073. Aucun chiffre, aucune confiance, aucune hypothese -- et la source
    qui manque est NOMMEE."""
    report = engine_reports["revenue_decline"]
    index = next(i for i, f in enumerate(report["root_causes"][0]["factors"])
                 if f["observability"] == "not_observed")
    for field, value in (("current_value", 0.0), ("factor_change_pct", 0.0),
                         ("contribution", 0.0), ("explains_share", 0.0),
                         ("missing_source", None),
                         ("observed_fact_key", "factor.observed_fact_vs_baseline")):
        broken = json.loads(json.dumps(report))
        broken["root_causes"][0]["factors"][index][field] = value
        assert_rejected(broken, f"facteur non observe avec {field} = {value!r}")
    borrowed = json.loads(json.dumps(report))
    observed = next(f for f in borrowed["root_causes"][0]["factors"]
                    if f["observability"] == "observed" and f["confidence"] is not None)
    borrowed["root_causes"][0]["factors"][index]["confidence"] = observed["confidence"]
    assert_rejected(borrowed, "facteur non observe portant une confiance")


def test_g3_an_observed_factor_declares_no_missing_source(report):
    broken = json.loads(json.dumps(report))
    for factor in broken["root_causes"][0]["factors"]:
        if factor["observability"] == "observed":
            factor["missing_source"] = "web_analytics_sessions"
            break
    assert_rejected(broken, "facteur observe avec une source manquante")


def test_g4_profitability_unavailable_publishes_no_margin_and_writes_what_is_missing(report):
    """Conditionnel 4: un profit complet non calculable n'est pas chiffre, et ce qui manque est
    ECRIT. Reciproquement, un profit disponible ne traine pas de limitation."""
    assert report["profitability"]["data_available"] is False
    for field in ("contribution_profit", "contribution_margin"):
        broken = json.loads(json.dumps(report))
        broken["profitability"][field] = 1234.5
        assert_rejected(broken, f"profit indisponible avec {field}")
    silent = json.loads(json.dumps(report))
    silent["profitability"]["limitations"] = []
    assert_rejected(silent, "profit indisponible sans aucune limitation")


def test_g4_a_cost_component_cannot_contradict_its_availability(report):
    broken = json.loads(json.dumps(report))
    broken["profitability"]["components"]["shipping_cost"]["available"] = True
    assert_rejected(broken, "composante disponible sans valeur")
    other = json.loads(json.dumps(report))
    other["profitability"]["components"]["payment_fees"]["available"] = False
    assert_rejected(other, "composante indisponible avec une valeur")


def test_g5_an_excluded_health_dimension_publishes_no_score_but_a_reason(engine_reports):
    """Conditionnel 5: une dimension non calculable est EXCLUE et son motif ecrit; une dimension
    notee ne porte aucun motif. La noter reviendrait a recompenser l'absence de donnee."""
    report = engine_reports["orders_only"]
    excluded = next(i for i, d in enumerate(report["business_health_score"]["dimensions"])
                    if not d["available"])
    scored = next(i for i, d in enumerate(report["business_health_score"]["dimensions"])
                  if d["available"])
    broken = json.loads(json.dumps(report))
    broken["business_health_score"]["dimensions"][excluded]["score"] = 50.0
    assert_rejected(broken, "dimension exclue avec une note")
    mute = json.loads(json.dumps(report))
    mute["business_health_score"]["dimensions"][excluded]["reason_unavailable"] = ""
    assert_rejected(mute, "dimension exclue sans motif")
    noisy = json.loads(json.dumps(report))
    noisy["business_health_score"]["dimensions"][scored]["reason_unavailable"] = "indisponible"
    assert_rejected(noisy, "dimension notee avec un motif d'indisponibilite")


def test_g5_a_non_computable_score_excludes_every_dimension(engine_reports):
    """Score `null`: aucune dimension n'etait exploitable, donc les six sont exclues et aucun
    poids n'a ete renormalise."""
    report = engine_reports["root_cause_unavailable"]
    assert report["business_health_score"]["score"] is None
    assert_valid(report, "score non calculable refuse")
    broken = json.loads(json.dumps(report))
    broken["business_health_score"]["excluded_dimensions"] = ["profitability"]
    assert_rejected(broken, "score null avec une seule dimension exclue")
    renormalised = json.loads(json.dumps(report))
    renormalised["business_health_score"]["weights_renormalised"] = True
    assert_rejected(renormalised, "score null avec des poids renormalises")


def test_g6_an_unknown_freshness_has_no_delay_and_says_so(engine_reports):
    """Conditionnel 6: `status`, `message_key` et `delay_days` viennent du MEME calcul."""
    unknown = engine_reports["freshness_unknown"]
    assert unknown["data_freshness"]["status"] == "unknown"
    assert_valid(unknown, "fraicheur inconnue refusee")
    broken = json.loads(json.dumps(unknown))
    broken["data_freshness"]["delay_days"] = 0
    assert_rejected(broken, "fraicheur inconnue avec un retard")
    mismatched = json.loads(json.dumps(unknown))
    mismatched["data_freshness"]["message_key"] = "data_freshness.fresh"
    assert_rejected(mismatched, "statut et cle de message incoherents")


def test_g6_a_known_freshness_carries_both_dates(engine_reports):
    for name in ("revenue_decline", "freshness_stale"):
        report = engine_reports[name]
        assert report["data_freshness"]["status"] in ("fresh", "stale")
        for field in ("analysis_date", "latest_data_day", "delay_days"):
            broken = json.loads(json.dumps(report))
            broken["data_freshness"][field] = None
            assert_rejected(broken, f"fraicheur {report['data_freshness']['status']} sans {field}")


def test_g_an_impossible_decomposition_publishes_no_factor(engine_reports):
    """La branche `available: false` de la cause racine: `analyse_revenue_change` rend la main
    avant d'ecrire quoi que ce soit, et le contrat doit refuser qu'on y ajoute une sortie."""
    report = engine_reports["root_cause_unavailable"]
    assert report["root_causes"][0]["available"] is False
    assert_valid(report, "decomposition impossible refusee")
    source = engine_reports["revenue_decline"]["root_causes"][0]
    for field in ("factors", "product_contributors", "campaign_contributors"):
        broken = json.loads(json.dumps(report))
        broken["root_causes"][0][field] = json.loads(json.dumps(source[field]))
        assert_rejected(broken, f"decomposition impossible avec {field}")
    proof = json.loads(json.dumps(report))
    proof["root_causes"][0]["orders_per_paid_click"] = json.loads(
        json.dumps(source["orders_per_paid_click"]))
    assert_rejected(proof, "decomposition impossible avec une preuve de conversion")
    named = json.loads(json.dumps(report))
    named["root_causes"][0]["primary_factor"] = "order_volume"
    assert_rejected(named, "decomposition impossible avec un facteur primaire")


def test_g_the_grain_governs_the_period_labels_and_the_comparisons(engine_reports):
    """Le decoupage est UN: `_meta.grain`, les deux periodes et le type de comparaison en
    viennent. Un rapport hebdomadaire publiant `mom` serait incoherent."""
    week, month = engine_reports["revenue_decline"], engine_reports["month_grain"]
    assert week["_meta"]["grain"] == "week" and month["_meta"]["grain"] == "month"
    assert set(month["comparisons"]["revenue"]) == {"mom", "yoy"}
    assert_valid(month, "decoupage mensuel refuse")
    broken = json.loads(json.dumps(week))
    broken["comparisons"]["revenue"]["mom"] = dict(broken["comparisons"]["revenue"]["wow"],
                                                   kind="mom")
    assert_rejected(broken, "`mom` sur un decoupage hebdomadaire")
    swapped = json.loads(json.dumps(week))
    swapped["comparisons"]["revenue"]["mom"] = swapped["comparisons"]["revenue"].pop("wow")
    swapped["comparisons"]["revenue"]["mom"]["kind"] = "mom"
    assert_rejected(swapped, "`wow` remplace par `mom` en semaine")
    label = json.loads(json.dumps(week))
    label["period"]["current"]["label"] = "2026-09"
    assert_rejected(label, "label mensuel sur un decoupage hebdomadaire")


def test_g_an_unavailable_comparison_carries_no_value_but_a_reason(report):
    unavailable = next(c for c in report["comparisons"]["revenue"].values() if not c["available"])
    assert unavailable["reason"]
    broken = json.loads(json.dumps(report))
    for kind, comparison in broken["comparisons"]["revenue"].items():
        if not comparison["available"]:
            comparison["current_value"] = 1.0
    assert_rejected(broken, "comparaison indisponible avec une valeur")
    mute = json.loads(json.dumps(report))
    for comparison in mute["comparisons"]["revenue"].values():
        if not comparison["available"]:
            comparison["reason"] = ""
    assert_rejected(mute, "comparaison indisponible sans motif")


def test_g_an_evidence_fact_cannot_mix_measured_and_unmeasured(report):
    """AN-15: la distinction `observed` / `not_observed` existe pour empecher exactement cela."""
    broken = json.loads(json.dumps(report))
    broken["anomalies"][0]["evidence"][0]["current_value"] = None
    assert_rejected(broken, "fait mesure sans mesure")
    orphan = json.loads(json.dumps(report))
    orphan["anomalies"][0]["evidence"][0]["previous_value"] = None
    assert_rejected(orphan, "comparaison declaree sans valeur de reference")
    unit = json.loads(json.dumps(report))
    unit["anomalies"][0]["evidence"][0]["unit"] = None
    assert_rejected(unit, "fait mesure sans unite")


# == H -- `thresholds` est une structure (K-6) ===========================================

#: seule dimension SANS bareme: `HealthConfig` n'en definit aucun pour elle, et sa note est une
#: application lineaire de la part de champs fiables (cf. `test_report_contract_2_0.NO_SCALE`)
NO_SCALE = "data_quality"


def test_h_the_schema_refuses_a_python_repr_in_the_thresholds_field(report):
    """K-6: le champ publiait `str(cfg.revenue_growth_thresholds)`. Le contrat doit refuser ce
    retour en arriere, pas seulement cesser de le produire."""
    broken = json.loads(json.dumps(report))
    broken["business_health_score"]["dimensions"][0]["thresholds"] = (
        "((-0.25, 0.0), (-0.1, 40.0), (0.0, 70.0), (0.1, 100.0))")
    assert_rejected(broken, "`thresholds` en texte")


@pytest.mark.parametrize("bands", [
    [[0.0]],                      # bande incomplete
    [[0.0, 1.0, 2.0]],            # bande a trois valeurs
    [["0.0", 1.0]],               # seuil en texte
    [0.0, 1.0],                   # bareme plat, non structure
    [],                           # bareme vide
], ids=["bande_incomplete", "bande_a_trois", "seuil_en_texte", "bareme_plat", "bareme_vide"])
def test_h_a_malformed_scale_is_rejected(report, bands):
    broken = json.loads(json.dumps(report))
    for dimension in broken["business_health_score"]["dimensions"]:
        if dimension["key"] != NO_SCALE:
            dimension["thresholds"] = bands
    assert_rejected(broken, f"bareme {bands!r}")


def test_h_the_scaleless_dimension_publishes_null_and_no_other(report):
    """Bilateral: `data_quality` n'a pas d'echelle, et aucune autre dimension n'a le droit de
    s'en passer. Sans la reciproque, une seconde dimension pourrait perdre son bareme en
    silence."""
    for dimension in report["business_health_score"]["dimensions"]:
        assert (dimension["thresholds"] is None) == (dimension["key"] == NO_SCALE)
    invented = json.loads(json.dumps(report))
    for dimension in invented["business_health_score"]["dimensions"]:
        if dimension["key"] == NO_SCALE:
            dimension["thresholds"] = [[0.0, 0.0], [1.0, 100.0]]
    assert_rejected(invented, "bareme invente pour `data_quality`")
    dropped = json.loads(json.dumps(report))
    for dimension in dropped["business_health_score"]["dimensions"]:
        if dimension["key"] == "revenue_growth":
            dimension["thresholds"] = None
    assert_rejected(dropped, "`revenue_growth` sans bareme")


def test_h_the_published_scales_are_those_of_the_configuration(report):
    """Les valeurs viennent de `HealthConfig`, inchangees: le schema decrit la FORME, la
    configuration reste proprietaire des seuils (AN-16)."""
    from mervio.config import HealthConfig
    cfg = HealthConfig()
    expected = {
        "revenue_growth": cfg.revenue_growth_thresholds,
        "profitability": cfg.profitability_thresholds,
        "marketing_efficiency": cfg.roas_thresholds,
        "customer_health": cfg.repeat_thresholds,
        "product_health": cfg.product_margin_thresholds,
    }
    for dimension in report["business_health_score"]["dimensions"]:
        if dimension["key"] == NO_SCALE:
            continue
        assert dimension["thresholds"] == [[float(a), float(b)]
                                           for a, b in expected[dimension["key"]]], dimension["key"]


# == I -- version de contrat (K-2) =======================================================

def test_i_the_schema_pins_the_contract_version_to_2_0():
    from mervio.config import REPORT_CONTRACT
    assert REPORT_CONTRACT == "2.0"
    meta = load_schema()["$defs"]["meta"]["properties"]
    assert meta["report_contract"]["const"] == "2.0"
    assert "report_contract" in load_schema()["$defs"]["meta"]["required"]


def test_i_every_report_declares_the_contract_and_a_1_x_report_is_rejected(engine_reports):
    for name, report in engine_reports.items():
        assert report["_meta"]["report_contract"] == "2.0", name
    legacy = json.loads(json.dumps(engine_reports["revenue_decline"]))
    legacy["_meta"]["report_contract"] = "1.0"
    assert_rejected(legacy, "rapport annoncant le contrat 1.0")
    absent = json.loads(json.dumps(engine_reports["revenue_decline"]))
    absent["_meta"].pop("report_contract")
    assert_rejected(absent, "rapport sans version de contrat")


def test_i_the_schema_file_lives_at_the_version_addressed_path():
    """Le chemin PORTE la version: un contrat 3.0 sera un second fichier, pas une reecriture."""
    assert SCHEMA_PATH == ROOT / "schemas" / "report" / "2.0.json"
    assert SCHEMA_PATH.exists()


# == J -- aucun identifiant client (AN-17) ===============================================

#: formes d'identification d'une personne que le rapport ne doit pas pouvoir porter
IDENTIFIER_NAMES = ("customer_id", "customer_ref", "customer_key", "email", "phone", "name_hash",
                    "pseudonym", "customer")


def test_j_the_schema_declares_no_customer_identifier_anywhere():
    """AN-17: le rapport publiait `cust_` + sha256(customer_ref)[:12] -- un IDENTIFIANT stable,
    recalculable, inscrit dans une table que `reports_forbid_update` interdit de modifier. Le
    pseudonyme d'un client efface (D-056) y survivait indefiniment."""
    schema = load_schema()
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if path.endswith("/properties") and key in IDENTIFIER_NAMES:
                    found.append(f"{path}/{key}")
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(schema, "")
    assert not found, f"identifiant client au contrat: {found}"


def test_j_a_customer_identifier_added_to_the_report_is_rejected(report):
    """`additionalProperties: false` sur `top_customer` est ce qui empeche le retour du champ."""
    for field in ("customer_id", "email", "customer_ref"):
        broken = json.loads(json.dumps(report))
        broken["customers"]["top_customers"][0][field] = "cust_7f3a91b2c4d5"
        assert_rejected(broken, f"top_customers[].{field}")


def test_j_a_top_customer_describes_a_rank_and_nothing_more():
    top = load_schema()["$defs"]["top_customer"]
    assert set(top["properties"]) == {"rank", "orders", "revenue_share"}
    assert set(top["required"]) == {"rank", "orders", "revenue_share"}
    assert top["additionalProperties"] is False


def test_j_no_report_of_the_corpus_carries_an_identifying_field(engine_reports):
    """Garde de parcours: la forme interdite ne doit pas non plus etre PRODUITE."""
    for name, report in engine_reports.items():
        found = []

        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in IDENTIFIER_NAMES:
                        found.append(f"{path}/{key}")
                    walk(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

        walk(report, "")
        assert not found, f"{name}: {found}"


# == K -- coherence avec le catalogue de messages (AN-19 / D-085) =========================

#: cles enumerees par le schema qui ne sont PAS des entrees de catalogue, et pourquoi.
#: `data_freshness.*`: identifiants stables poses par `pipeline._FRESHNESS_KEYS`; leur rendu
#: n'est pas encore au catalogue (D-1 laisse ce bloc a G-27 / 004.7).
NON_CATALOGUE_KEYS = {"data_freshness.fresh", "data_freshness.stale", "data_freshness.unknown"}
#: entrees du catalogue qui servent au RENDU et ne sont jamais publiees comme cle dans le
#: rapport: enumerations rendues en francais, libelles de facteur, etats vides de section
RENDER_ONLY_PREFIXES = ("severity.", "data_quality.", "confidence.", "criterion_state.",
                        "section.")


def _schema_string_values() -> set:
    values = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "enum" and isinstance(value, list):
                    values.update(item for item in value if isinstance(item, str))
                elif key == "const" and isinstance(value, str):
                    values.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(load_schema())
    return values


def _message_keys_of_the_schema() -> set:
    """Cles de message enumerees par le schema: celles qui portent un point, moins la version
    de contrat et les identifiants de fraicheur."""
    return {value for value in _schema_string_values()
            if "." in value and value != "2.0"} - NON_CATALOGUE_KEYS


def test_k_every_message_key_of_the_schema_exists_in_the_catalogue():
    """Une cle inconnue du catalogue ne rendrait AUCUN texte: `messages.render` leve, sans repli.
    Le schema ne doit donc pas pouvoir exiger une cle qui n'existe pas."""
    unknown = sorted(_message_keys_of_the_schema() - set(messages.MESSAGES))
    assert not unknown, f"cles absentes du catalogue: {unknown}"


def test_k_every_catalogue_entry_is_either_published_or_render_only():
    """Partition EXACTE du catalogue: une cle est soit publiee par le rapport (et alors
    enumeree), soit du vocabulaire de rendu. Sans cette partition, une nouvelle cle de constat
    pourrait echapper au contrat en silence."""
    published = _message_keys_of_the_schema() & set(messages.MESSAGES)
    render_only = {key for key in messages.MESSAGES
                   if key.startswith(RENDER_ONLY_PREFIXES) or key.endswith(".label")}
    assert not published & render_only, sorted(published & render_only)
    uncovered = sorted(set(messages.MESSAGES) - published - render_only)
    assert not uncovered, f"cles du catalogue ni publiees ni classees rendu: {uncovered}"


def _key_conditionals(defname: str, field: str) -> dict:
    """`<field>_key` -> parametres EXIGES par le schema, lus dans ses conditionnels."""
    out = {}
    for clause in load_schema()["$defs"][defname].get("allOf", []):
        condition = clause.get("if", {}).get("properties", {}).get(field + "_key")
        if not condition or "const" not in condition:
            continue
        params = clause["then"].get("properties", {}).get(field + "_params")
        if params is None:
            # ce conditionnel porte sur autre chose que les parametres (par exemple la
            # non-calculabilite d'un facteur): il n'a pas sa place dans cette table
            continue
        out[condition["const"]] = set(params.get("required", []))
    return out


@pytest.mark.parametrize("defname, field", [("finding", "fact"), ("finding", "recommendation"),
                                            ("recommendation", "recommendation"),
                                            ("cause_factor", "observed_fact")])
def test_k_the_schema_requires_every_parameter_the_template_demands(defname, field):
    """Le GABARIT decide. `messages.required_params` les lit DANS le gabarit, jamais dans une
    liste tenue a la main a cote -- qui divergerait. Le schema doit exiger au moins ceux-la."""
    conditionals = _key_conditionals(defname, field)
    assert conditionals, f"aucun conditionnel sur {defname}.{field}_key"
    for key, required in sorted(conditionals.items()):
        demanded = set(messages.required_params(key))
        assert demanded <= required, (
            f"{key}: le gabarit exige {sorted(demanded - required)}, le schema ne l'impose pas")


@pytest.mark.parametrize("defname, field, enum_path", [
    ("finding", "fact", ("properties", "fact_key", "enum")),
    ("cause_factor", "observed_fact", ("properties", "observed_fact_key", "enum")),
])
def test_k_every_enumerated_key_carries_its_own_conditional(defname, field, enum_path):
    """Une cle enumeree sans conditionnel serait admise SANS ses parametres."""
    node = load_schema()["$defs"][defname]
    for step in enum_path:
        node = node[step]
    assert set(node) == set(_key_conditionals(defname, field))


def test_k_the_rendered_text_of_every_finding_matches_its_key_and_parameters(engine_reports):
    """Le texte est DERIVE: il ne peut pas contredire sa cle. Le schema verrouille la forme,
    ce test verrouille l'accord -- un rapport valide dont le texte ne se rend pas serait un
    contrat tenu pour rien."""
    checked = 0
    for name, report in engine_reports.items():
        for section in ("critical_issues", "warnings", "setup", "opportunities"):
            for finding in report[section]:
                for field in ("fact", "hypothesis", "recommendation", "estimated_impact_basis"):
                    key, params = finding[field + "_key"], finding[field + "_params"]
                    if key is None:
                        continue
                    assert messages.render(key, params) == finding[field], f"{name}/{key}"
                    checked += 1
    assert checked >= 100, f"seulement {checked} messages verifies"


def test_k_the_anomaly_criterion_renders_from_its_published_parameters(engine_reports):
    checked = 0
    for name, report in engine_reports.items():
        for anomaly in report["anomalies"]:
            assert messages.render(anomaly["criterion_key"],
                                   anomaly["criterion_params"]) == anomaly["criterion"], name
            checked += 1
    assert checked >= 20, f"seulement {checked} criteres verifies"


def test_k_the_root_cause_texts_render_from_their_keys(engine_reports):
    for name, report in engine_reports.items():
        for analysis in report["root_causes"]:
            assert messages.render(analysis["method_key"]) == analysis["method"], name
            assert [messages.render(key) for key in analysis["limitation_keys"]] == \
                analysis["limitations"], name
            for factor in analysis["factors"]:
                assert messages.render(factor["observed_fact_key"],
                                       factor["observed_fact_params"]) == factor["observed_fact"]
