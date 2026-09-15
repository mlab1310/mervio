"""Connecteur Google Ads (export rapport campagne journalier)."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from ..logging_config import get_logger
from ..domain.models import Campaign, DailyAdPerformance
from ..domain.quality import DataQualityReport
from .base import first_present, parse_date, parse_float, parse_int, read_csv, require_columns, resolve_date_order

SOURCE = "google_ads"
log = get_logger("ingestion.google_ads")


def ingest_google_ads(
    path: str | Path, quality: DataQualityReport
) -> Tuple[Dict[str, Campaign], List[DailyAdPerformance]]:
    rows = read_csv(path, SOURCE, quality)
    require_columns(rows, ("Day", "Campaign", "Cost"), SOURCE)
    quality.mark_source("google_ads")

    campaigns: Dict[str, Campaign] = {}
    performance: List[DailyAdPerformance] = []
    seen: set = set()
    with_conv_value = 0
    date_order = resolve_date_order((row.get("Day") for row in rows), source=SOURCE, column="Day", quality=quality)

    for index, row in enumerate(rows, start=2):
        day = parse_date(row.get("Day"), source=SOURCE, column="Day", row=index, required=False, date_order=date_order)
        if day is None:
            quality.add_issue(SOURCE, "invalid_date", "error", "ligne Google Ads sans date valide, ignoree")
            continue
        name = (row.get("Campaign") or "").strip()
        if not name:
            quality.add_issue(SOURCE, "missing_campaign", "error", "ligne sans nom de campagne, ignoree")
            continue
        campaign_id = (row.get("Campaign ID") or name).strip()
        campaigns.setdefault(campaign_id, Campaign(campaign_id=campaign_id, name=name, channel="google_ads"))

        key = (campaign_id, day)
        if key in seen:
            quality.add_issue(SOURCE, "duplicate_ad_row", "warning",
                              "ligne campagne/jour dupliquee, dedupliquee")
            continue
        seen.add(key)

        spend = parse_float(row.get("Cost"), source=SOURCE, column="Cost", row=index, default=0.0) or 0.0
        if spend < 0:
            quality.add_issue(SOURCE, "negative_spend", "error", f"depense negative sur {name} le {day}, ignoree")
            continue
        conv_value = parse_float(
            first_present(row, ("Conv. value", "Conversion value", "Total conv. value")),
            source=SOURCE, column="Conv. value", row=index,
        )
        if conv_value is not None:
            with_conv_value += 1

        performance.append(DailyAdPerformance(
            day=day, campaign_id=campaign_id, spend=spend,
            impressions=parse_int(row.get("Impressions"), source=SOURCE, column="Impressions", row=index, default=0) or 0,
            clicks=parse_int(row.get("Clicks"), source=SOURCE, column="Clicks", row=index, default=0) or 0,
            conversions=parse_float(row.get("Conversions"), source=SOURCE, column="Conversions", row=index, default=0.0) or 0.0,
            conversion_value=conv_value or 0.0,
        ))

    quality.set_rows(SOURCE, total=len(rows), accepted=len(performance))
    quality.set_field("ad_spend", covered=len(performance), total=len(performance) or 1,
                      note="depense publicitaire Google Ads uniquement (Meta Ads non connecte)")
    performance.sort(key=lambda a: (a.day, a.campaign_id))
    log.info("google ads: %s campagnes, %s lignes jour", len(campaigns), len(performance))
    return campaigns, performance
