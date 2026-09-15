"""Connecteur Stripe (export paiements/balance).

Stripe est la source de verite des frais de paiement. Ses remboursements sont
conserves separement de ceux de Shopify: la reconciliation est explicite et
documentee dans le rapport, jamais silencieuse.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from ..logging_config import get_logger
from ..domain.models import Payment, Refund
from ..domain.quality import DataQualityReport
from .base import first_present, parse_datetime, parse_float, read_csv, require_columns, resolve_date_order

SOURCE = "stripe"
log = get_logger("ingestion.stripe")


def ingest_stripe(path: str | Path, quality: DataQualityReport) -> Tuple[List[Payment], List[Refund]]:
    rows = read_csv(path, SOURCE, quality)
    require_columns(rows, ("id", "Amount", "Status"), SOURCE)
    quality.mark_source("stripe")

    payments: List[Payment] = []
    refunds: List[Refund] = []
    seen_ids: set = set()
    with_fee = 0
    date_columns = ("Created (UTC)", "Created", "created")
    date_order = resolve_date_order((first_present(row, date_columns) for row in rows), source=SOURCE,
                                    column="Created (UTC)", quality=quality)

    for index, row in enumerate(rows, start=2):
        payment_id = (row.get("id") or "").strip()
        if not payment_id:
            quality.add_issue(SOURCE, "missing_id", "error", "transaction sans identifiant, ignoree")
            continue
        if payment_id in seen_ids:
            quality.add_issue(SOURCE, "duplicate_payment", "warning", "transaction Stripe dupliquee, dedupliquee")
            continue
        seen_ids.add(payment_id)

        created_at = parse_datetime(
            first_present(row, date_columns),
            source=SOURCE, column="Created (UTC)", row=index, required=False, date_order=date_order,
        )
        if created_at is None:
            quality.add_issue(SOURCE, "invalid_date", "error", f"transaction {payment_id} sans date valide, ignoree")
            continue

        amount = parse_float(row.get("Amount"), source=SOURCE, column="Amount", row=index, default=0.0) or 0.0
        fee = parse_float(first_present(row, ("Fee", "fee")), source=SOURCE, column="Fee", row=index)
        net = parse_float(first_present(row, ("Net", "net")), source=SOURCE, column="Net", row=index)
        if fee is not None:
            if fee < 0:
                quality.add_issue(SOURCE, "negative_fee", "error", f"frais negatif sur {payment_id}, ignore")
                fee = None
            else:
                with_fee += 1
        quality.observe_currency(SOURCE, row.get("Currency") or "")
        status = (row.get("Status") or "unknown").strip().lower()
        order_ref = first_present(row, ("order_id", "Order ID", "metadata.order_id"))
        email = first_present(row, ("Customer Email", "customer_email", "Email"))

        payments.append(Payment(
            payment_id=payment_id, created_at=created_at, amount=amount, fee=fee, net=net,
            status=status, order_id=order_ref.strip() if order_ref else None,
            customer_email=email.strip().lower() if email else None,
        ))

        refunded = parse_float(
            first_present(row, ("Amount Refunded", "amount_refunded")),
            source=SOURCE, column="Amount Refunded", row=index,
        )
        if refunded:
            if refunded < 0:
                quality.add_issue(SOURCE, "negative_refund", "error",
                                  f"remboursement negatif sur {payment_id}, ignore")
            else:
                refunds.append(Refund(
                    refund_id=f"stripe-refund-{payment_id}", created_at=created_at, amount=refunded,
                    order_id=order_ref.strip() if order_ref else None, source=SOURCE,
                ))

    quality.set_rows(SOURCE, total=len(rows), accepted=len(payments))
    quality.set_field("payment_fees", covered=with_fee, total=len(payments),
                      note="frais Stripe reels, indispensables a la marge de contribution")
    log.info("stripe: %s paiements, %s remboursements", len(payments), len(refunds))
    return payments, refunds
