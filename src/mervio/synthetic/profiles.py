"""Profils de boutique synthetiques (parametres de base, sans scenario)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class ProductSpec:
    sku: str
    title: str
    category: str
    price: float
    unit_cost: float
    weight: float            # poids relatif dans le mix de demande


@dataclass(frozen=True)
class CampaignSpec:
    campaign_id: str
    name: str
    budget_share: float      # part des sessions payantes
    cvr_multiplier: float    # conversion relative au taux de base
    cpc: float


@dataclass(frozen=True)
class StoreProfile:
    key: str
    store_name: str
    currency: str
    tax_rate: float                       # taxes hors prix (ajoutees au Total)
    shipping_fee: float
    free_shipping_threshold: float
    base_cvr: float                       # commandes / session
    paid_share: float                     # part des sessions issues de Google Ads
    organic_cvr_multiplier: float
    line_count_weights: Tuple[float, float, float]   # 1, 2, 3 articles
    quantity_two_probability: float
    discount_probability: float
    discount_rate: float
    refund_probability: float
    full_refund_share: float
    returning_customer_share: float
    weekday_factors: Tuple[float, ...]    # lundi -> dimanche
    seasonal_amplitude: float             # amplitude annuelle (pic mi-decembre)
    annual_trend: float
    daily_noise: float
    cart_rate: float                      # paniers / session
    substitution_rate: float              # rupture: part des clients qui prennent un autre article
    late_delivery_share: float
    products: List[ProductSpec] = field(default_factory=list)
    campaigns: List[CampaignSpec] = field(default_factory=list)


def _catalog(prefix: str, rows) -> List[ProductSpec]:
    return [ProductSpec(f"{prefix}-{i + 1:03d}", title, category, price, cost, weight)
            for i, (title, category, price, cost, weight) in enumerate(rows)]


FASHION_EU = StoreProfile(
    key="fashion_eu", store_name="Synthetic Atelier (FR)", currency="EUR", tax_rate=0.20,
    shipping_fee=5.90, free_shipping_threshold=80.0, base_cvr=0.024, paid_share=0.55,
    organic_cvr_multiplier=1.10, line_count_weights=(0.70, 0.22, 0.08), quantity_two_probability=0.10,
    discount_probability=0.18, discount_rate=0.10, refund_probability=0.035, full_refund_share=0.60,
    returning_customer_share=0.32, weekday_factors=(1.10, 1.08, 1.04, 1.02, 0.98, 0.88, 0.90),
    seasonal_amplitude=0.04, annual_trend=0.06, daily_noise=0.06, cart_rate=0.09, substitution_rate=0.25,
    late_delivery_share=0.04,
    products=_catalog("ATL", [
        ("Chemise lin", "tops", 69.0, 22.0, 0.12), ("T-shirt coton bio", "tops", 29.0, 8.5, 0.16),
        ("Pull merinos", "knitwear", 119.0, 41.0, 0.08), ("Jean droit", "bottoms", 89.0, 31.0, 0.10),
        ("Pantalon toile", "bottoms", 79.0, 26.0, 0.07), ("Veste legere", "outerwear", 169.0, 62.0, 0.05),
        ("Robe midi", "dresses", 99.0, 33.0, 0.08), ("Baskets cuir", "shoes", 139.0, 58.0, 0.07),
        ("Ceinture cuir", "accessories", 45.0, 14.0, 0.07), ("Sac cabas", "accessories", 89.0, 30.0, 0.06),
        ("Echarpe laine", "accessories", 49.0, 15.0, 0.06), ("Chaussettes (x3)", "accessories", 19.0, 5.0, 0.08),
    ]),
    campaigns=[
        CampaignSpec("90010001", "Search - Brand", 0.20, 1.80, 0.45),
        CampaignSpec("90010002", "Shopping - Core", 0.45, 1.00, 0.62),
        CampaignSpec("90010003", "Search - Generic", 0.20, 0.70, 0.85),
        CampaignSpec("90010004", "PMax - Retargeting", 0.15, 1.40, 0.55),
    ],
)

ELECTRONICS_US = StoreProfile(
    key="electronics_us", store_name="Synthetic Circuit Co (US)", currency="USD", tax_rate=0.08,
    shipping_fee=7.50, free_shipping_threshold=99.0, base_cvr=0.018, paid_share=0.65,
    organic_cvr_multiplier=1.05, line_count_weights=(0.78, 0.17, 0.05), quantity_two_probability=0.06,
    discount_probability=0.12, discount_rate=0.15, refund_probability=0.05, full_refund_share=0.70,
    returning_customer_share=0.22, weekday_factors=(1.02, 1.00, 1.00, 1.02, 1.04, 0.96, 0.96),
    seasonal_amplitude=0.05, annual_trend=0.04, daily_noise=0.07, cart_rate=0.07, substitution_rate=0.35,
    late_delivery_share=0.05,
    products=_catalog("CRC", [
        ("Wireless earbuds", "audio", 129.0, 58.0, 0.14), ("Over-ear headphones", "audio", 249.0, 118.0, 0.07),
        ("USB-C charger 65W", "power", 39.0, 12.0, 0.16), ("Power bank 20k", "power", 59.0, 22.0, 0.12),
        ("Smartwatch", "wearables", 299.0, 150.0, 0.06), ("Fitness band", "wearables", 79.0, 31.0, 0.09),
        ("Mechanical keyboard", "peripherals", 149.0, 66.0, 0.08), ("Wireless mouse", "peripherals", 49.0, 17.0, 0.12),
        ("4K webcam", "peripherals", 119.0, 52.0, 0.06), ("Phone case", "accessories", 25.0, 5.0, 0.10),
    ]),
    campaigns=[
        CampaignSpec("70020001", "Search - Brand", 0.18, 1.70, 0.60),
        CampaignSpec("70020002", "Shopping - Catalog", 0.50, 1.00, 0.80),
        CampaignSpec("70020003", "PMax - Prospecting", 0.32, 0.75, 0.95),
    ],
)

PROFILES: Dict[str, StoreProfile] = {p.key: p for p in (FASHION_EU, ELECTRONICS_US)}
