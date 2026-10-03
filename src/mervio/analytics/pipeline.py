"""Orchestration: CSV -> Dataset normalise -> analyse -> rapport structure.

C'est le point d'entree unique reutilisable par la CLI aujourd'hui et par un
service FastAPI demain. Aucune I/O de presentation ici.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..config import ENGINE_VERSION, AnalyticsConfig, resolve_timezone
from ..errors import InsufficientDataError
from ..ingestion import ingest_google_ads, ingest_shopify_orders, ingest_shopify_products, ingest_stripe
from ..logging_config import get_logger
from ..domain.models import Dataset
from ..identity import CustomerIdentity
from ..domain.quality import DataQualityReport, QualityStatus
from . import kpi as kpi_module
from .anomaly import Anomaly, baseline_points, detect_anomalies
from .health import compute_business_health
from .insights import build_insights
from .kpi import campaign_performance, compute_kpis, product_performance
from .periods import (
    DEFAULT_TIMEZONE, Period, last_complete_period, previous_period,
)
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
    "orders_per_paid_click": lambda ds: (len(ds.orders) / kpi_module.total_clicks(ds)) if kpi_module.total_clicks(ds) else None,
    "roas": lambda ds: (kpi_module.total_revenue(ds) / kpi_module.total_ad_spend(ds)) if kpi_module.total_ad_spend(ds) else None,
    "refunds": lambda ds: kpi_module.total_refunds(ds),
}


def load_dataset(paths: SourcePaths, identity: Optional[CustomerIdentity] = None,
                 config: Optional[AnalyticsConfig] = None) -> Dataset:
    """Connecteurs -> modele normalise. `identity`: cle des references client (D-053, D-058).

    Sans `identity`, une cle EPHEMERE est tiree pour cet appel: comportement des outils locaux
    (CLI analytique, scripts, evaluation synthetique). Ce repli ne peut pas atteindre la base:
    le jeu porte `identity_key_id` et `persistence.snapshots.write_snapshot` n'accepte qu'un jeu
    calcule avec la cle de l'ORGANISATION de la session (004.4.2, F-08).
    """
    identity = identity or CustomerIdentity.ephemeral()
    cfg = config or AnalyticsConfig()
    quality = DataQualityReport()
    dataset = Dataset(quality=quality, identity_key_id=identity.key_id)

    if paths.shopify_products:
        dataset.products = ingest_shopify_products(paths.shopify_products, quality)
    else:
        quality.add_issue("shopify_products", "source_missing", "warning",
                          "export produits absent: aucun cout produit, marge non calculable")
        quality.set_field("product_cogs", covered=0, total=0,
                          note="export produits Shopify non fourni")

    if paths.shopify_orders:
        orders, refunds, currency = ingest_shopify_orders(paths.shopify_orders, quality, identity)
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
        _reconcile_refunds(dataset, quality, cfg.refund_reconciliation_tolerance)
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


def _reconcile_refunds(dataset: Dataset, quality: DataQualityReport,
                       tolerance: float) -> None:
    """Shopify fait foi sur les remboursements; tout ecart Stripe est signale.

    AN-16: `tolerance` venait de `AnalyticsConfig.refund_reconciliation_tolerance`, champ qui
    existait avec exactement cette valeur sans jamais etre lu (code mort non releve par
    l'audit A-Z).
    """
    shopify_total = sum(r.amount for r in dataset.refunds if r.source == "shopify")
    stripe_total = sum(r.amount for r in dataset.refunds if r.source == "stripe")
    if shopify_total and stripe_total:
        gap = abs(shopify_total - stripe_total) / max(shopify_total, stripe_total)
        if gap > tolerance:
            quality.add_issue(
                "reconciliation", "refund_mismatch", "warning",
                f"ecart de {gap:.1%} entre remboursements Shopify ({shopify_total:,.2f}) "
                f"et Stripe ({stripe_total:,.2f}); Shopify fait foi dans les KPI",
            )


def latest_day_by_source(dataset: Dataset, tz: str = DEFAULT_TIMEZONE) -> Dict[str, Optional[date]]:
    """Dernier jour CIVIL LOCAL par source datee, dans le fuseau `tz` (AN-06 / AN-05).

    Les `created_at` sont des instants naifs signifiant UTC: leur jour civil depend donc du
    fuseau. `ad_performance.day` est deja un jour d'agregation sans instant: il n'y a rien a
    convertir, et l'inventer serait faux.

    Une source sans ligne rend `None`, jamais la date d'une AUTRE source: le detail par source
    sert a repondre << lequel de mes exports est en retard ? >>, et emprunter une date y
    repondrait faux.
    """
    zone = resolve_timezone(tz)

    def civil(moment: datetime) -> date:
        return moment.replace(tzinfo=timezone.utc).astimezone(zone).date()

    stripe = [civil(p.created_at) for p in dataset.payments]
    stripe += [civil(r.created_at) for r in dataset.refunds if r.source == "stripe"]
    orders = [civil(o.created_at) for o in dataset.orders]
    orders += [civil(r.created_at) for r in dataset.refunds if r.source != "stripe"]
    return {
        "shopify_orders": max(orders) if orders else None,
        "google_ads": max((a.day for a in dataset.ad_performance), default=None),
        "stripe": max(stripe) if stripe else None,
    }


def latest_data_day(dataset: Dataset, tz: str = DEFAULT_TIMEZONE) -> Optional[date]:
    """Dernier jour CIVIL LOCAL couvert par TOUTES les sources datees.

    AN-05: `payments` et `refunds` etaient ignores. Un export Stripe plus recent que l'export
    Shopify faisait donc declarer la donnee perimee a tort.
    """
    days = [d for d in latest_day_by_source(dataset, tz).values() if d is not None]
    return max(days) if days else None


#: cles de message du bloc de fraicheur. Stables et techniques: le rendu viendra du catalogue
#: AN-19, pas d'une chaine libre ecrite ici.
_FRESHNESS_KEYS = {"fresh": "data_freshness.fresh", "stale": "data_freshness.stale",
                   "unknown": "data_freshness.unknown"}


def build_data_freshness(dataset: Dataset, today: Optional[date], cfg: AnalyticsConfig) -> dict:
    """Bloc `data_freshness` (AN-05 / D-079).

    `last_complete_period` choisit QUELLE periode analyser; elle ne dit rien du retard. Les deux
    notions sont disjointes: un export arrete 40 jours plus tot produit une periode parfaitement
    close, analysee, et pourtant perimee.

    `analysis_date` est le parametre `today`, JAMAIS l'horloge murale: `_meta.generated_at` est le
    seul champ non deterministe autorise (D-058, reserve 1).
    """
    sources = latest_day_by_source(dataset, cfg.timezone)
    observed = [d for d in sources.values() if d is not None]
    latest = max(observed) if observed else None
    delay = (today - latest).days if (today is not None and latest is not None) else None
    if delay is None:
        status = "unknown"
    else:
        status = "stale" if delay > cfg.freshness_stale_days else "fresh"
    return {
        "latest_data_day": latest.isoformat() if latest else None,
        "analysis_date": today.isoformat() if today else None,
        "delay_days": delay,
        "threshold_days": cfg.freshness_stale_days,
        "status": status,
        "message_key": _FRESHNESS_KEYS[status],
        "sources": {name: (day.isoformat() if day else None) for name, day in sources.items()},
    }


def run_analysis(
    paths: SourcePaths,
    config: Optional[AnalyticsConfig] = None,
    today: Optional[date] = None,
    identity: Optional[CustomerIdentity] = None,
) -> dict:
    return analyze_loaded_dataset(load_dataset(paths, identity, config), config, today)


def analyze_loaded_dataset(
    dataset: Dataset,
    config: Optional[AnalyticsConfig] = None,
    today: Optional[date] = None,
) -> dict:
    """Analyse un Dataset deja normalise, quelle que soit son origine (CSV, instantane persiste).

    Meme calcul que `run_analysis`, qui n'est que chargement + cet appel. Le
    Dataset est modifie comme lors d'une analyse CSV (signaux qualite ajoutes):
    ne pas le reutiliser pour une seconde analyse.
    """
    cfg = config or AnalyticsConfig()
    if dataset.is_empty():
        raise InsufficientDataError("aucune donnee exploitable dans les sources fournies")

    latest_day = latest_data_day(dataset, cfg.timezone)
    period = last_complete_period(latest_day, today, cfg.grain, cfg.timezone)
    prior = previous_period(period)
    window = dataset.window(period.start, period.end)

    kpis = compute_kpis(window, period, cfg.quality)
    profitability = compute_profitability(window, period.label, cfg.profitability)

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
    # AN-01: la decomposition recoit la baseline DECLAREE par le detecteur, elle ne la
    # recalcule jamais. `baseline_points` est la source unique de la definition de la fenetre:
    # une periode sans donnee ne peut donc pas faire divergier les deux referentiels.
    revenue_baseline = [point.period for point in baseline_points(series["revenue"], cfg.anomaly)]
    root_cause = analyse_revenue_change(dataset, period, prior,
                                       baseline=revenue_baseline or None,
                                       cfg=cfg.root_cause, cfg_anomaly=cfg.anomaly)
    health = compute_business_health(kpis, comparisons, profitability, window, dataset.quality, cfg.health)
    insights = build_insights(kpis, anomalies, root_cause, profitability, window,
                              dataset.currency, cfg)

    freshness = build_data_freshness(dataset, today, cfg)
    return build_report(dataset, window, period, prior, kpis, profitability, series,
                        anomalies, comparisons, root_cause, health, insights, cfg,
                        freshness=freshness)


def build_report(dataset, window, period: Period, prior: Period, kpis, profitability,
                 series, anomalies, comparisons, root_cause, health, insights, cfg,
                 freshness: Optional[dict] = None) -> dict:
    from .report import assemble_report
    return assemble_report(dataset, window, period, prior, kpis, profitability, series,
                           anomalies, comparisons, root_cause, health, insights, cfg,
                           engine_version=ENGINE_VERSION, freshness=freshness)
