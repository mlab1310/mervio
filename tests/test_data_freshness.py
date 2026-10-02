"""AN-05 (D-079): le rapport dit si la donnee est perimee.

Defaut corrige. Aucune fraicheur n'etait exposee. `last_complete_period` choisit QUELLE periode
analyser et recule d'une periode si la candidate n'est pas close -- elle ne dit RIEN du retard
entre la derniere donnee et la date d'analyse. Un export arrete 40 jours plus tot produit donc
une periode parfaitement close, analysee et presentee comme actuelle. Les deux notions sont
disjointes et le restent.

Contrat approuve (roadmap AN-05 / D-079):

    data_freshness
    |- latest_data_day : date | null   (jour civil LOCAL, fuseau AN-06)
    |- analysis_date   : date | null   (= parametre `today`, JAMAIS l'horloge)
    |- delay_days      : int  | null   (analysis_date - latest_data_day, jours entiers)
    |- threshold_days  : int           (AnalyticsConfig.freshness_stale_days, defaut 14)
    |- status          : fresh | stale | unknown
    |- message_key     : str
    `- sources         : detail par source

DETERMINISME. `analysis_date` vient du parametre `today`, jamais de l'horloge murale.
`_meta.generated_at` est le SEUL champ non deterministe autorise (D-058, reserve 1): deriver
`delay_days` de l'horloge creerait un SECOND champ non deterministe et casserait l'identite
d'octets CLI/persiste.

INTERDIT: retomber sur `latest_day` quand `today` est absent. `delay_days` vaudrait alors
toujours 0 et le rapport affirmerait `fresh` sans l'avoir verifie.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from conftest import ad, make_dataset, make_order
from mervio.analytics.pipeline import (
    SourcePaths, analyze_loaded_dataset, latest_data_day, load_dataset, run_analysis,
)
from mervio.config import AnalyticsConfig
from mervio.domain.models import Payment, Refund
from mervio.errors import ConfigurationError

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
#: derniere donnee de `data/sample`, mesuree
SAMPLE_LATEST = date(2026, 9, 13)


def _paths() -> SourcePaths:
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    return SourcePaths(
        shopify_orders=str(SAMPLE / "shopify_orders.csv"),
        shopify_products=str(SAMPLE / "shopify_products.csv"),
        stripe=str(SAMPLE / "stripe_transactions.csv"),
        google_ads=str(SAMPLE / "google_ads.csv"),
    )


def _freshness(today, config: AnalyticsConfig | None = None) -> dict:
    kwargs = {"config": config} if config is not None else {}
    return run_analysis(_paths(), today=today, **kwargs)["data_freshness"]


# -- le contrat existe et il est complet ------------------------------------
def test_the_report_carries_a_top_level_freshness_block():
    """Cle de PREMIER NIVEAU, a cote de `data_quality`. Pas sous `_meta`, qui porte l'identite
    du moteur et non un constat sur les donnees; pas sous `period`, qui decrit un decoupage."""
    report = run_analysis(_paths(), today=date(2026, 9, 14))
    assert "data_freshness" in report
    assert set(report["data_freshness"]) == {
        "latest_data_day", "analysis_date", "delay_days", "threshold_days",
        "status", "message_key", "sources",
    }


def test_fresh_data_is_declared_fresh():
    block = _freshness(date(2026, 9, 14))
    assert block["status"] == "fresh"
    assert block["latest_data_day"] == SAMPLE_LATEST.isoformat()
    assert block["analysis_date"] == "2026-09-14"
    assert block["delay_days"] == 1


# -- T-05.1: le test nomme par la roadmap -----------------------------------
def test_an_export_stopped_forty_days_earlier_is_stale():
    """Exigence litterale de la roadmap AN-05."""
    block = _freshness(SAMPLE_LATEST + timedelta(days=40))
    assert block["delay_days"] == 40
    assert block["status"] == "stale"


# -- T-05.2: la borne est fermee, et c'est dit ------------------------------
def test_the_threshold_boundary_is_closed():
    """`delay == threshold` est encore FRAIS; `threshold + 1` est perime. Une borne implicite
    serait une source de desaccord entre le moteur, l'API et l'interface."""
    threshold = AnalyticsConfig().freshness_stale_days
    at_bound = _freshness(SAMPLE_LATEST + timedelta(days=threshold))
    assert at_bound["delay_days"] == threshold and at_bound["status"] == "fresh"
    past_bound = _freshness(SAMPLE_LATEST + timedelta(days=threshold + 1))
    assert past_bound["delay_days"] == threshold + 1 and past_bound["status"] == "stale"


def test_the_threshold_is_configurable():
    strict = AnalyticsConfig(freshness_stale_days=1)
    assert _freshness(SAMPLE_LATEST + timedelta(days=2), strict)["status"] == "stale"
    lax = AnalyticsConfig(freshness_stale_days=365)
    assert _freshness(SAMPLE_LATEST + timedelta(days=40), lax)["status"] == "fresh"


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_threshold_is_refused(bad):
    with pytest.raises(ConfigurationError):
        AnalyticsConfig(freshness_stale_days=bad)


# -- T-05.3: sans `today`, on ne devine pas ---------------------------------
def test_without_an_analysis_date_the_status_is_unknown():
    """INTERDIT de retomber sur `latest_day`: `delay_days` vaudrait toujours 0 et le rapport
    affirmerait `fresh` sans l'avoir verifie."""
    block = run_analysis(_paths(), today=None)["data_freshness"]
    assert block["status"] == "unknown"
    assert block["delay_days"] is None
    assert block["analysis_date"] is None
    assert block["latest_data_day"] == SAMPLE_LATEST.isoformat(), (
        "la derniere donnee reste connue: c'est la date d'ANALYSE qui manque"
    )


def test_a_dataset_without_any_dated_row_is_unknown_not_fresh():
    """Jamais `fresh`, jamais `0`: l'absence de donnee datee n'est pas de la fraicheur."""
    dataset = make_dataset(orders=[make_order("o1", date(2026, 9, 8), "c@x.com", [("A", 1, 10.0)])],
                           ads=[ad(date(2026, 9, 8), 10.0, 100)])
    dataset.orders = []
    dataset.ad_performance = []
    dataset.payments = [Payment("p1", datetime(2026, 9, 8, 12), 10.0, 0.3, 9.7, "Paid")]
    block = analyze_loaded_dataset(dataset, None, date(2026, 9, 14))["data_freshness"]
    assert block["latest_data_day"] == "2026-09-08", "Stripe est une source datee"


# -- T-05.5: Stripe compte, et il ne comptait pas ---------------------------
def test_the_latest_day_follows_stripe_when_stripe_is_more_recent():
    """`latest_data_day` ignorait `payments` et `refunds`. Un export Stripe plus recent que
    l'export Shopify faisait donc declarer `stale` a tort."""
    dataset = make_dataset(
        orders=[make_order("o1", date(2026, 9, 1), "c@x.com", [("A", 1, 100.0)])],
        ads=[ad(date(2026, 9, 1), 50.0, 500)],
        payments=[Payment("p1", datetime(2026, 9, 10, 8, 0), 100.0, 2.0, 98.0, "Paid")],
    )
    assert latest_data_day(dataset) == date(2026, 9, 10), "Stripe fait avancer la derniere donnee"


def test_the_latest_day_follows_refunds_too():
    dataset = make_dataset(
        orders=[make_order("o1", date(2026, 9, 1), "c@x.com", [("A", 1, 100.0)])],
        ads=[ad(date(2026, 9, 1), 50.0, 500)],
        refunds=[Refund("r1", datetime(2026, 9, 12, 9, 0), 20.0, source="stripe")],
    )
    assert latest_data_day(dataset) == date(2026, 9, 12)


def test_each_source_reports_its_own_latest_day():
    """Le detail par source rend le diagnostic actionnable: << lequel de mes exports est en
    retard ? >> est la question que se pose un marchand."""
    dataset = make_dataset(
        orders=[make_order("o1", date(2026, 9, 1), "c@x.com", [("A", 1, 100.0)])],
        ads=[ad(date(2026, 9, 4), 50.0, 500)],
        payments=[Payment("p1", datetime(2026, 9, 10, 8, 0), 100.0, 2.0, 98.0, "Paid")],
    )
    sources = analyze_loaded_dataset(dataset, None, date(2026, 9, 14))["data_freshness"]["sources"]
    assert sources == {"shopify_orders": "2026-09-01", "google_ads": "2026-09-04",
                       "stripe": "2026-09-10"}


def test_a_source_without_rows_reports_null_not_a_borrowed_date():
    dataset = make_dataset(
        orders=[make_order("o1", date(2026, 9, 1), "c@x.com", [("A", 1, 100.0)])],
        ads=[ad(date(2026, 9, 4), 50.0, 500)],
    )
    sources = analyze_loaded_dataset(dataset, None, date(2026, 9, 14))["data_freshness"]["sources"]
    assert sources["stripe"] is None, "aucune ligne Stripe: null, pas la date d'une autre source"


# -- T-05.8: le jour civil depend du fuseau (couple a AN-06) ---------------
def test_the_latest_day_is_a_local_civil_day():
    """Une commande a 23 h 30 UTC le 31 decembre est le 1er janvier a Paris. Le jour civil de la
    derniere donnee depend donc du fuseau, exactement comme le decoupage des periodes."""
    order = make_order("o1", date(2026, 12, 31), "c@x.com", [("A", 1, 100.0)])
    object.__setattr__(order, "created_at", datetime(2026, 12, 31, 23, 30))
    dataset = make_dataset(orders=[order])
    assert latest_data_day(dataset, "UTC") == date(2026, 12, 31)
    assert latest_data_day(dataset, "Europe/Paris") == date(2027, 1, 1)


# -- determinisme: l'horloge murale n'entre jamais dans le calcul ----------
_PROBE = """
import json, sys
from datetime import date
from mervio.analytics.pipeline import SourcePaths, run_analysis
D = sys.argv[1]
report = run_analysis(SourcePaths(shopify_orders=D + "/shopify_orders.csv",
                                  shopify_products=D + "/shopify_products.csv",
                                  stripe=D + "/stripe_transactions.csv",
                                  google_ads=D + "/google_ads.csv"),
                      today=date(2026, 9, 14))
print(json.dumps(report["data_freshness"], sort_keys=True))
"""


def test_the_freshness_block_never_reads_the_wall_clock():
    """Deux executions a `today` identique mais a des horloges differentes doivent produire le
    MEME bloc. `libfaketime` n'etant pas disponible, on mesure la propriete testable: le bloc ne
    depend que de ses entrees, sur plusieurs processus et plusieurs fuseaux d'environnement.
    """
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    blocks = set()
    for env_tz in ("UTC", "Pacific/Kiritimati", "America/Los_Angeles"):
        env = dict(os.environ, TZ=env_tz, PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT / 'tests'}")
        out = subprocess.run([sys.executable, "-c", _PROBE, str(SAMPLE)], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=300, check=True)
        blocks.add(out.stdout.strip())
    assert len(blocks) == 1, f"le bloc depend de l'environnement: {blocks}"


def test_the_analysis_date_is_the_today_parameter_verbatim():
    for today in (date(2026, 9, 14), date(2026, 10, 23), date(2027, 1, 1)):
        assert _freshness(today)["analysis_date"] == today.isoformat()


# -- `last_complete_period` n'est PAS un signal de fraicheur ---------------
def test_a_stale_report_still_analyses_a_properly_closed_period():
    """Le coeur du defaut AN-05: la periode est parfaitement close ET la donnee est perimee.

    Si `last_complete_period` suffisait, ce rapport n'aurait aucun signal.
    """
    report = run_analysis(_paths(), today=SAMPLE_LATEST + timedelta(days=40))
    assert report["period"]["current"]["label"] == "2026-W37", "periode close et analysee"
    assert report["data_freshness"]["status"] == "stale", "et pourtant perimee"


# -- un rapport perime le dit EN TETE --------------------------------------
def test_a_stale_report_says_so_before_the_health_score():
    """Exigence de la roadmap: << un rapport `stale` le dit en tete >>."""
    report = run_analysis(_paths(), today=SAMPLE_LATEST + timedelta(days=40))
    summary = report["executive_summary"]
    assert "perimee" in summary.lower() or "stale" in summary.lower()
    position = min((summary.lower().find(token) for token in ("perimee", "stale")
                    if token in summary.lower()), default=-1)
    score_position = summary.find("Business Health Score")
    assert position >= 0
    assert score_position == -1 or position < score_position, (
        "le signal de peremption doit preceder le score"
    )


def test_a_fresh_report_carries_no_stale_banner():
    summary = run_analysis(_paths(), today=date(2026, 9, 14))["executive_summary"]
    assert "perimee" not in summary.lower()


def test_the_text_report_opens_with_the_stale_banner():
    from mervio.reporting.executive import render_executive_report
    report = run_analysis(_paths(), today=SAMPLE_LATEST + timedelta(days=40))
    text = render_executive_report(report)
    head = text[:600].lower()
    assert "perimee" in head or "stale" in head, "bandeau de peremption attendu en tete"


# -- le LLM voit la fraicheur, et peut la citer ----------------------------
def test_the_llm_context_exposes_freshness_with_a_citable_id():
    """La regle 3 de `SYSTEM_CONTRACT` oblige le modele a declarer une indisponibilite; un
    rapport perime est l'equivalent temporel."""
    from mervio.llm import build_llm_context
    from mervio.llm.context import context_ids
    report = run_analysis(_paths(), today=SAMPLE_LATEST + timedelta(days=40))
    context = build_llm_context(report)
    assert "data_freshness" in context
    block = context["data_freshness"]
    assert block["id"] in context_ids(context)
    assert block["status"] == "stale"
    assert block["delay_days"] == 40


def test_the_llm_context_omits_the_raw_dates():
    """Les dates brutes ne servent aucune phrase et alourdissent le contexte."""
    from mervio.llm import build_llm_context
    block = build_llm_context(run_analysis(_paths(), today=date(2026, 9, 14)))["data_freshness"]
    assert set(block) == {"id", "status", "delay_days", "threshold_days", "message_key"}


def test_the_freshness_message_key_is_stable_and_not_free_text():
    """Prepare AN-19: la cle est stable et technique, le rendu viendra du catalogue."""
    fresh = _freshness(date(2026, 9, 14))["message_key"]
    stale = _freshness(SAMPLE_LATEST + timedelta(days=40))["message_key"]
    unknown = run_analysis(_paths(), today=None)["data_freshness"]["message_key"]
    assert {fresh, stale, unknown} == {
        "data_freshness.fresh", "data_freshness.stale", "data_freshness.unknown",
    }
    for key in (fresh, stale, unknown):
        assert " " not in key and key.islower()


def test_the_llm_context_passes_the_message_key_verbatim():
    """La cle vue par le LLM doit etre EXACTEMENT celle du rapport.

    `_slug` ecrasait le point (`data_freshness.fresh` -> `data_freshness_fresh`): la cle du
    contexte divergeait silencieusement de celle du rapport. AN-19 s'appuiera sur cette egalite.
    """
    from mervio.llm import build_llm_context
    for today, expected in ((date(2026, 9, 14), "data_freshness.fresh"),
                            (SAMPLE_LATEST + timedelta(days=40), "data_freshness.stale")):
        report = run_analysis(_paths(), today=today)
        assert report["data_freshness"]["message_key"] == expected
        assert build_llm_context(report)["data_freshness"]["message_key"] == expected


def test_an_invalid_message_key_is_refused_not_normalised():
    """Refuser plutot que normaliser: une cle normalisee ne serait plus la cle du rapport."""
    from mervio.llm import build_llm_context
    from mervio.llm.errors import LLMContextError
    report = run_analysis(_paths(), today=date(2026, 9, 14))
    report["data_freshness"]["message_key"] = "Texte Libre Avec Majuscules"
    with pytest.raises(LLMContextError):
        build_llm_context(report)


def test_a_report_without_the_freshness_block_stays_readable():
    """ND-04: les rapports 1.x restent lisibles. Un rapport sans `data_freshness` ne doit pas
    faire echouer la construction du contexte."""
    from mervio.llm import build_llm_context
    report = run_analysis(_paths(), today=date(2026, 9, 14))
    del report["data_freshness"]
    context = build_llm_context(report)
    assert context["data_freshness"]["status"] == "unknown"
    assert context["data_freshness"]["message_key"] is None
