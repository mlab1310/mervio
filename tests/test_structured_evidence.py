"""AN-15 (D-082, perimetre etendu par D-090 = A): la preuve devient une donnee, pas une phrase.

DEFAUT. `Insight.evidence`, `Anomaly.evidence` et `DimensionScore.evidence` etaient des
`List[str]` de francais preformate contenant des nombres:

    "revenue observe sur 2026-W37: 24,051.10"
    "attendu (moyenne 8 periodes): 29,569.65"
    "ecart relatif: -18.7%"
    "z-score: -2.93"
    "contribution estimee a la variation: 159%"
    "CA -23.5% (2026-W37 vs 2026-W36)"

Mesure a `65409f5`: 28 chaines de preuve contenant un chiffre sur le seul `data/sample`, et
l'audit A-Z en a compte jusqu'a 146 dans un seul golden.

Trois consequences, chacune testee ici:

  1. L'API, l'interface et le LLM devaient REPARSER des nombres depuis du texte francais.
  2. L'identite de la grandeur etait perdue: << CA -23.5% >> ne dit pas quelle cle KPI, quelle
     source, ni contre quoi la comparaison est faite.
  3. Le LLM ne pouvait pas distinguer un FAIT MESURE d'un texte genere: les deux arrivaient
     comme des chaines.

Le plus parlant: `Anomaly.evidence` DUPLIQUAIT des champs typés qui existaient deja sur le meme
objet (`observed_value`, `expected_value`, `delta`, `delta_pct`, `z_score`). La preuve etait une
reformulation en prose de donnees dont on disposait.

CONTRAT (D-082 + liste du GO): chaque preuve est un fait structure portant son identifiant, sa
cle de message, sa grandeur, sa source, son observabilite, sa periode, sa comparaison, son unite
et ses valeurs. Les champs NON OBSERVES restent `null` -- jamais 0, jamais une chaine ambigue.

PRINCIPE:  fait structure -> rendu
JAMAIS:    nombre -> chaine de preuve libre -> interpretation LLM
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from mervio.analytics.pipeline import SourcePaths, run_analysis

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
DECLINE = Path(__file__).resolve().parent / "fixtures" / "an01_prolonged_decline"
DIGIT = re.compile(r"\d")
#: champs obligatoires de tout fait structure
REQUIRED = {"fact_id", "message_key", "metric_key", "source", "observability", "period",
            "comparison", "unit", "current_value", "previous_value", "delta", "delta_pct"}
COMPARISONS = {"baseline", "previous_period", "none"}


def _paths(directory: Path) -> SourcePaths:
    kwargs = {"shopify_orders": str(directory / "shopify_orders.csv"),
              "shopify_products": str(directory / "shopify_products.csv"),
              "google_ads": str(directory / "google_ads.csv")}
    if (directory / "stripe_transactions.csv").exists():
        kwargs["stripe"] = str(directory / "stripe_transactions.csv")
    return SourcePaths(**kwargs)


@pytest.fixture(scope="module")
def report() -> dict:
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    return run_analysis(_paths(SAMPLE), today=date(2026, 9, 14))


@pytest.fixture(scope="module")
def declining() -> dict:
    return run_analysis(_paths(DECLINE), today=date(2026, 9, 21))


def _evidence_blocks(report: dict):
    """Chaque liste de preuves du rapport, avec son emplacement. LES TROIS producteurs."""
    for section in ("critical_issues", "warnings", "opportunities"):
        for item in report.get(section, []):
            yield f"{section}[{item['type']}]", item["evidence"]
    for anomaly in report.get("anomalies", []):
        yield f"anomalies[{anomaly['metric']}]", anomaly["evidence"]
    for dimension in report["business_health_score"]["dimensions"]:
        yield f"health[{dimension['key']}]", dimension["evidence"]


def _all_items(report: dict):
    for where, block in _evidence_blocks(report):
        for index, item in enumerate(block):
            yield f"{where}.evidence[{index}]", item


# -- NO FREE-FORM FACTS: ce n'est pas un renommage -------------------------
def test_no_evidence_is_a_bare_string(report, declining):
    """Le defaut central. Un `List[str]` renomme resterait un `List[str]`."""
    for current in (report, declining):
        for where, item in _all_items(current):
            assert isinstance(item, dict), f"{where}: encore une chaine -> {item!r}"


def test_no_number_hides_in_a_proof_string(report, declining):
    """Critere d'acceptation A-04, etendu par D-090 = A a `health.py`.

    Un nombre dans une chaine de preuve oblige l'API, l'interface et le LLM a le reparser.
    """
    offenders = []
    for current in (report, declining):
        for where, item in _all_items(current):
            for key, value in item.items():
                # `fact_id`, `metric_key` et `source` sont des IDENTIFIANTS, pas des faits
                # chiffres: un identifiant de campagne Google Ads EST un nombre, et le masquer
                # serait une perte d'identite. `period` est un label de periode.
                if key in ("fact_id", "metric_key", "source", "period"):
                    continue
                if isinstance(value, str) and DIGIT.search(value):
                    offenders.append(f"{where}.{key} = {value!r}")
    assert not offenders, "nombres dans du texte de preuve:\n" + "\n".join(offenders)


def test_every_evidence_item_carries_the_whole_contract(report):
    for where, item in _all_items(report):
        assert set(item) == REQUIRED, f"{where}: champs {sorted(set(item) ^ REQUIRED)}"


# -- l'identite de la grandeur et de la source est PRESERVEE --------------
def test_an_evidence_item_names_its_metric_and_source(report):
    """<< CA -23.5% >> ne disait pas quelle cle KPI ni quelle source. Un consommateur devait
    deviner."""
    identified = [item for _w, item in _all_items(report) if item["metric_key"]]
    assert identified, "aucune preuve ne nomme sa grandeur"
    for where, item in _all_items(report):
        if item["metric_key"] is None:
            assert item["observability"] == "not_observed", (
                f"{where}: une preuve observee doit nommer sa grandeur"
            )
            continue
        assert isinstance(item["metric_key"], str) and " " not in item["metric_key"]
        assert item["source"], f"{where}: source non nommee"


def test_the_metric_keys_are_the_contract_keys(report):
    """Les cles de preuve doivent etre celles du contrat, pas des variantes de prose."""
    known = set(report["kpis"]) | {
        "revenue", "orders", "aov", "paid_clicks", "orders_per_paid_click", "roas", "refunds",
        "ad_spend", "contribution", "order_volume", "average_order_value", "paid_traffic",
        "organic_traffic", "site_conversion_rate", "cogs", "refunds_product_share",
        "shipping_cost", "payment_fees", "contribution_profit", "gross_margin",
        "customer_concentration_top5", "repeat_purchase_rate", "product_gross_margin",
        "data_quality_coverage", "campaign_spend", "campaign_conversions", "advertising",
    }
    unknown = {item["metric_key"] for _w, item in _all_items(report)
               if item["metric_key"] and item["metric_key"] not in known}
    assert not unknown, f"cles de preuve hors contrat: {sorted(unknown)}"


# -- plus d'ambiguite actuel / precedent / delta --------------------------
def test_current_previous_and_delta_are_separate_typed_fields(report):
    """<< depense: 749.46 vs 710.56 EUR >> melangeait deux valeurs et une devise dans une chaine.

    Un consommateur devait couper sur << vs >> et deviner l'ordre.
    """
    comparative = [item for _w, item in _all_items(report)
                   if item["previous_value"] is not None]
    assert comparative, "aucune preuve comparative: le test ne mesurerait rien"
    for where, item in _all_items(report):
        if item["previous_value"] is None or item["current_value"] is None:
            continue
        assert isinstance(item["current_value"], (int, float))
        assert isinstance(item["previous_value"], (int, float))
        if item["delta"] is not None:
            assert item["delta"] == pytest.approx(
                item["current_value"] - item["previous_value"], abs=0.02), (
                f"{where}: delta incoherent avec les deux valeurs"
            )


def test_a_comparison_declares_what_it_compares_against(report):
    """<< vs baseline >> et << vs 2026-W36 >> etaient noyes dans le texte. La comparaison est
    desormais une enumeration fermee: une part de variation vs baseline et vs periode precedente
    ne sont PAS la meme grandeur (lecon d'AN-01)."""
    for where, item in _all_items(report):
        assert item["comparison"] in COMPARISONS, f"{where}: {item['comparison']!r}"
        if item["previous_value"] is not None:
            assert item["comparison"] != "none", f"{where}: comparaison non declaree"


def test_a_unit_is_declared_whenever_a_value_is(report):
    for where, item in _all_items(report):
        if item["current_value"] is None:
            continue
        assert item["unit"], f"{where}: valeur sans unite"
        assert item["unit"] in {"currency", "count", "ratio", "score", "days"}, (
            f"{where}: unite inconnue {item['unit']!r}"
        )


# -- les champs non observes restent NON OBSERVES -------------------------
def test_an_unobserved_evidence_fabricates_no_value(report):
    """Ni 0, ni chaine ambigue (D-073, meme regle que pour les facteurs)."""
    unobserved = [(w, i) for w, i in _all_items(report)
                  if i["observability"] == "not_observed"]
    assert unobserved, "le rapport doit contenir au moins une preuve non observee"
    for where, item in unobserved:
        for field in ("current_value", "previous_value", "delta", "delta_pct"):
            assert item[field] is None, f"{where}.{field} doit etre null, pas {item[field]!r}"


def test_the_observability_is_a_closed_enumeration(report):
    states = {item["observability"] for _w, item in _all_items(report)}
    assert states <= {"observed", "not_observed"}, states


# -- identite stable du fait et du constat --------------------------------
def test_every_evidence_item_has_a_stable_fact_id(report):
    for where, item in _all_items(report):
        assert item["fact_id"], f"{where}: identifiant manquant"
        assert re.match(r"^[a-z0-9_.:-]+$", item["fact_id"]), f"{where}: {item['fact_id']!r}"


def test_fact_ids_are_unique_within_their_block(report):
    for where, block in _evidence_blocks(report):
        ids = [item["fact_id"] for item in block]
        assert len(ids) == len(set(ids)), f"{where}: identifiants dupliques {ids}"


def test_every_finding_has_a_stable_finding_id(report):
    """D-082: `finding_id` existait UNIQUEMENT dans `llm/context.py`, frappe localement, et etait
    absent du rapport. L'identifiant du constat doit venir du moteur, pas du consommateur."""
    for section in ("critical_issues", "warnings", "opportunities"):
        for item in report.get(section, []):
            assert item["finding_id"], f"{item['type']}: identifiant de constat manquant"
            assert re.match(r"^[a-z0-9_.:-]+$", item["finding_id"])


def test_finding_ids_are_unique_across_the_report(report):
    ids = [i["finding_id"] for s in ("critical_issues", "warnings", "opportunities")
           for i in report.get(s, [])]
    assert len(ids) == len(set(ids)), f"identifiants de constat dupliques: {ids}"


def test_the_finding_id_is_stable_across_runs(report):
    second = run_analysis(_paths(SAMPLE), today=date(2026, 9, 14))
    def ids(r):
        return [i["finding_id"] for s in ("critical_issues", "warnings", "opportunities")
                for i in r.get(s, [])]
    assert ids(report) == ids(second)


# -- la cle de message existe, le texte n'est plus la source de verite ----
def test_every_evidence_item_carries_a_message_key(report):
    """Prepare AN-19. La cle est technique et stable; le catalogue viendra ensuite."""
    for where, item in _all_items(report):
        key = item["message_key"]
        assert key, f"{where}: cle de message manquante"
        assert re.match(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$", key), f"{where}: {key!r}"
        assert not DIGIT.search(key), f"{where}: un nombre dans une cle de message"


def test_the_anomaly_evidence_no_longer_duplicates_its_own_typed_fields(report):
    """Le cas le plus parlant: l'anomalie portait DEJA `observed_value`, `expected_value`,
    `delta`, `delta_pct` et `z_score` en champs typés, et les reformulait en prose."""
    anomaly = next(a for a in report["anomalies"] if a["metric"] == "revenue")
    for item in anomaly["evidence"]:
        assert isinstance(item, dict)
    by_id = {i["fact_id"]: i for i in anomaly["evidence"]}
    main = by_id["anomaly.revenue"]
    assert main["current_value"] == pytest.approx(anomaly["observed_value"])
    assert main["previous_value"] == pytest.approx(anomaly["expected_value"])
    assert main["delta"] == pytest.approx(anomaly["delta"], abs=0.02)
    assert main["comparison"] == "baseline"
    assert main["metric_key"] == "revenue" and main["unit"] == "currency"
    # le z-score est un fait SEPARE, pas un suffixe de phrase
    # NOTE de contrat, mesuree: `Anomaly.to_dict` arrondit `z_score` a 3 decimales tandis que
    # la preuve structuree arrondit a 4. Le MEME nombre est donc expose a deux precisions dans un
    # meme rapport. Divergence PREEXISTANTE a AN-15 (elle vient de `Anomaly.to_dict`), sans effet
    # sur la valeur: a verrouiller par le JSON Schema en 004.5-E. L'egalite est donc assertee a la
    # precision la plus GROSSIERE des deux.
    zscore = by_id["anomaly.revenue.zscore"]
    assert round(zscore["current_value"], 3) == pytest.approx(anomaly["z_score"])
    assert zscore["unit"] == "score"


# -- le LLM distingue un fait mesure d'un texte genere -------------------
def test_the_llm_context_receives_structured_evidence_not_prose(report):
    from mervio.llm import build_llm_context
    context = build_llm_context(report)
    for finding in context["findings"]:
        assert "evidence_text" not in finding, (
            "`evidence_text` envoyait des chaines francaises au modele comme preuves"
        )
        for item in finding.get("evidence", []):
            assert isinstance(item, dict)
            assert "metric_key" in item


def test_the_llm_context_evidence_carries_no_number_in_text(report, declining):
    from mervio.llm import build_llm_context, serialize_context
    for current in (report, declining):
        context = json.loads(serialize_context(build_llm_context(current)))
        offenders = []
        for item in context["evidence"]:
            for key, value in item.items():
                # FRONTIERE AN-15 / AN-19, nommee et non franchie en silence. `criterion` et
                # `method` ne sont pas des FAITS: ce sont les descriptions de la REGLE, que D-007
                # exige d'ecrire dans l'objet retourne (<< une variation n'est jamais appelee
                # anomalie sans que le critere utilise soit ecrit >>). Leurs nombres sont les
                # SEUILS CONFIGURES, deja centralises par AN-16. AN-19 les remplacera par des
                # cles de message + parametres; AN-15 structure les faits, pas les textes.
                if key in ("id", "kind", "period", "message_key", "criterion", "method",
                           "label", "name", "factor", "observed_fact", "statement",
                           "hypothesis", "action", "reason_unavailable", "formula",
                           "thresholds", "definition", "note", "fact_id", "metric_key",
                           "source", "observability", "comparison", "unit", "confidence"):
                    continue
                if isinstance(value, str) and DIGIT.search(value):
                    offenders.append(f"evidence[{item.get('id')}].{key} = {value!r}")
        assert not offenders, "\n".join(offenders)


def test_the_health_dimension_evidence_is_in_scope(report):
    """D-090 = A: `health.py` EST dans le perimetre, et le critere A-04 ne distingue pas les
    producteurs."""
    dimensions = {d["key"]: d for d in report["business_health_score"]["dimensions"]}
    with_evidence = [d for d in dimensions.values() if d["evidence"]]
    assert with_evidence, "au moins une dimension doit porter une preuve"
    for dimension in with_evidence:
        for item in dimension["evidence"]:
            assert isinstance(item, dict), f"{dimension['key']}: encore une chaine"
            assert set(item) == REQUIRED


# -- DETERMINISME --------------------------------------------------------
def test_the_evidence_order_is_stable_within_a_block(report):
    second = run_analysis(_paths(SAMPLE), today=date(2026, 9, 14))
    for (where, first_block), (_w2, second_block) in zip(_evidence_blocks(report),
                                                         _evidence_blocks(second)):
        assert [i["fact_id"] for i in first_block] == [i["fact_id"] for i in second_block], where


_PROBE = """
import hashlib, json, sys
from datetime import date
from mervio.analytics.pipeline import SourcePaths, run_analysis
from mervio.identity import CustomerIdentity
D = sys.argv[1]
report = run_analysis(SourcePaths(shopify_orders=D + "/shopify_orders.csv",
                                  shopify_products=D + "/shopify_products.csv",
                                  google_ads=D + "/google_ads.csv"),
                      today=date(2026, 9, 21),
                      identity=CustomerIdentity(bytes.fromhex("cd" * 32)))
facts = [i for s in ("critical_issues", "warnings", "opportunities") for item in report[s]
         for i in item["evidence"]]
facts += [i for a in report["anomalies"] for i in a["evidence"]]
facts += [i for d in report["business_health_score"]["dimensions"] for i in d["evidence"]]
blob = json.dumps(facts, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
print(hashlib.sha256(blob.encode()).hexdigest())
"""


def test_the_structured_facts_are_byte_identical_across_hash_seeds():
    """Memes entrees -> memes faits -> memes octets. Aucun ensemble non ordonne sur ce chemin."""
    digests = set()
    for seed in ("0", "1", "2", "7", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT / 'tests'}")
        out = subprocess.run([sys.executable, "-c", _PROBE, str(DECLINE)], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=300, check=True)
        digests.add(out.stdout.strip())
    assert len(digests) == 1, f"faits non deterministes: {digests}"


def test_no_unordered_set_feeds_the_evidence_path():
    """Garde statique: un `set` itere directement redeviendrait F-01, dans un autre objet."""
    import ast
    offenders = []
    for relative in ("src/mervio/analytics/insights.py", "src/mervio/analytics/anomaly.py",
                     "src/mervio/analytics/health.py", "src/mervio/analytics/evidence.py"):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.ListComp, ast.GeneratorExp)):
                continue
            iterables = ([node.iter] if isinstance(node, ast.For)
                         else [gen.iter for gen in node.generators])
            for iterable in iterables:
                rendered = ast.unparse(iterable)
                if rendered.startswith("set(") or isinstance(iterable, ast.SetComp):
                    offenders.append(f"{relative}:{node.lineno} {rendered}")
    assert not offenders, "iteration sur un ensemble non ordonne:\n" + "\n".join(offenders)


# -- le rendu texte existe encore, mais il DERIVE du fait ----------------
def test_the_text_report_still_renders_evidence(report):
    """Le texte reste la representation finale, il n'est plus la source de verite."""
    from mervio.reporting.executive import render_executive_report
    text = render_executive_report(report)
    assert "PREUVES" in text or "preuve" in text.lower()
    assert "24,051.10" in text or "24051" in text.replace(",", ""), (
        "les nombres doivent encore apparaitre dans le rendu, depuis les champs typés"
    )


# -- FRONTIERE AN-15 / AN-19, explicite -----------------------------------
def test_rule_descriptions_remain_prose_until_an19(report):
    """Ce qui reste du texte apres AN-15, et POURQUOI.

    `Anomaly.criterion` et `Anomaly.method` decrivent la REGLE appliquee, pas le fait mesure.
    D-007 exige qu'ils soient ecrits dans l'objet retourne: << une variation n'est jamais appelee
    anomalie sans que le critere utilise soit ecrit >>. Leurs nombres sont les SEUILS CONFIGURES,
    deja centralises par AN-16 -- ils ne sont donc pas des mesures non ancrees.

    AN-15 structure les FAITS; AN-19 remplacera les TEXTES par des cles de message et des
    parametres. Ce test fige la frontiere pour qu'elle soit lue, pas devinee.
    """
    anomaly = next(a for a in report["anomalies"] if a["metric"] == "revenue")
    assert DIGIT.search(anomaly["criterion"]), "le critere porte encore ses seuils, par D-007"
    assert isinstance(anomaly["method"], str)
    # mais AUCUNE de ces chaines n'est un fait: les faits sont tous structures
    assert all(isinstance(item, dict) for item in anomaly["evidence"])


def test_the_engine_texts_still_carry_numbers_and_that_is_an19(report):
    """`fact`, `hypothesis` et `recommendation` restent de la prose avec des nombres.

    C'est la ligne de partage du roadmap: la ligne AN-15 nomme `evidence`, la ligne AN-19 nomme
    les cles de message pour les textes. Ce test le constate au lieu de le laisser croire corrige.
    """
    decline = next((i for i in report["critical_issues"] if i["type"] == "revenue_decline"), None)
    assert decline is not None
    assert DIGIT.search(decline["fact"]), "le FAIT reste de la prose jusqu'a AN-19"
    assert all(isinstance(item, dict) for item in decline["evidence"]), (
        "les PREUVES, elles, sont structurees"
    )
