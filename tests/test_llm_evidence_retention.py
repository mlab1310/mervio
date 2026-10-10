"""D1: le plafond de preuves ne doit pas eliminer une categorie entiere (004.5 -- phase D1).

DEFAUT MESURE. `llm/context.py` assemble `evidence` en concatenant cinq natures de preuve,
puis tronque la tete de liste a `ContextLimits.max_evidence`:

    evidence = finding_facts + evidence          # context.py:104
    "evidence": self._bound(evidence, self.limits.max_evidence, "evidence")

Les contributions produit et campagne sont emises EN DERNIER. AN-15 (`dd1bb9f`) a ajoute les
faits de constat (`finding_fact`) en TETE: le budget est desormais consomme avant d'arriver aux
contributions, qui sortent du contexte. Mesure sur 57 rapports (6 scenarios moteur + 17 scenarios
de verite terrain a 1 200, 6 000 et 20 000 commandes), plafond 30:

    28 rapports tronques
    10 rapports perdent la TOTALITE de leurs contributions de campagne
    29 contributions produit perdues
    engine/revenue_decline: 37 produites, 30 transmises, 0 campagne, 2 produits sur 5

Ce n'est pas une violation du contrat 2.0 -- le contexte reste borne et valide -- c'est une
PERTE D'INFORMATION vers le modele: sur le scenario meme qui motive l'explication, la campagne
responsable de la baisse de conversions n'est plus citable.

CE QUI EST VERROUILLE ICI:

  1. aucune nature de preuve n'est eliminee en entier par la troncature;
  2. la selection ne depend pas de l'ordre ENTRE les natures (ordre d'emission accidentel);
  3. la selection est deterministe;
  4. le contexte reste loin sous `max_context_bytes`;
  5. faire croitre les `finding_fact` ne doit pas effacer les contributions.

AUCUN SCORE METIER N'EST INVENTE: la nature de la preuve (`kind`) est deja dans la structure, et
c'est elle seule qui repartit le budget.
"""
from __future__ import annotations

import collections
import itertools
import json
import logging
import tempfile

import pytest

import llm_scenarios
from mervio.llm import build_llm_context
from mervio.llm.context import serialize_context
from mervio.llm.contract import ContextLimits

#: natures de preuve que le contexte derive du rapport 2.0
KINDS = ("finding_fact", "anomaly", "root_cause_factor", "product_contribution",
         "campaign_contribution")
#: natures qui portent l'information COMMERCIALE que D1 a vue disparaitre
BUSINESS_KINDS = ("product_contribution", "campaign_contribution")
#: plafond hors d'atteinte: sert a mesurer ce que le moteur PRODUIT
UNBOUNDED = ContextLimits(max_evidence=10_000)
#: profils alternes, comme `test_synthetic_scenarios`
PROFILES = ("fashion_eu", "electronics_us")
#: volume reduit: on mesure la RETENTION, pas la verite terrain
SCENARIO_ORDERS = 1200


def kinds_of(context: dict) -> collections.Counter:
    return collections.Counter(item["kind"] for item in context["evidence"])


@pytest.fixture(scope="module")
def reports() -> dict:
    """Les scenarios du moteur (dont `revenue_decline`, le cas D1) et les 17 scenarios generes."""
    logging.disable(logging.WARNING)
    try:
        from mervio.synthetic import SCENARIOS, GeneratorConfig
        from mervio.synthetic.evaluation import run_scenario
        corpus = {name: build() for name, build in llm_scenarios.SCENARIOS.items()
                  if name != "minimal_report"}
        for index, name in enumerate(sorted(SCENARIOS)):
            with tempfile.TemporaryDirectory() as directory:
                corpus["scenario/" + name] = run_scenario(
                    GeneratorConfig(scenario=name, orders=SCENARIO_ORDERS, days=126, seed=3,
                                    profile=PROFILES[index % 2]), directory)["report"]
    finally:
        logging.disable(logging.NOTSET)
    return corpus


@pytest.fixture(scope="module")
def decline(reports) -> dict:
    return reports["revenue_decline"]


# == 1 -- le cas D1 ======================================================================

def test_the_decline_scenario_transmits_its_campaign_contributions(decline):
    """RED avant correction: 0 campagne transmise sur 4 produites.

    C'est la campagne dont les conversions chutent le plus qui porte l'explication -- celle que
    `recommendation.factor.order_volume_with_campaign` NOMME au marchand. Le modele doit pouvoir
    la citer.
    """
    produced = kinds_of(build_llm_context(decline, UNBOUNDED))
    sent = kinds_of(build_llm_context(decline))
    assert produced["campaign_contribution"] > 0, "le jeu doit produire des campagnes"
    assert sent["campaign_contribution"] == produced["campaign_contribution"], (
        f"{produced['campaign_contribution']} campagnes produites, "
        f"{sent['campaign_contribution']} transmises")


def test_the_decline_scenario_transmits_every_product_contribution(decline):
    """RED avant correction: 2 contributions produit sur 5."""
    produced = kinds_of(build_llm_context(decline, UNBOUNDED))
    sent = kinds_of(build_llm_context(decline))
    assert sent["product_contribution"] == produced["product_contribution"], (
        f"{produced['product_contribution']} produites, {sent['product_contribution']} transmises")


def test_the_decline_scenario_loses_no_evidence_at_all(decline):
    """Le plafond doit laisser passer la totalite de ce que ce rapport produit."""
    context = build_llm_context(decline)
    assert context["truncation"]["evidence"]["total"] == \
        context["truncation"]["evidence"]["included"]


# == 2 -- aucune nature eliminee, sur tout le corpus =====================================

def test_no_report_loses_a_whole_evidence_kind(reports):
    """RED avant correction: 10 rapports sur 57 perdaient toutes leurs campagnes.

    La propriete est generale: une nature PRODUITE ne doit jamais etre transmise a zero.
    """
    offenders = []
    for name, report in reports.items():
        produced = kinds_of(build_llm_context(report, UNBOUNDED))
        sent = kinds_of(build_llm_context(report))
        for kind in KINDS:
            if produced[kind] and not sent[kind]:
                offenders.append(f"{name}: {kind} {produced[kind]} -> 0")
    assert not offenders, "natures eliminees en entier:\n" + "\n".join(offenders)


def test_no_report_loses_a_business_contribution(reports):
    """Plus fort sur les deux natures commerciales: aucune perte, pas seulement pas de zero."""
    offenders = []
    for name, report in reports.items():
        produced = kinds_of(build_llm_context(report, UNBOUNDED))
        sent = kinds_of(build_llm_context(report))
        for kind in BUSINESS_KINDS:
            if sent[kind] < produced[kind]:
                offenders.append(f"{name}: {kind} {produced[kind]} -> {sent[kind]}")
    assert not offenders, "contributions perdues:\n" + "\n".join(offenders)


def test_the_corpus_really_exercises_the_ceiling(reports):
    """Garde: sans rapport depassant le plafond, les tests precedents passeraient a vide."""
    produced = [build_llm_context(report, UNBOUNDED)["truncation"]["evidence"]["total"]
                for report in reports.values()]
    limit = ContextLimits().max_evidence
    assert max(produced) >= 30, f"le corpus ne produit au plus que {max(produced)} preuves"
    assert any(count > 30 for count in produced), (
        "aucun rapport ne depasse l'ancien plafond de 30: le defaut D1 ne serait pas reproduit")
    assert max(produced) <= limit, (
        f"le corpus produit jusqu'a {max(produced)} preuves pour un plafond de {limit}: "
        "le plafond n'est plus celui que la mesure a demontre suffisant")


# == 3 -- la selection ne depend pas de l'ordre entre natures =============================

def _permute_kind_blocks(items, order):
    """Permute les BLOCS de nature; l'ordre INTERNE de chaque nature est conserve.

    C'est exactement la variable en cause: `evidence = finding_facts + evidence` place une
    nature devant les autres, et c'est cette position -- non une propriete de la preuve -- qui
    decidait de sa survie.
    """
    buckets = collections.OrderedDict()
    for item in items:
        buckets.setdefault(item["kind"], []).append(item)
    return [item for kind in order if kind in buckets for item in buckets[kind]]


def test_the_surviving_selection_ignores_the_order_between_kinds(decline):
    """RED avant correction: l'ordre des blocs change l'ensemble transmis.

    Mesure a l'etat initial: 27 rapports sur 57 changeaient de selection selon l'ordre des blocs.
    """
    from mervio.llm.context import _ContextBuilder
    produced = build_llm_context(decline, UNBOUNDED)["evidence"]
    assert len(produced) > ContextLimits(max_evidence=12).max_evidence
    builder = _ContextBuilder(ContextLimits(max_evidence=12))
    reference = sorted(item["id"] for item in builder.select_evidence(list(produced)))
    kinds = list(collections.OrderedDict.fromkeys(item["kind"] for item in produced))
    # TOUTES les permutations, pas un echantillon: les 4! premieres laissent `finding_fact` en
    # tete, et une coupe de tete y survivrait sans etre vue.
    orders = list(itertools.permutations(kinds))
    assert len(orders) == 120, len(orders)
    for order in orders:
        builder = _ContextBuilder(ContextLimits(max_evidence=12))
        shuffled = _permute_kind_blocks(produced, list(order))
        got = sorted(item["id"] for item in builder.select_evidence(shuffled))
        assert got == reference, f"ordre {order}: selection differente"


def test_the_selection_is_deterministic(reports):
    """Deux constructions, le meme contexte: la selection n'introduit aucun hasard."""
    for name, report in reports.items():
        first = serialize_context(build_llm_context(report))
        assert first == serialize_context(build_llm_context(report)), name


# == 4 -- regression explicite demandee par D1 ============================================

def _with_more_findings(report: dict, copies: int) -> dict:
    """Le MEME rapport, dont les constats sont multiplies: chaque constat porte ses preuves,
    donc le nombre de `finding_fact` croit sans que rien d'autre change."""
    inflated = json.loads(json.dumps(report))
    template = [item for item in inflated["warnings"] if item["evidence"]]
    assert template, "le rapport de reference doit porter un avertissement avec des preuves"
    for index in range(copies):
        clone = json.loads(json.dumps(template[0]))
        clone["type"] = f"{clone['type']}_clone{index}"
        clone["finding_id"] = f"{clone['finding_id']}:clone{index}"
        for rank, block in enumerate(clone["evidence"]):
            block["fact_id"] = f"{block['fact_id']}.clone{index}.{rank}"
        inflated["warnings"].append(clone)
    return inflated


@pytest.mark.parametrize("copies", [1, 5, 20, 60])
def test_growing_finding_facts_never_wipes_the_campaign_contributions(decline, copies):
    """REGRESSION EXPLICITE (D1): faire croitre les faits de constat ne doit pas faire
    disparaitre les contributions de campagne d'un contexte qui reste sous la limite d'octets.

    Avec la troncature de tete, 60 copies suffisent a remplir le budget avec des faits de
    constat seuls. Le plafond d'octets, lui, n'est jamais atteint: ce test echoue pour la
    SELECTION, pas pour le volume.
    """
    inflated = _with_more_findings(decline, copies)
    context = build_llm_context(inflated)
    sent = kinds_of(context)
    produced = kinds_of(build_llm_context(inflated, UNBOUNDED))
    size = len(serialize_context(context).encode("utf-8"))
    assert size <= ContextLimits().max_context_bytes, f"{size} octets"
    assert produced["campaign_contribution"] > 0
    assert sent["campaign_contribution"] > 0, (
        f"{copies} constats ajoutes: {produced['campaign_contribution']} campagnes produites, "
        f"aucune transmise ({sent['finding_fact']} faits de constat occupent le budget)")
    assert sent["product_contribution"] > 0


def test_a_saturated_context_still_spreads_the_budget_across_kinds(decline):
    """Budget EGAL au nombre de natures: chacune garde exactement une preuve.

    C'est le cas extreme de la repartition, et celui ou une coupe de tete ne garderait que la
    premiere nature emise.
    """
    produced = kinds_of(build_llm_context(decline, UNBOUNDED))
    budget = len(produced)
    context = build_llm_context(decline, ContextLimits(max_evidence=budget))
    sent = kinds_of(context)
    assert sum(sent.values()) == budget
    assert set(sent) == set(produced), f"natures eliminees: {set(produced) - set(sent)}"
    assert all(count == 1 for count in sent.values()), dict(sent)


# == 5 -- bornes conservees ===============================================================

def test_the_context_keeps_a_wide_margin_under_the_byte_ceiling(reports):
    """Le plafond d'octets reste la vraie borne de securite, et il n'est pas approche.

    Mesure sur 57 rapports: 38 461 octets au maximum, soit 25 539 de marge sous 64 000.
    """
    limits = ContextLimits()
    worst = max(len(serialize_context(build_llm_context(report)).encode("utf-8"))
                for report in reports.values())
    assert worst <= limits.max_context_bytes
    assert worst <= limits.max_context_bytes * 0.75, (
        f"{worst} octets: la marge sous {limits.max_context_bytes} n'est plus confortable")


def test_the_evidence_section_still_honours_its_limit(reports):
    limit = ContextLimits().max_evidence
    for name, report in reports.items():
        context = build_llm_context(report)
        assert len(context["evidence"]) <= limit, name
        assert context["truncation"]["evidence"]["included"] == len(context["evidence"]), name


def test_every_evidence_reference_still_resolves(reports):
    """La selection ne doit pas laisser une reference pendante: un constat qui cite une preuve
    absente ferait inventer au modele ce qu'il ne peut pas lire."""
    from mervio.llm.context import context_ids
    for name, report in reports.items():
        context = build_llm_context(report)
        ids = context_ids(context)
        for finding in context["findings"]:
            for reference in finding["evidence_ids"]:
                assert reference in ids, f"{name}: {finding['id']} cite {reference}"
        for hypothesis in context["hypotheses"]:
            for reference in hypothesis["supports"]:
                assert reference in ids, f"{name}: {hypothesis['id']} cite {reference}"
