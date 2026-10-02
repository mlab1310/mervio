"""AN-02 (D-072 = A, D-074, D-075, ND-05 = A): decomposition du CA a deux couches.

Defaut corrige. << Taux de conversion >> designait TOUTES les commandes divisees par les SEULS
clics payants. Une baisse du trafic organique apparaissait donc comme un probleme de conversion,
et le moteur recommandait d'auditer le checkout. Le scenario `order_volume_drop` documentait ce
mensonge comme ATTENDU: << le moteur ne mesure que les clics Google Ads: une baisse organique
apparait comme une baisse de conversion >>.

Structure ratifiee (D-072 = A):

    COUCHE A -- identite arithmetique du CA, ADDITIVE, somme des contributions = 1
        Revenue = Commandes x Panier moyen
        order_volume . average_order_value

    COUCHE B -- explications du VOLUME DE COMMANDES, NON additives
        paid_traffic           observe
        organic_traffic        NON observe
        site_conversion_rate   NON observe

Pourquoi la couche B ne porte PAS de `contribution`: une contribution est une part de la
variation du CA. La couche B explique la variation du VOLUME DE COMMANDES. Mettre les deux dans
un meme champ serait exactement le melange de referentiels qu'AN-01 vient de supprimer. La
couche B porte donc `explains` + `explains_share`, et la somme des `contribution` reste exacte
sur la seule couche A.

Pas d'inference interdite (D-075): sans `web_analytics_sessions` observees, le trafic organique
et la conversion du site ne sont pas mesures, donc jamais chiffres et jamais recommandes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from mervio.analytics.pipeline import SourcePaths, run_analysis

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
TIES = Path(__file__).resolve().parent / "fixtures" / "f01_ties"
DECLINE = Path(__file__).resolve().parent / "fixtures" / "an01_prolonged_decline"

LAYER_A = {"order_volume", "average_order_value"}
LAYER_B_OBSERVED = {"paid_traffic"}
LAYER_B_NOT_OBSERVED = {"organic_traffic", "site_conversion_rate"}


def _report(directory: Path, today: date) -> dict:
    paths = {"shopify_orders": str(directory / "shopify_orders.csv"),
             "shopify_products": str(directory / "shopify_products.csv"),
             "google_ads": str(directory / "google_ads.csv")}
    if (directory / "stripe_transactions.csv").exists():
        paths["stripe"] = str(directory / "stripe_transactions.csv")
    return run_analysis(SourcePaths(**paths), today=today)


@pytest.fixture(scope="module")
def sample() -> dict:
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    return _report(SAMPLE, date(2026, 9, 14))


@pytest.fixture(scope="module")
def decline() -> dict:
    return _report(DECLINE, date(2026, 9, 21))


def _factors(report: dict) -> dict:
    return {f["factor"]: f for f in report["root_causes"][0]["factors"]}


# -- D-074: le facteur conversion_rate DISPARAIT, sans alias ---------------
def test_the_conversion_rate_factor_no_longer_exists(sample, decline):
    for report in (sample, decline):
        assert "conversion_rate" not in _factors(report), (
            "AN-02: << toutes commandes / clics payants >> n'est pas un taux de conversion"
        )


def test_no_factor_claims_to_measure_site_conversion_from_paid_clicks(sample):
    """Garde semantique: aucun facteur OBSERVE ne doit pretendre mesurer la conversion."""
    for key, factor in _factors(sample).items():
        if factor["observability"] != "observed":
            continue
        assert "conversion" not in key or key == "orders_per_paid_click", (
            f"{key}: un facteur observe ne peut pas pretendre mesurer la conversion du site"
        )


# -- ND-05 = A et D-074: nommage des KPI ----------------------------------
def test_the_paid_clicks_kpi_key_is_unambiguous(sample):
    """ND-05 = A: `kpis.clicks` et `series.paid_clicks` designaient la MEME grandeur.

    Un contrat public avec deux noms pour une grandeur serait fige par l'API 004.6.
    """
    assert "paid_clicks" in sample["kpis"]
    assert "clicks" not in sample["kpis"], "l'ancienne cle ambigue est retiree, sans alias"
    assert sample["kpis"]["paid_clicks"]["key"] == "paid_clicks"


def test_the_orders_per_paid_click_kpi_replaces_the_conversion_rate_one(sample):
    assert "orders_per_paid_click" in sample["kpis"]
    assert "paid_conversion_rate" not in sample["kpis"], "ancienne cle retiree, sans alias"
    label = sample["kpis"]["orders_per_paid_click"]["label"].lower()
    assert "taux de conversion" not in label, (
        "le libelle ne doit plus appeler cela un taux de conversion"
    )


def test_the_series_and_kpi_keys_agree(sample):
    for key in ("paid_clicks", "orders_per_paid_click"):
        assert key in sample["time_series"], f"{key} absent des series"
        assert key in sample["kpis"], f"{key} absent des KPI"


# -- D-072: couche A additive, somme exacte -------------------------------
def test_layer_a_is_exactly_the_revenue_identity(sample, decline):
    for report in (sample, decline):
        factors = _factors(report)
        present = {k for k, f in factors.items() if f["layer"] == "a"}
        assert present == LAYER_A, f"couche A attendue {LAYER_A}, obtenue {present}"


def test_layer_a_contributions_sum_to_one(sample, decline):
    for report in (sample, decline):
        shares = [f["contribution"] for f in _factors(report).values()
                  if f["layer"] == "a" and f["contribution"] is not None]
        assert len(shares) == 2
        assert sum(shares) == pytest.approx(1.0, abs=1e-6)


def test_layer_b_carries_no_revenue_contribution(sample, decline):
    """Anti-melange de referentiels: la couche B explique le VOLUME, pas le CA."""
    for report in (sample, decline):
        for key, factor in _factors(report).items():
            if factor["layer"] == "b":
                assert factor["contribution"] is None, (
                    f"{key}: une contribution de couche B serait une part de la variation du CA, "
                    "ce qu'elle n'est pas"
                )


def test_layer_b_observed_declares_what_it_explains(sample):
    paid = _factors(sample)["paid_traffic"]
    assert paid["layer"] == "b" and paid["observability"] == "observed"
    assert paid["explains"] == "order_volume"
    assert paid["explains_share"] is not None


# -- D-073: invariants des facteurs NON observes ---------------------------
def test_the_not_observed_factors_are_present_and_named(sample, decline):
    """Un angle mort doit etre DECLARE. Avant AN-02 il etait une absence pure, donc invisible."""
    for report in (sample, decline):
        factors = _factors(report)
        for key in LAYER_B_NOT_OBSERVED:
            assert key in factors, f"{key} doit apparaitre comme facteur non observe"
            assert factors[key]["observability"] == "not_observed"


@pytest.mark.parametrize("field", ["current_value", "previous_value", "factor_change_pct",
                                   "contribution", "confidence", "hypothesis", "explains_share"])
def test_a_not_observed_factor_fabricates_no_value(sample, field):
    """`contribution: 0.0` serait une AFFIRMATION d'absence d'effet -- la meme faute que
    << CAC = 0 >>. Tout champ chiffre ou interpretatif doit etre null."""
    for key in LAYER_B_NOT_OBSERVED:
        assert _factors(sample)[key][field] is None, f"{key}.{field} doit etre null"


def test_a_not_observed_factor_names_its_missing_source(sample):
    for key in LAYER_B_NOT_OBSERVED:
        missing = _factors(sample)[key]["missing_source"]
        assert missing, f"{key}: la source manquante doit etre nommee"
        assert "web_analytics" in missing or "sessions" in missing


def test_a_not_observed_factor_is_never_the_primary_factor(sample, decline):
    for report in (sample, decline):
        primary = report["root_causes"][0]["primary_factor"]
        assert primary not in LAYER_B_NOT_OBSERVED, (
            f"{primary} n'est pas mesure: il ne peut pas etre designe comme cause"
        )


def test_not_observed_factors_come_last_in_lexicographic_order(sample):
    keys = [f["factor"] for f in sample["root_causes"][0]["factors"]]
    observed = [k for k in keys if _factors(sample)[k]["observability"] == "observed"]
    unobserved = [k for k in keys if _factors(sample)[k]["observability"] == "not_observed"]
    assert keys == observed + unobserved, "les non observes doivent venir APRES les observes"
    assert unobserved == sorted(unobserved), "ordre lexicographique entre non observes"


# -- D-075: aucune recommandation checkout sans sessions observees --------
def test_no_checkout_recommendation_is_ever_emitted(sample, decline):
    """`web_analytics_sessions` n'est observe par aucune source aujourd'hui."""
    for report in (sample, decline):
        for item in (report["critical_issues"] + report["warnings"]
                     + report["opportunities"]):
            recommendation = (item.get("recommendation") or "").lower()
            assert "checkout" not in recommendation, (
                f"{item['type']}: recommandation checkout sans trafic total observe"
            )


def test_no_recommendation_category_is_conversion_checkout(sample, decline):
    for report in (sample, decline):
        categories = {r.get("category") for r in report.get("recommendations", [])}
        assert "conversion_checkout" not in categories


# -- ordre des facteurs: deterministe a contributions EGALES --------------
def test_factor_ordering_is_total_under_equal_contributions():
    """Question posee explicitement par le GO.

    `analysis.factors.sort(key=abs(contribution))` et `primary_factor()` utilisant
    `max(key=abs(contribution))` sont des cles NON TOTALES: a egalite l'ordre d'entree decidait.
    Mesure a 7580ad4 sur `f01_ties`: `paid_traffic` et `average_order_value` etaient tous deux a
    -0.0.
    """
    report = _report(TIES, date(2026, 9, 14))
    factors = report["root_causes"][0]["factors"]
    keys = [(0 if f["observability"] == "observed" else 1,
             {"a": 0, "b": 1}.get(f["layer"], 9),
             -abs(f["contribution"]) if f["contribution"] is not None else 0.0,
             f["factor"]) for f in factors]
    assert keys == sorted(keys), f"ordre des facteurs non conforme a une cle totale: {keys}"


_ORDER_PROBE = """
import json, sys
from datetime import date
from mervio.analytics.pipeline import SourcePaths, run_analysis
D = sys.argv[1]
report = run_analysis(SourcePaths(shopify_orders=D + "/shopify_orders.csv",
                                 shopify_products=D + "/shopify_products.csv",
                                 google_ads=D + "/google_ads.csv"),
                      today=date(2026, 9, 14))
root = report["root_causes"][0]
print(json.dumps({"order": [f["factor"] for f in root["factors"]],
                  "primary": root["primary_factor"]}))
"""


def test_factor_ordering_is_identical_across_hash_seeds():
    """Preuve de la PROPRIETE, pas de l'implementation, et sur plusieurs processus."""
    results = set()
    for seed in ("0", "1", "2", "7", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT / 'tests'}")
        out = subprocess.run([sys.executable, "-c", _ORDER_PROBE, str(TIES)],
                             cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=300, check=True)
        results.add(out.stdout.strip())
    assert len(results) == 1, f"ordre ou facteur primaire instable: {results}"


def test_the_primary_factor_comes_from_a_measured_quantity(sample, decline):
    for report in (sample, decline):
        root = report["root_causes"][0]
        primary = root["primary_factor"]
        if primary is None:
            continue
        factor = _factors(report)[primary]
        assert factor["observability"] == "observed"
        assert factor["contribution"] is not None or factor["explains_share"] is not None


# -- D-089 = 0.5: seuil ratifie sur MESURE, centralise --------------------
def test_the_paid_traffic_primacy_threshold_is_the_ratified_value():
    """D-089 ratifiee a 0,5 sur la mesure, pas par calibration.

    17 scenarios x 2 profils x 3 graines = 51 executions: AUCUNE valeur de
    `paid_traffic.explains_share` ne tombe dans [0,40 ; 0,60], la plus proche etant 0,719 (0,219
    d'ecart). La distribution est bimodale. La specification Phase 1 craignait une marge de 2,6
    points sur `traffic_drop`/`fashion_eu`; mesure, ce scenario donne 1,0023 / 0,9942 / 0,9993.
    """
    from mervio.config import RootCauseConfig
    assert RootCauseConfig().paid_traffic_primacy_share == 0.5


def test_the_primacy_threshold_has_no_hard_coded_duplicate():
    """Le seuil vit dans `RootCauseConfig` et nulle part ailleurs.

    Un duplicata en dur divergerait en silence du jour ou la configuration changerait.
    """
    import ast
    source = (ROOT / "src" / "mervio" / "analytics" / "root_cause.py").read_text(encoding="utf-8")
    offenders = [
        node.lineno for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Compare)
        and any(isinstance(c, ast.Constant) and c.value == 0.5 for c in node.comparators)
    ]
    assert not offenders, f"comparaison a 0.5 en dur, lignes {offenders}"
    assert "paid_traffic_primacy_share" in source, "le seuil doit venir de la configuration"


def test_the_primacy_threshold_actually_gates_the_primary_factor():
    """Garde anti-placebo: si le seuil n'etait pas consomme, le rendre inatteignable ne
    changerait rien."""
    from dataclasses import replace
    from mervio.config import AnalyticsConfig
    paths = SourcePaths(shopify_orders=str(DECLINE / "shopify_orders.csv"),
                        shopify_products=str(DECLINE / "shopify_products.csv"),
                        google_ads=str(DECLINE / "google_ads.csv"))
    base = AnalyticsConfig()
    permissive = AnalyticsConfig(root_cause=replace(base.root_cause,
                                                    paid_traffic_primacy_share=0.0))
    strict = AnalyticsConfig(root_cause=replace(base.root_cause,
                                                paid_traffic_primacy_share=1e9))
    low = run_analysis(paths, config=permissive, today=date(2026, 9, 21))
    high = run_analysis(paths, config=strict, today=date(2026, 9, 21))
    assert low["root_causes"][0]["primary_factor"] == "paid_traffic", (
        "seuil nul: toute part de couche B suffit a la primaute"
    )
    assert high["root_causes"][0]["primary_factor"] == "order_volume", (
        "seuil inatteignable: la couche A garde la primaute"
    )


def test_layer_a_and_layer_b_are_never_ranked_on_one_flat_scale():
    """D-072, et raison du KNOWN_GAP de `mixed_root_causes`.

    Les deux couches mesurent des quantites differentes: la couche A des parts de la variation
    du CA, la couche B des parts de la variation du VOLUME. Aucune regle inter-couches n'existe,
    et il ne doit pas en apparaitre une par inadvertance: la primaute consulte la couche B
    UNIQUEMENT quand la couche A est dominee par `order_volume`.
    """
    from mervio.analytics.root_cause import LAYER_ORDER_VOLUME, LAYER_REVENUE
    report = _report(SAMPLE, date(2026, 9, 14)) if (SAMPLE / "shopify_orders.csv").exists() \
        else _report(DECLINE, date(2026, 9, 21))
    factors = _factors(report)
    layer_a = {k: f for k, f in factors.items() if f["layer"] == LAYER_REVENUE}
    layer_b = {k: f for k, f in factors.items() if f["layer"] == LAYER_ORDER_VOLUME}
    assert all(f["contribution"] is not None for f in layer_a.values())
    assert all(f["contribution"] is None for f in layer_b.values())
    dominant_a = max(layer_a.values(), key=lambda f: abs(f["contribution"]))
    primary = report["root_causes"][0]["primary_factor"]
    if primary in layer_b:
        assert dominant_a["factor"] == "order_volume", (
            "la couche B ne peut etre primaire que si la couche A est dominee par le VOLUME"
        )
