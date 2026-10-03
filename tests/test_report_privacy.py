"""AN-17: le rapport 2.0 ne porte plus aucun identifiant client.

DEFAUT CORRIGE. `report.assemble_report` publiait, pour chacun des cinq plus gros clients,
`customer_id = "cust_" + sha256(customer_ref)[:12]`. Trois proprietes en faisaient un
IDENTIFIANT, pas une statistique:

1. DETERMINISTE ET RECALCULABLE. `customer_ref` est deja un HMAC a cle (D-053); le SHA-256 qui
   le suit n'est ni sale ni a cle. Qui detient la cle d'identite de l'organisation -- le SaaS
   lui-meme -- recalcule `cust_<...>` depuis une simple liste d'e-mails et retrouve le client.
2. STABLE DANS LE TEMPS. Le meme client porte le meme pseudonyme dans TOUS les rapports de
   l'organisation. C'etait presente comme une fonctionnalite (D-023: "un client reste suivable
   dans le temps"). C'est exactement ce qui distingue un pseudo-identifiant d'un agregat.
3. DANS UN ARTEFACT NON EFFACABLE. `reports` est protegee par `reports_forbid_update` (revision
   0003): AUCUNE mise a jour, jamais. L'effacement client (D-056, revision 0014) remplace
   `orders.customer_ref` par un tombstone et detruit les octets bruts -- il ne touche pas, et ne
   PEUT pas toucher, un rapport deja persiste. Le pseudonyme d'un client efface survivait donc
   dans chaque rapport historique.

CONTRAT 2.0 -- `customers.top_customers[]`:

    rank          int         rang par CA decroissant, 1..5, sans trou
    orders        int         nombre de commandes du client sur la periode
    revenue_share float|null  part du CA de la periode (meme denominateur que
                              `customer_concentration_top5`), null si le CA est nul

Aucune autre cle. Le CA absolu reste DERIVABLE (`revenue_share` x `kpis.revenue.value`): rien
n'est perdu, seule la designation disparait.

CE QUI N'EST PAS SUPPRIME. Un agregat n'est pas un identifiant. `total_active`,
`new_customers`, `repeat_purchase_rate`, `concentration_top5`, la dimension de sante
`customer_health` et le champ de qualite `customer_identity` mesurent une POPULATION et
restent intacts.

FRONTIERE 1.x / 2.0 (ND-04). Les rapports 1.x deja produits portent encore leur pseudonyme et
ne sont PAS reecrits: aucune migration, aucune mise a jour d'une table immuable. Ils restent
lisibles -- `render_executive_report` n'a jamais rendu `top_customers`, et `build_llm_context`
ne copie jamais le bloc `customers`. Un pseudonyme 1.x n'atteint donc pas le modele.

EFFET DE BORD MESURE (D-058). Le pseudonyme etait la SEULE partie du rapport qui dependait de
la cle d'identite. Sans lui, deux analyses des memes octets avec des cles differentes produisent
le MEME rapport: le risque residuel de D-058 -- "sans cle explicite, les rapports CLI ne sont pas
reproductibles octet pour octet" -- disparait.
"""
from __future__ import annotations

import json
import re
from datetime import date

import pytest

from conftest import make_dataset, make_order
from mervio.analytics import report as report_module
from mervio.analytics.pipeline import analyze_loaded_dataset, run_analysis
from mervio.identity import CustomerIdentity
from mervio.llm import build_llm_context
from mervio.llm.context import serialize_context
from mervio.reporting.executive import render_executive_report

#: toute forme de designation d'un client, directe ou pseudonyme
IDENTIFIER_SHAPES = (
    re.compile(r"cust_[0-9a-f]{6,}"),          # pseudonyme de rapport 1.x
    re.compile(r"\b(?:c1|g1):[0-9a-f]{32}\b"),  # reference a cle persistee (D-053)
    re.compile(r"redacted:[0-9a-f-]{36}"),      # tombstone d'effacement (D-056)
    re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+"),    # e-mail (identite d'avant 004.4.2)
)

REF_A = "c1:" + "a1" * 16
REF_B = "c1:" + "b2" * 16
REF_C = "g1:" + "c3" * 16


def _refs_in(text: str):
    return [m.group(0) for pattern in IDENTIFIER_SHAPES for m in pattern.finditer(text)]


#: semaine 2026-W37, close: la derniere commande tombe le DERNIER jour de la periode, sinon
#: `last_complete_period` recule d'une semaine et la fenetre est vide.
WEEK_DAYS = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 13))
ANALYSIS_DAY = date(2026, 9, 21)


def _dataset(refs=(REF_A, REF_B, REF_C)):
    """Trois clients de CA strictement decroissant (900, 400, 100), sur une semaine close."""
    orders = []
    for index, ref in enumerate(refs):
        for position in range(3 - index):
            orders.append(make_order(f"#{index}{position}", WEEK_DAYS[position], ref,
                                     [("A", 1, 100.0 * (3 - index))]))
    ds = make_dataset(orders=orders)
    ds.quality.set_field("customer_identity", covered=len(orders), total=len(orders))
    return ds


@pytest.fixture
def report():
    return analyze_loaded_dataset(_dataset(), today=ANALYSIS_DAY)


@pytest.fixture
def sample(sample_paths):
    return run_analysis(sample_paths, today=date(2026, 9, 14))


# -- 1. l'exposition elle-meme ------------------------------------------------------------

def test_the_report_exposes_no_customer_identifier_at_all(sample):
    """Critere d'acceptation AN-17: aucune cle `customer_id` dans le rapport."""
    payload = json.dumps(sample, ensure_ascii=False)
    assert _refs_in(payload) == []
    assert '"customer_id"' not in payload and '"customer_ref"' not in payload


def test_no_top_customer_entry_carries_a_designation(sample):
    for entry in sample["customers"]["top_customers"]:
        assert set(entry) == {"rank", "orders", "revenue_share"}


def test_the_pseudonymisation_helper_no_longer_exists():
    """Le defaut est la FONCTION autant que son appel: une aide qui transforme une reference
    client en pseudonyme stable n'a plus d'emploi legitime dans le moteur."""
    assert not hasattr(report_module, "pseudonymise")


def test_the_report_does_not_depend_on_the_identity_key(sample_paths):
    """Deux cles differentes, memes octets: le rapport est desormais IDENTIQUE. C'etait
    impossible tant que le pseudonyme etait derive de la reference (D-058, risque residuel)."""
    first = run_analysis(sample_paths, today=date(2026, 9, 14), identity=CustomerIdentity.ephemeral())
    second = run_analysis(sample_paths, today=date(2026, 9, 14), identity=CustomerIdentity.ephemeral())
    first["_meta"].pop("generated_at"), second["_meta"].pop("generated_at")
    assert first == second


def test_two_datasets_differing_only_by_their_references_give_the_same_report():
    """Formulation forte de la meme propriete, sans passer par le chargement CSV."""
    a = analyze_loaded_dataset(_dataset(), today=ANALYSIS_DAY)
    b = analyze_loaded_dataset(_dataset(refs=("c1:" + "f0" * 16, "g1:" + "e1" * 16, "c1:" + "d2" * 16)),
                               today=ANALYSIS_DAY)
    a["_meta"].pop("generated_at"), b["_meta"].pop("generated_at")
    assert a == b


def test_an_erased_customer_leaves_no_tombstone_in_the_report():
    """Apres un effacement (D-056), la reference devient `redacted:<uuid4>`. Elle ne doit pas
    davantage apparaitre: le rapport ne porte plus AUCUNE reference, quelle que soit sa forme."""
    tombstone = "redacted:6f1c4a2e-0b7d-4c3a-9f55-2d8e7b1a4c90"
    produced = analyze_loaded_dataset(_dataset(refs=(tombstone, REF_B, REF_C)), today=ANALYSIS_DAY)
    assert _refs_in(json.dumps(produced, ensure_ascii=False)) == []


# -- 2. le contrat qui remplace le pseudonyme ---------------------------------------------

def test_rank_is_dense_and_ordered_by_decreasing_revenue_share(report):
    entries = report["customers"]["top_customers"]
    assert [e["rank"] for e in entries] == list(range(1, len(entries) + 1))
    shares = [e["revenue_share"] for e in entries]
    assert shares == sorted(shares, reverse=True)


def test_order_counts_are_preserved(report):
    assert [e["orders"] for e in report["customers"]["top_customers"]] == [3, 2, 1]


def test_the_shares_sum_to_the_published_concentration(report):
    """Meme denominateur que `customer_concentration_top5`: les deux chiffres du rapport ne
    peuvent pas se contredire. Tolerance = l'arrondi a 4 decimales de chaque part, celui de
    tout ratio du contrat (`Metric.to_dict`)."""
    shares = [e["revenue_share"] for e in report["customers"]["top_customers"]]
    concentration = report["customers"]["concentration_top5"]["value"]
    assert sum(shares) == pytest.approx(concentration, abs=5e-4)


def test_absolute_revenue_stays_derivable(report):
    """Rien n'est perdu: part x CA de la periode redonne le CA du client, a l'arrondi de la
    part publiee pres."""
    revenue = report["kpis"]["revenue"]["value"]
    top = report["customers"]["top_customers"][0]
    assert top["revenue_share"] * revenue == pytest.approx(900.0, abs=0.1)


def test_a_period_without_revenue_reports_an_unknown_share_never_zero():
    """Un CA nul ne permet pas de calculer une part. `null`, jamais 0.0, comme le fait deja
    `customer_concentration_top5`."""
    orders = [make_order("#1", WEEK_DAYS[-1], REF_A, [("A", 1, 0.0)])]
    produced = analyze_loaded_dataset(make_dataset(orders=orders), today=ANALYSIS_DAY)
    assert produced["kpis"]["revenue"]["value"] == 0.0
    assert [e["revenue_share"] for e in produced["customers"]["top_customers"]] == [None]


def test_at_most_five_customers_are_published():
    orders = [make_order(f"#{index}", WEEK_DAYS[-1], f"c1:{index:02d}" + "0" * 30,
                         [("A", 1, 100.0 - index)]) for index in range(9)]
    produced = analyze_loaded_dataset(make_dataset(orders=orders), today=ANALYSIS_DAY)
    entries = produced["customers"]["top_customers"]
    assert produced["customers"]["total_active"] == 9 and len(entries) == 5
    assert [e["rank"] for e in entries] == [1, 2, 3, 4, 5]


# -- 3. ce qui DOIT survivre: les agregats ne sont pas des identifiants --------------------

def test_aggregate_customer_metrics_are_untouched(sample):
    customers = sample["customers"]
    assert customers["total_active"] > 0
    for key in ("new_customers", "repeat_purchase_rate", "concentration_top5"):
        assert customers[key] is not None and customers[key]["value"] is not None
    dimensions = {d["key"] for d in sample["business_health_score"]["dimensions"]}
    assert "customer_health" in dimensions


def test_the_customer_identity_quality_field_is_still_measured(sample):
    fields = {f["field"] for f in sample["data_quality"]["fields"]}
    assert "customer_identity" in fields


# -- 4. constats, preuves, rendu, contexte LLM ---------------------------------------------

def test_findings_and_evidence_carry_no_identifier(sample):
    findings = (sample["critical_issues"] + sample["warnings"] + sample["opportunities"]
                + sample["anomalies"] + sample["root_causes"] + sample["recommendations"])
    assert findings
    assert _refs_in(json.dumps(findings, ensure_ascii=False)) == []


def test_the_readable_report_carries_no_identifier(sample):
    assert _refs_in(render_executive_report(sample)) == []


def test_the_llm_context_carries_no_identifier(sample):
    context = build_llm_context(sample)
    assert "customers" not in context
    assert _refs_in(serialize_context(context)) == []


def test_the_serialized_deliverable_carries_no_identifier(tmp_path, sample_paths):
    """Les trois livrables ecrits sur disque: report.json, report.txt, data_quality.json."""
    from mervio.application.service import AnalysisRequest, analyze_dataset
    from mervio.reporting.writers import write_outputs
    result = analyze_dataset(AnalysisRequest(
        shopify_orders=sample_paths.shopify_orders, shopify_products=sample_paths.shopify_products,
        stripe=sample_paths.stripe, google_ads=sample_paths.google_ads,
        today=date(2026, 9, 14), label="an-17",
    ))
    written = write_outputs(result, tmp_path)
    assert len(written) == 3
    for path in written:
        assert _refs_in(path.read_text(encoding="utf-8")) == [], path.name


# -- 5. frontiere 1.x / 2.0 (ND-04) --------------------------------------------------------

def _as_1x(produced: dict) -> dict:
    """Rapport 1.x: le bloc `top_customers` d'avant AN-17, pseudonyme compris."""
    legacy = json.loads(json.dumps(produced))
    legacy["customers"]["top_customers"] = [
        {"customer_id": "cust_0123456789ab", "orders": 3, "revenue": 900.0},
        {"customer_id": "cust_fedcba987654", "orders": 2, "revenue": 400.0},
    ]
    return legacy


def test_a_1x_report_stays_readable(report):
    """ND-04: on ne reecrit pas les anciens rapports, donc on doit encore savoir les lire."""
    rendered = render_executive_report(_as_1x(report))
    assert "CUSTOMER HEALTH" in rendered


def test_a_1x_pseudonym_never_reaches_the_model(report):
    """Le bloc `customers` n'entre pas dans le contexte: la garantie tient aussi pour les
    rapports produits AVANT AN-17, qui portent encore leur pseudonyme."""
    legacy = _as_1x(report)
    context = build_llm_context(legacy)
    assert "customers" not in context
    assert "cust_" not in serialize_context(context)
