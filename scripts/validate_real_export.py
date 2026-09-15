"""Validation d'un export marchand REEL, sans jamais exposer une valeur brute.

    python scripts/validate_real_export.py --shopify-orders orders_export.csv \
        [--products products.csv] [--stripe payments.csv] [--google-ads ads.csv] \
        [--today AAAA-MM-JJ] [--out analysis/validation.json]

Le script ne modifie ni le moteur ni les donnees. Il enchaine:

1. inspection et validation par les vrais outils Mervio (D-014, D-024);
2. analyse complete par analyze_dataset();
3. un calcul de REFERENCE independant (parseur CSV propre a ce script) des KPI
   de la periode analysee, compare au rapport avec une tolerance documentee;
4. des sondes semantiques sur ce que les fixtures synthetiques ne peuvent pas
   prouver: convention du Subtotal (avant ou apres remise, deduite de la
   colonne Total), commandes annulees ou non payees, devises, periodes,
   particularites des exports Stripe et Google Ads;
5. des controles du rapport (refus du profit, coherence et determinisme du
   score) et du contexte LLM 1.0 (taille, absence de PII), avec le fournisseur
   mock uniquement: aucune donnee ne quitte la machine.

Sortie: agregats, comptages, noms de colonnes et codes. Un garde-fou final
refuse d'emettre la sortie si un email, un numero de commande ou un nom lu
dans les fichiers s'y retrouve.
"""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import re
import resource
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mervio.analytics.pipeline import SourcePaths, load_dataset  # noqa: E402
from mervio.application.imports import validate_file  # noqa: E402
from mervio.application.inspector import inspect_file  # noqa: E402
from mervio.application.service import AnalysisRequest, analyze_dataset  # noqa: E402
from mervio.config import AnalyticsConfig  # noqa: E402
from mervio.errors import MervioError  # noqa: E402
from mervio.llm import LLMConfig, MockLLMProvider, build_llm_context, explain_report, serialize_context  # noqa: E402
from mervio.llm.contract import ContextLimits  # noqa: E402
from mervio.llm.safety import sensitive_findings  # noqa: E402
from mervio.reporting.executive import render_executive_report  # noqa: E402

#: Tolerance de rapprochement. Le rapport arrondit les KPI a 4 decimales et les
#: montants de profitabilite a 2: au-dela d'un centime (plus une marge relative
#: pour les tres gros totaux en float), l'ecart n'est plus un arrondi.
TOLERANCE_ABS = 0.01
TOLERANCE_REL = 1e-9
#: une commande "respecte" une convention si Total est reconstruit au centime
#: pres (arrondi independant de chaque composante: 2 centimes)
TOTAL_MATCH = 0.02
#: nombre minimal de commandes remisees pour conclure sur la convention du Subtotal
MIN_DISCOUNTED_ORDERS = 10
CONVENTION_SHARE = 0.95
#: seuil d'inactivite client, signal de risque et non constat d'attrition
INACTIVITY_DAYS = 90
#: statuts Shopify dont le montant n'est pas un chiffre d'affaires encaisse. "refunded"
#: n'en fait pas partie: Mervio garde la commande dans le CA et deduit le
#: remboursement a part (taux de remboursement), ce qui est coherent.
NON_REVENUE_STATUSES = {"pending", "authorized", "voided", "expired", "partially_paid"}

_NULLS = {"", "-", "--", "n/a", "na", "null", "none", "nan"}
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                 "%Y-%m-%d %H:%M", "%Y-%m-%d")
#: dates a barres: ordre jour/mois etabli par fichier, jamais devine (reimplemente ici, independant du moteur)
_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
_EMAIL_RE = re.compile(r"[^@\s,;\"']+@[^@\s,;\"']+\.[A-Za-z]{2,}")


class PrivacyError(RuntimeError):
    """La sortie contiendrait une valeur brute issue des fichiers."""


class ReferenceError_(ValueError):
    """Valeur illisible pour le calcul de reference."""


# -- lecture independante -------------------------------------------------------
def read_rows(path: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    header = text.split("\n", 1)[0]
    delimiter = max((",", ";", "\t", "|"), key=header.count)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    fieldnames = [(name or "").strip() for name in (reader.fieldnames or [])]
    rows = []
    for raw_row in reader:
        rows.append({(key or "").strip(): (" ".join(value) if isinstance(value, list) else (value or "")).strip()
                     for key, value in raw_row.items() if key is not None})
    return rows, fieldnames


def amount(text: Optional[str]) -> Optional[float]:
    if text is None or text.strip().lower() in _NULLS:
        return None
    value = text.strip()
    negative = value.startswith("-") or (value.startswith("(") and value.endswith(")"))
    value = re.sub(r"[^\d,.]", "", value)
    if "," in value and "." in value:
        cut = max(value.rfind(","), value.rfind("."))
        value = re.sub(r"[,.]", "", value[:cut]) + "." + value[cut + 1:]
    elif "," in value:
        head, _, tail = value.rpartition(",")
        value = f"{head}.{tail}" if len(tail) in (1, 2) and "," not in head else value.replace(",", "")
    try:
        number = float(value)
    except ValueError:
        raise ReferenceError_("montant illisible") from None
    return -number if negative else number


def slash_order(values: Iterable[Optional[str]]) -> Optional[str]:
    """'dmy', 'mdy', 'conflict' ou None, d'apres les seules valeurs dont une composante depasse 12."""
    seen = set()
    for value in values:
        m = _SLASH.match((value or "").strip())
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12 >= b:
                seen.add("dmy")
            elif b > 12 >= a:
                seen.add("mdy")
    return "conflict" if len(seen) > 1 else (seen.pop() if seen else None)


def timestamp(text: Optional[str], order: Optional[str] = None) -> Optional[datetime]:
    if text is None or text.strip().lower() in _NULLS:
        return None
    value = text.strip()
    parsed = None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    m = _SLASH.match(value) if parsed is None else None
    if m and order in ("dmy", "mdy"):
        a, b = int(m.group(1)), int(m.group(2))
        day, month = (a, b) if order == "dmy" else (b, a)
        try:
            parsed = datetime(int(m.group(3)), month, day, int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0))
        except ValueError:
            parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


# -- reference Shopify -------------------------------------------------------------
@dataclass
class RefOrder:
    name: str
    created: datetime
    customer: str
    identified: bool
    currency: str
    subtotal: float
    discount: float
    shipping: float
    taxes: float
    total: Optional[float]
    refunded: float
    status: str
    cancelled: bool
    units: int = 0
    line_revenue: float = 0.0
    revenue_by_sku: Dict[str, float] = field(default_factory=dict)
    lines: Set[tuple] = field(default_factory=set)

    @property
    def net(self) -> float:
        # contrat Shopify (D-041): le Subtotal est deja net des remises de commande
        return self.subtotal


def reference_orders(rows: List[Dict[str, str]]) -> Tuple[Dict[str, RefOrder], Counter, Set[str]]:
    orders: Dict[str, RefOrder] = {}
    stats: Counter = Counter()
    identifiers: Set[str] = set()
    blocks: Counter = Counter()
    previous = None

    def money(row, column):
        try:
            return amount(row.get(column))
        except ReferenceError_:
            stats[f"unparseable_{column.lower().replace(' ', '_')}"] += 1
            return None

    order = slash_order(row.get("Created at") for row in rows)
    for row in rows:
        name = row.get("Name", "")
        if not name:
            stats["rows_without_order_name"] += 1
            continue
        identifiers.add(name)
        for column in ("Email", "Billing Name", "Shipping Name", "Phone", "Billing Phone", "Shipping Phone",
                       "Billing Address1", "Shipping Address1"):
            if row.get(column):
                identifiers.add(row[column])
        if name != previous:
            blocks[name] += 1
            previous = name
        order = orders.get(name)
        if order is None:
            created = timestamp(row.get("Created at"), order)
            if created is None:
                stats["rows_with_invalid_or_missing_date"] += 1
                continue
            email = row.get("Email", "").lower()
            refunded = money(row, "Refunded Amount") or 0.0
            if refunded < 0:
                stats["negative_refunds"] += 1
            for column in ("Subtotal", "Discount Amount", "Shipping", "Taxes"):
                value = money(row, column)
                if value is not None and value < 0:
                    stats[f"negative_{column.lower().replace(' ', '_')}"] += 1
            order = RefOrder(
                name=name, created=created, customer=email or f"guest:{name}", identified=bool(email),
                currency=(row.get("Currency") or "").upper() or "MISSING",
                subtotal=money(row, "Subtotal") or 0.0, discount=money(row, "Discount Amount") or 0.0,
                shipping=money(row, "Shipping") or 0.0, taxes=money(row, "Taxes") or 0.0,
                total=money(row, "Total"), refunded=max(refunded, 0.0),
                status=(row.get("Financial Status") or "missing").lower(),
                cancelled=bool(row.get("Cancelled at")),
            )
            orders[name] = order
        quantity = money(row, "Lineitem quantity")
        price = money(row, "Lineitem price") or 0.0
        quantity = int(round(quantity)) if quantity is not None else 0
        if quantity <= 0:
            stats["line_rows_without_positive_quantity"] += 1
            continue
        sku = row.get("Lineitem sku", "")
        signature = (sku, quantity, round(price, 4), row.get("Lineitem name", ""))
        if signature in order.lines:
            stats["identical_line_rows"] += 1
            continue
        order.lines.add(signature)
        if not sku:
            stats["line_rows_without_sku"] += 1
        order.units += quantity
        order.line_revenue += quantity * price
        order.revenue_by_sku[sku or "UNKNOWN"] = order.revenue_by_sku.get(sku or "UNKNOWN", 0.0) + quantity * price
    stats["orders_split_across_blocks"] = sum(1 for count in blocks.values() if count > 1)
    return orders, stats, identifiers


def in_period(moment: datetime, period: Dict[str, str]) -> bool:
    start = datetime.fromisoformat(period["start"])
    end = datetime.fromisoformat(period["end"]) + timedelta(days=1)
    return start <= moment < end


def first_orders(orders: Dict[str, RefOrder]) -> Dict[str, datetime]:
    first: Dict[str, datetime] = {}
    for order in orders.values():
        if order.customer not in first or order.created < first[order.customer]:
            first[order.customer] = order.created
    return first


def period_reference(orders: Dict[str, RefOrder], period: Dict[str, str]) -> Dict[str, Any]:
    selected = [o for o in orders.values() if in_period(o.created, period)]
    first = first_orders(orders)
    revenue = sum(o.net for o in selected)
    customers = {o.customer for o in selected}
    return {
        "orders": float(len(selected)),
        "gross_subtotal": sum(o.subtotal for o in selected),
        "discounts": sum(o.discount for o in selected),
        "revenue": revenue,
        "units": float(sum(o.units for o in selected)),
        "refunds": sum(o.refunded for o in selected),
        "aov": revenue / len(selected) if selected else None,
        "refund_rate": (sum(o.refunded for o in selected) / revenue) if revenue else None,
        "customers": float(len(customers)),
        "new_customers": float(sum(1 for c in customers if in_period(first[c], period))),
        "line_revenue": sum(o.line_revenue for o in selected),
        "revenue_by_sku": _merge(o.revenue_by_sku for o in selected),
    }


def _merge(dicts: Iterable[Dict[str, float]]) -> Dict[str, float]:
    out: Dict[str, float] = defaultdict(float)
    for item in dicts:
        for key, value in item.items():
            out[key] += value
    return out


# -- sondes semantiques ----------------------------------------------------------
def shopify_semantics(orders: Dict[str, RefOrder], fieldnames: List[str], grain: str) -> Dict[str, Any]:
    close = lambda a, b: abs(a - b) <= TOTAL_MATCH  # noqa: E731
    with_total = [o for o in orders.values() if o.total is not None]
    discounted = [o for o in with_total if o.discount > 0]
    pre = sum(1 for o in discounted if close(o.total, o.subtotal - o.discount + o.shipping + o.taxes)
              or close(o.total, o.subtotal - o.discount + o.shipping))
    post = sum(1 for o in discounted if close(o.total, o.subtotal + o.shipping + o.taxes)
               or close(o.total, o.subtotal + o.shipping))
    if len(discounted) < MIN_DISCOUNTED_ORDERS:
        convention = "undetermined_insufficient_discounted_orders"
    elif pre / len(discounted) >= CONVENTION_SHARE and post / len(discounted) < CONVENTION_SHARE:
        convention = "pre_discount"
    elif post / len(discounted) >= CONVENTION_SHARE and pre / len(discounted) < CONVENTION_SHARE:
        convention = "post_discount"
    else:
        convention = "undetermined_mixed"
    undiscounted = [o for o in with_total if o.discount == 0]
    consistent = sum(1 for o in undiscounted if close(o.total, o.subtotal + o.shipping + o.taxes)
                     or close(o.total, o.subtotal + o.shipping))

    total_net = sum(o.net for o in orders.values()) or 0.0
    by_status = Counter(o.status for o in orders.values())
    status_revenue = _merge({o.status: o.net} for o in orders.values())
    non_revenue = [o for o in orders.values() if o.status in NON_REVENUE_STATUSES or o.cancelled]
    days = sorted(o.created.date() for o in orders.values())
    customers = defaultdict(list)
    for order in orders.values():
        customers[order.customer].append(order)
    latest = max((o.created for o in orders.values()), default=None)
    revenue_by_customer = sorted((sum(o.net for o in items) for items in customers.values()), reverse=True)
    return {
        "subtotal_convention": {
            "conclusion": convention,
            "mervio_assumes": "post_discount (revenue = Subtotal, already net of discounts, D-041)",
            "orders_with_total": len(with_total),
            "discounted_orders": len(discounted),
            "discounted_matching_pre_discount": pre,
            "discounted_matching_post_discount": post,
            "undiscounted_orders_arithmetically_consistent": consistent,
            "undiscounted_orders": len(undiscounted),
            "discount_total_full_file": round(sum(o.discount for o in orders.values()), 2),
        },
        "financial_status_counts": dict(sorted(by_status.items())),
        "financial_status_revenue_share": {k: _share(v, total_net) for k, v in sorted(status_revenue.items())},
        "cancelled_column_present": "Cancelled at" in fieldnames,
        "cancelled_orders": sum(1 for o in orders.values() if o.cancelled),
        "non_revenue_or_cancelled_orders": len(non_revenue),
        "non_revenue_or_cancelled_revenue_share": _share(sum(o.net for o in non_revenue), total_net),
        "currencies": dict(sorted(Counter(o.currency for o in orders.values()).items())),
        "refund_date_semantics": "Shopify orders export has no refund date: refunds are dated at order creation",
        "orders_with_refund_above_net_revenue": sum(1 for o in orders.values() if o.refunded > o.net > 0),
        "period": _period_profile(days, grain),
        "customers": {
            "customers": len(customers),
            "identified_order_share": _share(sum(1 for o in orders.values() if o.identified), len(orders)),
            "repeat_customer_share": _share(sum(1 for items in customers.values() if len(items) > 1), len(customers)),
            "top5_revenue_share": _share(sum(revenue_by_customer[:5]), total_net),
            "inactive_customers_signal": sum(
                1 for items in customers.values()
                if latest and latest - max(o.created for o in items) > timedelta(days=INACTIVITY_DAYS)),
            "inactivity_days": INACTIVITY_DAYS,
            "note": "elevated churn-risk signal only: no churn outcome exists in the export",
        },
    }


def _period_profile(days: List[date], grain: str) -> Dict[str, Any]:
    if not days:
        return {"earliest": None, "latest": None}
    earliest, latest = days[0], days[-1]
    if grain == "month":
        buckets = {(d.year, d.month) for d in days}
        span = (latest.year - earliest.year) * 12 + latest.month - earliest.month + 1
        first_partial, last_partial = earliest.day != 1, (latest + timedelta(days=1)).day != 1
    else:
        buckets = {d - timedelta(days=d.weekday()) for d in days}
        span = ((latest - timedelta(days=latest.weekday())) - (earliest - timedelta(days=earliest.weekday()))).days // 7 + 1
        first_partial, last_partial = earliest.weekday() != 0, latest.weekday() != 6
    return {"earliest": earliest.isoformat(), "latest": latest.isoformat(), "grain": grain,
            "periods_spanned": span, "periods_without_orders": span - len(buckets),
            "first_period_partial": first_partial, "last_period_partial": last_partial,
            "ordered_ascending_in_file": None}


def _share(part: float, whole: float) -> Optional[float]:
    return round(part / whole, 4) if whole else None


def stripe_reference(path: Path, period: Optional[Dict[str, str]]) -> Dict[str, Any]:
    rows, fieldnames = read_rows(path)
    date_column = next((c for c in ("Created (UTC)", "Created", "created") if c in fieldnames), None)
    unsupported_date_columns = [c for c in fieldnames if "created" in c.lower() and c != date_column]
    seen, fees, refunds, parsed = set(), [], [], 0
    order = slash_order(row.get(date_column) for row in rows) if date_column else None
    for row in rows:
        pid = row.get("id", "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        created = timestamp(row.get(date_column), order) if date_column else None
        if created is None:
            continue
        parsed += 1
        try:
            fee, refunded = amount(row.get("Fee")), amount(row.get("Amount Refunded"))
        except ReferenceError_:
            continue
        if period is None or in_period(created, period):
            if fee is not None and fee >= 0:
                fees.append(fee)
            if refunded and refunded > 0:
                refunds.append(refunded)
    return {
        "date_column_used_by_mervio": date_column,
        "other_created_columns": unsupported_date_columns,
        "rows": len(rows), "unique_ids_with_valid_date": parsed,
        "fees": sum(fees) if fees else None, "refunds": sum(refunds),
    }


def google_ads_reference(path: Path, period: Optional[Dict[str, str]]) -> Dict[str, Any]:
    rows, fieldnames = read_rows(path)
    raw_lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[:6]
    header_line = next((i for i, line in enumerate(raw_lines) if "Day" in line and "Campaign" in line), None)
    seen, spend, clicks, total_rows = set(), 0.0, 0, 0
    order = slash_order(row.get("Day") for row in rows)
    for row in rows:
        day = timestamp(row.get("Day"), order)
        name = row.get("Campaign", "")
        if day is None or not name:
            if (row.get("Campaign") or row.get("Day") or "").lower().startswith("total"):
                total_rows += 1
            continue
        key = (row.get("Campaign ID") or name, day.date())
        if key in seen:
            continue
        seen.add(key)
        try:
            cost, click = amount(row.get("Cost")) or 0.0, amount(row.get("Clicks")) or 0.0
        except ReferenceError_:
            continue
        if cost < 0:
            continue
        if period is None or in_period(day, period):
            spend += cost
            clicks += int(round(click))
    return {"header_line_index": header_line, "total_rows": total_rows, "spend": spend, "clicks": float(clicks),
            "has_rows": bool(seen)}


def products_reference(path: Path) -> Dict[str, float]:
    rows, _ = read_rows(path)
    costs = {}
    for row in rows:
        sku = row.get("SKU", "")
        raw = next((row[c] for c in ("Cost per item", "Cost", "COGS") if row.get(c)), None)
        try:
            cost = amount(raw)
        except ReferenceError_:
            cost = None
        if sku and cost is not None and cost >= 0:
            costs[sku] = cost
    return costs


# -- comparaisons -------------------------------------------------------------------
def compare(metric: str, mervio: Optional[float], reference: Optional[float]) -> Dict[str, Any]:
    if mervio is None or reference is None:
        status = "PASS" if mervio is None and reference is None else "FAIL"
        return {"metric": metric, "mervio": mervio, "reference": reference, "abs_diff": None,
                "pct_diff": None, "status": status}
    diff = mervio - reference
    ok = abs(diff) <= TOLERANCE_ABS + TOLERANCE_REL * abs(reference)
    return {"metric": metric, "mervio": round(mervio, 4), "reference": round(reference, 4),
            "abs_diff": round(abs(diff), 6), "pct_diff": round(diff / abs(reference), 8) if reference else None,
            "status": "PASS" if ok else "FAIL"}


def report_checks(report: Dict[str, Any], rerun: Dict[str, Any]) -> Dict[str, str]:
    health = report["business_health_score"]
    available = [d for d in health["dimensions"] if d["available"] and d["score"] is not None]
    recomputed = (round(sum(d["score"] * d["weight"] for d in available) / sum(d["weight"] for d in available))
                  if available else None)
    stripped = [copy.deepcopy(r) for r in (report, rerun)]
    for item in stripped:
        item["_meta"].pop("generated_at", None)
    insight_types = {i["type"] for s in ("critical_issues", "warnings", "opportunities") for i in report[s]}
    profit = report["profitability"]
    return {
        "health_score_recomputed_from_dimensions": "PASS" if recomputed == health["score"] else "FAIL",
        "analysis_deterministic_on_rerun": "PASS" if stripped[0] == stripped[1] else "FAIL",
        "profit_refused_when_costs_missing": (
            "PASS" if profit["data_available"] or profit["contribution_profit"] is None else "FAIL"),
        "recommendations_linked_to_findings": (
            "PASS" if all(r["source_insight"] in insight_types for r in report["recommendations"]) else "FAIL"),
        "root_cause_limits_state_no_causality": (
            "PASS" if not report["root_causes"] or any("causalite" in l for l in report["root_causes"][0]["limitations"])
            or not report["root_causes"][0]["available"] else "FAIL"),
    }


def classify(result: Dict[str, Any]) -> Tuple[str, List[str]]:
    orders = result["files"].get("shopify_orders", {})
    inspection, validation = orders.get("inspection", {}), orders.get("validation", {})
    if not inspection.get("readable"):
        return "INVALID", [f"unreadable: {inspection.get('error')}"]
    if validation.get("detected_source") != "shopify_orders" and validation.get("forced_status") == "invalid" \
            and "manquantes" in (validation.get("forced_error") or ""):
        return "UNSUPPORTED", ["no Shopify orders signature: " + (validation.get("forced_error") or "")]
    if validation.get("forced_status") == "invalid":
        return "INVALID", [validation.get("forced_error") or "no accepted row"]
    semantics = result.get("semantics", {})
    if len(semantics.get("currencies", {})) > 1:
        return "INVALID", ["multiple currencies in one analysis (D-020): totals are not meaningful"]
    if result.get("analysis", {}).get("status") != "completed":
        return "INVALID", [result.get("analysis", {}).get("error") or "analysis rejected"]
    reasons = list(result["blockers"])
    if not result["report_summary"]["profitability_available"]:
        reasons.append("profitability_unavailable (missing cost components)")
    return ("SUPPORTED", []) if not reasons else ("PARTIALLY_SUPPORTED", reasons)


def blockers(result: Dict[str, Any]) -> List[str]:
    found = []
    convention = result.get("semantics", {}).get("subtotal_convention", {}).get("conclusion", "")
    if convention == "pre_discount":
        found.append("subtotal_before_discount: file contradicts the Shopify contract, revenue overstated by discounts (D-041)")
    elif convention.startswith("undetermined"):
        found.append(f"subtotal_convention_unverified ({convention})")
    if result.get("semantics", {}).get("non_revenue_or_cancelled_orders"):
        found.append("non_revenue_or_cancelled_orders_counted_as_revenue")
    if any(c["status"] == "FAIL" for c in result.get("reconciliation", [])):
        found.append("reconciliation_mismatch")
    if any(v == "FAIL" for v in result.get("report_checks", {}).values()):
        found.append("report_check_failed")
    stripe = result.get("stripe_reference")
    if stripe and stripe["date_column_used_by_mervio"] is None:
        found.append("stripe_date_column_not_recognised: " + ", ".join(stripe["other_created_columns"] or ["none"]))
    ads = result.get("google_ads_reference")
    if ads and ads["header_line_index"] not in (0, None):
        found.append("google_ads_header_not_on_first_line")
    llm = result.get("llm", {})
    if llm and (llm.get("raw_identifiers_in_context") or llm.get("sensitive_kinds_in_context")):
        found.append("pii_in_llm_context")
    return found


# -- orchestration --------------------------------------------------------------------
def run(sources: Dict[str, Optional[str]], today: Optional[date] = None, grain: str = "week") -> Dict[str, Any]:
    timings: Dict[str, float] = {}
    result: Dict[str, Any] = {"mervio_validation_script": "1.0", "files": {}, "timings_seconds": timings,
                              "tolerance": {"absolute": TOLERANCE_ABS, "relative": TOLERANCE_REL}}
    identifiers: Set[str] = set()
    for source, path in sources.items():
        if not path:
            continue
        entry = result["files"][source] = {"file": Path(path).name}
        started = time.perf_counter()
        inspection = inspect_file(path).to_dict()
        timings[f"inspection.{source}"] = time.perf_counter() - started
        entry["inspection"] = {k: inspection[k] for k in ("readable", "encoding", "delimiter", "size_bytes", "rows",
                                                          "columns_count", "duplicate_rows", "period", "currencies",
                                                          "personal_data_columns", "granularity", "error")}
        entry["inspection"]["columns"] = [{k: c[k] for k in ("column", "inferred_type", "missing_pct",
                                                              "distinct", "looks_personal")}
                                          for c in inspection["columns"]]
        entry["inspection"]["sensitive_findings"] = inspection["sensitive_findings"]
        started = time.perf_counter()
        detected, forced = validate_file(path), validate_file(path, source)
        timings[f"validation.{source}"] = time.perf_counter() - started
        entry["validation"] = {
            "detected_source": detected.source, "detected_status": detected.status,
            "forced_status": forced.status, "forced_error": forced.error, "rows": forced.rows,
            "accepted_rows": forced.accepted_rows, "rejected_rows": forced.rejected_rows,
            "issue_kinds": dict(sorted(Counter(i["kind"] for i in forced.issues).items())),
            "period": {"start": forced.period_start, "end": forced.period_end}, "currency": forced.currency,
        }

    orders_path = sources.get("shopify_orders")
    readable = result["files"].get("shopify_orders", {}).get("validation", {}).get("forced_status") not in (None, "invalid")
    ref_orders: Dict[str, RefOrder] = {}
    if orders_path and readable:
        rows, fieldnames = read_rows(Path(orders_path))
        ref_orders, stats, identifiers = reference_orders(rows)
        result["reference_parse_stats"] = dict(sorted(stats.items()))
        result["semantics"] = shopify_semantics(ref_orders, fieldnames, grain)

    # sondes de format independantes de l'analyse: elles doivent etre rapportees
    # meme quand un fichier optionnel fait rejeter l'analyse entiere
    if sources.get("stripe"):
        stripe = stripe_reference(Path(sources["stripe"]), None)
        result["stripe_reference"] = {k: stripe[k] for k in ("date_column_used_by_mervio", "other_created_columns",
                                                             "rows", "unique_ids_with_valid_date")}
    if sources.get("google_ads"):
        ads = google_ads_reference(Path(sources["google_ads"]), None)
        result["google_ads_reference"] = {k: ads[k] for k in ("header_line_index", "total_rows", "has_rows")}

    result["analysis"] = {"status": "not_run"}
    result["blockers"] = []
    if readable:
        config = AnalyticsConfig(grain=grain)
        paths = SourcePaths(**{k: v for k, v in sources.items()})
        started = time.perf_counter()
        try:
            load_dataset(paths)
        except MervioError as exc:
            # un fichier OPTIONNEL invalide fait rejeter toute l'analyse: constat, pas correction
            result["normalization_error"] = f"{type(exc).__name__}: {getattr(exc, 'source', '')}"
        timings["normalization"] = time.perf_counter() - started
        request = AnalysisRequest(**{k: v for k, v in sources.items()}, config=config, today=today)
        started = time.perf_counter()
        analysis = analyze_dataset(request)
        timings["validation_normalization_analytics"] = time.perf_counter() - started
        result["analysis"] = {"status": analysis.status, "error": analysis.error}
        if analysis.succeeded:
            report = analysis.report
            started = time.perf_counter()
            json.dumps(report, ensure_ascii=False)
            render_executive_report(report)
            timings["reporting"] = time.perf_counter() - started
            rerun = analyze_dataset(request).report
            _analyse_report(result, report, rerun, ref_orders, sources, identifiers, timings)

    result["blockers"] = blockers(result)
    result["classification"], result["classification_reasons"] = classify(result)
    result["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    result["timings_seconds"] = {k: round(v, 3) for k, v in timings.items()}
    assert_private(result, identifiers)
    return result


def _analyse_report(result, report, rerun, ref_orders, sources, identifiers, timings) -> None:
    kpis = report["kpis"]
    period = report["period"]["current"]
    previous = report["period"]["previous"]
    value = lambda key: kpis[key]["value"] if key in kpis else None  # noqa: E731
    ref = period_reference(ref_orders, period)
    ref_previous = period_reference(ref_orders, previous)
    reconciliation = [
        compare("revenue", value("revenue"), ref["revenue"]),
        compare("orders", value("orders"), ref["orders"]),
        compare("units", value("units"), ref["units"]),
        compare("aov", value("aov"), ref["aov"]),
        compare("refunds", value("refunds"), ref["refunds"]),
        compare("refund_rate", value("refund_rate"), ref["refund_rate"]),
        compare("active_customers", float(report["customers"]["total_active"]), ref["customers"]),
        compare("new_customers", value("new_customers"), ref["new_customers"]),
        compare("product_revenue", sum(p["revenue"] for p in report["products"]), ref["line_revenue"]),
    ]
    wow = report["comparisons"].get("revenue", {}).get("wow") or report["comparisons"].get("revenue", {}).get("mom")
    if wow and wow["available"]:
        ref_change = ((ref["revenue"] - ref_previous["revenue"]) / abs(ref_previous["revenue"])
                      if ref_previous["revenue"] else None)
        reconciliation.append(compare("revenue_change_vs_previous_period", wow["pct_change"], ref_change))

    if sources.get("shopify_products"):
        costs = products_reference(Path(sources["shopify_products"]))
        covered = sum(v for sku, v in ref["revenue_by_sku"].items() if sku in costs)
        coverage = covered / ref["line_revenue"] if ref["line_revenue"] else None
        notes = " ".join(kpis.get("gross_margin", {}).get("notes", []))
        match = re.search(r"sur (\d+)% du CA article", notes)
        reconciliation.append(compare("cogs_coverage_pct_rounded", float(match.group(1)) if match else None,
                                      float(round(coverage * 100)) if coverage is not None and coverage > 0 else None))
    if sources.get("stripe"):
        stripe = stripe_reference(Path(sources["stripe"]), period)
        reconciliation.append(compare("payment_fees", value("payment_fees"), stripe["fees"]))
        shopify_total = sum(o.refunded for o in ref_orders.values())
        stripe_total = stripe_reference(Path(sources["stripe"]), None)["refunds"]
        gap = abs(shopify_total - stripe_total) / max(shopify_total, stripe_total) if shopify_total and stripe_total else None
        flagged = any(i["kind"] == "refund_mismatch" for i in report["data_quality"]["issues"])
        result["refund_reconciliation"] = {
            "gap_share": round(gap, 4) if gap is not None else None,
            "configured_tolerance": AnalyticsConfig().refund_reconciliation_tolerance,
            "flagged_by_mervio": flagged,
            "status": "PASS" if gap is None or (gap > AnalyticsConfig().refund_reconciliation_tolerance) == flagged else "FAIL",
            "source_of_truth": "shopify (D-006)",
        }
        if result["refund_reconciliation"]["status"] == "FAIL":
            reconciliation.append({"metric": "refund_mismatch_flag", "status": "FAIL"})
    if sources.get("google_ads"):
        ads = google_ads_reference(Path(sources["google_ads"]), period)
        reconciliation.append(compare("ad_spend", value("ad_spend"), ads["spend"] if ads["has_rows"] else 0.0))
        reconciliation.append(compare("paid_clicks", value("clicks"), ads["clicks"] if ads["has_rows"] else 0.0))
        reconciliation.append(compare("roas", value("roas"), ref["revenue"] / ads["spend"] if ads["spend"] else None))
        cac = ads["spend"] / ref["new_customers"] if ads["has_rows"] and ref["new_customers"] else None
        reconciliation.append(compare("cac", value("cac"), cac))
    else:
        reconciliation.append(compare("roas", value("roas"), None))
        reconciliation.append(compare("cac", value("cac"), None))
    result["reconciliation"] = reconciliation
    result["report_checks"] = report_checks(report, rerun)

    health = report["business_health_score"]
    result["report_summary"] = {
        "period": period, "previous_period": previous,
        "health_score": health["score"], "excluded_dimensions": health["excluded_dimensions"],
        "profitability_available": report["profitability"]["data_available"],
        "missing_cost_components": report["profitability"]["missing_components"],
        "unavailable_kpis": sorted(k for k, m in kpis.items() if m["value"] is None),
        "anomalies": [{"metric": a["metric"], "direction": a["direction"], "severity": a["severity"],
                       "delta_pct": a["delta_pct"]} for a in report["anomalies"]],
        "root_cause_primary_factor": report["root_causes"][0]["primary_factor"] if report["root_causes"] else None,
        "finding_types": [i["type"] for s in ("critical_issues", "warnings", "opportunities") for i in report[s]],
        "data_quality_issue_kinds": dict(sorted(Counter(i["kind"] for i in report["data_quality"]["issues"]).items())),
        "limitations_count": len(report["limitations"]),
    }

    started = time.perf_counter()
    context = build_llm_context(report)
    serialized = serialize_context(context)
    explanation = explain_report(report, MockLLMProvider(), LLMConfig(model="mock"), sleep=lambda _s: None)
    timings["llm_context_and_mock"] = time.perf_counter() - started
    leaked = sum(1 for value_ in identifiers if _is_identifying(value_) and value_ in serialized)
    result["llm"] = {
        "provider": "mock (local, no network)",
        "context_bytes": len(serialized.encode("utf-8")),
        "context_limit_bytes": ContextLimits().max_context_bytes,
        "raw_identifiers_in_context": leaked,
        "emails_in_context": len(_EMAIL_RE.findall(serialized)),
        "sensitive_kinds_in_context": sorted(set(sensitive_findings(serialized))),
        "unavailable_metrics": [u["metric"] for u in context["unavailable_metrics"]],
        "flagged_untrusted_fields": context["untrusted_content"]["flagged_count"],
        "mock_explanation_status": explanation.status,
        "mock_explanation_error": explanation.error_code,
        "context_deterministic": "PASS" if serialize_context(build_llm_context(rerun)) == serialized else "FAIL",
    }


def _is_identifying(value: str) -> bool:
    """Evite les faux positifs: un numero de commande '12' apparait partout."""
    return len(value) >= 5 and not value.replace(".", "").isdigit()


def assert_private(result: Dict[str, Any], identifiers: Set[str]) -> None:
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    if _EMAIL_RE.search(serialized):
        raise PrivacyError("la sortie contiendrait un email: emission refusee")
    if any(_is_identifying(value) and value in serialized for value in identifiers):
        raise PrivacyError("la sortie contiendrait un identifiant lu dans les fichiers: emission refusee")


def write_output(result: Dict[str, Any], out: Path) -> None:
    resolved = out.resolve()
    allowed = (ROOT / "analysis").resolve()
    if ROOT.resolve() in resolved.parents and allowed not in resolved.parents:
        raise PrivacyError("dans le depot, la sortie n'est autorisee que sous analysis/ (ignore par Git)")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def summary_lines(result: Dict[str, Any]) -> List[str]:
    lines = [f"CLASSIFICATION : {result['classification']}"]
    lines += [f"  - {reason}" for reason in result["classification_reasons"]]
    lines.append(f"ANALYSE        : {result['analysis']['status']}")
    for check in result.get("reconciliation", []):
        lines.append(f"  [{check['status']}] {check['metric']}: mervio={check.get('mervio')} "
                     f"reference={check.get('reference')} ecart={check.get('abs_diff')}")
    for name, status in result.get("report_checks", {}).items():
        lines.append(f"  [{status}] {name}")
    if "semantics" in result:
        convention = result["semantics"]["subtotal_convention"]
        lines.append(f"SUBTOTAL       : {convention['conclusion']} "
                     f"({convention['discounted_matching_pre_discount']} avant / "
                     f"{convention['discounted_matching_post_discount']} apres sur "
                     f"{convention['discounted_orders']} commandes remisees)")
    lines.append("BLOQUANTS      : " + (", ".join(result["blockers"]) or "aucun"))
    lines.append("DUREES (s)     : " + ", ".join(f"{k}={v}" for k, v in result["timings_seconds"].items()))
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--shopify-orders", required=True)
    parser.add_argument("--products")
    parser.add_argument("--stripe")
    parser.add_argument("--google-ads")
    parser.add_argument("--today", help="date de reference AAAA-MM-JJ")
    parser.add_argument("--grain", choices=("week", "month"), default="week")
    parser.add_argument("--out", help="fichier JSON de sortie (hors depot, ou sous analysis/)")
    args = parser.parse_args(argv)
    sources = {"shopify_orders": args.shopify_orders, "shopify_products": args.products,
               "stripe": args.stripe, "google_ads": args.google_ads}
    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else None
    result = run(sources, today=today, grain=args.grain)
    if args.out:
        write_output(result, Path(args.out))
    print("\n".join(summary_lines(result)))
    return 0 if result["classification"] in ("SUPPORTED", "PARTIALLY_SUPPORTED") else 1


if __name__ == "__main__":
    sys.exit(main())
