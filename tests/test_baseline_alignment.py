"""AN-01: l'anomalie et la decomposition doivent partager la MEME baseline.

Defaut mesure a `7580ad4` sur `tests/fixtures/an01_prolonged_decline/`:

    anomalie CA       observe 2200, attendu 3750, delta_pct = -41,33 %   (vs moyenne 8 periodes)
    decomposition     observed_change_pct = +10 %                        (vs semaine precedente)

Le rapport annonce une BAISSE et en explique une HAUSSE. Un seul `Insight` porte trois chaines
issues de deux referentiels: `estimated_impact` vient de l'anomalie, `observed_fact` et
`contribution` de la decomposition.

Contrat approuve (roadmap AN-01): << clics, commandes et CA agreges sur la fenetre de baseline,
puis ratios derives (l'identite CA = clics x commandes/clics x panier reste exacte) >>.

L'identite se verifie:

    n = nombre de periodes de baseline retenues
    R = somme(CA) / n          <- egale EXACTEMENT anomaly.expected_value
    C = somme(clics) / n
    cvr = somme(commandes) / somme(clics)       (ratio de sommes, PAS moyenne de ratios)
    aov = somme(CA) / somme(commandes)          (ratio de sommes)

    C x cvr x aov = (somme(C)/n) x (somme(O)/somme(C)) x (somme(R)/somme(O)) = somme(R)/n = R

Piege evite: prendre `somme(CA)` sans diviser par `n` fausserait `ln(R1/R0)` d'un facteur `ln n`.
Les agregats servent a construire les RATIOS, jamais la reference de niveau.

La comparaison S/S-1 n'est pas supprimee: elle est conservee, etiquetee a part, et NE PORTE
AUCUNE CONTRIBUTION.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from statistics import mean

import pytest

from mervio.analytics.pipeline import SourcePaths, run_analysis

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "an01_prolonged_decline"
TODAY = date(2026, 9, 21)


@pytest.fixture(scope="module")
def declining() -> dict:
    """Declin prolonge: S/S-1 POSITIF (+10 %), vs baseline NEGATIF (-41 %)."""
    return run_analysis(
        SourcePaths(shopify_orders=str(FIXTURE / "shopify_orders.csv"),
                    shopify_products=str(FIXTURE / "shopify_products.csv"),
                    google_ads=str(FIXTURE / "google_ads.csv")),
        today=TODAY,
    )


def _revenue_anomaly(report: dict) -> dict:
    return next(a for a in report["anomalies"] if a["metric"] == "revenue")


def _root_cause(report: dict) -> dict:
    return report["root_causes"][0]


# -- garde du jeu: la contradiction doit exister pour etre mesurable -------
def test_the_fixture_really_has_a_sign_contradiction(declining):
    """Sans divergence de signe entre S/S-1 et vs-baseline, les tests suivants ne mesurent rien."""
    wow = declining["comparisons"]["revenue"]["wow"]
    assert wow["pct_change"] > 0, "S/S-1 doit etre POSITIF sur ce jeu"
    assert _revenue_anomaly(declining)["delta_pct"] < 0, "vs baseline doit etre NEGATIF"


# -- l'anomalie expose la baseline qu'elle a utilisee ---------------------
def test_the_anomaly_declares_its_baseline_periods(declining):
    """Une baseline non declaree ne peut pas etre verifiee, ni reutilisee sans risque de
    divergence."""
    anomaly = _revenue_anomaly(declining)
    assert anomaly["baseline_periods"], "l'anomalie doit nommer les periodes de sa baseline"
    assert len(anomaly["baseline_periods"]) == 8, "fenetre de baseline par defaut"
    assert anomaly["baseline_periods"] == sorted(anomaly["baseline_periods"]), "ordre stable"


def test_the_expected_value_is_the_mean_of_the_declared_baseline(declining):
    anomaly = _revenue_anomaly(declining)
    assert anomaly["expected_value"] == pytest.approx(3750.0)
    assert len(anomaly["baseline_periods"]) == 8


# -- la decomposition utilise la MEME baseline ----------------------------
def test_the_decomposition_declares_the_same_baseline_as_the_anomaly(declining):
    """C'est l'exigence centrale d'AN-01: un seul referentiel par rapport."""
    assert _root_cause(declining)["baseline_periods"] == \
        _revenue_anomaly(declining)["baseline_periods"]


def test_the_decomposition_now_explains_the_decline_not_the_rise(declining):
    """Avant AN-01: observed_change_pct = +10 % alors que l'anomalie annoncait -41 %."""
    root = _root_cause(declining)
    anomaly = _revenue_anomaly(declining)
    assert root["observed_change_pct"] < 0, "la decomposition doit expliquer une BAISSE"
    assert root["observed_change_pct"] == pytest.approx(anomaly["delta_pct"], abs=1e-4)


def test_the_reference_level_is_the_anomaly_expected_value(declining):
    """`R` de la decomposition == `expected_value` de l'anomalie, exactement."""
    root = _root_cause(declining)
    assert root["baseline_revenue"] == pytest.approx(
        _revenue_anomaly(declining)["expected_value"], abs=1e-6)


def test_the_absolute_change_matches_the_anomaly_delta(declining):
    root = _root_cause(declining)
    assert root["observed_change_absolute"] == pytest.approx(
        _revenue_anomaly(declining)["delta"], abs=1e-2)


# -- l'identite arithmetique exigee par la roadmap ------------------------
def test_the_aggregate_to_ratio_identity_holds_exactly():
    """C x cvr x aov == R, EXACTEMENT.

    Mesure sur les flottants du moteur, pas sur le rapport: `to_dict()` arrondit a 4 decimales,
    et cvr = 300/3200 = 0,09375 devient 0,0938 -- l'identite y semblerait fausse de 2 EUR alors
    qu'elle est exacte. L'arrondi est une precision d'affichage (D-083 l'exclut du perimetre
    des seuils); l'identite, elle, est une propriete du calcul.

    Si l'implementation prenait les sommes sans diviser par n, ou une MOYENNE DE RATIOS au lieu
    d'un RATIO DE SOMMES, cette egalite casserait.
    """
    from mervio.analytics.anomaly import baseline_points
    from mervio.analytics.pipeline import (
        SERIES_METRICS, last_complete_period, latest_data_day, load_dataset, previous_period,
    )
    from mervio.analytics.root_cause import analyse_revenue_change
    from mervio.analytics.timeseries import build_series
    from mervio.config import AnalyticsConfig

    cfg = AnalyticsConfig()
    dataset = load_dataset(SourcePaths(
        shopify_orders=str(FIXTURE / "shopify_orders.csv"),
        shopify_products=str(FIXTURE / "shopify_products.csv"),
        google_ads=str(FIXTURE / "google_ads.csv")))
    period = last_complete_period(latest_data_day(dataset, cfg.timezone), TODAY, cfg.grain,
                                  cfg.timezone)
    series = build_series(dataset, period, cfg.lookback_periods, SERIES_METRICS["revenue"])
    baseline = [point.period for point in baseline_points(series, cfg.anomaly)]
    analysis = analyse_revenue_change(dataset, period, previous_period(period), baseline=baseline)

    factors = {f.factor: f for f in analysis.factors}
    product = 1.0
    for key in ("paid_traffic", "conversion_rate", "average_order_value"):
        value = factors[key].previous_value
        assert value is not None, f"{key}: valeur de reference manquante"
        product *= value
    assert product == pytest.approx(analysis.baseline_revenue, rel=1e-12), (
        "identite agregats->ratios rompue"
    )
    assert analysis.baseline_revenue == pytest.approx(3750.0, rel=1e-12)


def test_the_contributions_still_sum_to_one(declining):
    root = _root_cause(declining)
    contributions = [f["contribution"] for f in root["factors"]
                     if f["contribution"] is not None]
    assert sum(contributions) == pytest.approx(1.0, abs=1e-6)


# -- S/S-1 conservee, etiquetee, SANS contribution ------------------------
def test_the_week_over_week_comparison_is_preserved_and_labelled(declining):
    """La comparaison a la periode precedente reste disponible: elle est utile, elle n'est
    simplement plus le referentiel de la decomposition."""
    change = _root_cause(declining)["change_vs_previous_period"]
    assert change["previous_period"] == declining["period"]["previous"]["label"]
    assert change["pct"] > 0, "S/S-1 reste positif, et c'est dit"


def test_the_week_over_week_block_carries_no_contribution(declining):
    """Regle anti-melange de referentiels: aucune contribution ne peut etre attachee a un
    referentiel different de celui de la decomposition."""
    change = _root_cause(declining)["change_vs_previous_period"]
    assert "contribution" not in change
    assert not any(key.startswith("contribution") for key in change)


def test_the_insight_impact_and_decomposition_share_one_reference(declining):
    """`estimated_impact` venait de l'anomalie, `contribution` de la decomposition: deux
    referentiels dans un seul constat."""
    decline = next(i for i in declining["critical_issues"] if i["type"] == "revenue_decline")
    assert decline["estimated_impact"] == pytest.approx(
        _root_cause(declining)["observed_change_absolute"], abs=1e-2)


# -- series trouees: les deux baselines ne doivent pas divergier ----------
def test_a_period_without_data_does_not_split_the_two_baselines(declining):
    """Le detecteur EXCLUT les points `None`. Si la decomposition recalculait sa fenetre au lieu
    de recevoir celle du detecteur, une periode sans donnee les ferait silencieusement divergier.
    """
    anomaly = _revenue_anomaly(declining)
    root = _root_cause(declining)
    assert root["baseline_periods"] == anomaly["baseline_periods"]
    # aucune periode de baseline ne doit etre vide: elles viennent de points non-None
    assert all(label for label in root["baseline_periods"])


# -- non-regression: une baseline d'UNE periode reproduit l'ancien calcul --
def test_a_single_period_baseline_reproduces_the_previous_behaviour():
    """Cas degenere n = 1: la baseline agregee vaut EXACTEMENT la periode precedente.

    Avec n = 1: somme(R)/1 = R0, somme(O)/somme(C) = cvr0, somme(R)/somme(O) = aov0. C'est ce
    qui rend les dix tests de `tests/test_root_cause.py` valides SANS modification -- leur
    maintien au vert est la seconde moitie de cette preuve.
    """
    from conftest import ad, make_dataset, make_order
    from mervio.analytics.periods import make_period
    from mervio.analytics.root_cause import analyse_revenue_change

    current, previous = make_period(date(2026, 9, 7), "week"), make_period(date(2026, 8, 31), "week")
    orders = [make_order(f"p{i}", date(2026, 9, 1), f"p{i}@x.com", [("A", 1, 100.0)])
              for i in range(100)]
    orders += [make_order(f"c{i}", date(2026, 9, 8), f"c{i}@x.com", [("A", 1, 100.0)])
               for i in range(60)]
    dataset = make_dataset(orders=orders,
                           ads=[ad(date(2026, 9, 1), 500.0, 1000), ad(date(2026, 9, 8), 500.0, 1000)])

    implicit = analyse_revenue_change(dataset, current, previous)
    explicit = analyse_revenue_change(dataset, current, previous, baseline=[previous])
    assert implicit.to_dict() == explicit.to_dict(), (
        "baseline implicite et baseline explicite d'une seule periode doivent coincider"
    )
    assert implicit.observed_change_pct == pytest.approx(-0.4)
