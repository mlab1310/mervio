"""Connecteur Shopify (export orders + export products).

L'export Shopify natif produit une ligne par article, les champs de niveau
commande n'etant renseignes que sur la premiere ligne du bloc. Le connecteur
regroupe ces lignes et deduplique les articles strictement identiques.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..logging_config import get_logger
from ..domain.models import Order, OrderItem, Product, Refund
from ..domain.quality import DataQualityReport, QualityStatus
from .base import first_present, is_null, parse_datetime, parse_float, parse_int, read_csv, require_columns

SOURCE = "shopify"
log = get_logger("ingestion.shopify")

REQUIRED_ORDER_COLUMNS = ("Name", "Created at", "Lineitem quantity", "Lineitem price")


def ingest_shopify_products(path: str | Path, quality: DataQualityReport) -> Dict[str, Product]:
    rows = read_csv(path, SOURCE, quality)
    require_columns(rows, ("SKU", "Title"), SOURCE)
    quality.mark_source("shopify_products")

    products: Dict[str, Product] = {}
    with_cogs = 0
    for index, row in enumerate(rows, start=2):
        sku = (row.get("SKU") or "").strip()
        if not sku:
            quality.add_issue(SOURCE, "missing_sku", "warning", "produit sans SKU ignore")
            continue
        cogs = parse_float(
            first_present(row, ("Cost per item", "Cost", "COGS")),
            source=SOURCE, column="Cost per item", row=index,
        )
        if cogs is not None and cogs < 0:
            quality.add_issue(SOURCE, "negative_cogs", "error", f"COGS negatif pour {sku}, ignore")
            cogs = None
        if cogs is not None:
            with_cogs += 1
        products[sku] = Product(
            product_id=(row.get("Product ID") or sku).strip(),
            sku=sku,
            title=(row.get("Title") or sku).strip(),
            unit_cogs=cogs,
        )
    quality.set_rows(SOURCE + "_products", total=len(rows), accepted=len(products))
    quality.set_field(
        "product_cogs", covered=with_cogs, total=len(products),
        note="cout unitaire renseigne dans l'export produits Shopify",
    )
    log.info("shopify products: %s produits, %s avec COGS", len(products), with_cogs)
    return products


def ingest_shopify_orders(
    path: str | Path, quality: DataQualityReport
) -> Tuple[List[Order], List[Refund], str]:
    rows = read_csv(path, SOURCE, quality)
    require_columns(rows, REQUIRED_ORDER_COLUMNS, SOURCE)
    quality.mark_source("shopify_orders")

    orders: Dict[str, Order] = {}
    seen_items: Dict[str, set] = {}
    refunds: List[Refund] = []
    currency = "EUR"
    identified_customers = 0
    invalid_rows = 0
    accepted_rows = 0

    for index, row in enumerate(rows, start=2):
        order_id = (row.get("Name") or "").strip()
        if not order_id:
            invalid_rows += 1
            quality.add_issue(SOURCE, "missing_order_id", "error", "ligne sans identifiant de commande, ignoree")
            continue

        if order_id not in orders:
            created_at = parse_datetime(row.get("Created at"), source=SOURCE, column="Created at", row=index, required=False)
            if created_at is None:
                invalid_rows += 1
                quality.add_issue(SOURCE, "invalid_date", "error", f"commande {order_id} sans date valide, ignoree")
                continue
            email = (row.get("Email") or "").strip().lower() or None
            if email:
                identified_customers += 1
            else:
                quality.add_issue(SOURCE, "missing_email", "warning", "commande sans email: client traite comme invite")
            currency = (row.get("Currency") or currency).strip() or currency
            quality.observe_currency(SOURCE, currency)
            orders[order_id] = Order(
                order_id=order_id,
                customer_id=email or f"guest:{order_id}",
                created_at=created_at,
                currency=currency,
                subtotal=parse_float(row.get("Subtotal"), source=SOURCE, column="Subtotal", row=index, default=0.0) or 0.0,
                discount=parse_float(row.get("Discount Amount"), source=SOURCE, column="Discount Amount", row=index, default=0.0) or 0.0,
                shipping=parse_float(row.get("Shipping"), source=SOURCE, column="Shipping", row=index, default=0.0) or 0.0,
                tax=parse_float(row.get("Taxes"), source=SOURCE, column="Taxes", row=index, default=0.0) or 0.0,
                total=parse_float(row.get("Total"), source=SOURCE, column="Total", row=index, default=0.0) or 0.0,
                financial_status=(row.get("Financial Status") or "unknown").strip().lower(),
                customer_email=email,
            )
            seen_items[order_id] = set()
            accepted_rows += 1

            refunded = parse_float(row.get("Refunded Amount"), source=SOURCE, column="Refunded Amount", row=index)
            if refunded:
                if refunded < 0:
                    quality.add_issue(SOURCE, "negative_refund", "error", f"remboursement negatif sur {order_id}, ignore")
                else:
                    refunds.append(Refund(
                        refund_id=f"shopify-refund-{order_id}",
                        created_at=orders[order_id].created_at,
                        amount=refunded,
                        order_id=order_id,
                        source=SOURCE,
                    ))

        created_here = order_id in orders and len(orders[order_id].items) == 0 and order_id in seen_items
        quantity = parse_int(row.get("Lineitem quantity"), source=SOURCE, column="Lineitem quantity", row=index, default=0) or 0
        price = parse_float(row.get("Lineitem price"), source=SOURCE, column="Lineitem price", row=index, default=0.0) or 0.0
        sku = (row.get("Lineitem sku") or "").strip()
        if quantity <= 0:
            continue
        signature = (sku, quantity, round(price, 4), (row.get("Lineitem name") or "").strip())
        if signature in seen_items[order_id]:
            quality.add_issue(SOURCE, "duplicate_line_item", "warning",
                              "ligne d'article dupliquee detectee et dedupliquee")
            continue
        seen_items[order_id].add(signature)
        if not created_here:
            accepted_rows += 1
        if not sku:
            quality.add_issue(SOURCE, "missing_line_sku", "warning", "article sans SKU: marge produit non calculable")
        orders[order_id].items.append(OrderItem(
            order_id=order_id, sku=sku or "UNKNOWN",
            title=(row.get("Lineitem name") or sku or "unknown").strip(),
            quantity=quantity, unit_price=price,
        ))

    quality.set_rows(SOURCE + "_orders", total=len(rows), accepted=accepted_rows)
    order_list = sorted(orders.values(), key=lambda o: o.created_at)
    quality.set_field("order_revenue", covered=len(order_list), total=len(order_list) + invalid_rows,
                      note="CA net de remise, hors port et hors taxes")
    quality.set_field("customer_identity", covered=identified_customers, total=len(order_list),
                      note="email present: necessaire au calcul de retention et de CAC")
    if invalid_rows:
        log.warning("shopify orders: %s lignes rejetees", invalid_rows)
    log.info("shopify orders: %s commandes, %s remboursements", len(order_list), len(refunds))
    return order_list, refunds, currency
