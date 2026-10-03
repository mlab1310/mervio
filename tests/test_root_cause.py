"""Tests du Root Cause Engine."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from conftest import ad, make_dataset, make_order
from mervio.analytics.periods import make_period
from mervio.analytics.root_cause import analyse_revenue_change

CURRENT = make_period(date(2026, 9, 7), "week")
PREVIOUS = make_period(date(2026, 8, 31), "week")


def build(n_prev, n_cur, price_prev=100.0, price_cur=100.0, clicks_prev=1000, clicks_cur=1000):
    orders = []
    for i in range(n_prev):
        orders.append(make_order(f"p{i}", date(2026, 9, 1), f"p{i}@x.com", [("A", 1, price_prev)]))
    for i in range(n_cur):
        orders.append(make_order(f"c{i}", date(2026, 9, 8), f"c{i}@x.com", [("A", 1, price_cur)]))
    ads = [ad(date(2026, 9, 1), 500.0, clicks_prev), ad(date(2026, 9, 8), 500.0, clicks_cur)]
    return make_dataset(orders=orders, ads=ads)


def test_order_volume_driven_decline_is_not_called_a_conversion_problem():
    """Trafic PAYE stable, commandes en chute.

    AVANT AN-02, le moteur repondait `conversion_rate` -- en divisant TOUTES les commandes par
    les SEULS clics payants. C'etait faux: avec un trafic paye plat, une chute des commandes peut
    venir du trafic organique, de la conversion du site, ou des deux. Aucun des deux n'est
    observe. Le moteur nomme donc le SYMPTOME mesure (`order_volume`) et declare les angles
    morts, au lieu d'affirmer une cause qu'il ne mesure pas.
    """
    analysis = analyse_revenue_change(build(100, 60), CURRENT, PREVIOUS)
    primary = analysis.primary_factor()
    assert primary.factor == "order_volume"
    assert primary.layer == "a", "la couche A porte l'identite du CA"
    assert analysis.observed_change_pct == pytest.approx(-0.40)
    unobserved = {f.factor for f in analysis.factors if f.observability == "not_observed"}
    assert unobserved == {"organic_traffic", "site_conversion_rate"}


def test_traffic_driven_decline_identifies_traffic():
    """Taux de conversion stable, trafic divise par deux -> cause = trafic."""
    analysis = analyse_revenue_change(build(100, 50, clicks_prev=1000, clicks_cur=500), CURRENT, PREVIOUS)
    assert analysis.primary_factor().factor == "paid_traffic"


def test_aov_driven_decline_identifies_aov():
    analysis = analyse_revenue_change(build(100, 100, price_prev=100.0, price_cur=60.0), CURRENT, PREVIOUS)
    assert analysis.primary_factor().factor == "average_order_value"


def test_contributions_sum_to_one():
    """La decomposition log doit etre exhaustive: somme des contributions = 1."""
    analysis = analyse_revenue_change(build(100, 55, price_cur=90.0, clicks_cur=900), CURRENT, PREVIOUS)
    total = sum(f.contribution for f in analysis.factors if f.contribution is not None)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_zero_revenue_makes_analysis_unavailable():
    analysis = analyse_revenue_change(build(100, 0), CURRENT, PREVIOUS)
    assert analysis.available is False
    assert analysis.limitations


def test_hypothesis_is_never_stated_as_proven_cause():
    analysis = analyse_revenue_change(build(100, 60), CURRENT, PREVIOUS)
    for factor in analysis.factors:
        if factor.hypothesis is None:
            # D-073: un facteur NON observe ne porte AUCUNE hypothese. Une hypothese sur une
            # grandeur non mesuree serait une affirmation gratuite.
            assert factor.observability == "not_observed"
            continue
        assert "associee" in factor.hypothesis or "insuffisante" in factor.hypothesis
    assert any("causalite" in lim for lim in analysis.limitations)


def test_confidence_is_qualitative_and_never_claims_certainty():
    """Meme intention qu'avant AN-14, nouveau contrat.

    Le test imposait `0.1 <= confidence <= 0.9`: des bornes qui disaient << jamais certain >> en
    exhibant une fausse precision. D-081 remplace le flottant par trois niveaux et expose les
    criteres. << Jamais certain >> devient: le niveau le plus haut est `high`, il n'y a pas de
    `certain`, et ce qui l'a produit est lisible.
    """
    analysis = analyse_revenue_change(build(100, 60), CURRENT, PREVIOUS)
    for factor in analysis.factors:
        if factor.confidence is None:
            assert factor.observability == "not_observed"  # D-073: aucune valeur fabriquee
            continue
        assert factor.confidence.level in ("low", "medium", "high")
        assert factor.confidence.criteria, "les criteres doivent etre exposes"
        rendered = factor.confidence.to_dict()
        assert set(rendered) == {"level", "criteria"}
        assert not isinstance(rendered["level"], (int, float))


def test_each_factor_carries_its_supporting_metric():
    analysis = analyse_revenue_change(build(100, 60), CURRENT, PREVIOUS)
    observed = {f.supporting_metric for f in analysis.factors if f.observability == "observed"}
    assert observed == {"orders", "aov", "paid_clicks"}, "cles alignees sur les KPI (ND-05 = A)"
    assert all(f.supporting_metric is None for f in analysis.factors
               if f.observability == "not_observed"), "pas de metrique support sans mesure"


def test_product_contributors_are_ranked_by_impact():
    orders = [make_order("p1", date(2026, 9, 1), "a@x.com", [("A", 1, 500.0), ("B", 1, 100.0)]),
              make_order("c1", date(2026, 9, 8), "b@x.com", [("B", 1, 100.0)])]
    ds = make_dataset(orders=orders, ads=[ad(date(2026, 9, 1), 10.0, 50), ad(date(2026, 9, 8), 10.0, 50)])
    analysis = analyse_revenue_change(ds, CURRENT, PREVIOUS)
    assert analysis.product_contributors[0]["sku"] == "A"


def test_method_is_documented_in_output():
    analysis = analyse_revenue_change(build(100, 60), CURRENT, PREVIOUS)
    assert "multiplicative" in analysis.method
