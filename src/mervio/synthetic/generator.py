"""Simulation journaliere deterministe et ecriture en flux.

Modele (par jour):
  demande attendue = volume cible x saisonnalite x jour de semaine x tendance x bruit
  sessions payantes / organiques -> commandes par canal (conversion)
  commande -> client (nouveau ou recurrent) -> lignes -> remise -> port -> taxes
           -> Total -> paiement -> remboursement eventuel -> expedition

Les fichiers ecrits suivent les formats que les connecteurs Mervio lisent deja
(export commandes et produits Shopify, export Stripe, rapport Google Ads), plus
des tables canoniques que le moteur n'ingere pas encore (clients, sessions,
stock, expeditions). Les commandes sont ecrites au fil de l'eau: la memoire du
generateur ne croit pas avec le volume de commandes.

Contrat de montants, identique au contrat Mervio (D-041): Subtotal = somme des
lignes APRES remise, avant port et taxes; Total = Subtotal + port + taxes.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .profiles import PROFILES, CampaignSpec, ProductSpec, StoreProfile
from .rng import rng_for
from .scenarios import Effects, ScenarioSpec, get_scenario

GENERATOR_VERSION = "0.1.0"
#: version du schema des fichiers ecrits; toute evolution de colonne l'incremente
SCHEMA_VERSION = "1"

ORDERS_HEADER = ["Name", "Email", "Financial Status", "Paid at", "Fulfillment Status", "Currency", "Subtotal",
                 "Shipping", "Taxes", "Total", "Discount Code", "Discount Amount", "Created at",
                 "Lineitem quantity", "Lineitem name", "Lineitem price", "Lineitem sku", "Cancelled at",
                 "Refunded Amount", "Source"]
PRODUCTS_HEADER = ["SKU", "Product ID", "Title", "Product Category", "Variant Price", "Cost per item"]
STRIPE_HEADER = ["id", "Created (UTC)", "Amount", "Amount Refunded", "Currency", "Fee", "Net", "Status",
                 "Description", "order_id"]
ADS_HEADER = ["Day", "Campaign", "Campaign ID", "Cost", "Impressions", "Clicks", "Conversions", "Conv. value"]
CUSTOMERS_HEADER = ["customer_id", "first_order_date", "acquisition_channel"]
SESSIONS_HEADER = ["date", "channel", "campaign_id", "sessions", "carts", "orders"]
INVENTORY_HEADER = ["date", "sku", "available", "units_sold", "lost_demand_orders"]
SHIPMENTS_HEADER = ["order_name", "shipped_date", "delivered_date", "late"]

FILES = {
    "shopify_orders.csv": ORDERS_HEADER, "shopify_products.csv": PRODUCTS_HEADER,
    "stripe_transactions.csv": STRIPE_HEADER, "google_ads.csv": ADS_HEADER,
    "customers.csv": CUSTOMERS_HEADER, "sessions_daily.csv": SESSIONS_HEADER,
    "inventory_daily.csv": INVENTORY_HEADER, "shipments.csv": SHIPMENTS_HEADER,
}
#: fichiers aux formats des connecteurs du moteur actuel
ENGINE_FILES = ("shopify_orders.csv", "shopify_products.csv", "stripe_transactions.csv", "google_ads.csv")


@dataclass(frozen=True)
class GeneratorConfig:
    scenario: str = "healthy_store"
    orders: int = 10_000            # volume cible sur l'horizon, avant effet du scenario
    days: int = 365
    end_date: date = date(2026, 9, 13)
    seed: int = 42
    profile: str = "fashion_eu"

    def __post_init__(self) -> None:
        if self.orders < 1:
            raise ValueError("orders doit etre >= 1")
        if self.days < 14:
            raise ValueError("days doit etre >= 14 (au moins deux semaines closes)")
        if self.end_date.weekday() != 6:
            raise ValueError("end_date doit etre un dimanche: la derniere semaine ISO doit etre close")
        if self.profile not in PROFILES:
            raise ValueError(f"profil inconnu: {self.profile} (connus: {', '.join(sorted(PROFILES))})")
        get_scenario(self.scenario)

    @property
    def start_date(self) -> date:
        return self.end_date - timedelta(days=self.days - 1)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["end_date"] = self.end_date.isoformat()
        data["start_date"] = self.start_date.isoformat()
        return data


@dataclass
class GenerationResult:
    directory: Path
    manifest: dict
    counts: Dict[str, int] = field(default_factory=dict)


def _money(value: float) -> str:
    return f"{value:.2f}"


def _ranked_products(profile: StoreProfile) -> List[ProductSpec]:
    """Produits classes par part de CA attendue (poids x prix): le rang 0 pese le plus dans le CA."""
    return sorted(profile.products, key=lambda p: (-p.weight * p.price, p.sku))


class _Simulation:
    def __init__(self, config: GeneratorConfig):
        self.config = config
        self.profile = PROFILES[config.profile]
        self.scenario: ScenarioSpec = get_scenario(config.scenario)
        self.effects: Effects = self.scenario.effects
        self.ranked = _ranked_products(self.profile)
        self.customer_count = 0
        self.order_seq = 100_000
        amplitude = self.effects.seasonal_amplitude
        self.seasonal_amplitude = self.profile.seasonal_amplitude if amplitude is None else amplitude
        self.base_factors = [self._base_factor(i) for i in range(config.days)]
        self.factor_sum = sum(self.base_factors)
        profile = self.profile
        #: commandes par session a conversion de base, tous canaux confondus: calibre le volume cible
        self.channel_mix = (profile.paid_share * sum(c.budget_share * c.cvr_multiplier for c in profile.campaigns)
                            + (1 - profile.paid_share) * profile.organic_cvr_multiplier)

    # --- facteurs ---------------------------------------------------------
    def _day(self, index: int) -> date:
        return self.config.start_date + timedelta(days=index)

    def _base_factor(self, index: int) -> float:
        day = self._day(index)
        peak = self.effects.seasonal_peak_day
        seasonal = 1.0 + self.seasonal_amplitude * math.cos(2 * math.pi * (day.timetuple().tm_yday - peak) / 365.25)
        trend = 1.0 + self.profile.annual_trend * index / 365.25
        return max(0.05, seasonal) * trend * self.profile.weekday_factors[day.weekday()]

    def strength(self, index: int) -> float:
        """Intensite de l'effet du scenario ce jour-la (0 = aucun, 1 = plein)."""
        window = self.effects.window
        if window == "always":
            return 1.0
        if window == "ramp":
            return index / max(1, self.config.days - 1)
        return 1.0 if index >= self.config.days - 7 else 0.0

    @staticmethod
    def _mix(multiplier: float, strength: float) -> float:
        return 1.0 + (multiplier - 1.0) * strength

    # --- simulation ---------------------------------------------------------
    def run(self, sink: "_Sink") -> None:
        self.sink = sink
        profile, config = self.profile, self.config
        cost_multiplier = self.effects.cost_multiplier
        for product in profile.products:
            sink.product(product, round(product.unit_cost * cost_multiplier, 2))
        for index in range(config.days):
            self._simulate_day(index, sink)

    def _simulate_day(self, index: int, sink: "_Sink") -> None:
        profile, effects = self.profile, self.effects
        day = self._day(index)
        rng = rng_for(self.config.seed, f"day:{day.isoformat()}")
        s = self.strength(index)
        expected_orders = self.config.orders * self.base_factors[index] / self.factor_sum
        expected_orders *= 1.0 + rng.uniform(-profile.daily_noise, profile.daily_noise)
        sessions = expected_orders / (profile.base_cvr * self.channel_mix)

        paid_sessions = sessions * profile.paid_share * self._mix(effects.paid_traffic, s)
        organic_sessions = sessions * (1 - profile.paid_share) * self._mix(effects.organic_traffic, s)
        cvr = profile.base_cvr * self._mix(effects.cvr, s)
        available = self._available_products(s)
        weights = self._product_weights(s)
        keep = {self.ranked[rank].sku: self._mix(m, s) for rank, m in effects.product_demand}
        demand = _Demand(profile.products, weights, available, keep)

        channels = []
        for campaign in profile.campaigns:
            clicks = paid_sessions * campaign.budget_share
            campaign_cvr = cvr * campaign.cvr_multiplier * self._campaign_multiplier(campaign, s)
            channels.append((campaign, clicks, clicks * campaign_cvr))
        organic_cvr = cvr * profile.organic_cvr_multiplier
        channels.append((None, organic_sessions, organic_sessions * organic_cvr))

        units_by_sku: Dict[str, int] = {}
        lost_by_sku: Dict[str, int] = {}
        for campaign, traffic, expected in channels:
            planned = int(expected) + (1 if rng.random() < expected - int(expected) else 0)
            realised, revenue = 0, 0.0
            for _ in range(planned):
                order = self._order(day, rng, s, demand, campaign, units_by_sku, lost_by_sku)
                if order is None:
                    continue
                realised += 1
                revenue += order
            clicks = int(round(traffic))
            sink.session(day, "paid" if campaign else "organic", campaign.campaign_id if campaign else "",
                         clicks, int(round(traffic * profile.cart_rate)), realised)
            if campaign is not None:
                cpc = campaign.cpc * (1 + rng.uniform(-0.08, 0.08))
                ctr = 0.035 * (1 + rng.uniform(-0.15, 0.15))
                sink.ad(day, campaign, round(clicks * cpc, 2), int(clicks / ctr), clicks, realised, round(revenue, 2))
        for product in profile.products:
            sink.inventory(day, product.sku, product.sku in available, units_by_sku.get(product.sku, 0),
                           lost_by_sku.get(product.sku, 0))

    def _campaign_multiplier(self, campaign: CampaignSpec, strength: float) -> float:
        for fragment, multiplier in self.effects.campaign_cvr:
            if fragment in campaign.name:
                return self._mix(multiplier, strength)
        return 1.0

    def _available_products(self, strength: float) -> set:
        skus = {p.sku for p in self.profile.products}
        if strength > 0:
            skus -= {self.ranked[rank].sku for rank in self.effects.stockout_products}
        return skus

    def _product_weights(self, strength: float) -> List[float]:
        effects = self.effects
        average_price = sum(p.price for p in self.profile.products) / len(self.profile.products)
        weights = []
        for product in self.profile.products:
            weight = product.weight
            if effects.cheap_mix:
                weight *= (average_price / product.price) ** (effects.cheap_mix * strength)
            weights.append(weight)
        return weights

    def _order(self, day: date, rng, strength: float, demand: "_Demand", campaign: Optional[CampaignSpec],
               units_by_sku: Dict[str, int], lost_by_sku: Dict[str, int]) -> Optional[float]:
        profile, effects = self.profile, self.effects
        one, two, three = profile.line_count_weights
        shift = effects.single_line_shift * strength
        line_weights = (one + (two + three) * shift, two * (1 - shift), three * (1 - shift))
        n_lines = rng.choices((1, 2, 3), weights=line_weights)[0]

        lines = []
        for _ in range(n_lines):
            product = rng.choices(demand.products, cum_weights=demand.cumulative)[0]
            if product.sku in demand.keep and rng.random() >= demand.keep[product.sku]:
                lost_by_sku[product.sku] = lost_by_sku.get(product.sku, 0) + 1
                continue
            if product.sku not in demand.available:
                if rng.random() >= profile.substitution_rate or not demand.substitutes:
                    lost_by_sku[product.sku] = lost_by_sku.get(product.sku, 0) + 1
                    continue
                product = rng.choices(demand.substitutes, cum_weights=demand.substitute_cumulative)[0]
            if any(line[0].sku == product.sku for line in lines):
                continue
            quantity = 2 if rng.random() < profile.quantity_two_probability else 1
            lines.append((product, quantity))
        if not lines:
            return None

        self.order_seq += 1
        name = f"#{self.order_seq}"
        gross = sum(p.price * q for p, q in lines)
        p_discount = profile.discount_probability if effects.discount_probability is None or strength == 0 \
            else profile.discount_probability + (effects.discount_probability - profile.discount_probability) * strength
        rate = profile.discount_rate if effects.discount_rate is None or strength == 0 else effects.discount_rate
        discounted = rng.random() < p_discount
        discount = round(gross * rate, 2) if discounted else 0.0
        subtotal = round(gross - discount, 2)
        shipping = 0.0 if subtotal >= profile.free_shipping_threshold else profile.shipping_fee
        tax = round(subtotal * profile.tax_rate, 2)
        total = round(subtotal + shipping + tax, 2)

        returning_share = profile.returning_customer_share * self._mix(effects.returning_share_multiplier, strength)
        if self.customer_count and rng.random() < returning_share:
            customer = rng.randint(1, self.customer_count)
            new_customer = False
        else:
            self.customer_count += 1
            customer = self.customer_count
            new_customer = True

        refund_multiplier = self._mix(effects.refund_probability_multiplier, strength)
        product_refund = dict((self.ranked[rank].sku, m) for rank, m in effects.product_refund_multiplier)
        for product, _ in lines:
            refund_multiplier = max(refund_multiplier, self._mix(product_refund.get(product.sku, 1.0), strength))
        refunded = 0.0
        status = "paid"
        if rng.random() < min(0.95, profile.refund_probability * refund_multiplier):
            if rng.random() < profile.full_refund_share:
                refunded, status = total, "refunded"
            else:
                refunded, status = round(subtotal * rng.uniform(0.2, 0.6), 2), "partially_refunded"

        stamp = datetime.combine(day, datetime.min.time()) + timedelta(hours=rng.randint(7, 22), minutes=rng.randint(0, 59))
        late_share = profile.late_delivery_share if effects.late_delivery_share is None or strength == 0 \
            else effects.late_delivery_share
        late = rng.random() < late_share
        channel = f"google_ads:{campaign.campaign_id}" if campaign else "organic"
        self.sink.order(name, customer, new_customer, channel, stamp, status, lines, subtotal, discount,
                        "SYNTH10" if discounted else "", shipping, tax, total, refunded, late, rng)
        for product, quantity in lines:
            units_by_sku[product.sku] = units_by_sku.get(product.sku, 0) + quantity
        return subtotal


class _Demand:
    """Tirage des produits d'un jour: poids cumules calcules une fois par jour, pas par commande."""

    def __init__(self, products: List[ProductSpec], weights: List[float], available: set,
                 keep: Optional[Dict[str, float]] = None):
        from itertools import accumulate
        #: probabilite qu'une demande pour le produit aboutisse (effondrement de demande: le client renonce)
        self.keep = keep or {}
        self.products = list(products)
        self.cumulative = list(accumulate(weights))
        self.available = available
        pairs = [(p, w) for p, w in zip(products, weights) if p.sku in available]
        self.substitutes = [p for p, _ in pairs]
        self.substitute_cumulative = list(accumulate(w for _, w in pairs))


class _Sink:
    """Destination des enregistrements: fichiers CSV en flux, sommes de controle."""

    def __init__(self, directory: Path, profile: StoreProfile):
        self.directory = directory
        self.profile = profile
        self.handles = {}
        self.writers = {}
        self.counts: Dict[str, int] = {name: 0 for name in FILES}
        directory.mkdir(parents=True, exist_ok=True)
        for name, header in FILES.items():
            handle = (directory / name).open("w", encoding="utf-8", newline="")
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            self.handles[name], self.writers[name] = handle, writer

    def _write(self, name: str, row: list) -> None:
        self.writers[name].writerow(row)
        self.counts[name] += 1

    def product(self, product: ProductSpec, unit_cost: float) -> None:
        self._write("shopify_products.csv", [product.sku, f"gid-{product.sku}", product.title, product.category,
                                             _money(product.price), _money(unit_cost)])

    def order(self, name, customer, new_customer, channel, stamp, status, lines, subtotal, discount, code,
              shipping, tax, total, refunded, late, rng) -> None:
        currency = self.profile.currency
        created = stamp.strftime("%Y-%m-%d %H:%M:%S") + " +0000"
        email = f"c{customer:07d}@customers.synthetic.invalid"
        for index, (product, quantity) in enumerate(lines):
            first = index == 0
            self._write("shopify_orders.csv", [
                name, email if first else "", status if first else "", created if first else "",
                "fulfilled" if first else "", currency if first else "",
                _money(subtotal) if first else "", _money(shipping) if first else "", _money(tax) if first else "",
                _money(total) if first else "", code if first else "", _money(discount) if first else "",
                created if first else "", quantity, product.title, _money(product.price), product.sku,
                "", _money(refunded) if first and refunded else "", "web" if first else "",
            ])
        fee = round(total * 0.014 + 0.25, 2)
        self._write("stripe_transactions.csv", [
            f"ch_synth_{name[1:]}", stamp.strftime("%Y-%m-%d %H:%M:%S"), _money(total), _money(refunded),
            currency.lower(), _money(fee), _money(total - fee), "Paid", f"Synthetic order {name}", name,
        ])
        if new_customer:
            self._write("customers.csv", [f"C{customer:07d}", stamp.date().isoformat(), channel])
        shipped = stamp.date() + timedelta(days=1)
        delivered = shipped + timedelta(days=(rng.randint(6, 12) if late else rng.randint(2, 4)))
        self._write("shipments.csv", [name, shipped.isoformat(), delivered.isoformat(), "true" if late else "false"])

    def ad(self, day, campaign, cost, impressions, clicks, conversions, value) -> None:
        self._write("google_ads.csv", [day.isoformat(), campaign.name, campaign.campaign_id, _money(cost),
                                       impressions, clicks, f"{conversions:.2f}", _money(value)])

    def session(self, day, channel, campaign_id, sessions, carts, orders) -> None:
        self._write("sessions_daily.csv", [day.isoformat(), channel, campaign_id, sessions, carts, orders])

    def inventory(self, day, sku, available, units, lost) -> None:
        self._write("inventory_daily.csv", [day.isoformat(), sku, "true" if available else "false", units, lost])

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_dataset(config: GeneratorConfig, directory: str | Path) -> GenerationResult:
    """Genere un jeu synthetique complet dans `directory` et retourne son manifeste.

    Deux appels avec la meme configuration produisent des fichiers identiques
    octet pour octet (le manifeste ne contient aucun horodatage).
    """
    target = Path(directory)
    simulation = _Simulation(config)
    sink = _Sink(target, simulation.profile)
    try:
        simulation.run(sink)
    finally:
        sink.close()
    files = {name: {"rows": sink.counts[name], "sha256": _sha256(target / name),
                    "consumed_by_engine": name in ENGINE_FILES} for name in FILES}
    manifest = {
        "synthetic": True,
        "not_for_production": True,
        "notice": "Donnees entierement fabriquees par mervio.synthetic. Aucune entreprise, aucun client reel.",
        "generator": "mervio.synthetic", "generator_version": GENERATOR_VERSION, "schema_version": SCHEMA_VERSION,
        "config": config.to_dict(), "profile": simulation.profile.key, "currency": simulation.profile.currency,
        "scenario": simulation.scenario.to_dict(),
        "resolved_products_by_rank": [p.sku for p in simulation.ranked],
        "counts": {"orders": simulation.order_seq - 100_000, "customers": simulation.customer_count},
        "files": files,
        "amount_contract": "Subtotal = lignes apres remise, avant port et taxes; Total = Subtotal + port + taxes (D-041)",
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")
    return GenerationResult(target, manifest, dict(sink.counts))
