"""Report Contract 2.0: integration et invariants NON exprimables en JSON Schema (004.5 -- phase 3).

Le schema (`schemas/report/2.0.json`) verifie la STRUCTURE. Il ne peut pas verifier qu'un texte
rende sa cle, que deux points d'entree s'accordent, qu'un pseudonyme ne soit pas recalculable,
ni que les octets d'un rapport ne dependent pas de `PYTHONHASHSEED`. C'est l'objet de ce module.

Le schema est GELE pour cette phase: aucun test ici ne le modifie, et aucun n'a eu a le faire.

PERIMETRE (sections numerotees comme la phase):

  1  rapport <-> schema: les deux points d'entree, jeu normal et synthetique
  2  rapport <-> contexte LLM: une seule version de contrat, 2.0
  3  contrat de message: cles verbatim, rendu deterministe, catalogue statique
  4  confidentialite: aucun identifiant client, nulle part, meme recalculable
  5  determinisme: memes octets a graine, a ordre d'insertion et a execution differents
  7  scenarios: recensement MATCH / KNOWN_GAP, sans toucher a la matrice
  8  matrice des versions: une divergence accidentelle doit echouer
  9  compatibilite 1.x: les chemins de lecture historiques restent intacts

La section 6 (CLI <-> persiste) vit dans `tests/persistence/test_contract_2_0_persisted.py`:
elle exige une base.

CORPUS. Le corpus de rapports reels a UN proprietaire: `tests/test_report_schema_2_0.py`
(phase 2). Ce module l'importe plutot que d'en tenir une seconde copie -- deux corpus
divergeraient, et c'est la faute AN-16 transposee dans les tests.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

import pytest

import llm_scenarios
import test_report_schema_2_0 as schema_contract
from mervio.analytics import messages
from mervio.analytics.pipeline import SourcePaths, run_analysis
from mervio.application.service import AnalysisRequest, analyze_dataset
from mervio.config import AnalyticsConfig
from mervio.llm import build_llm_context
from mervio.llm.context import serialize_context

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
#: jeu AVEC egalites volontaires, construit pour F-01: la cible la plus severe du determinisme
TIES = Path(__file__).resolve().parent / "fixtures" / "f01_ties"
TODAY = date(2026, 9, 14)

REPORT_CONTRACT = "2.0"
LLM_CONTRACT_VERSION = "2.0"

#: seul champ non deterministe autorise par D-058 (reserve 1)
NON_DETERMINISTIC = "generated_at"
#: valeurs de `_meta` que la couche application RENSEIGNE (K-5): elles distinguent legitimement
#: `analyze_dataset` de `run_analysis`, et elles seules
DELIVERY_META = ("analysis_label", "dataset_is_synthetic")


def _sample_paths(**sources) -> SourcePaths:
    names = {"shopify_orders": "shopify_orders.csv", "shopify_products": "shopify_products.csv",
             "stripe": "stripe_transactions.csv", "google_ads": "google_ads.csv"}
    return SourcePaths(**{key: str(SAMPLE / names[key]) for key in sources or names})


def _request_files(**sources) -> dict:
    paths = _sample_paths(**sources)
    return {name: value for name, value in vars(paths).items() if value}


@pytest.fixture(scope="module")
def reports() -> dict:
    """Le corpus de la phase 2, reconstruit par ses propres constructeurs."""
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    logging.disable(logging.WARNING)
    try:
        corpus = {name: llm_scenarios.SCENARIOS[name]()
                  for name in schema_contract.ENGINE_SCENARIOS}
        corpus.update(schema_contract._branch_reports())
    finally:
        logging.disable(logging.NOTSET)
    return corpus


@pytest.fixture(scope="module")
def report(reports) -> dict:
    return reports["revenue_decline"]


def _without_clock(report: dict) -> bytes:
    """Octets du rapport, horloge retiree, par le CODEC REEL du livrable.

    `persistence.codec.serialize_report` EST la serialisation de `report.json` (D-058): comparer
    une autre mise en forme mesurerait un codec que personne ne publie.
    """
    from mervio.persistence.codec import serialize_report
    copy = json.loads(json.dumps(report))
    copy["_meta"].pop(NON_DETERMINISTIC, None)
    return serialize_report(copy).encode("utf-8")


# == 1 -- rapport <-> schema, par point d'entree ==========================================

def test_1_the_engine_entry_point_produces_a_2_0_report():
    report = run_analysis(_sample_paths(), today=TODAY)
    schema_contract.assert_valid(report, "run_analysis")
    assert report["_meta"]["report_contract"] == REPORT_CONTRACT


@pytest.mark.parametrize("synthetic", [False, True], ids=["reel", "synthetique"])
def test_1_the_application_entry_point_produces_a_2_0_report(synthetic):
    """La phase 2 ne validait la couche application que sur un jeu DECLARE synthetique: le
    marqueur ne doit pas etre ce qui rend un rapport valide."""
    result = analyze_dataset(AnalysisRequest(**_request_files(), today=TODAY,
                                             label="phase-3", synthetic=synthetic))
    assert result.succeeded, result.error
    schema_contract.assert_valid(result.report, f"analyze_dataset(synthetic={synthetic})")
    assert result.report["_meta"]["report_contract"] == REPORT_CONTRACT
    assert result.report["_meta"]["dataset_is_synthetic"] is synthetic
    assert result.report["_meta"]["analysis_label"] == "phase-3"


@pytest.mark.parametrize("sources", [
    ("shopify_orders",),
    ("shopify_orders", "shopify_products"),
    ("shopify_orders", "google_ads"),
    ("shopify_orders", "shopify_products", "stripe"),
], ids=lambda sources: "+".join(name.replace("shopify_", "") for name in sources))
def test_1_partial_sources_still_produce_a_2_0_report(sources):
    """Une source absente change les dimensions exclues, pas le contrat."""
    files = _request_files(**{name: True for name in sources})
    result = analyze_dataset(AnalysisRequest(**files, today=TODAY, label="partiel"))
    assert result.succeeded, result.error
    schema_contract.assert_valid(result.report, "+".join(sources))
    assert result.report["_meta"]["report_contract"] == REPORT_CONTRACT


def test_1_the_two_entry_points_differ_only_by_the_delivery_metadata():
    """K-5 mesure jusqu'au bout: la couche application n'ajoute aucune cle ET ne touche aucune
    autre valeur. Mesure a f3717bd+phase1: le seul ecart est `_meta.analysis_label`.

    C'est l'invariant qui rend D-058 tenable des DEUX cotes: si l'application reformait le
    rapport, l'identite octet pour octet CLI/persiste ne serait qu'une coincidence.
    """
    engine = run_analysis(_sample_paths(), today=TODAY)
    application = analyze_dataset(AnalysisRequest(**_request_files(), today=TODAY,
                                                 label="phase-3")).report
    for name in (NON_DETERMINISTIC,) + DELIVERY_META:
        engine["_meta"].pop(name, None)
        application["_meta"].pop(name, None)
    assert list(engine) == list(application)
    differing = [key for key in engine
                 if json.dumps(engine[key], ensure_ascii=False, sort_keys=False)
                 != json.dumps(application[key], ensure_ascii=False, sort_keys=False)]
    assert not differing, f"la couche application modifie: {differing}"


def test_1_every_report_of_the_corpus_declares_the_contract(reports):
    for name, report in reports.items():
        assert report["_meta"]["report_contract"] == REPORT_CONTRACT, name


# == 2 -- rapport <-> contexte LLM ========================================================

def test_2_the_report_and_the_llm_context_declare_the_same_contract_version(reports):
    """Une seule rupture de contrat en 004.5: le rapport et le contexte annoncent 2.0 ENSEMBLE.

    Deux versions distinctes rendraient intracable une explication deja produite -- c'est la
    raison d'etre de `llm/contract.py`.
    """
    for name, report in reports.items():
        context = build_llm_context(report)
        assert report["_meta"]["report_contract"] == REPORT_CONTRACT, name
        assert context["contract_version"] == LLM_CONTRACT_VERSION, name


def test_2_the_context_builds_and_serialises_for_every_report_of_the_corpus(reports):
    """Le contexte est construit DEPUIS le rapport: toute forme que le contrat 2.0 autorise doit
    le traverser. Les branches degenerees comptent -- score non calculable, decomposition
    impossible, fraicheur inconnue, devise absente."""
    for name, report in reports.items():
        context = build_llm_context(report)
        payload = serialize_context(context)
        assert payload.startswith("{") and payload.endswith("}"), name
        assert len(payload) <= 64_000, name


#: natures de preuve que le contexte derive du rapport 2.0 (`llm/context.py`)
CONTEXT_EVIDENCE_KINDS = {"finding_fact", "anomaly", "root_cause_factor", "product_contribution",
                          "campaign_contribution"}


def test_2_the_context_consumes_the_blocks_the_2_0_contract_publishes(reports, report):
    """Garde d'integration: ce que le contexte expose vient du rapport, valeur par valeur.

    Ce n'est pas une reformulation du contexte: c'est l'egalite entre ce que le contrat 2.0
    publie et ce que le modele recoit. Un bloc renomme d'un cote et pas de l'autre casse ici.
    """
    context = build_llm_context(report)
    assert context["currency"] == report["_meta"]["currency"]
    assert context["engine_version"] == report["_meta"]["engine_version"]
    assert context["analysis_period"] == report["period"]
    assert context["business_health"]["score"] == report["business_health_score"]["score"]
    assert context["business_health"]["interpretation"] == \
        report["business_health_score"]["interpretation"]
    for field in ("status", "delay_days", "threshold_days", "message_key"):
        assert context["data_freshness"][field] == report["data_freshness"][field], field
    observed = {item["kind"] for item in context["evidence"]}
    assert observed <= CONTEXT_EVIDENCE_KINDS, observed - CONTEXT_EVIDENCE_KINDS
    assert {"anomaly", "root_cause_factor", "finding_fact"} <= observed
    corpus = {item["kind"] for one in reports.values()
              for item in build_llm_context(one)["evidence"]}
    assert corpus == CONTEXT_EVIDENCE_KINDS, corpus ^ CONTEXT_EVIDENCE_KINDS


def test_2_the_prompt_and_response_versions_are_untouched_by_the_report_contract():
    """Seul le contrat de CONTEXTE suit le rapport. Durcir le prompt serait un changement non
    demande (hors perimetre 004.5)."""
    from mervio.llm import contract
    assert contract.PROMPT_VERSION == "1.0"
    assert contract.RESPONSE_SCHEMA_VERSION == "1.0"
    assert contract.CONTRACT_VERSION == LLM_CONTRACT_VERSION


# == 3 -- contrat de message (AN-19 / D-085) ==============================================

#: cle de message: technique et stable. Meme expression que `llm/context._MESSAGE_KEY_RE`, dont
#: la stricte egalite rapport <-> contexte depend.
MESSAGE_KEY_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
#: familles de cles publiees par le rapport et portees VERBATIM par le contexte
KEY_FIELDS = ("message_key", "fact_key", "hypothesis_key", "recommendation_key",
              "estimated_impact_basis_key", "criterion_key", "method_key", "observed_fact_key")


def _keys_of(node, out: set) -> set:
    if isinstance(node, dict):
        for name, value in node.items():
            if isinstance(value, str) and (name in KEY_FIELDS or name == "limitation_keys"):
                out.add(value)
            if name == "limitation_keys" and isinstance(value, list):
                out.update(item for item in value if isinstance(item, str))
            _keys_of(value, out)
    elif isinstance(node, list):
        for value in node:
            _keys_of(value, out)
    return out


def test_3_every_message_key_of_the_report_is_technical_and_never_built_from_data(reports):
    """Une cle construite depuis une donnee ne serait ni relisible, ni traduisible, ni
    testable: D-085 interdit la f-string, et `context._message_key` REFUSE la cle qui ne
    satisfait pas cette forme."""
    for name, report in reports.items():
        for key in _keys_of(report, set()):
            assert MESSAGE_KEY_RE.match(key), f"{name}: cle non technique {key!r}"


def test_3_the_context_carries_the_report_keys_verbatim(reports):
    """AN-19 exige l'egalite rapport <-> contexte. `_message_key` transmet VERBATIM et refuse
    plutot que de normaliser: une cle normalisee ne serait plus celle du rapport."""
    for name, report in reports.items():
        in_report = _keys_of(report, set())
        in_context = _keys_of(build_llm_context(report), set())
        unknown = sorted(in_context - in_report)
        assert not unknown, f"{name}: le contexte invente {unknown}"


def test_3_the_evidence_keys_stay_pure_identifiers_without_a_catalogue_entry(reports):
    """D-2: les cles `evidence.*` n'ont PAS d'entree de catalogue. Une preuve se rend depuis ses
    champs types -- `metric_key`, `unit`, valeurs -- et lui donner un gabarit reintroduirait la
    phrase comme source de verite.

    Le test est bilateral: elles sont toutes prefixees `evidence.`, et aucune n'est au catalogue.
    """
    seen = set()
    for name, report in reports.items():
        for block in _evidence_blocks(report):
            key = block["message_key"]
            assert key.startswith("evidence."), f"{name}: {key!r}"
            assert key not in messages.MESSAGES, f"{name}: {key!r} a une entree de catalogue"
            seen.add(key)
    assert len(seen) >= 14, f"seulement {len(seen)} cles de preuve observees"


def _evidence_blocks(report: dict):
    for section in ("critical_issues", "warnings", "setup", "opportunities"):
        for finding in report[section]:
            yield from finding["evidence"]
    for anomaly in report["anomalies"]:
        yield from anomaly["evidence"]
    for dimension in report["business_health_score"]["dimensions"]:
        yield from dimension["evidence"]


def test_3_the_rendering_is_deterministic(reports):
    """Le rendu ne CALCULE rien: deux appels sur les memes parametres donnent le meme texte, et
    ce texte est celui que le rapport publie."""
    checked = 0
    for name, report in reports.items():
        for section in ("critical_issues", "warnings", "setup", "opportunities"):
            for finding in report[section]:
                for field in ("fact", "hypothesis", "recommendation", "estimated_impact_basis"):
                    key, params = finding[field + "_key"], finding[field + "_params"]
                    if key is None:
                        continue
                    first = messages.render(key, params)
                    assert first == messages.render(key, params), f"{name}: {key} non deterministe"
                    assert first == finding[field], f"{name}: {key} ne rend pas son texte"
                    checked += 1
    assert checked >= 100, f"seulement {checked} messages rendus"


def test_3_the_catalogue_is_static_and_cannot_be_extended_at_runtime():
    """B-5: la staticite n'etait garantie qu'a la LECTURE du source; `MESSAGES["x"] = ...`
    fonctionnait a l'execution. La vue est non mutable."""
    with pytest.raises(TypeError):
        messages.MESSAGES["finding.injectee.fact"] = "texte injecte"  # type: ignore[index]
    with pytest.raises(messages.MessageError):
        messages.render("finding.inconnue.fact")


def test_3_a_missing_parameter_never_produces_a_hole_in_a_text():
    """Aucun repli: un texte a trou ne doit jamais circuler. C'est l'invariant que le schema
    verrouille par ses conditionnels `*_params`, et que le catalogue refuse de contourner."""
    with pytest.raises(messages.MessageError):
        messages.render("finding.revenue_decline.fact", {"period": "2026-W37"})
    assert messages.render("finding.revenue_decline.fact",
                           {"period": "2026-W37", "delta_pct_abs": 0.187, "extra": 1})


#: champs d'un constat qui portent du TEXTE RENDU; tous les autres sont des identifiants
RENDERED_FIELDS = ("fact", "hypothesis", "recommendation", "estimated_impact_basis")
#: identifiant publie dans un constat: technique, jamais une phrase
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:\-]*$")
#: SEULE exception mesuree, et elle est NOMMEE: `profitability.components["shipping_cost"]`
#: declare `source = "non connecte"` (`analytics/profitability.py`), un libelle francais dans un
#: emplacement d'identifiant. Il remonte dans `evidence[].source` du constat `setup`. Le corriger
#: changerait les octets de tout rapport et le contexte LLM: hors perimetre de cette phase, donc
#: documente ici plutot que masque par une expression plus permissive.
UNCONNECTED_SOURCE = "non connecte"


def test_3_a_finding_carries_no_unexpected_business_text(reports):
    """Hors des quatre textes RENDUS et des parametres de message, toute chaine d'un constat est
    un identifiant. C'est ce qui garantit qu'aucune prose n'echappe au catalogue.

    Les PARAMETRES sont exclus a dessein: ils portent des valeurs de source (titre d'article,
    nom de campagne), donnees non fiables que le contrat publie telles quelles.
    """
    offenders = []
    for name, report in reports.items():
        for section in ("critical_issues", "warnings", "setup", "opportunities"):
            for finding in report[section]:
                for field, value in finding.items():
                    if field in RENDERED_FIELDS or field.endswith("_params"):
                        continue
                    if isinstance(value, str) and not IDENTIFIER_RE.match(value):
                        offenders.append(f"{name}/{section}: {field} = {value[:60]!r}")
                for block in finding["evidence"]:
                    for field, value in block.items():
                        if not isinstance(value, str) or IDENTIFIER_RE.match(value):
                            continue
                        if field == "source" and value == UNCONNECTED_SOURCE:
                            continue
                        offenders.append(f"{name}/{section}/evidence: {field} = {value[:60]!r}")
    assert not offenders, "texte libre dans un constat:\n" + "\n".join(offenders[:10])


def test_3_the_only_non_technical_source_is_the_unconnected_shipping_cost(reports):
    """Anomalie DECOUVERTE par cette phase et NON corrigee: `source` est un emplacement
    d'identifiant, et une seule valeur y est une phrase francaise.

    `analytics/profitability.py` declare `CostComponent("shipping_cost", ..., "non connecte", ...)`;
    la valeur remonte dans `evidence[].source` du constat `setup` et dans
    `profitability.components.shipping_cost.source`. La corriger changerait les octets de tout
    rapport et les goldens: hors perimetre. Ce test l'EPINGLE pour qu'une seconde ne s'y ajoute
    pas en silence.
    """
    non_technical = set()
    for report in reports.values():
        for block in _evidence_blocks(report):
            if block["source"] and not IDENTIFIER_RE.match(block["source"]):
                non_technical.add(block["source"])
        for component in report["profitability"]["components"].values():
            if not IDENTIFIER_RE.match(component["source"]):
                non_technical.add(component["source"])
    assert non_technical == {UNCONNECTED_SOURCE}, non_technical


# == 4 -- confidentialite (AN-17 / D-056) =================================================

#: designations PRODUITES PAR MERVIO. Aucune n'a le droit d'exister dans un rapport 2.0, ou que
#: ce soit: ce sont elles qui faisaient du rapport un artefact non effacable (AN-17 / D-056).
MERVIO_DESIGNATIONS = (
    re.compile(r"cust_[0-9a-f]{6,}"),           # pseudonyme de rapport 1.x
    re.compile(r"\b(?:c1|g1):[0-9a-f]{32}\b"),  # reference a cle persistee (D-053)
    re.compile(r"redacted:[0-9a-f-]{36}"),      # tombstone d'effacement (D-056)
)
#: e-mail. Traite A PART: un e-mail peut legitimement figurer dans une valeur de SOURCE que le
#: contrat publie verbatim (titre d'article, nom de campagne). Ce n'est pas Mervio qui designe un
#: client, c'est le marchand qui a ecrit une adresse dans son catalogue. Le test ci-dessous borne
#: donc les emplacements ou un e-mail peut apparaitre, au lieu de l'interdire partout -- ce qui
#: aurait oblige a exclure le scenario hostile, c'est-a-dire a ne pas le mesurer.
EMAIL_SHAPE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
#: champs qui portent une valeur de source, mesures sur le corpus (`malicious_source_text`)
SOURCE_TEXT_FIELDS = {"title", "label", "fact"}
#: emplacements du contrat 2.0 ou un identifiant aurait pu se glisser
PRIVACY_SURFACES = ("customers", "critical_issues", "warnings", "setup", "opportunities",
                    "anomalies", "root_causes", "recommendations", "products", "marketing",
                    "data_quality", "limitations", "executive_summary")


def _designations(text: str) -> list:
    return [match.group(0) for pattern in MERVIO_DESIGNATIONS for match in pattern.finditer(text)]


def _email_fields(node, field: str = "") -> set:
    """Noms de champ ou un e-mail apparait, quelle que soit la profondeur."""
    found = set()
    if isinstance(node, dict):
        for name, value in node.items():
            found |= _email_fields(value, name)
    elif isinstance(node, list):
        for value in node:
            found |= _email_fields(value, field)
    elif isinstance(node, str) and EMAIL_SHAPE.search(node):
        found.add(field)
    return found


@pytest.mark.parametrize("surface", PRIVACY_SURFACES)
def test_4_no_surface_of_the_contract_carries_a_customer_designation(reports, surface):
    """AN-17 bloc par bloc, sur les trente rapports du corpus -- y compris celui dont les titres
    d'article et les noms de campagne sont hostiles et porteurs de donnees personnelles."""
    for name, report in reports.items():
        found = _designations(json.dumps(report[surface], ensure_ascii=False))
        assert not found, f"{name}/{surface}: {found[:3]}"


def test_4_no_message_parameter_carries_a_customer_designation(reports):
    """Les parametres de message sont le seul endroit du contrat ou une valeur de SOURCE circule
    telle quelle (titre d'article, nom de campagne). Un identifiant client ne doit pas s'y
    trouver, et c'est la surface la plus facile a oublier."""
    checked = 0
    for name, report in reports.items():
        for section in ("critical_issues", "warnings", "setup", "opportunities"):
            for finding in report[section]:
                for field in RENDERED_FIELDS:
                    params = finding[field + "_params"]
                    if params is None:
                        continue
                    found = _designations(json.dumps(params, ensure_ascii=False))
                    assert not found, f"{name}/{field}_params: {found[:3]}"
                    checked += 1
    assert checked >= 100, f"seulement {checked} jeux de parametres verifies"


def test_4_no_evidence_block_carries_a_customer_designation(reports):
    checked = 0
    for name, report in reports.items():
        for block in _evidence_blocks(report):
            found = _designations(json.dumps(block, ensure_ascii=False))
            assert not found, f"{name}: {found[:3]}"
            checked += 1
    assert checked >= 200, f"seulement {checked} preuves verifiees"


def test_4_the_1x_pseudonym_is_not_recomputable_from_the_report():
    """La propriete qui faisait de `cust_<...>` un IDENTIFIANT: il etait RECALCULABLE par qui
    detient la cle d'identite de l'organisation (le SaaS lui-meme), depuis une simple liste
    d'e-mails. Le test refait ce calcul et verifie que le resultat n'est nulle part.

    Ce n'est pas une garde sur un nom de cle: c'est la garde sur la VALEUR.
    """
    from conftest import make_dataset, make_order
    from mervio.analytics.pipeline import analyze_loaded_dataset
    from mervio.identity import CustomerIdentity

    identity = CustomerIdentity(bytes.fromhex("ab" * 32))
    emails = [f"client{index}@example.com" for index in range(4)]
    refs = [identity.ref_email(email) for email in emails]
    # la derniere commande tombe le DERNIER jour de 2026-W37, sinon `last_complete_period` recule
    # d'une semaine et la fenetre analysee est vide
    days = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 10), date(2026, 9, 13))
    orders = [make_order(f"#{index}", days[index], ref, [("A", 1, 100.0 * (4 - index))])
              for index, ref in enumerate(refs)]
    dataset = make_dataset(orders=orders)
    dataset.quality.set_field("customer_identity", covered=len(orders), total=len(orders))
    report = analyze_loaded_dataset(dataset, today=date(2026, 9, 21))
    schema_contract.assert_valid(report, "jeu a identite explicite")

    payload = json.dumps(report, ensure_ascii=False)
    for email, ref in zip(emails, refs):
        pseudonym = "cust_" + hashlib.sha256(ref.encode("utf-8")).hexdigest()[:12]
        assert pseudonym not in payload, f"pseudonyme 1.x recalculable present: {pseudonym}"
        assert ref not in payload and email not in payload
    assert _designations(payload) == []
    assert report["customers"]["top_customers"], "le bloc doit rester publie, decrit sans designer"


def test_4_an_e_mail_only_reaches_the_slots_that_carry_source_text(reports):
    """Le scenario `malicious_source_text` ecrit une adresse DANS un titre d'article. Le contrat
    la republie telle quelle, et c'est voulu: le rapport ne reecrit pas la donnee du marchand.

    La propriete a tenir n'est donc pas << aucun e-mail >> mais << aucun e-mail ailleurs que dans
    une valeur de source >>. Mesure sur le corpus: `products[].title`, le parametre `title` d'un
    constat et son texte rendu, et `product_contributors[].label` -- tous les trois portent le
    MEME titre d'article. Aucun bloc client, aucune preuve, aucune cle.
    """
    hostile = [name for name, report in reports.items()
               if _email_fields(report)]
    assert hostile == ["malicious_source_text"], hostile
    for name in hostile:
        fields = _email_fields(reports[name])
        assert fields <= SOURCE_TEXT_FIELDS, fields - SOURCE_TEXT_FIELDS
        assert not _email_fields(reports[name]["customers"])
        assert not _email_fields(reports[name]["data_quality"])
        for block in _evidence_blocks(reports[name]):
            assert not _email_fields(block), block


def test_4_the_anonymous_aggregates_are_preserved(reports):
    """Un agregat n'est pas un identifiant: ce qui mesure une POPULATION reste publie."""
    for name, report in reports.items():
        customers = report["customers"]
        assert set(customers) == {"total_active", "new_customers", "repeat_purchase_rate",
                                  "concentration_top5", "top_customers"}, name
        for entry in customers["top_customers"]:
            assert set(entry) == {"rank", "orders", "revenue_share"}, name
        assert isinstance(customers["total_active"], int), name


# == 5 -- determinisme (D-058 / F-01) =====================================================

#: cle d'identite FIXE: une seule variable bouge a la fois dans cette section
IDENTITY_HEX = "f0" * 32
SEEDS = ("0", "1", "42")
#: CSV dont l'ordre des lignes de donnees est permute, et ce que la permutation deplace
PERMUTATIONS = {
    "base": (),
    "products": ("shopify_products.csv",),
    "campaigns": ("google_ads.csv",),
    "products+campaigns": ("shopify_products.csv", "google_ads.csv"),
    "orders": ("shopify_orders.csv",),
}
#: les trois fichiers du jeu a egalites
TIE_FILES = ("shopify_orders.csv", "shopify_products.csv", "google_ads.csv")

#: analyse le repertoire fourni, retire le seul champ non deterministe autorise et serialise.
#: Meme codec que `reporting.writers` (D-058): c'est cet octet-la qui circule.
_SUBPROCESS = """
import json, sys
from datetime import date
from mervio.analytics.pipeline import SourcePaths, run_analysis
from mervio.identity import CustomerIdentity
source, out = sys.argv[1], sys.argv[2]
report = run_analysis(
    SourcePaths(shopify_orders=source + "/shopify_orders.csv",
                shopify_products=source + "/shopify_products.csv",
                google_ads=source + "/google_ads.csv"),
    today=date(2026, 9, 14),
    identity=CustomerIdentity(bytes.fromhex(%r)),
)
report["_meta"].pop("generated_at", None)
from mervio.persistence.codec import serialize_report
with open(out, "w", encoding="utf-8") as fh:
    fh.write(serialize_report(report))
""" % IDENTITY_HEX


def _permuted(source: Path, target: Path, files, seed: int) -> Path:
    """Copie du jeu dont l'ordre des lignes de DONNEES est permute pour `files`.

    Permutation a graine FIXE: le test doit etre reproductible, et c'est l'ordre d'insertion qui
    est la variable mesuree, pas le hasard.
    """
    target.mkdir(parents=True, exist_ok=True)
    generator = random.Random(seed)
    for name in TIE_FILES:
        lines = (source / name).read_text(encoding="utf-8").splitlines(keepends=True)
        header, body = lines[0], lines[1:]
        if name in files:
            body = list(body)
            generator.shuffle(body)
        (target / name).write_text(header + "".join(body), encoding="utf-8")
    return target


def _report_bytes(source: Path, out: Path, seed: str) -> bytes:
    environment = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT / 'tests'}")
    subprocess.run([sys.executable, "-c", _SUBPROCESS, str(source), str(out)],
                   cwd=ROOT, env=environment, capture_output=True, text=True,
                   timeout=300, check=True)
    return out.read_bytes()


@pytest.fixture(scope="module")
def permuted_sets(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("contract-2-0-permutations")
    return {name: _permuted(TIES, root / name.replace("+", "_"), files, index)
            for index, (name, files) in enumerate(PERMUTATIONS.items())}


@pytest.fixture(scope="module")
def permuted_bytes(permuted_sets, tmp_path_factory) -> dict:
    """Un rapport par (permutation, graine), chacun dans son propre processus."""
    out = tmp_path_factory.mktemp("contract-2-0-bytes")
    return {(name, seed): _report_bytes(source, out / f"{name}-{seed}.json".replace("+", "_"), seed)
            for name, source in permuted_sets.items() for seed in SEEDS}


def test_5_the_permutations_really_change_the_input_bytes(permuted_sets):
    """Garde: sans elle, les tests de permutation pourraient comparer cinq fois le meme jeu et
    appeler cela une preuve d'independance a l'ordre d'insertion."""
    base = {name: (permuted_sets["base"] / name).read_bytes() for name in TIE_FILES}
    for label, files in PERMUTATIONS.items():
        if not files:
            continue
        for name in TIE_FILES:
            moved = (permuted_sets[label] / name).read_bytes() != base[name]
            assert moved == (name in files), f"{label}/{name}: permutation inattendue"
        for name in files:
            permuted = (permuted_sets[label] / name).read_text(encoding="utf-8").splitlines()
            original = (permuted_sets["base"] / name).read_text(encoding="utf-8").splitlines()
            assert sorted(permuted) == sorted(original), f"{label}/{name}: lignes perdues"
            assert permuted[0] == original[0], f"{label}/{name}: en-tete deplace"


def test_5_the_tie_fixture_still_contains_the_ties_it_was_built_for(permuted_bytes):
    """Le jeu doit garder ses egalites: c'est elles qui rendent l'ordre ambigu, donc le test
    severe. Sans egalite, tout tri paraitrait total."""
    report = json.loads(permuted_bytes[("base", SEEDS[0])])
    deltas = [row["conversions_delta"]
              for row in report["root_causes"][0]["campaign_contributors"]]
    assert len(deltas) != len(set(deltas)), f"aucune egalite dans {deltas}"


def test_5_the_report_bytes_are_identical_across_seeds_and_insertion_orders(permuted_bytes):
    """D-058 et F-01 dans une seule propriete: memes octets de source -- a l'ordre des lignes
    pres -- et meme rapport, quelle que soit `PYTHONHASHSEED`.

    L'ordre d'insertion des articles et des campagnes est la variable que K-4 a rendue
    inoffensive; la graine est celle que F-01 a rendue inoffensive. Les deux sont ici mesurees
    ENSEMBLE, sur le jeu a egalites.
    """
    distinct = set(permuted_bytes.values())
    assert len(distinct) == 1, (
        f"{len(distinct)} rapports distincts sur {len(permuted_bytes)} executions: "
        + json.dumps({f"{name}/{seed}": hashlib.sha256(value).hexdigest()[:12]
                      for (name, seed), value in permuted_bytes.items()}, indent=1))


def test_5_the_permuted_reports_still_satisfy_the_contract(permuted_bytes):
    """Un rapport deterministe mais hors contrat ne vaudrait rien: les deux proprietes
    s'asserent ensemble."""
    report = json.loads(permuted_bytes[("products+campaigns", SEEDS[-1])])
    report["_meta"][NON_DETERMINISTIC] = "2026-09-14T06:30:00+00:00"
    schema_contract.assert_valid(report, "rapport issu d'un jeu permute")


def test_5_two_executions_differ_only_by_the_clock():
    """La liste d'exclusion de D-058 est EXACTE: deux executions successives ne diffeerent que
    par `_meta.generated_at`. Si un second champ devenait temporel, ce test le nommerait."""
    first = run_analysis(_sample_paths(), today=TODAY)
    second = run_analysis(_sample_paths(), today=TODAY)
    assert _without_clock(first) == _without_clock(second)
    differing = [key for key in first["_meta"] if first["_meta"][key] != second["_meta"][key]]
    assert differing in ([], [NON_DETERMINISTIC]), differing


def test_5_the_persisted_codec_is_the_cli_deliverable_byte_for_byte(tmp_path):
    """Le codec de persistance et le livrable CLI sont le MEME serialiseur (D-058). Sans cela,
    l'identite octet pour octet CLI/persiste ne serait qu'une coincidence de mise en forme.

    La comparaison porte sur le FICHIER ecrit par `reporting.writers`, pas sur une mise en forme
    reecrite ici: un test qui reproduirait le codec ne mesurerait que lui-meme.
    """
    from mervio.persistence.codec import serialize_report
    from mervio.reporting.writers import REPORT_JSON, write_outputs
    result = analyze_dataset(AnalysisRequest(**_request_files(), today=TODAY, label="codec"))
    assert result.succeeded, result.error
    write_outputs(result, tmp_path / "cli")
    assert (tmp_path / "cli" / REPORT_JSON).read_bytes() == \
        serialize_report(result.report).encode("utf-8")


# == 7 -- recensement des scenarios (D-076) ===============================================

#: les CINQ scenarios entierement couverts, ratifies par D-076 = A. L'enonce precedent en
#: annoncait sept; la mesure apres implementation honnete d'AN-02 en a donne cinq, et trois
#: scenarios ont ete retrogrades en KNOWN_GAP avec justification ecrite.
FULLY_COVERED = ("AOV_decline", "healthy_store", "refund_spike", "revenue_drop", "traffic_drop")
#: recensement MESURE de la matrice. Il n'est PAS modifie par cette phase: il est epingle, pour
#: qu'un KNOWN_GAP ne devienne pas un MATCH -- ni l'inverse -- sans decision.
EXPECTED_CENSUS = {"scenarios": 17, "expectations": 61, "match": 44, "known_gap": 17}


def _census() -> dict:
    from mervio.synthetic import SCENARIOS
    statuses = [expectation.status for scenario in SCENARIOS.values()
                for expectation in scenario.expectations]
    return {"scenarios": len(SCENARIOS), "expectations": len(statuses),
            "match": statuses.count("MATCH"), "known_gap": statuses.count("KNOWN_GAP")}


def test_7_the_scenario_matrix_census_is_unchanged():
    """La matrice est un ARBITRAGE ratifie, pas un reglage. Ce test ne la verifie pas contre le
    moteur -- `tests/test_synthetic_scenarios.py` le fait, a 20 000 commandes -- il verifie
    qu'elle n'a pas bouge."""
    assert _census() == EXPECTED_CENSUS


def test_7_exactly_the_five_ratified_scenarios_are_fully_covered():
    from mervio.synthetic import SCENARIOS
    covered = tuple(sorted(name for name, scenario in SCENARIOS.items()
                           if all(e.status == "MATCH" for e in scenario.expectations)))
    assert covered == tuple(sorted(FULLY_COVERED)), covered


def test_7_every_known_gap_carries_a_written_justification():
    """Un KNOWN_GAP sans note serait un echec deguise en decision."""
    from mervio.synthetic import SCENARIOS
    silent = [f"{name}/{expectation.check}" for name, scenario in sorted(SCENARIOS.items())
              for expectation in scenario.expectations
              if expectation.status == "KNOWN_GAP" and not expectation.note.strip()]
    assert not silent, silent


def test_7_the_declared_statuses_are_only_match_or_known_gap():
    from mervio.synthetic import SCENARIOS
    statuses = {expectation.status for scenario in SCENARIOS.values()
                for expectation in scenario.expectations}
    assert statuses == {"MATCH", "KNOWN_GAP"}, statuses


# == 8 -- matrice des versions ============================================================

#: etat MESURE des versions du depot. Une divergence accidentelle doit echouer ICI, en un seul
#: endroit, plutot que se manifester dans un golden ou dans un rapport de production.
#:
#: ENGINE_VERSION: `0.2.0` depuis la phase 3.1, comme la roadmap 004.5 le ratifie. Le moteur du
#: contrat 2.0 n'est plus celui du contrat 1.x: le numero le dit. La version de distribution
#: (`pyproject.toml`) la suit, et `test_8_the_distribution_version_follows_the_engine_version`
#: refuse qu'elles se desynchronisent.
#:
#: GENERATOR_VERSION (`synthetic/generator.py`) vaut encore "0.1.0" et n'est PAS concernee: elle
#: versionne le generateur de donnees de test, pas le moteur analytique.
VERSION_MATRIX = {
    "ENGINE_VERSION": "0.2.0",
    "REPORT_CONTRACT": "2.0",
    "CONTRACT_VERSION": "2.0",
    "PROMPT_VERSION": "1.0",
    "RESPONSE_SCHEMA_VERSION": "1.0",
    "NORMALIZATION_VERSION": "mervio-normalization/2",
    "CSV_SCHEMA_VERSION": "shopify_orders_csv/1,shopify_products_csv/1,stripe_csv/1,google_ads_csv/1",
    "QUALITY_DOCUMENT_VERSION": 1,
}


def _declared_versions() -> dict:
    from mervio import config
    from mervio.llm import contract
    from mervio.persistence import codec, snapshots
    return {
        "ENGINE_VERSION": config.ENGINE_VERSION,
        "REPORT_CONTRACT": config.REPORT_CONTRACT,
        "CONTRACT_VERSION": contract.CONTRACT_VERSION,
        "PROMPT_VERSION": contract.PROMPT_VERSION,
        "RESPONSE_SCHEMA_VERSION": contract.RESPONSE_SCHEMA_VERSION,
        "NORMALIZATION_VERSION": snapshots.NORMALIZATION_VERSION,
        "CSV_SCHEMA_VERSION": snapshots.CSV_SCHEMA_VERSION,
        "QUALITY_DOCUMENT_VERSION": codec.QUALITY_DOCUMENT_VERSION,
    }


def test_8_the_version_matrix_is_exactly_the_declared_one():
    """Un seul test pour huit versions: une divergence accidentelle n'a plus a etre devinee."""
    assert _declared_versions() == VERSION_MATRIX


@pytest.mark.parametrize("name", sorted(VERSION_MATRIX))
def test_8_each_version_is_pinned_individually(name):
    """Parametre pour que l'echec NOMME la version qui a bouge."""
    assert _declared_versions()[name] == VERSION_MATRIX[name]


def test_8_the_distribution_version_follows_the_engine_version():
    """`mervio.__version__` EST `ENGINE_VERSION`; la version de distribution doit la suivre.
    Les desynchroniser ferait livrer une roue qui annonce une autre version que son moteur."""
    import mervio
    from mervio.config import ENGINE_VERSION
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert mervio.__version__ == ENGINE_VERSION
    assert project["version"] == ENGINE_VERSION


def test_8_the_report_contract_governs_the_schema_file_and_the_published_value():
    """Trois endroits, une seule version: la constante, le CHEMIN du schema, et la valeur que le
    schema impose."""
    from mervio.config import REPORT_CONTRACT as declared
    schema = schema_contract.load_schema()
    assert schema_contract.SCHEMA_PATH.name == f"{declared}.json"
    assert schema["$defs"]["meta"]["properties"]["report_contract"]["const"] == declared
    assert schema["$id"].endswith(declared)


def test_8_the_llm_contract_and_the_report_contract_move_together():
    """K-11: le contexte a suivi le rapport en 2.0. Le prompt et le schema de reponse n'ont pas
    change et ne doivent pas etre entraines."""
    from mervio.config import REPORT_CONTRACT as report_contract
    from mervio.llm.contract import CONTRACT_VERSION, PROMPT_VERSION, RESPONSE_SCHEMA_VERSION
    assert CONTRACT_VERSION == report_contract
    assert PROMPT_VERSION != CONTRACT_VERSION and RESPONSE_SCHEMA_VERSION != CONTRACT_VERSION


# == 9 -- compatibilite 1.x (ND-04) =======================================================

def test_9_the_schema_is_a_test_artefact_and_gates_no_read_path():
    """Un rapport 1.x deja persiste ne doit JAMAIS etre rejete a la lecture.

    La garantie n'est pas une intention: aucun module de `src/` ne connait `jsonschema` ni le
    fichier de schema, et `jsonschema` n'est pas une dependance runtime (K-1). Le contrat 2.0 ne
    peut donc pas se mettre en travers d'une lecture 1.x.
    """
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in ("jsonschema", "schemas/report", "2.0.json"):
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert not offenders, offenders
    assert "jsonschema" not in (ROOT / "requirements-runtime.lock").read_text(encoding="utf-8")


def _as_1x(produced: dict) -> dict:
    """Forme 1.x d'un rapport: ni version de contrat, le bloc client d'AVANT AN-17 (pseudonyme
    compris), et le bareme de sante en `repr()` Python comme avant K-6.

    Construite depuis un rapport REEL: un 1.x fabrique de toutes pieces ne prouverait rien des
    chemins de lecture.
    """
    legacy = json.loads(json.dumps(produced))
    legacy["_meta"].pop("report_contract")
    legacy["customers"]["top_customers"] = [
        {"customer_id": "cust_0123456789ab", "orders": 3, "revenue": 900.0},
        {"customer_id": "cust_fedcba987654", "orders": 2, "revenue": 400.0},
    ]
    for dimension in legacy["business_health_score"]["dimensions"]:
        dimension["thresholds"] = "((-0.25, 0.0), (-0.1, 40.0), (0.0, 70.0), (0.1, 100.0))"
    return legacy


def test_9_a_1x_report_stays_readable_by_every_consumer(report):
    """Les deux consommateurs doivent lire un 1.x sans lever: l'un rend un texte pour le
    marchand, l'autre construit le contexte du modele. ND-04: on ne reecrit pas les anciens
    rapports, donc on doit encore savoir les lire."""
    from mervio.reporting.executive import render_executive_report
    legacy = _as_1x(report)
    assert "report_contract" not in legacy["_meta"]
    rendered = render_executive_report(legacy)
    assert "CUSTOMER HEALTH" in rendered
    assert "cust_" not in rendered, "le pseudonyme 1.x n'a jamais ete rendu"
    context = build_llm_context(legacy)
    assert context["contract_version"] == LLM_CONTRACT_VERSION
    assert "customers" not in context, "le bloc client n'atteint jamais le modele"
    assert "cust_" not in serialize_context(context)


def test_9_the_minimal_1x_context_fixture_still_builds_a_context():
    """La plus petite entree acceptee par la couche LLM -- quatre cles, sans version de contrat --
    doit continuer de produire un contexte: c'est la borne basse de la lecture 1.x."""
    minimal = llm_scenarios.SCENARIOS["minimal_report"]()
    assert "report_contract" not in minimal["_meta"]
    context = build_llm_context(minimal)
    assert context["contract_version"] == LLM_CONTRACT_VERSION
    assert context["engine_version"] == minimal["_meta"]["engine_version"]


def test_9_a_1x_report_is_not_a_2_0_report(report):
    """La frontiere est nette dans les deux sens: un 1.x reste lisible et n'est pas conforme."""
    schema_contract.assert_rejected(_as_1x(report), "rapport 1.x complet")
    schema_contract.assert_rejected(llm_scenarios.SCENARIOS["minimal_report"](),
                                    "contexte minimal 1.x")


def test_9_a_2_0_report_carrying_a_1x_pseudonym_is_rejected():
    """Ce qui est interdit, c'est de PRODUIRE un 1.x sous l'etiquette 2.0."""
    report = run_analysis(_sample_paths(), today=TODAY)
    schema_contract.assert_valid(report)
    report["customers"]["top_customers"][0]["customer_id"] = "cust_7f3a91b2c4d5"
    schema_contract.assert_rejected(report, "rapport 2.0 portant un pseudonyme 1.x")


# -- 5 bis: egalites CONSTRUITES, mesurees sur les octets ---------------------------------
#: SKU et campagnes volontairement inseres dans le mauvais ordre, a valeur EXACTEMENT egale.
#: `data/sample` et `tests/fixtures/f01_ties` n'ont aucune egalite de CA article ni de depense:
#: sans ce jeu, la permutation ne mesurerait pas la cle totale de K-4.
TIED_SKUS = ("ZZ-second", "AA-first")
TIED_CAMPAIGNS = ("c-zz", "c-aa")
TIE_DAY, TIE_ANALYSIS = date(2026, 9, 13), date(2026, 9, 21)


def _tied_products_report(order: tuple) -> dict:
    from conftest import make_dataset, make_order
    from mervio.analytics.pipeline import analyze_loaded_dataset
    from mervio.domain.models import Product
    orders = [make_order("#big", TIE_DAY, "c1:" + "0" * 32, [("BIG", 2, 500.0)])]
    for index, sku in enumerate(order):
        orders.append(make_order(f"#{index}", TIE_DAY, f"c1:{index:02d}" + "0" * 30,
                                 [(sku, 1, 100.0)]))
    products = {sku: Product(sku, sku, sku, unit_cogs=10.0) for sku in order}
    products["BIG"] = Product("BIG", "BIG", "BIG", unit_cogs=10.0)
    dataset = make_dataset(orders=orders, products=products)
    dataset.quality.set_field("customer_identity", covered=3, total=3)
    return analyze_loaded_dataset(dataset, today=TIE_ANALYSIS)


def _tied_campaigns_report(order: tuple) -> dict:
    from conftest import make_dataset, make_order
    from mervio.analytics.pipeline import analyze_loaded_dataset
    from mervio.domain.models import Campaign, DailyAdPerformance
    orders = [make_order("#1", TIE_DAY, "c1:" + "0" * 32, [("A", 1, 100.0)])]
    ads = [DailyAdPerformance(day=TIE_DAY, campaign_id=name, spend=50.0, impressions=300,
                              clicks=10, conversions=1.0, conversion_value=100.0)
           for name in order]
    dataset = make_dataset(orders=orders, ads=ads)
    dataset.campaigns.clear()
    for name in order:
        dataset.campaigns[name] = Campaign(name, f"Campagne {name}")
    return analyze_loaded_dataset(dataset, today=TIE_ANALYSIS)


@pytest.mark.parametrize("build, tied", [(_tied_products_report, TIED_SKUS),
                                         (_tied_campaigns_report, TIED_CAMPAIGNS)],
                         ids=["products", "campaigns"])
def test_5_a_tie_gives_the_same_bytes_in_both_insertion_orders(build, tied):
    """K-4 mesure l'ordre PUBLIE a egalite; ici c'est la propriete entiere qui est mesuree: les
    OCTETS du rapport ne doivent pas dependre de l'ordre d'insertion, meme a valeur egale.

    `tests/fixtures/f01_ties` n'a d'egalite que sur `conversions_delta`: la permutation des
    lignes CSV ne peut donc pas atteindre le tri des articles ni celui des campagnes. Ce jeu-ci
    le fait, et il est la seule chose qui distingue une cle totale d'une cle qui ne l'est pas.
    """
    first = _without_clock(build(tied))
    second = _without_clock(build(tuple(reversed(tied))))
    assert first == second, (
        f"les octets dependent de l'ordre d'insertion: "
        f"{hashlib.sha256(first).hexdigest()[:12]} != {hashlib.sha256(second).hexdigest()[:12]}")


def test_5_the_tie_datasets_really_tie():
    """Garde: sans egalite exacte, les deux tests precedents passeraient a vide."""
    products = _tied_products_report(TIED_SKUS)["products"]
    revenues = [row["revenue"] for row in products if row["sku"] in TIED_SKUS]
    assert len(revenues) == 2 and revenues[0] == revenues[1], revenues
    campaigns = _tied_campaigns_report(TIED_CAMPAIGNS)["marketing"]["campaigns"]
    spends = [row["spend"] for row in campaigns if row["campaign_id"] in TIED_CAMPAIGNS]
    assert len(spends) == 2 and spends[0] == spends[1], spends
