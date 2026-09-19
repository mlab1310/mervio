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
from ..identity import CustomerIdentity
from ..domain.quality import DataQualityReport, QualityStatus
from .base import (
    first_present, is_null, parse_datetime, parse_float, parse_int, read_csv, require_columns, resolve_date_order,
)

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
    path: str | Path, quality: DataQualityReport, identity: Optional[CustomerIdentity] = None,
) -> Tuple[List[Order], List[Refund], str]:
    """Commandes et remboursements de l'export Shopify.

    `identity` calcule la reference client (D-053): l'e-mail de l'export est lu ici, transforme
    en reference a cle, puis oublie; il n'entre jamais dans le modele. Sans `identity` (CLI
    locale, validation), une cle ephemere est tiree pour cet appel (D-058).
    """
    identity = identity or CustomerIdentity.ephemeral()
    rows = read_csv(path, SOURCE, quality)
    require_columns(rows, REQUIRED_ORDER_COLUMNS, SOURCE)
    quality.mark_source("shopify_orders")

    orders: Dict[str, Order] = {}
    orders_with_total: set = set()
    cancelled_orders: set = set()
    draft_orders: set = set()
    seen_items: Dict[str, set] = {}
    refunds: List[Refund] = []
    #: derniere devise LUE dans l'export. Jamais de valeur par defaut: une devise
    #: absente reste absente (Mission 003, l'ancien defaut "EUR" etait invente).
    currency = ""
    identified_customers = 0
    invalid_rows = 0
    accepted_rows = 0
    # ordre jour/mois des dates a barres: une seule convention par fichier (D-042)
    date_order = resolve_date_order((row.get("Created at") for row in rows), source=SOURCE,
                                    column="Created at", quality=quality)

    for index, row in enumerate(rows, start=2):
        order_id = (row.get("Name") or "").strip()
        if not order_id:
            invalid_rows += 1
            quality.add_issue(SOURCE, "missing_order_id", "error", "ligne sans identifiant de commande, ignoree")
            continue

        if order_id not in orders:
            created_at = parse_datetime(row.get("Created at"), source=SOURCE, column="Created at", row=index,
                                        required=False, date_order=date_order)
            if created_at is None:
                invalid_rows += 1
                quality.add_issue(SOURCE, "invalid_date", "error", f"commande {order_id} sans date valide, ignoree")
                continue
            email = (row.get("Email") or "").strip().lower() or None
            if email:
                identified_customers += 1
            else:
                quality.add_issue(SOURCE, "missing_email", "warning", "commande sans email: client traite comme invite")
            order_currency = (row.get("Currency") or "").strip()
            if order_currency:
                currency = order_currency
                quality.observe_currency(SOURCE, order_currency)
            else:
                quality.add_issue(SOURCE, "missing_currency", "warning",
                                  "commande sans devise: montant agrege sans devise verifiee")
            total = parse_float(row.get("Total"), source=SOURCE, column="Total", row=index)
            if total is not None:
                orders_with_total.add(order_id)
            orders[order_id] = Order(
                order_id=order_id,
                customer_id=identity.ref_customer(email, order_id),
                created_at=created_at,
                currency=order_currency,
                # Contrat Shopify (D-041): "Subtotal" est deja net des remises de commande.
                # C'est la forme canonique du domaine: aucune conversion, et surtout
                # aucune soustraction de "Discount Amount".
                subtotal=parse_float(row.get("Subtotal"), source=SOURCE, column="Subtotal", row=index, default=0.0) or 0.0,
                discount=parse_float(row.get("Discount Amount"), source=SOURCE, column="Discount Amount", row=index, default=0.0) or 0.0,
                shipping=parse_float(row.get("Shipping"), source=SOURCE, column="Shipping", row=index, default=0.0) or 0.0,
                tax=parse_float(row.get("Taxes"), source=SOURCE, column="Taxes", row=index, default=0.0) or 0.0,
                total=total or 0.0,
                financial_status=(row.get("Financial Status") or "unknown").strip().lower(),
            )
            seen_items[order_id] = set()
            accepted_rows += 1
            if not is_null(row.get("Cancelled at")):
                cancelled_orders.add(order_id)
            if (row.get("Source") or "").strip().lower() == "shopify_draft_order":
                draft_orders.add(order_id)

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
    _check_subtotal_contract(order_list, orders_with_total, quality)
    _check_refund_basis(order_list, orders_with_total, refunds, quality)
    _report_order_semantics(order_list, cancelled_orders, draft_orders, refunds, quality)
    quality.set_field("order_revenue", covered=len(order_list), total=len(order_list) + invalid_rows,
                      note="CA avant ajustements: Subtotal net de remise, hors port et hors taxes")
    quality.set_field("customer_identity", covered=identified_customers, total=len(order_list),
                      note="email present: necessaire au calcul de retention et de CAC")
    if invalid_rows:
        log.warning("shopify orders: %s lignes rejetees", invalid_rows)
    log.info("shopify orders: %s commandes, %s remboursements", len(order_list), len(refunds))
    return order_list, refunds, currency


#: arrondi independant au centime de chaque composante du Total
_TOTAL_TOLERANCE = 0.02


def _check_subtotal_contract(orders: List[Order], orders_with_total: set, quality: DataQualityReport) -> None:
    """Controle arithmetique PARTIEL du contrat "Subtotal apres remise" (D-041, D-046).

    Le Total d'une commande Shopify vaut Subtotal + Shipping + Taxes (taxes
    eventuellement incluses dans les prix). Si le Total ne se reconstruit
    qu'en retirant la remise du Subtotal, le fichier contredit le contrat:
    erreur, sans changer de convention en silence.

    Ce controle ne prouve pas la semantique Shopify. Il ne peut rien conclure
    sans Total, quand le Total ne se reconstruit d'aucune facon (remise absente
    de l'export, montants hors contrat) ou quand les deux lectures reconstruisent
    le meme Total (remise egale aux taxes): ces commandes sont declarees non
    verifiables, jamais comptees comme conformes.
    """
    close = lambda a, b: abs(a - b) <= _TOTAL_TOLERANCE  # noqa: E731
    contradicting, overstatement = 0, 0.0
    ambiguous = unreconstructible = 0
    for order in orders:
        if order.order_id not in orders_with_total:
            continue
        after = close(order.total, order.subtotal + order.shipping + order.tax) or close(order.total, order.subtotal + order.shipping)
        if order.discount <= 0:
            unreconstructible += not after
            continue
        net = order.subtotal - order.discount
        before = close(order.total, net + order.shipping + order.tax) or close(order.total, net + order.shipping)
        if before and not after:
            contradicting += 1
            overstatement += order.discount
        elif before and after:
            ambiguous += 1
        elif not after:
            unreconstructible += 1
    if contradicting:
        quality.add_issue(
            SOURCE, "subtotal_convention_contradiction", "error",
            f"{contradicting} commande(s) remisee(s) dont le Total suppose un Subtotal AVANT remise: "
            f"le contrat Shopify (Subtotal apres remise, D-041) est applique, le CA peut etre surestime "
            f"d'au plus {overstatement:,.2f}",
        )
    missing = len(orders) - len(orders_with_total)
    if missing or ambiguous or unreconstructible:
        quality.add_issue(
            SOURCE, "subtotal_contract_unverified", "warning",
            f"contrat du Subtotal (apres remise, D-041) non verifiable sur {missing + ambiguous + unreconstructible} "
            f"commande(s): {missing} sans Total, {unreconstructible} dont le Total ne se reconstruit pas depuis "
            f"Subtotal, port et taxes, {ambiguous} remisee(s) dont le Total admet les deux lectures",
        )


def _check_refund_basis(orders: List[Order], orders_with_total: set, refunds: List[Refund],
                       quality: DataQualityReport) -> None:
    """Controle la base du taux de remboursement (D-045).

    "Refunded Amount" se compare au Total (montant facture, port et taxes
    compris), plafond du remboursable selon Shopify. Un Total absent rend la
    base incomplete; un remboursement superieur au Total est incoherent.
    """
    missing = len(orders) - len(orders_with_total)
    if missing:
        quality.add_issue(SOURCE, "missing_order_total", "warning",
                          f"{missing} commande(s) sans Total: base du taux de remboursement incomplete")
    totals = {o.order_id: o.total for o in orders if o.order_id in orders_with_total}
    above = [r for r in refunds if r.order_id in totals and r.amount > totals[r.order_id] + _TOTAL_TOLERANCE]
    if above:
        quality.add_issue(SOURCE, "refund_exceeds_order_total", "warning",
                          f"{len(above)} remboursement(s) superieur(s) au Total de la commande: "
                          "montants conserves tels quels, a verifier dans l'export")


#: statuts financiers Shopify dont le montant n'est pas (encore) encaisse
UNSETTLED_STATUSES = ("pending", "authorized", "partially_paid", "voided", "expired")


def _report_order_semantics(orders: List[Order], cancelled: set, drafts: set, refunds: List[Refund],
                            quality: DataQualityReport) -> None:
    """Rend visibles les commandes dont l'effet sur les chiffres merite d'etre lu (D-043, D-044).

    Perimetre decide (D-044, aligne sur les rapports Shopify): toute commande de
    l'export compte dans les commandes, le CA avant ajustements et le panier
    moyen. Aucune n'est exclue ni requalifiee. Le signal dit combien et combien
    d'argent, pour que le lecteur juge l'impact.
    """
    def total(subset):
        return sum(o.subtotal for o in subset)

    negative = [o for o in orders if o.subtotal < 0]
    if negative:
        quality.add_issue(SOURCE, "negative_subtotal", "warning",
                          f"{len(negative)} commande(s) avec Subtotal negatif ({total(negative):,.2f}): "
                          "conservees telles quelles, le CA d'une periode peut etre negatif")
    flagged = [o for o in orders if o.order_id in cancelled]
    if flagged:
        refunded_ids = {r.order_id for r in refunds}
        unrefunded = [o for o in flagged if o.subtotal > 0 and o.order_id not in refunded_ids]
        quality.add_issue(SOURCE, "cancelled_orders_counted", "warning",
                          f"{len(flagged)} commande(s) annulee(s) (Cancelled at renseigne) comptees dans les commandes "
                          f"et le CA avant ajustements ({total(flagged):,.2f}), dont {len(unrefunded)} a montant positif "
                          f"sans remboursement ({total(unrefunded):,.2f}): l'annulation n'est pas deduite du CA")
    unsettled = [o for o in orders if o.financial_status in UNSETTLED_STATUSES]
    if unsettled:
        quality.add_issue(SOURCE, "unsettled_orders_counted", "warning",
                          f"{len(unsettled)} commande(s) non encaissee(s) ({', '.join(sorted({o.financial_status for o in unsettled}))}) "
                          f"comptees dans les commandes et le CA ({total(unsettled):,.2f})")
    zero = [o for o in orders if o.subtotal == 0]
    if zero:
        quality.add_issue(SOURCE, "zero_value_orders_counted", "info",
                          f"{len(zero)} commande(s) a Subtotal nul comptees dans les commandes et le panier moyen")
    draft = [o for o in orders if o.order_id in drafts]
    if draft:
        quality.add_issue(SOURCE, "draft_orders_counted", "info",
                          f"{len(draft)} commande(s) creee(s) depuis un brouillon (Source shopify_draft_order) "
                          f"comptees dans les commandes et le CA ({total(draft):,.2f})")
