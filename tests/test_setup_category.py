"""AN-18 (D-084): `profitability_unavailable` cesse d'etre une alerte business.

DEFAUT CORRIGE. Le constat etait emis `severity="medium"`, `category="warning"`:

    Insight(type="profitability_unavailable", severity="medium", category="warning", ...)

Il n'est pas emis par exception mais par CONSTRUCTION. `profitability.REQUIRED_COMPONENTS`
exige `shipping_cost`, et `compute_profitability` pose cette composante a `None` sans condition
-- un export Shopify contient le port FACTURE au client, jamais le cout transporteur. Aucune
boutique alimentee par CSV ne peut donc rendre `data_available` vrai: le constat est present
dans CHAQUE rapport. Un avertissement permanent n'avertit de rien, et il occupait la section ou
le marchand cherche ce qui va mal cette semaine.

Ce n'est pourtant pas un probleme de l'entreprise: c'est une donnee qui n'est pas branchee.
D-084 tranche:

    severity  medium -> info
    category  warning -> setup
    ordre     critical_issues, warnings, setup, opportunities

CE QUE AN-18 NE FAIT PAS. Le constat n'est pas supprime, sa preuve n'est pas touchee (les
composantes de cout manquantes restent des faits NON OBSERVES, AN-15), sa recommandation
reste -- << renseigner les couts manquants >> est precisement l'action de branchement, et elle
doit rester reliee a son constat (`report["recommendations"]`). Aucun autre constat ne change
de severite ni de categorie.

FRONTIERE 1.x / 2.0 (ND-04). Un rapport 1.x n'a pas de cle `setup`. Les deux lecteurs la
traitent comme absente, jamais comme une erreur: `render_executive_report` lit la section
nouvelle avec `.get`, `build_llm_context` passe par `_list`, qui rend `[]` sur une cle
manquante. Aucun rapport existant n'est reecrit.

ORDRE DES CATEGORIES. L'ordre ratifie est une propriete du CONTRAT, pas du rendu: il est
verifie ici dans les trois vues -- l'ordre des cles du rapport, l'ordre des sections de
`report.txt`, et l'ordre des constats du contexte LLM.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from conftest import make_dataset, make_order
from mervio.analytics.insights import SEVERITY_ORDER
from mervio.analytics.pipeline import analyze_loaded_dataset, run_analysis
from mervio.llm import build_llm_context
from mervio.reporting.executive import render_executive_report

#: ordre ratifie par D-084
CATEGORY_ORDER = ("critical_issue", "warning", "setup", "opportunity")
SECTION_ORDER = ("critical_issues", "warnings", "setup", "opportunities")
SECTION_TITLES = ("## CRITICAL ISSUES", "## WARNINGS", "## SETUP", "## OPPORTUNITIES")

#: etat de reference mesure a 6ec4f37 sur data/sample, hors AN-18:
#: aucune de ces lignes ne doit bouger (critere 8 du GO)
UNCHANGED = {
    "revenue_decline": ("medium", "critical_issue"),
    "refunds_anomaly": ("high", "critical_issue"),
    "ad_spend_anomaly": ("low", "warning"),
    "campaign_opportunity": ("info", "opportunity"),
}

ANALYSIS_DAY = date(2026, 9, 21)
DATA_DAY = date(2026, 9, 13)


def _only_setup_dataset():
    """Vingt clients, une semaine close, aucun catalogue produit.

    Ce jeu n'emet AUCUN autre constat: pas d'anomalie (une seule periode), pas de
    `low_margin_product` (sans cout produit la marge est `null`, jamais zero) et une
    concentration sous le seuil. Le seul constat possible est celui d'AN-18, ce qui rend le
    critere d'acceptation du roadmap directement observable: << boutique sans couts -> aucun
    probleme de profitabilite en tete >>.
    """
    orders = [make_order(f"#{index}", DATA_DAY, f"c1:{index:02d}" + "0" * 30,
                         [("A", 1, 100.0 + index)]) for index in range(20)]
    ds = make_dataset(orders=orders)
    ds.quality.set_field("customer_identity", covered=len(orders), total=len(orders))
    return ds


def _findings(report: dict):
    return {item["type"]: item for section in SECTION_ORDER
            for item in (report.get(section) or [])}


@pytest.fixture
def sample(sample_paths):
    """data/sample peuple les QUATRE categories: c'est le jeu des tests d'ordre."""
    return run_analysis(sample_paths, today=date(2026, 9, 14))


@pytest.fixture
def only_setup():
    return analyze_loaded_dataset(_only_setup_dataset(), today=ANALYSIS_DAY)


# -- 1. la severite et la categorie du constat --------------------------------------------

def test_profitability_unavailable_is_no_longer_medium_nor_a_warning(sample):
    finding = _findings(sample)["profitability_unavailable"]
    assert finding["severity"] != "medium"
    assert finding["category"] != "warning"
    assert "profitability_unavailable" not in {i["type"] for i in sample["warnings"]}


def test_profitability_unavailable_is_info_in_the_setup_category(sample):
    finding = _findings(sample)["profitability_unavailable"]
    assert finding["severity"] == "info"
    assert finding["category"] == "setup"


def test_it_sits_in_the_setup_section_of_the_report(sample):
    assert [i["type"] for i in sample["setup"]] == ["profitability_unavailable"]


# -- 2. ce n'est plus une alerte business -------------------------------------------------

def test_a_store_without_costs_has_no_profitability_problem_in_front(only_setup):
    """Critere d'acceptation du roadmap AN-18."""
    assert only_setup["critical_issues"] == []
    assert only_setup["warnings"] == []
    assert [i["type"] for i in only_setup["setup"]] == ["profitability_unavailable"]


def test_the_executive_summary_does_not_raise_it(only_setup):
    summary = only_setup["executive_summary"]
    assert "Aucun probleme critique detecte sur les dimensions mesurees." in summary
    assert "profit" not in summary.lower() and "cout" not in summary.lower()


def test_it_does_not_weigh_on_the_business_health_score(only_setup):
    """Le score vient des dimensions, jamais des constats: une composante de cout absente
    EXCLUT la dimension, elle ne la note pas mal. AN-18 ne touche pas a ce chemin."""
    health = only_setup["business_health_score"]
    assert "profitability" in health["excluded_dimensions"]
    dimension = next(d for d in health["dimensions"] if d["key"] == "profitability")
    assert dimension["available"] is False and dimension["score"] is None


def test_the_readable_report_puts_it_under_setup_and_leaves_warnings_empty(only_setup):
    text = render_executive_report(only_setup)
    sections = {title: text.index(title) for title in SECTION_TITLES}
    setup_block = text[sections["## SETUP"]:sections["## OPPORTUNITIES"]]
    warnings_block = text[sections["## WARNINGS"]:sections["## SETUP"]]
    assert "profit de contribution complet n'est pas calculable" in setup_block
    assert "[INFO]" in setup_block
    assert "profit" not in warnings_block.lower()


# -- 3. ordre des categories, dans les trois vues -----------------------------------------

def test_the_report_keys_follow_the_ratified_order(sample):
    positions = [list(sample).index(section) for section in SECTION_ORDER]
    assert positions == sorted(positions)


def test_the_serialized_report_keeps_that_order(sample):
    """L'ordre doit survivre a la serialisation: c'est elle qui circule."""
    payload = json.dumps(sample, ensure_ascii=False)
    positions = [payload.index(f'"{section}"') for section in SECTION_ORDER]
    assert positions == sorted(positions)


def test_the_readable_report_renders_the_sections_in_that_order(sample):
    positions = [render_executive_report(sample).index(title) for title in SECTION_TITLES]
    assert positions == sorted(positions)


def test_the_llm_context_groups_the_findings_in_that_order(sample):
    categories = [f["category"] for f in build_llm_context(sample)["findings"]]
    assert set(categories) == set(CATEGORY_ORDER), categories
    assert categories == sorted(categories, key=CATEGORY_ORDER.index)


def test_critical_issues_stay_first(sample):
    assert [i["type"] for i in sample["critical_issues"]]
    first = build_llm_context(sample)["findings"][0]
    assert first["category"] == "critical_issue"


def test_warnings_come_before_setup(sample):
    text = render_executive_report(sample)
    assert text.index("## WARNINGS") < text.index("## SETUP")
    assert list(sample).index("warnings") < list(sample).index("setup")


def test_setup_comes_before_opportunities(sample):
    text = render_executive_report(sample)
    assert text.index("## SETUP") < text.index("## OPPORTUNITIES")
    assert list(sample).index("setup") < list(sample).index("opportunities")


# -- 4. aucun effet de bord sur les autres constats ---------------------------------------

def test_no_other_finding_changes_severity_or_category(sample):
    observed = {t: (i["severity"], i["category"]) for t, i in _findings(sample).items()
                if t != "profitability_unavailable"}
    assert observed == UNCHANGED


def test_the_severity_vocabulary_is_untouched():
    """AN-18 deplace UN constat; il ne reecrit pas l'echelle de severite ni son ordre."""
    assert SEVERITY_ORDER == {"high": 0, "medium": 1, "low": 2, "info": 3}


def test_every_finding_carries_a_known_category(sample):
    for section, items in ((s, sample[s]) for s in SECTION_ORDER):
        for item in items:
            assert item["category"] in CATEGORY_ORDER
            assert item["severity"] in SEVERITY_ORDER


# -- 5. profitabilite disponible / indisponible -------------------------------------------

def test_no_setup_finding_when_profitability_is_available():
    """Teste chez le PRODUCTEUR: `compute_profitability` ne peut pas rendre `data_available`
    vrai depuis un CSV (`shipping_cost` est `None` sans condition), donc le cas disponible
    n'existe pas au bout du pipeline. Il existe dans `build_insights`, et c'est la qu'on le
    mesure -- sans fabriquer un cout transporteur que l'export ne contient pas.
    """
    from mervio.analytics.insights import build_insights
    from mervio.analytics.kpi import compute_kpis
    from mervio.analytics.periods import make_period
    from mervio.analytics.profitability import ProfitabilityResult, compute_profitability
    ds = _only_setup_dataset()
    period = make_period(DATA_DAY, "week")
    window = ds.window(period.start, period.end)
    kpis = compute_kpis(window, period)

    unavailable = compute_profitability(window, period.label)
    assert unavailable.data_available is False
    types = [i.type for i in build_insights(kpis, [], None, unavailable, window)]
    assert "profitability_unavailable" in types

    available = ProfitabilityResult(period=period.label, revenue=unavailable.revenue,
                                    components={}, data_available=True)
    types = [i.type for i in build_insights(kpis, [], None, available, window)]
    assert "profitability_unavailable" not in types


def test_an_empty_setup_section_is_still_present_and_rendered(sample):
    """Une categorie vide ne doit ni disparaitre du contrat ni casser le rendu."""
    empty = dict(sample, setup=[])
    assert "setup" in empty
    text = render_executive_report(empty)
    assert "## SETUP" in text
    block = text[text.index("## SETUP"):text.index("## OPPORTUNITIES")]
    assert "Aucun element detecte sur les dimensions mesurees." in block
    assert build_llm_context(empty)["findings"]


def test_several_findings_coexist_without_collision(sample):
    findings = [i for section in SECTION_ORDER for i in sample[section]]
    assert len(findings) >= 4
    ids = [i["finding_id"] for i in findings]
    assert len(ids) == len(set(ids))


# -- 6. ce que AN-18 ne doit pas casser ---------------------------------------------------

def test_the_recommendation_stays_linked_to_its_finding(sample):
    """Reproduit le controle `recommendations_linked_to_findings` de
    scripts/validate_real_export.py: chaque recommandation du rapport doit pointer un constat
    EXISTANT. Deplacer le constat sans deplacer le scan le mettait en FAIL sur toute boutique
    Shopify."""
    types = set(_findings(sample))
    assert "profitability_unavailable" in types
    for recommendation in sample["recommendations"]:
        assert recommendation["source_insight"] in types, recommendation["source_insight"]


def test_the_setup_finding_keeps_its_structured_evidence(sample):
    """AN-15 ne doit pas perdre ce producteur en changeant de section."""
    finding = _findings(sample)["profitability_unavailable"]
    assert finding["evidence"]
    assert all(isinstance(item, dict) for item in finding["evidence"])
    assert any(item["observability"] == "not_observed" for item in finding["evidence"])
    assert finding["recommendation"] and finding["confidence"] is not None


def test_a_1x_report_without_the_setup_key_stays_readable(sample):
    """ND-04: on ne reecrit pas les anciens rapports, donc on doit encore les lire."""
    legacy = {k: v for k, v in sample.items() if k != "setup"}
    assert "## SETUP" in render_executive_report(legacy)
    context = build_llm_context(legacy)
    assert "setup" not in {f["category"] for f in context["findings"]}
    assert context["findings"]
