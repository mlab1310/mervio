"""Contrat de revenu (D-041, Mission 003.2).

Constate sur un registre de ventes reel depose publiquement (Mission 003.1,
OH5): le "Subtotal" de l'export Shopify est deja net des remises de commande.
L'ancien calcul `Subtotal - Discount Amount` soustrayait la remise deux fois.

Niveaux de preuve, a ne pas confondre:
- cas B (remise a 100 %): prouve par la source reelle (30 commandes sur 30);
- cas C (remise partielle): contrat documentaire (API Admin Shopify,
  `subtotalPriceSet` = somme des lignes APRES remises). OH5 n'en contient aucune.
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from mervio.analytics.kpi import compute_kpis
from mervio.analytics.periods import make_period
from mervio.analytics.pipeline import SourcePaths, load_dataset
from mervio.analytics.profitability import compute_profitability

HEADER = ("Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,Shipping,Taxes,Total,"
          "Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku\n")
PERIOD = make_period(date(2026, 9, 7), "week")
SRC = Path(__file__).resolve().parent.parent / "src" / "mervio"


def _analyse(tmp_path, body):
    path = tmp_path / "orders.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    dataset = load_dataset(SourcePaths(shopify_orders=str(path)))
    return dataset, compute_kpis(dataset, PERIOD)


def _issue_kinds(dataset):
    return {issue.kind for issue in dataset.quality.issues}


def test_case_a_no_discount(tmp_path):
    ds, kpis = _analyse(tmp_path, "#1,a@x.com,paid,2026-09-08 10:00:00,EUR,100.00,0.00,0.00,20.00,120.00,,1,A,100.00,A\n")
    assert kpis["revenue"].value == pytest.approx(100.0)
    assert "subtotal_convention_contradiction" not in _issue_kinds(ds)


def test_case_b_full_discount_as_observed_in_the_real_record(tmp_path):
    """OH5: ligne 100, remise 100, Subtotal 0, Total 0. CA 0, jamais -100."""
    ds, kpis = _analyse(tmp_path, "#1,a@x.com,paid,2026-09-08 10:00:00,USD,0.00,100.00,0.00,0.00,0.00,,1,A,100.00,A\n")
    assert kpis["revenue"].value == pytest.approx(0.0)
    assert kpis["aov"].value == pytest.approx(0.0)
    assert "subtotal_convention_contradiction" not in _issue_kinds(ds)


def test_case_c_partial_discount_documented_contract(tmp_path):
    """Contrat documentaire, non prouve par OH5: ligne 100, remise 20, Subtotal 80, taxes 16, Total 96."""
    ds, kpis = _analyse(tmp_path, "#1,a@x.com,paid,2026-09-08 10:00:00,EUR,80.00,20.00,0.00,16.00,96.00,,1,A,100.00,A\n")
    assert kpis["revenue"].value == pytest.approx(80.0)
    assert "subtotal_convention_contradiction" not in _issue_kinds(ds)


def test_case_d_multi_line_order_counts_the_discount_once(tmp_path):
    body = ("#1,a@x.com,paid,2026-09-08 10:00:00,EUR,75.00,25.00,0.00,15.00,90.00,,1,A,60.00,A\n"
            "#1,,,,,,,,,,,1,B,40.00,B\n")
    ds, kpis = _analyse(tmp_path, body)
    assert kpis["orders"].value == 1 and kpis["units"].value == 2
    assert kpis["revenue"].value == pytest.approx(75.0)
    # la valeur des lignes reste brute (prix catalogue x quantite): elle ne porte pas la remise de commande
    assert sum(item.line_revenue for item in ds.orders[0].items) == pytest.approx(100.0)


def test_case_e_refund_is_never_subtracted_from_revenue_nor_doubled(tmp_path):
    body = "#1,a@x.com,refunded,2026-09-08 10:00:00,EUR,80.00,20.00,0.00,16.00,96.00,96.00,1,A,100.00,A\n"
    ds, kpis = _analyse(tmp_path, body)
    assert kpis["revenue"].value == pytest.approx(80.0)        # ni 80 - 96, ni 80 - 20
    assert kpis["refunds"].value == pytest.approx(96.0)        # montant Shopify tel quel, compte une fois
    profitability = compute_profitability(ds.window(PERIOD.start, PERIOD.end), PERIOD.label)
    assert profitability.components["refunds"].value == pytest.approx(96.0)
    assert profitability.partial_contribution_profit == pytest.approx(80.0 - 96.0)  # une seule deduction


def test_file_contradicting_the_contract_is_flagged_not_silently_converted(tmp_path):
    """Total = Subtotal - remise + taxes: le fichier suppose un Subtotal avant remise."""
    body = "#1,a@x.com,paid,2026-09-08 10:00:00,EUR,100.00,20.00,0.00,16.00,96.00,,1,A,100.00,A\n"
    ds, kpis = _analyse(tmp_path, body)
    assert kpis["revenue"].value == pytest.approx(100.0)       # contrat applique...
    issue = next(i for i in ds.quality.issues if i.kind == "subtotal_convention_contradiction")
    assert issue.severity == "error" and "20.00" in issue.message   # ...et surestimation maximale annoncee


def test_tax_inclusive_totals_are_not_mistaken_for_a_contradiction(tmp_path):
    body = "#1,a@x.com,paid,2026-09-08 10:00:00,EUR,80.00,20.00,5.00,13.33,85.00,,1,A,100.00,A\n"
    ds, _ = _analyse(tmp_path, body)
    assert "subtotal_convention_contradiction" not in _issue_kinds(ds)


def test_no_code_path_subtracts_the_discount_from_the_subtotal_again():
    """Garde structurelle: seule la verification du contrat calcule la lecture 'avant remise'."""
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = {id(node) for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name == "_check_subtotal_contract"
                   for node in ast.walk(fn)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub) and id(node) not in allowed
                    and "subtotal" in ast.unparse(node.left).lower() and "discount" in ast.unparse(node.right).lower()):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []
