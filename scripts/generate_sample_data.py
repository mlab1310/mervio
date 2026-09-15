"""Generateur de fixtures SYNTHETIQUES / DEMO pour Mervio.

ATTENTION: ces donnees sont entierement fabriquees. Elles ne proviennent
d'aucun client reel et ne doivent jamais etre presentees comme telles.

Scenario injecte volontairement (pour exercer le Root Cause Engine):
  - 12 semaines d'historique, boutique e-commerce FR.
  - Trafic paye stable sur la derniere semaine.
  - Taux de conversion en chute (~-28%) sur la derniere semaine.
  - Chute concentree sur la campagne "Shopping - Core".
  => Le CA doit baisser d'environ 25-30% en WoW, cause = conversion, pas trafic.

Defauts qualite injectes volontairement:
  - 2 produits sans "Cost per item" (marge brute incomplete)
  - aucun cout de transport reel (profitabilite complete impossible)
  - 1 ligne d'article dupliquee
  - 1 commande sans email
  - 1 ligne Google Ads avec date invalide
  - 1 remboursement Stripe negatif
"""
from __future__ import annotations

import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 20260914
OUT = Path(__file__).resolve().parent.parent / "data" / "sample"
END = date(2026, 9, 13)          # dernier dimanche complet
WEEKS = 12
START = END - timedelta(days=WEEKS * 7 - 1)

PRODUCTS = [
    # sku, titre, prix, cout unitaire (None = inconnu), poids dans le mix
    ("BRW-001", "Bracelet Acier Noir", 49.0, 14.5, 0.20),
    ("BRW-002", "Bracelet Cuir Camel", 59.0, 19.0, 0.16),
    ("WTC-010", "Montre Automatique 38mm", 289.0, 121.0, 0.10),
    ("WTC-011", "Montre Quartz 34mm", 149.0, 58.0, 0.14),
    ("BAG-100", "Sac Bandouliere Cuir", 189.0, None, 0.12),   # COGS inconnu
    ("BAG-101", "Pochette Zippee", 69.0, 26.0, 0.13),
    ("ACC-200", "Ceinture Reversible", 79.0, 31.5, 0.10),
    ("ACC-201", "Porte-cartes", 39.0, None, 0.05),            # COGS inconnu
]

CAMPAIGNS = [
    ("11122334", "Search - Brand", 0.18, 0.085),
    ("11122335", "Shopping - Core", 0.52, 0.031),
    ("11122336", "Search - Generic", 0.20, 0.022),
    ("11122337", "PMax - Retargeting", 0.10, 0.061),
]

FIRST_NAMES = ["camille", "lucas", "emma", "hugo", "lea", "nathan", "chloe", "theo",
               "manon", "louis", "jade", "gabriel", "ines", "arthur", "sarah", "paul"]
LAST_NAMES = ["martin", "bernard", "dubois", "thomas", "robert", "petit", "durand",
              "leroy", "moreau", "simon", "laurent", "lefebvre", "michel", "garcia"]


def week_index(day: date) -> int:
    return (day - START).days // 7


def main() -> None:
    rng = random.Random(SEED)
    OUT.mkdir(parents=True, exist_ok=True)

    customer_pool = [
        f"{rng.choice(FIRST_NAMES)}.{rng.choice(LAST_NAMES)}{rng.randint(1, 99)}@example.com"
        for _ in range(340)
    ]

    ad_rows, order_rows, stripe_rows = [], [], []
    order_seq = 1080
    payment_seq = 1

    day = START
    while day <= END:
        w = week_index(day)
        is_last_week = w == WEEKS - 1
        weekday_factor = 1.15 if day.weekday() < 4 else 0.85
        seasonal = 1.0 + 0.022 * w  # legere croissance de fond

        daily_orders_total = 0
        for campaign_id, campaign_name, budget_share, base_cvr in CAMPAIGNS:
            daily_budget = 420.0 * budget_share * seasonal * weekday_factor
            spend = round(daily_budget * rng.uniform(0.90, 1.10), 2)
            cpc = rng.uniform(0.62, 0.94)
            clicks = max(1, int(spend / cpc))
            impressions = int(clicks / rng.uniform(0.028, 0.052))

            cvr = base_cvr * rng.uniform(0.88, 1.12)
            if is_last_week:
                # incident de checkout: s'abat surtout sur Shopping - Core
                cvr *= 0.34 if campaign_name == "Shopping - Core" else 0.88

            conversions = clicks * cvr
            orders_today = int(conversions) + (1 if rng.random() < (conversions % 1) else 0)
            daily_orders_total += orders_today

            conv_value = 0.0
            for _ in range(orders_today):
                order_seq += 1
                order_id = f"#{order_seq}"
                n_lines = 1 if rng.random() < 0.72 else 2
                skus = rng.sample(
                    [p[0] for p in PRODUCTS],
                    counts=None, k=n_lines,
                ) if n_lines <= len(PRODUCTS) else [PRODUCTS[0][0]]
                lines = []
                subtotal = 0.0
                for sku in skus:
                    product = next(p for p in PRODUCTS if p[0] == sku)
                    qty = 1 if rng.random() < 0.88 else 2
                    price = product[2]
                    subtotal += qty * price
                    lines.append((sku, product[1], qty, price))

                discount = round(subtotal * (0.10 if rng.random() < 0.22 else 0.0), 2)
                shipping = 0.0 if subtotal >= 80 else 5.90
                net = subtotal - discount
                tax = round(net * 0.20, 2)
                total = round(net + shipping + tax, 2)
                conv_value += net

                # ~40% de nouveaux clients: sans cela le CAC n'est jamais calculable
                if rng.random() < 0.40:
                    email = (f"{rng.choice(FIRST_NAMES)}.{rng.choice(LAST_NAMES)}"
                             f"{rng.randint(100, 99999)}@example.com")
                    customer_pool.append(email)
                else:
                    email = rng.choice(customer_pool)
                # une commande invite volontaire
                if order_seq == 1200:
                    email = ""
                stamp = datetime.combine(day, datetime.min.time()) + timedelta(
                    hours=rng.randint(8, 22), minutes=rng.randint(0, 59)
                )
                created = stamp.strftime("%Y-%m-%d %H:%M:%S") + " +0200"
                refunded = round(net * rng.uniform(0.5, 1.0), 2) if rng.random() < 0.035 else ""
                status = "refunded" if refunded else "paid"

                for idx, (sku, title, qty, price) in enumerate(lines):
                    order_rows.append({
                        "Name": order_id,
                        "Email": email if idx == 0 else "",
                        "Financial Status": status if idx == 0 else "",
                        "Created at": created if idx == 0 else "",
                        "Currency": "EUR" if idx == 0 else "",
                        # export Shopify: le Subtotal est deja net des remises (D-041)
                        "Subtotal": f"{net:.2f}" if idx == 0 else "",
                        "Discount Amount": f"{discount:.2f}" if idx == 0 else "",
                        "Shipping": f"{shipping:.2f}" if idx == 0 else "",
                        "Taxes": f"{tax:.2f}" if idx == 0 else "",
                        "Total": f"{total:.2f}" if idx == 0 else "",
                        "Refunded Amount": refunded if idx == 0 else "",
                        "Lineitem quantity": qty,
                        "Lineitem name": title,
                        "Lineitem price": f"{price:.2f}",
                        "Lineitem sku": sku,
                    })

                payment_seq += 1
                fee = round(total * 0.014 + 0.25, 2)
                stripe_rows.append({
                    "id": f"ch_3Q{payment_seq:06d}",
                    "Created (UTC)": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "Amount": f"{total:.2f}",
                    "Amount Refunded": refunded if refunded else "0.00",
                    "Currency": "eur",
                    "Fee": f"{fee:.2f}",
                    "Net": f"{total - fee:.2f}",
                    "Status": "Paid",
                    "Description": f"Shopify order {order_id}",
                    "Customer Email": email,
                    "order_id": order_id,
                })

            ad_rows.append({
                "Day": day.isoformat(),
                "Campaign": campaign_name,
                "Campaign ID": campaign_id,
                "Cost": f"{spend:.2f}",
                "Impressions": impressions,
                "Clicks": clicks,
                "Conversions": f"{orders_today:.2f}",
                "Conv. value": f"{conv_value:.2f}",
            })
        day += timedelta(days=1)

    # --- defauts qualite injectes ------------------------------------
    ad_rows.append({"Day": "31/02/2026", "Campaign": "Search - Brand", "Campaign ID": "11122334",
                    "Cost": "12.40", "Impressions": "900", "Clicks": "14",
                    "Conversions": "0.00", "Conv. value": "0.00"})
    if len(order_rows) > 40:
        order_rows.insert(41, dict(order_rows[40]))  # ligne d'article dupliquee
    stripe_rows.append({"id": "ch_3QBADREF01", "Created (UTC)": "2026-08-12 11:04:00",
                        "Amount": "120.00", "Amount Refunded": "-30.00", "Currency": "eur",
                        "Fee": "1.93", "Net": "118.07", "Status": "Paid",
                        "Description": "corrupted row", "Customer Email": "", "order_id": ""})

    _write(OUT / "shopify_orders.csv", order_rows)
    _write(OUT / "stripe_transactions.csv", stripe_rows)
    _write(OUT / "google_ads.csv", ad_rows)
    _write(OUT / "shopify_products.csv", [
        {"SKU": sku, "Product ID": f"gid-{sku}", "Title": title,
         "Variant Price": f"{price:.2f}", "Cost per item": ("" if cogs is None else f"{cogs:.2f}")}
        for sku, title, price, cogs, _ in PRODUCTS
    ])
    print(f"SYNTHETIC DATA ecrite dans {OUT}")
    print(f"  commandes (lignes): {len(order_rows)}")
    print(f"  transactions stripe: {len(stripe_rows)}")
    print(f"  lignes google ads  : {len(ad_rows)}")
    print(f"  periode            : {START} -> {END}")


def _write(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise SystemExit(f"aucune ligne pour {path}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
