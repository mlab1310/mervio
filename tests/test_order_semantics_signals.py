"""B-4 et semantiques non etablies (Mission 003.2, D-043).

Aucune de ces regles ne change un chiffre: un CA negatif reste negatif, les
commandes annulees, non encaissees, brouillons ou a zero restent comptees.
Elles deviennent visibles, avec leur nombre et leur montant.
"""
from __future__ import annotations

from datetime import date

import pytest

from mervio.analytics.pipeline import SourcePaths, load_dataset, run_analysis

HEADER = ("Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,Shipping,Taxes,Total,"
          "Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku,Cancelled at,Source\n")


def _row(name, day, subtotal, status="paid", cancelled="", source="web", refunded=""):
    return (f"#{name},c{name}@x.com,{status},{day} 10:00:00,EUR,{subtotal:.2f},0.00,0.00,0.00,{subtotal:.2f},"
            f"{refunded},1,Produit,{max(subtotal, 0):.2f},A,{cancelled},{source}\n")


def _write(tmp_path, body):
    path = tmp_path / "orders.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return str(path)


def _issues(dataset):
    return {issue.kind: issue for issue in dataset.quality.issues}


def _weekly_history(extra=""):
    rows = []
    for week in range(12):
        day = date.fromordinal(date(2026, 6, 22).toordinal() + 7 * week + 1)
        rows.append(_row(f"{1000 + week}", day.isoformat(), 100.0))
    return "".join(rows) + extra


def test_negative_period_revenue_is_kept_and_signalled(tmp_path):
    # derniere date = 2026-09-08: la derniere periode close couverte est 2026-W36 (31/08 -> 06/09)
    body = _weekly_history(_row("2000", "2026-09-02", -250.0))
    report = run_analysis(SourcePaths(shopify_orders=_write(tmp_path, body)), today=date(2026, 9, 14))
    revenue = report["kpis"]["revenue"]
    assert revenue["value"] == pytest.approx(-150.0)                  # 100 - 250: jamais ramene a zero
    assert revenue["data_quality"] == "incomplete" and "negatif" in revenue["notes"][0]
    kinds = {i["kind"]: i for i in report["data_quality"]["issues"]}
    assert kinds["negative_period_revenue"]["severity"] == "warning" and "2026-W36" in kinds["negative_period_revenue"]["message"]
    assert kinds["negative_subtotal"]["severity"] == "warning"
    assert any("CA negatif" in limitation for limitation in report["limitations"])


def test_positive_history_raises_no_negative_signal(tmp_path):
    report = run_analysis(SourcePaths(shopify_orders=_write(tmp_path, _weekly_history())), today=date(2026, 9, 14))
    kinds = {i["kind"] for i in report["data_quality"]["issues"]}
    assert not {"negative_period_revenue", "negative_subtotal"} & kinds
    assert report["kpis"]["revenue"]["notes"] == []


def test_refunds_never_turn_revenue_negative(tmp_path):
    body = _row("1", "2026-09-08", 50.0, status="refunded", refunded="60.00")
    ds = load_dataset(SourcePaths(shopify_orders=_write(tmp_path, body)))
    assert sum(o.net_revenue for o in ds.orders) == pytest.approx(50.0)
    assert "negative_subtotal" not in _issues(ds)


def test_cancelled_orders_stay_counted_and_are_signalled(tmp_path):
    body = (_row("1", "2026-09-08", 40.0, cancelled="2026-09-09 10:00:00") + _row("2", "2026-09-08", 60.0)
            + _row("3", "2026-09-08", 15.0, cancelled="UNKNOWN"))
    ds = load_dataset(SourcePaths(shopify_orders=_write(tmp_path, body)))
    assert len(ds.orders) == 3 and sum(o.net_revenue for o in ds.orders) == pytest.approx(115.0)
    issue = _issues(ds)["cancelled_orders_counted"]
    assert issue.severity == "warning" and issue.message.startswith("2 commande(s)") and "55.00" in issue.message


def test_financial_status_and_cancellation_are_independent_signals(tmp_path):
    body = _row("1", "2026-09-08", 40.0, status="paid", cancelled="2026-09-09 10:00:00") + _row("2", "2026-09-08", 30.0, status="pending")
    issues = _issues(load_dataset(SourcePaths(shopify_orders=_write(tmp_path, body))))
    assert issues["cancelled_orders_counted"].message.startswith("1 commande(s)")      # payee ET annulee
    assert issues["unsettled_orders_counted"].message.startswith("1 commande(s)") and "pending" in issues["unsettled_orders_counted"].message


def test_zero_value_and_draft_orders_stay_counted_and_are_signalled(tmp_path):
    body = (_row("1", "2026-09-08", 0.0, source="shopify_draft_order") + _row("2", "2026-09-08", 25.0, source="shopify_draft_order")
            + _row("3", "2026-09-08", 75.0))
    ds = load_dataset(SourcePaths(shopify_orders=_write(tmp_path, body)))
    assert len(ds.orders) == 3
    issues = _issues(ds)
    assert issues["zero_value_orders_counted"].severity == "info" and issues["zero_value_orders_counted"].message.startswith("1 commande(s)")
    assert issues["draft_orders_counted"].severity == "info" and "25.00" in issues["draft_orders_counted"].message


def test_refund_rate_declares_its_basis(tmp_path):
    report = run_analysis(SourcePaths(shopify_orders=_write(tmp_path, _weekly_history())), today=date(2026, 9, 14))
    assert "taxes et port" in report["kpis"]["refund_rate"]["notes"][0]
