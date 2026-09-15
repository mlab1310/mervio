"""Modele de donnees normalise, independant de Shopify / Stripe / Google Ads.

Toute nouvelle source doit produire ces entites. L'Analytics Engine ne connait
que ce modele: ajouter WooCommerce ou Meta Ads ne doit jamais impliquer de
reecrire un KPI.

Convention monetaire: float, devise unique par dataset, arrondi uniquement a
la serialisation. Volume analytique, pas de comptabilite au centime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from .quality import DataQualityReport


@dataclass(frozen=True)
class Product:
    product_id: str
    sku: str
    title: str
    unit_cogs: Optional[float] = None  # None = cout inconnu, jamais 0 par defaut

    @property
    def has_cogs(self) -> bool:
        return self.unit_cogs is not None


@dataclass(frozen=True)
class OrderItem:
    order_id: str
    sku: str
    title: str
    quantity: int
    unit_price: float
    product_id: Optional[str] = None

    @property
    def line_revenue(self) -> float:
        return self.quantity * self.unit_price


@dataclass
class Order:
    """Commande normalisee.

    Contrat de revenu (D-041): `subtotal` est le sous-total des articles APRES
    remises de commande, avant port et taxes. `discount` est le montant de
    remise DEJA deduit de `subtotal`: il est informatif et ne doit jamais etre
    soustrait une seconde fois. Un connecteur dont la source fournit un
    sous-total avant remise doit le convertir a l'ingestion, pas ici.
    """

    order_id: str
    customer_id: str
    created_at: datetime
    currency: str
    subtotal: float
    discount: float
    shipping: float
    tax: float
    total: float
    financial_status: str
    items: List[OrderItem] = field(default_factory=list)
    customer_email: Optional[str] = None
    source: str = "shopify"

    @property
    def net_revenue(self) -> float:
        """CA produit net de remise, hors frais de port et hors taxes (= subtotal normalise)."""
        return self.subtotal

    @property
    def units(self) -> int:
        return sum(i.quantity for i in self.items)


@dataclass(frozen=True)
class Payment:
    payment_id: str
    created_at: datetime
    amount: float
    fee: Optional[float]
    net: Optional[float]
    status: str
    order_id: Optional[str] = None
    customer_email: Optional[str] = None
    source: str = "stripe"

    @property
    def is_successful(self) -> bool:
        return self.status.lower() in {"paid", "succeeded", "available", "captured"}


@dataclass(frozen=True)
class Refund:
    refund_id: str
    created_at: datetime
    amount: float  # positif = montant rembourse
    order_id: Optional[str] = None
    source: str = "shopify"


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    name: str
    channel: str = "google_ads"


@dataclass(frozen=True)
class DailyAdPerformance:
    day: date
    campaign_id: str
    spend: float
    impressions: int
    clicks: int
    conversions: float
    conversion_value: float


@dataclass
class Customer:
    customer_id: str
    email: Optional[str]
    orders_count: int
    revenue: float
    first_order_at: datetime
    last_order_at: datetime


@dataclass
class Dataset:
    """Conteneur du modele normalise + sa qualite."""

    orders: List[Order] = field(default_factory=list)
    products: Dict[str, Product] = field(default_factory=dict)  # cle = sku
    payments: List[Payment] = field(default_factory=list)
    refunds: List[Refund] = field(default_factory=list)
    campaigns: Dict[str, Campaign] = field(default_factory=dict)
    ad_performance: List[DailyAdPerformance] = field(default_factory=list)
    quality: DataQualityReport = field(default_factory=DataQualityReport)
    currency: str = "EUR"
    #: premiere commande par client, calculee sur l'HISTORIQUE COMPLET et
    #: conservee lors des decoupages temporels (indispensable pour le CAC)
    customer_first_order: Dict[str, datetime] = field(default_factory=dict)

    # -- derivations ----------------------------------------------------
    def compute_customer_index(self) -> None:
        index: Dict[str, datetime] = {}
        for order in self.orders:
            current = index.get(order.customer_id)
            if current is None or order.created_at < current:
                index[order.customer_id] = order.created_at
        self.customer_first_order = index

    def customers(self) -> Dict[str, Customer]:
        acc: Dict[str, Customer] = {}
        for order in self.orders:
            existing = acc.get(order.customer_id)
            if existing is None:
                acc[order.customer_id] = Customer(
                    customer_id=order.customer_id,
                    email=order.customer_email,
                    orders_count=1,
                    revenue=order.net_revenue,
                    first_order_at=order.created_at,
                    last_order_at=order.created_at,
                )
            else:
                existing.orders_count += 1
                existing.revenue += order.net_revenue
                existing.first_order_at = min(existing.first_order_at, order.created_at)
                existing.last_order_at = max(existing.last_order_at, order.created_at)
        return acc

    def product_for(self, sku: str) -> Optional[Product]:
        return self.products.get(sku)

    # -- decoupage temporel ---------------------------------------------
    def window(self, start: datetime, end: datetime) -> "Dataset":
        """Vue filtree [start, end). Partage produits, campagnes et index client."""
        start_day, end_day = start.date(), end.date()
        return Dataset(
            orders=[o for o in self.orders if start <= o.created_at < end],
            products=self.products,
            payments=[p for p in self.payments if start <= p.created_at < end],
            refunds=[r for r in self.refunds if start <= r.created_at < end],
            campaigns=self.campaigns,
            ad_performance=[a for a in self.ad_performance if start_day <= a.day < end_day],
            quality=self.quality,
            currency=self.currency,
            customer_first_order=self.customer_first_order,
        )

    def is_empty(self) -> bool:
        return not (self.orders or self.payments or self.ad_performance)
