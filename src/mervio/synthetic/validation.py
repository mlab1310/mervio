"""Validation relationnelle d'un jeu synthetique ECRIT, relu depuis le disque.

Le validateur ne reutilise aucune structure du generateur: il relit les CSV
comme le ferait un tiers. Il lit en flux; seules les cles (commandes, clients,
produits, campagnes) sont gardees en memoire.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Dict, List

TOLERANCE = 0.011


def _rows(path: Path):
    with path.open(encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def _f(value: str) -> float:
    return float(value) if value not in ("", None) else 0.0


def validate_dataset(directory: str | Path) -> dict:
    """Retourne {"status": "PASS"|"FAIL", "checks": {nom: {"status", "failures"}}}."""
    root = Path(directory)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    checks: Dict[str, int] = {}
    counted: Dict[str, int] = {}

    def fail(name: str, count: int = 1) -> None:
        checks[name] = checks.get(name, 0) + count

    for name in ("files_hash_match", "products_unique", "order_lines_reference_products", "orders_reference_customers",
                 "order_names_unique_blocks", "subtotal_equals_lines_minus_discount", "total_equals_components",
                 "refund_not_above_total", "dates_within_range", "payments_match_orders", "ad_campaigns_consistent",
                 "stockout_skus_not_sold", "shipments_match_orders", "synthetic_flag", "emails_reserved_domain"):
        checks.setdefault(name, 0)

    if manifest.get("synthetic") is not True or manifest.get("not_for_production") is not True:
        fail("synthetic_flag")
    for name, meta in manifest["files"].items():
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if digest != meta["sha256"]:
            fail("files_hash_match")

    start = date.fromisoformat(manifest["config"]["start_date"])
    end = date.fromisoformat(manifest["config"]["end_date"])

    products = {}
    for row in _rows(root / "shopify_products.csv"):
        if row["SKU"] in products:
            fail("products_unique")
        products[row["SKU"]] = float(row["Variant Price"])

    customers = set()
    for row in _rows(root / "customers.csv"):
        customers.add(row["customer_id"])

    unavailable = set()
    for row in _rows(root / "inventory_daily.csv"):
        if row["available"] == "false":
            unavailable.add((row["date"], row["sku"]))

    orders: Dict[str, float] = {}
    seen_blocks = set()
    current = None
    lines_total = 0.0

    def close_order() -> None:
        if current is None:
            return
        subtotal, discount = current["subtotal"], current["discount"]
        if abs(lines_total - discount - subtotal) > TOLERANCE:
            fail("subtotal_equals_lines_minus_discount")

    for row in _rows(root / "shopify_orders.csv"):
        name = row["Name"]
        if row["Created at"]:
            close_order()
            if name in seen_blocks:
                fail("order_names_unique_blocks")
            seen_blocks.add(name)
            subtotal, shipping, tax, total = (_f(row[k]) for k in ("Subtotal", "Shipping", "Taxes", "Total"))
            refunded = _f(row["Refunded Amount"])
            created = date.fromisoformat(row["Created at"][:10])
            current = {"name": name, "subtotal": subtotal, "discount": _f(row["Discount Amount"]), "created": created}
            lines_total = 0.0
            if abs(subtotal + shipping + tax - total) > TOLERANCE:
                fail("total_equals_components")
            if refunded > total + TOLERANCE:
                fail("refund_not_above_total")
            if not start <= created <= end:
                fail("dates_within_range")
            customer = "C" + row["Email"][1:8]
            if customer not in customers:
                fail("orders_reference_customers")
            if not row["Email"].endswith(".invalid"):
                fail("emails_reserved_domain")
            orders[name] = total
        sku = row["Lineitem sku"]
        if sku not in products:
            fail("order_lines_reference_products")
        else:
            lines_total += products[sku] * int(row["Lineitem quantity"])
        if current and (current["created"].isoformat(), sku) in unavailable:
            fail("stockout_skus_not_sold")
    close_order()

    for row in _rows(root / "stripe_transactions.csv"):
        counted["payments"] = counted.get("payments", 0) + 1
        if abs(orders.get(row["order_id"], -1.0) - _f(row["Amount"])) > TOLERANCE:
            fail("payments_match_orders")
    if counted.get("payments", 0) != len(orders):
        fail("payments_match_orders")

    campaigns: Dict[str, str] = {}
    for row in _rows(root / "google_ads.csv"):
        if campaigns.setdefault(row["Campaign ID"], row["Campaign"]) != row["Campaign"]:
            fail("ad_campaigns_consistent")
        if not start <= date.fromisoformat(row["Day"]) <= end:
            fail("dates_within_range")

    shipments = 0
    for row in _rows(root / "shipments.csv"):
        shipments += 1
        if row["order_name"] not in orders:
            fail("shipments_match_orders")
    if shipments != len(orders):
        fail("shipments_match_orders")

    results = {name: {"status": "PASS" if count == 0 else "FAIL", "failures": count} for name, count in sorted(checks.items())}
    return {"status": "PASS" if all(v["failures"] == 0 for v in results.values()) else "FAIL",
            "orders": len(orders), "customers": len(customers), "checks": results}
