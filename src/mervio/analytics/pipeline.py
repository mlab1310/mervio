"""Orchestration: CSV -> Dataset normalise -> analyse -> rapport structure.

C'est le point d'entree unique reutilisable par la CLI aujourd'hui et par un
service FastAPI demain. Aucune I/O de presentation ici.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..config import ENGINE_VERSION, AnalyticsConfig
from ..errors import InsufficientDataError
from ..ingestion import ingest_google_ads, ingest_shopify_orders, ingest_shopify_products, ingest_stripe
from ..logging_config import get_logger
from ..domain.models import Dataset
from ..domain.quality import DataQualityReport, QualityStatus
from . import kpi as kpi_module
from .anomaly import Anomaly, detect_anomalies
from .health import compute_business_health
from .insights import build_insights
from .kpi import campaign_performance, compute_kpis, product_performance
from .periods import Period, last_complete_period, previous_period
from .profitability import compute_profitability
from .root_cause import analyse_revenue_change
from .timeseries import build_series, compare_all, rolling_average

log = get_logger("pipeline")

#: devise d'un export qui n'en declare aucune. Jamais une devise supposee.
UNKNOWN_CURRENCY = "unknown"


@dataclass
class SourcePaths:
    shopify_orders: Optional[str] = None
    shopify_products: Optional[str] = None
    stripe: Optional[str] = None
    google_ads: Optional[str] = None


#: metriques suivies en serie temporelle et surveillees par le detecteur
SERIES_METRICS = {
    "revenue": lambda ds: kpi_module.total_revenue(ds),
    "orders": lambda ds: float(len(ds.orders)),
    "aov": lambda ds: (kpi_module.total_revenue(ds) / len(ds.orders)) if ds.orders else None,
    "ad_spend": lambda ds: kpi_module.total_ad_spend(ds),
    "paid_clicks": lambda ds: float(kpi_module.total_clicks(ds)),
    "paid_conversion_rate": lambda ds: (len(ds.orders) / kpi_module.total_clicks(ds)) if kpi_module.total_clicks(ds) else None,
    "roas": lambda ds: (kpi_module.total_revenue(ds) / kpi_module.total_ad_spend(ds)) if kpi_module.total_ad_spend(ds) else None,
    "refunds": lambda ds: kpi_module.total_refunds(ds),
}


def load_dataset(paths: SourcePaths) -> Dataset:
    quality = DataQualityReport()
    dataset = Dataset(quality=quality)

    if paths.shopify_products:
        dataset.products = ingest_shopify_products(paths.shopify_products, quality)
    else:
        quality.add_issue("shopify_products", "source_missing", "warning",
                          "export produits absent: aucun cout produit, marge non calculable")
        quality.set_field("product_cogs", covered=0, total=0,
                          note="export produits Shopify non fourni")

    if paths.shopify_orders:
        orders, refunds, currency = ingest_shopify_orders(paths.shopify_orders, quality)
        dataset.orders = orders
        dataset.refunds.extend(refunds)
        dataset.currency = currency or UNKNOWN_CURRENCY
    else:
        quality.add_issue("shopify_orders", "source_missing", "error",
                          "export commandes absent: aucun CA calculable")

    if paths.stripe:
        payments, stripe_refunds = ingest_stripe(paths.stripe, quality)
        dataset.payments = payments
        dataset.refunds.extend(stripe_refunds)
        _reconcile_refunds(dataset, quality)
    else:
        quality.add_issue("stripe", "source_missing", "warning",
                          "Stripe absent: frais de paiement inconnus, profitabilite incomplete")
        quality.set_field("payment_fees", covered=0, total=0, note="Stripe non connecte")

    if paths.google_ads:
        campaigns, performance = ingest_google_ads(paths.google_ads, quality)
        dataset.campaigns = campaigns
        dataset.ad_performance = performance
    else:
        quality.add_issue("google_ads", "source_missing", "warning",
                          "Google Ads absent: CAC et ROAS non calculables")
        quality.set_field("ad_spend", covered=0, total=0, note="Google Ads non connecte")

    _check_currency_coherence(dataset, quality)
    dataset.compute_customer_index()
    return dataset


def _check_currency_coherence(dataset: Dataset, quality: DataQualityReport) -> None:
    """Additionner des EUR et des USD produirait un CA faux sans aucun signal."""
    currencies = quality.distinct_currencies()
    if len(currencies) > 1:
        quality.add_issue(
            "reconciliation", "currency_mismatch", "error",
            "devises incoherentes entre sources (" + ", ".join(currencies)
            + f"): les montants sont agreges en {dataset.currency} sans conversion",
        )
        quality.set_status("currency", QualityStatus.UNAVAILABLE,
                           "plusieurs devises detectees: conversion non implementee")
    elif dataset.orders and not quality.observed_currencies.get("shopify"):
        # aucune devise lue: la supposer inventerait un fait
        quality.add_issue("shopify", "currency_absent", "warning",
                          "aucune devise dans l'export commandes: montants affiches sans devise verifiee")
        quality.set_status("currency", QualityStatus.UNAVAILABLE, "devise absente de l'export commandes")
    elif currencies:
        partial = any(i.source == "shopify" and i.kind == "missing_currency" for i in quality.issues)
        quality.set_status("currency", QualityStatus.INCOMPLETE if partial else QualityStatus.RELIABLE,
                           f"devise unique: {currencies[0]}"
                           + (" (certaines commandes sans devise)" if partial else ""))


def _reconcile_refunds(dataset: Dataset, quality: DataQualityReport) -> None:
    """Shopify fait foi sur les remboursements; tout ecart Stripe est signale."""
    shopify_total = sum(r.amount for r in dataset.refunds if r.source == "shopify")
    stripe_total = sum(r.amount for r in dataset.refunds if r.source == "stripe")
    if shopify_total and stripe_total:
        gap = abs(shopify_total - stripe_total) / max(shopify_total, stripe_total)
        if gap > 0.01:
            quality.add_issue(
                "reconciliation", "refund_mismatch", "warning",
                f"ecart de {gap:.1%} entre remboursements Shopify ({shopify_total:,.2f}) "
                f"et Stripe ({stripe_total:,.2f}); Shopify fait foi dans les KPI",
            )


def latest_data_day(dataset: Dataset) -> Optional[date]:
    days: List[date] = [o.created_at.date() for o in dataset.orders]
    days += [a.day for a in dataset.ad_performance]
    return max(days) if days else None


def run_analysis(
    paths: SourcePaths,
    config: Optional[AnalyticsConfig] = None,
    today: Optional[date] = None,
) -> dict:
    cfg = config or AnalyticsConfig()
    dataset = load_dataset(paths)
    if dataset.is_empty():
        raise InsufficientDataError("aucune donnee exploitable dans les sources fournies")

    latest_day = latest_data_day(dataset)
    period = last_complete_period(latest_day, today, cfg.grain)
    prior = previous_period(period)
    window = dataset.window(period.start, period.end)

    kpis = compute_kpis(window, period)
    profitability = compute_profitability(window, period.label)

    series: Dict[str, list] = {
        name: build_series(dataset, period, cfg.lookback_periods, fn)
        for name, fn in SERIES_METRICS.items()
    }
    negative = [point.period.label for point in series["revenue"] if point.value is not None and point.value < 0]
    if negative:
        dataset.quality.add_issue(
            "analytics", "negative_period_revenue", "warning",
            f"CA negatif sur {len(negative)} periode(s) de la serie ({', '.join(negative)}): "
            "valeur conservee, montants source a verifier",
        )
    anomalies: List[Anomaly] = []
    for name, points in series.items():
        anomalies.extend(detect_anomalies(name, points, cfg.anomaly))

    comparisons = compare_all(dataset, period, SERIES_METRICS)
    root_cause = analyse_revenue_change(dataset, period, prior)
    health = compute_business_health(kpis, comparisons, profitability, window, dataset.quality, cfg.health)
    insights = build_insights(kpis, anomalies, root_cause, profitability, window, dataset.currency)

    return build_report(dataset, window, period, prior, kpis, profitability, series,
                        anomalies, comparisons, root_cause, health, insights, cfg)


def build_report(dataset, window, period: Period, prior: Period, kpis, profitability,
                 series, anomalies, comparisons, root_cause, health, insights, cfg) -> dict:
    from .report import assemble_report
    return assemble_report(dataset, window, period, prior, kpis, profitability, series,
                           anomalies, comparisons, root_cause, health, insights, cfg,
                           engine_version=ENGINE_VERSION)
