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
    refund_rate = report["kpis"]["refund_rate"]
    assert refund_rate["formula"] == "refunds / sum(order.total)"
    assert "date de creation de la commande" in refund_rate["notes"][0]
    assert "date de creation de la commande" in report["kpis"]["refunds"]["notes"][0]


# --- Mission 003.3: perimetre des commandes (D-044) et base des remboursements (D-045) ---------

FULL_HEADER = ("Name,Email,Financial Status,Created at,Currency,Subtotal,Discount Amount,Shipping,Taxes,Total,"
               "Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku,Cancelled at,Source\n")


def _full_row(name, subtotal, *, shipping=0.0, tax=0.0, total=None, refunded="", status="paid", cancelled="",
              source="web", price=None, discount=0.0, day="2026-09-08"):
    total = f"{subtotal + shipping + tax:.2f}" if total is None else total
    price = subtotal + discount if price is None else price
    return (f"#{name},c{name}@x.com,{status},{day} 10:00:00,USD,{subtotal:.2f},{discount:.2f},{shipping:.2f},"
            f"{tax:.2f},{total},{refunded},1,Produit,{price:.2f},A,{cancelled},{source}\n")


def _load(tmp_path, body):
    path = tmp_path / "orders.csv"
    path.write_text(FULL_HEADER + body, encoding="utf-8")
    return load_dataset(SourcePaths(shopify_orders=str(path)))


def _kpis(ds):
    from mervio.analytics.kpi import compute_kpis
    from mervio.analytics.periods import make_period
    return compute_kpis(ds, make_period(date(2026, 9, 7), "week"))


def test_refund_rate_uses_the_billed_total_as_denominator(tmp_path):
    # forme observee sur OH5: remboursement du port seul sur une commande a Subtotal nul, et
    # remboursement integral (taxes comprises) d'une commande a 15.00
    ds = _load(tmp_path, _full_row("1", 0.0, shipping=4.94, refunded="4.94", status="refunded", discount=15.0, price=15.0)
               + _full_row("2", 15.0, tax=1.43, refunded="16.43", status="refunded")
               + _full_row("3", 100.0, shipping=10.0, tax=10.0))
    kpis = _kpis(ds)
    assert kpis["revenue"].value == pytest.approx(115.0)
    assert kpis["refunds"].value == pytest.approx(21.37)
    assert kpis["refund_rate"].value == pytest.approx(21.37 / (4.94 + 16.43 + 120.0))
    assert kpis["refund_rate"].data_quality == "reliable"
    assert "missing_order_total" not in _issues(ds) and "refund_exceeds_order_total" not in _issues(ds)


def test_missing_total_makes_the_refund_rate_incomplete(tmp_path):
    ds = _load(tmp_path, _full_row("1", 50.0, refunded="10.00") + _full_row("2", 50.0, total=""))
    assert _issues(ds)["missing_order_total"].message.startswith("1 commande(s) sans Total")
    kpis = _kpis(ds)
    assert kpis["refund_rate"].data_quality == "incomplete"
    assert kpis["revenue"].value == pytest.approx(100.0)                     # le CA ne depend pas du Total


def test_refund_rate_is_unavailable_without_any_billed_amount(tmp_path):
    ds = _load(tmp_path, _full_row("1", 0.0, discount=20.0, price=20.0))
    kpis = _kpis(ds)
    assert kpis["refund_rate"].value is None and kpis["refund_rate"].data_quality == "unavailable"


def test_refund_above_order_total_is_signalled_and_kept(tmp_path):
    ds = _load(tmp_path, _full_row("1", 40.0, tax=4.0, refunded="60.00", status="refunded"))
    assert _issues(ds)["refund_exceeds_order_total"].severity == "warning"
    assert _kpis(ds)["refunds"].value == pytest.approx(60.0)


def test_refund_is_dated_at_order_creation_and_keeps_the_pre_return_subtotal(tmp_path):
    ds = _load(tmp_path, _full_row("1", 15.0, tax=1.09, refunded="16.09", status="refunded", day="2026-08-03"))
    order, refund = ds.orders[0], ds.refunds[0]
    assert refund.created_at == order.created_at                             # cohorte de commande (D-045)
    assert order.net_revenue == pytest.approx(15.0)                          # Subtotal avant retours, jamais reduit


def test_decided_order_perimeter_counts_every_exported_order_in_orders_and_aov(tmp_path):
    body = (_full_row("1", 100.0)                                                          # vente ordinaire
            + _full_row("2", 0.0, discount=15.0, price=15.0, cancelled="UNKNOWN")          # annulee, remise 100 %, "paid"
            + _full_row("3", 25.0, source="shopify_draft_order")                           # brouillon converti
            + _full_row("4", 40.0, status="pending")                                       # non encaissee
            + _full_row("5", 0.0, shipping=4.10)                                           # port seul
            + _full_row("6", 30.0, status="voided", cancelled="2026-09-09 10:00:00"))      # annulee avant encaissement
    kpis = _kpis(_load(tmp_path, body))
    assert kpis["orders"].value == 6
    assert kpis["revenue"].value == pytest.approx(195.0)
    assert kpis["aov"].value == pytest.approx(195.0 / 6)


def test_partially_refunded_order_keeps_its_subtotal_and_is_not_unsettled(tmp_path):
    ds = _load(tmp_path, _full_row("1", 100.0, shipping=10.0, tax=10.0, refunded="10.00", status="partially_refunded"))
    kpis = _kpis(ds)
    assert (kpis["orders"].value, kpis["revenue"].value, kpis["refunds"].value) == (1, pytest.approx(100.0), pytest.approx(10.0))
    assert kpis["refund_rate"].value == pytest.approx(10.0 / 120.0)
    assert "unsettled_orders_counted" not in _issues(ds)


def test_cancelled_signal_isolates_positive_unrefunded_cancellations(tmp_path):
    body = (_full_row("1", 0.0, discount=15.0, price=15.0, cancelled="UNKNOWN")                        # nul
            + _full_row("2", 15.0, tax=1.43, refunded="16.43", status="refunded", cancelled="UNKNOWN")  # rembourse
            + _full_row("3", 40.0, cancelled="2026-09-09 10:00:00"))                                    # ni l'un ni l'autre
    message = _issues(_load(tmp_path, body))["cancelled_orders_counted"].message
    assert message.startswith("3 commande(s)") and "(55.00)" in message
    assert "dont 1 a montant positif sans remboursement (40.00)" in message
