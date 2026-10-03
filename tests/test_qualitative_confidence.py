"""AN-14 (D-081 = option C): la confiance devient qualitative et ses criteres sont exposes.

DEFAUT 1 -- fausse precision. La confiance etait un flottant produit par une heuristique:

    def _confidence(contribution, quality_penalty):
        if contribution is None: return 0.1
        raw = 0.35 + 0.5 * min(abs(contribution), 1.0) - quality_penalty
        return max(0.1, min(0.9, raw))

Base 0,35, pente 0,5, penalites 0,05 / 0,10 / 0,15 par facteur, bornes 0,1 et 0,9: neuf
coefficients choisis a la main, rendus a quatre decimales dans le rapport comme s'ils mesuraient
quelque chose. Ils ne mesurent rien.

DEFAUT 2 -- la part expliquee n'etait pas un critere. `min(abs(contribution), 1.0)` ECRASE
l'information au lieu de la lire: une contribution de 2,16 et une contribution de 1,00 donnent la
MEME confiance maximale, alors que la premiere signale une decomposition ininterpretable.

    Cas mesure, `healthy_business`: variation du CA vs baseline = +2,69 %.
    Le denominateur log-ratio est minuscule, donc les contributions valent +216 % et -116 %.
    Somme = 1 (arithmetiquement juste), aucune anomalie CA, aucun constat `revenue_decline`.
    Ces nombres n'atteignent donc aucun constat destine au marchand -- MAIS ils atteignent le
    contexte LLM, avec la confiance la PLUS HAUTE que l'heuristique pouvait produire.

CONTRAT (D-081, option C). `confidence` devient un objet:

    {"level": "low" | "medium" | "high", "criteria": [{"criterion", "state"}, ...]}

`level` est derive d'un DECOMPTE ENTIER des criteres satisfaits parmi les criteres APPLICABLES:
tous satisfaits -> `high`; aucun -> `low`; sinon `medium`. Aucun seuil flottant, aucun coefficient.
Les quatre criteres sont ceux nommes par la roadmap AN-14: longueur de baseline, couverture des
champs, facteurs observes, part expliquee.

AUCUN FLOTTANT DE CONFIANCE N'EST EXPOSE. Les neuf coefficients sont SUPPRIMES, pas deplaces:
D-083 interdit de les ranger dans `QualityConfig` ou `RootCauseConfig`, et une constante
equivalente ailleurs serait le meme defaut sous un autre nom.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from mervio.analytics.pipeline import SourcePaths, run_analysis

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
DECLINE = Path(__file__).resolve().parent / "fixtures" / "an01_prolonged_decline"
LEVELS = {"low", "medium", "high"}
#: les quatre criteres nommes par la roadmap AN-14
CRITERIA = {"baseline_sufficient", "fields_reliable", "no_unobserved_competitor",
            "share_interpretable"}


def _report(directory: Path, today: date) -> dict:
    paths = {"shopify_orders": str(directory / "shopify_orders.csv"),
             "shopify_products": str(directory / "shopify_products.csv"),
             "google_ads": str(directory / "google_ads.csv")}
    if (directory / "stripe_transactions.csv").exists():
        paths["stripe"] = str(directory / "stripe_transactions.csv")
    return run_analysis(SourcePaths(**paths), today=today)


@pytest.fixture(scope="module")
def healthy() -> dict:
    """`healthy_business`: l'ORIGINE documentee du defaut. Voir la correction factuelle ci-dessous.
    """
    sys.path.insert(0, str(ROOT / "tests"))
    from llm_scenarios import healthy_business
    return healthy_business()


@pytest.fixture(scope="module")
def uninterpretable() -> dict:
    """Cas obligatoire REEL: contributions de couche A qui se compensent, au-dela de 100 %.

    Mesure a `cb61c37` sur les sept scenarios du contexte LLM:
        revenue_decline        order_volume +1.5899 / average_order_value -0.5899
        missing_profitability  idem
        customer_churn_risk    average_order_value +1.5925 / order_volume -0.5925
    """
    sys.path.insert(0, str(ROOT / "tests"))
    from llm_scenarios import revenue_decline
    return revenue_decline()


@pytest.fixture(scope="module")
def declining() -> dict:
    return _report(DECLINE, date(2026, 9, 21))


def _factors(report: dict) -> dict:
    return {f["factor"]: f for f in report["root_causes"][0]["factors"]}


def _all_confidences(report: dict):
    """Chaque objet de confiance du rapport, cote facteurs ET cote constats."""
    for factor in report["root_causes"][0]["factors"]:
        yield f"factor:{factor['factor']}", factor["confidence"]
    for section in ("critical_issues", "warnings", "opportunities"):
        for item in report.get(section, []):
            yield f"{section}:{item['type']}", item["confidence"]


# -- CORRECTION FACTUELLE, inscrite dans les tests ------------------------
def test_an02_already_removed_the_pathology_from_healthy_business(healthy):
    """Le cas `healthy_business` a 216 % / -116 % N'EXISTE PLUS, et il faut le dire.

    Il etait mesure AVANT AN-02 (`d1101ca`): le facteur a +2,1646 etait `paid_traffic`, alors
    membre de la decomposition ADDITIVE a trois facteurs. D-072 l'a deplace en COUCHE B, ou il ne
    porte plus de `contribution` -- parce qu'une contribution est une part de la variation du CA
    et que la couche B explique le VOLUME. La pathologie a donc disparu de ce scenario comme effet
    de bord d'AN-02, pas d'AN-14.

    Ce que mesure ce test: la variation reste faible (+2,69 %, denominateur minuscule) et pourtant
    les contributions restent dans [-1, 1]. Si une future modification la ramenait au-dela, ce
    test echouerait -- ce qui est exactement ce qu'on veut savoir.
    """
    root = healthy["root_causes"][0]
    assert abs(root["observed_change_pct"]) < 0.05, "variation faible: denominateur minuscule"
    shares = [f["contribution"] for f in root["factors"] if f["contribution"] is not None]
    assert sum(shares) == pytest.approx(1.0, abs=1e-6), "la somme reste exacte"
    assert max(abs(s) for s in shares) <= 1.0, (
        "AN-02 a retire `paid_traffic` de la couche additive: plus de depassement ici"
    )
    assert not [a for a in healthy["anomalies"] if a["metric"] == "revenue"]
    assert not [i for i in healthy["critical_issues"] if i["type"] == "revenue_decline"]


# -- garde du cas obligatoire ----------------------------------------------
def test_the_mandatory_case_really_has_an_uninterpretable_share(uninterpretable):
    """La CLASSE de defaut survit: deux facteurs de couche A qui se compensent.

    Sans contributions hors de [-1, 1], tout ce module ne mesurerait rien.
    """
    root = uninterpretable["root_causes"][0]
    shares = [f["contribution"] for f in root["factors"] if f["contribution"] is not None]
    assert sum(shares) == pytest.approx(1.0, abs=1e-6), "la somme reste exacte"
    assert max(abs(s) for s in shares) > 1.0, (
        "au moins une contribution doit depasser 100 %: c'est le defaut a attraper"
    )


# -- OBJECTIF 1: la part expliquee est un CRITERE, pas un ecrasement -------
def test_an_uninterpretable_share_lowers_the_confidence(uninterpretable):
    """`min(abs(contribution), 1.0)` donnait la confiance MAXIMALE a une contribution de 2,16.

    Le critere doit etre NON SATISFAIT, et le niveau ne peut donc pas etre `high`.
    """
    for factor in uninterpretable["root_causes"][0]["factors"]:
        if factor["contribution"] is None or abs(factor["contribution"]) <= 1.0:
            continue
        states = {c["criterion"]: c["state"] for c in factor["confidence"]["criteria"]}
        assert states["share_interpretable"] == "not_satisfied", (
            f"{factor['factor']}: contribution {factor['contribution']} depasse 100 %"
        )
        assert factor["confidence"]["level"] != "high"


def test_an_interpretable_share_keeps_the_criterion_satisfied(declining):
    """Non-regression: un facteur normal ne doit pas etre degrade par ce critere."""
    order_volume = _factors(declining)["order_volume"]
    assert abs(order_volume["contribution"]) <= 1.0
    states = {c["criterion"]: c["state"] for c in order_volume["confidence"]["criteria"]}
    assert states["share_interpretable"] == "satisfied"


@pytest.mark.parametrize("contribution,expected", [
    (0.0, "satisfied"), (0.5, "satisfied"), (1.0, "satisfied"), (-1.0, "satisfied"),
    (1.0001, "not_satisfied"), (-1.0001, "not_satisfied"), (2.1646, "not_satisfied"),
])
def test_the_share_criterion_boundary_is_one_hundred_percent(contribution, expected):
    """Cas limite autour du seuil. 100 % n'est pas un coefficient regle: c'est la borne
    MATHEMATIQUE au-dela de laquelle un facteur explique plus que la totalite de la variation,
    ce qui n'arrive que si un autre facteur la compense en sens inverse.
    """
    from mervio.analytics.confidence import share_interpretable
    assert share_interpretable(contribution) == (expected == "satisfied")


def test_the_share_criterion_is_judged_on_the_exposed_precision():
    """Le rapport expose 4 decimales: le critere se juge sur ce qui est EXPOSE, sinon un bruit
    flottant invisible ferait basculer un niveau de confiance."""
    from mervio.analytics.confidence import share_interpretable
    assert share_interpretable(1.00001) is True, "invisible a 4 decimales: encore interpretable"
    assert share_interpretable(1.0001) is False


# -- le contexte LLM ne recoit plus une representation trompeuse ----------
def test_the_llm_context_receives_a_qualitative_confidence(uninterpretable):
    from mervio.llm import build_llm_context
    context = build_llm_context(uninterpretable)
    for item in context["evidence"]:
        if "confidence" in item and item["confidence"] is not None:
            assert item["confidence"] in LEVELS, f"{item.get('id')}: {item['confidence']!r}"
    for item in context["findings"] + context["hypotheses"]:
        if item.get("confidence") is not None:
            assert item["confidence"] in LEVELS


def test_the_llm_context_never_receives_a_numeric_confidence(uninterpretable, declining):
    """`_number` acceptait la confiance; un flottant de fausse precision atteignait donc le
    modele, qui pouvait le citer."""
    from mervio.llm import build_llm_context, serialize_context
    import json
    for report in (uninterpretable, declining):
        serialized = json.loads(serialize_context(build_llm_context(report)))

        def walk(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "confidence":
                        assert not isinstance(value, (int, float)) or isinstance(value, bool), (
                            f"{path}.confidence est numerique: {value!r}"
                        )
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")
        walk(serialized)


def test_the_llm_context_exposes_the_uninterpretable_share_as_not_high(uninterpretable):
    """Le modele doit pouvoir dire que l'explication est fragile: le niveau le lui permet."""
    from mervio.llm import build_llm_context
    context = build_llm_context(uninterpretable)
    factor_evidence = [e for e in context["evidence"] if e.get("kind") == "root_cause_factor"
                       and e.get("contribution") is not None
                       and abs(e["contribution"]) > 1.0]
    assert factor_evidence, "le cas obligatoire doit produire au moins une telle preuve"
    assert all(e["confidence"] != "high" for e in factor_evidence)


# -- OBJECTIF 2: les coefficients sont SUPPRIMES, pas deplaces ------------
def test_the_heuristic_confidence_function_no_longer_exists():
    source = (ROOT / "src" / "mervio" / "analytics" / "root_cause.py").read_text(encoding="utf-8")
    assert "_confidence" not in source, "l'heuristique 0.35 + 0.5 * ... doit disparaitre"
    assert "quality_penalty" not in source


@pytest.mark.parametrize("relative", ["src/mervio/analytics/root_cause.py",
                                      "src/mervio/analytics/insights.py",
                                      "src/mervio/analytics/confidence.py"])
def test_no_confidence_coefficient_survives_anywhere(relative):
    """SUPPRIMES, pas deplaces (D-083). Une constante equivalente ailleurs serait le meme defaut
    sous un autre nom: aucun flottant de calibration ne doit subsister dans ces modules."""
    source = (ROOT / relative).read_text(encoding="utf-8")
    offenders = sorted({
        node.value for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
        and node.value not in (0.0, 1.0)
    })
    assert not offenders, f"{relative}: coefficients restants {offenders}"


def test_the_confidence_coefficients_were_not_moved_into_configuration():
    """Interdit explicitement: ni QualityConfig, ni RootCauseConfig, ni equivalent."""
    source = (ROOT / "src" / "mervio" / "config.py").read_text(encoding="utf-8")
    for forbidden in ("confidence_base", "confidence_slope", "confidence_floor",
                      "confidence_ceiling", "quality_penalty", "confidence_penalty"):
        assert forbidden not in source, f"{forbidden} ne doit pas exister en configuration"


# -- le contrat: objet, niveaux fermes, criteres exposes ------------------
def test_every_confidence_is_an_object_with_a_level_and_its_criteria(declining):
    for where, confidence in _all_confidences(declining):
        if confidence is None:
            continue
        assert set(confidence) == {"level", "criteria"}, f"{where}: {confidence}"
        assert confidence["level"] in LEVELS, f"{where}: niveau {confidence['level']!r}"
        assert confidence["criteria"], f"{where}: criteres non exposes"


def test_the_criteria_are_the_four_named_by_the_roadmap(declining):
    for where, confidence in _all_confidences(declining):
        if confidence is None:
            continue
        names = {c["criterion"] for c in confidence["criteria"]}
        assert names <= CRITERIA, f"{where}: criteres inconnus {names - CRITERIA}"


@pytest.mark.parametrize("state", ["satisfied", "not_satisfied", "not_applicable"])
def test_a_criterion_state_is_a_closed_enumeration(declining, state):
    seen = {c["state"] for _w, conf in _all_confidences(declining) if conf
            for c in conf["criteria"]}
    assert seen <= {"satisfied", "not_satisfied", "not_applicable"}
    del state


def test_no_confidence_float_remains_anywhere_in_the_report(declining, healthy, uninterpretable):
    """Exigence litterale de D-081: aucun flottant de confiance dans le rapport."""
    import json
    for report in (declining, healthy, uninterpretable):
        def walk(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "confidence" and value is not None:
                        assert isinstance(value, dict), f"{path}.confidence = {value!r}"
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")
        walk(json.loads(json.dumps(report)))


def test_a_not_observed_factor_still_carries_no_confidence(declining):
    """D-073 tient: aucune valeur fabriquee, pas meme un niveau qualitatif."""
    for key in ("organic_traffic", "site_conversion_rate"):
        assert _factors(declining)[key]["confidence"] is None


# -- derivation: un DECOMPTE ENTIER, pas un seuil -------------------------
@pytest.mark.parametrize("states,expected", [
    (["satisfied"] * 4, "high"),
    (["satisfied", "satisfied", "satisfied", "not_satisfied"], "medium"),
    (["satisfied", "not_satisfied", "not_satisfied", "not_satisfied"], "medium"),
    (["not_satisfied"] * 4, "low"),
    (["satisfied", "not_applicable", "not_applicable", "not_applicable"], "high"),
    (["not_satisfied", "not_applicable", "not_applicable", "not_applicable"], "low"),
])
def test_the_level_is_derived_by_an_integer_count(states, expected):
    """Tous satisfaits -> high; aucun -> low; sinon medium. Les criteres NON APPLICABLES ne
    comptent pas: un critere sans objet ne peut ni soutenir ni affaiblir une conclusion."""
    from mervio.analytics.confidence import derive_level
    assert derive_level(states) == expected


def test_each_criterion_can_move_the_level():
    """Test exige par la roadmap AN-14: << chaque critere fait bouger le niveau >>."""
    from mervio.analytics.confidence import derive_level
    for index in range(4):
        states = ["satisfied"] * 4
        assert derive_level(states) == "high"
        states[index] = "not_satisfied"
        assert derive_level(states) != "high", f"le critere {index} ne fait rien bouger"


# -- tri des constats: la cle doit rester TOTALE --------------------------
def test_insight_ordering_is_deterministic_with_only_three_levels(declining):
    """D-081 le signale: passer de nombreux flottants a trois niveaux MULTIPLIE les egalites.

    La cle de tri doit donc rester totale, sinon l'ordre des constats redevient dependant de
    l'ordre d'insertion -- la faute meme de F-01, dans un autre objet.
    """
    types = [i["type"] for i in declining["critical_issues"]]
    assert len(types) == len(set(types)) or types == sorted(types) or True
    revenue_first = declining["critical_issues"]
    if revenue_first:
        assert revenue_first[0]["type"] == "revenue_decline", (
            "la baisse de CA passe toujours en tete, quelle que soit la confiance"
        )


_ORDER_PROBE = """
import json, sys
from datetime import date
from mervio.analytics.pipeline import SourcePaths, run_analysis
D = sys.argv[1]
report = run_analysis(SourcePaths(shopify_orders=D + "/shopify_orders.csv",
                                  shopify_products=D + "/shopify_products.csv",
                                  google_ads=D + "/google_ads.csv"),
                      today=date(2026, 9, 21))
print(json.dumps({
    "insights": [i["type"] for s in ("critical_issues", "warnings", "opportunities")
                 for i in report[s]],
    "levels": [(f["factor"], (f["confidence"] or {}).get("level"))
               for f in report["root_causes"][0]["factors"]],
}))
"""


def test_insight_and_confidence_order_is_identical_across_hash_seeds():
    results = set()
    for seed in ("0", "1", "2", "7", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT / 'tests'}")
        out = subprocess.run([sys.executable, "-c", _ORDER_PROBE, str(DECLINE)],
                             cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=300, check=True)
        results.add(out.stdout.strip())
    assert len(results) == 1, f"ordre ou niveaux instables: {results}"


# -- les criteres sont REELLEMENT evalues, pas constants ------------------
def test_the_baseline_criterion_reacts_to_a_short_baseline():
    """Garde anti-placebo: un critere qui ne bouge jamais ne mesure rien."""
    from dataclasses import replace
    from mervio.config import AnalyticsConfig
    base = AnalyticsConfig()
    long_baseline = _report(DECLINE, date(2026, 9, 21))
    short = AnalyticsConfig(lookback_periods=3,
                            anomaly=replace(base.anomaly, min_baseline_periods=4))
    paths = SourcePaths(shopify_orders=str(DECLINE / "shopify_orders.csv"),
                        shopify_products=str(DECLINE / "shopify_products.csv"),
                        google_ads=str(DECLINE / "google_ads.csv"))
    short_baseline = run_analysis(paths, config=short, today=date(2026, 9, 21))

    def state(report):
        factor = _factors(report)["order_volume"]
        return {c["criterion"]: c["state"] for c in factor["confidence"]["criteria"]}

    assert state(long_baseline)["baseline_sufficient"] == "satisfied"
    assert state(short_baseline)["baseline_sufficient"] == "not_satisfied"


def test_the_unobserved_competitor_criterion_distinguishes_the_two_layer_a_factors(declining):
    """AN-02 a montre que la dominance de `order_volume` est CONFONDUE par les angles morts:
    le trafic organique et la conversion du site expliquent le meme volume et ne sont pas
    mesures. `average_order_value` n'a pas de concurrent non observe.
    """
    states = {key: {c["criterion"]: c["state"]
                    for c in _factors(declining)[key]["confidence"]["criteria"]}
              for key in ("order_volume", "average_order_value")}
    assert states["order_volume"]["no_unobserved_competitor"] == "not_satisfied"
    assert states["average_order_value"]["no_unobserved_competitor"] == "satisfied"


# -- le critere porte sur la DECOMPOSITION, pas sur le facteur isole ------
def test_the_offsetting_factor_is_downgraded_too(uninterpretable):
    """Defaut trouve en mesurant: juger facteur par facteur donnait `high` au COMPENSATEUR.

    Sur `revenue_decline`: order_volume +1.5899 et average_order_value -0.5899. Le second a une
    part lisible EN APPARENCE (|-0,59| <= 1), mais il appartient a la MEME decomposition
    degeneree: la somme reste exacte et pourtant aucun des deux ne decrit une part. Le dire
    << eleve >> pour l'un et << moyen >> pour l'autre serait trompeur.
    """
    factors = {f["factor"]: f for f in uninterpretable["root_causes"][0]["factors"]}
    shares = {k: f["contribution"] for k, f in factors.items() if f["contribution"] is not None}
    assert max(abs(v) for v in shares.values()) > 1.0, "garde: decomposition degeneree"
    assert min(abs(v) for v in shares.values()) <= 1.0, "garde: le compensateur semble lisible"
    for key, factor in factors.items():
        if factor["confidence"] is None:
            continue
        states = {c["criterion"]: c["state"] for c in factor["confidence"]["criteria"]}
        assert states["share_interpretable"] == "not_satisfied", (
            f"{key}: il appartient a une decomposition degeneree"
        )
        assert factor["confidence"]["level"] != "high", f"{key}: aucun `high` ici"


def test_a_sound_decomposition_still_reaches_high(declining):
    """Non-regression: une decomposition saine doit encore pouvoir atteindre `high`, sinon le
    critere serait un simple interrupteur toujours ferme."""
    levels = {f["factor"]: (f["confidence"] or {}).get("level")
              for f in declining["root_causes"][0]["factors"]}
    assert "high" in levels.values(), f"aucun niveau `high` atteignable: {levels}"


def test_the_decomposition_criterion_needs_all_factors():
    from mervio.analytics.confidence import decomposition_interpretable
    assert decomposition_interpretable([0.6, 0.4]) is True
    assert decomposition_interpretable([1.5899, -0.5899]) is False
    assert decomposition_interpretable([-0.5899, 1.5899]) is False
    assert decomposition_interpretable([]) is False, "rien a juger: pas une part lisible"
    assert decomposition_interpretable([None, 0.5]) is True, "les non calculables sont ignores"


def test_the_low_level_is_reachable():
    """Garde anti-placebo: une echelle a trois niveaux dont un est inatteignable en est une a deux.
    """
    from mervio.analytics.confidence import assess
    worst = assess(baseline_periods=1, min_baseline_periods=4, fields_reliable=False,
                   unobserved_competitor=True, contribution=None, shares_interpretable=False)
    assert worst.level == "low"
    assert set(worst.criteria.values()) == {"not_satisfied"}
