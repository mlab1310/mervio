"""Confrontation d'un rapport du moteur a la verite terrain d'un scenario.

L'evaluateur lit uniquement le rapport JSON produit par le moteur (sa sortie
publique) et le manifeste du jeu genere. Il ne recalcule aucun KPI.

Resultat par attente:
- `MATCH`: la valeur observee correspond a l'attente;
- `MISMATCH`: elle ne correspond pas.
Une attente declaree `KNOWN_GAP` est satisfaite quand le moteur NE retrouve PAS
la verite terrain; une attente `MATCH` quand il la retrouve.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import AnomalyConfig
from .scenarios import ScenarioSpec

#: bande morte des directions semaine sur semaine: le plancher de materialite des anomalies du
#: moteur (D-007). En dessous, une variation n'a pas de portee business pour Mervio.
FLAT_BAND = AnomalyConfig().min_material_pct

#: facteur du moteur -> categorie de recommandation (insights._recommendation_for)
FACTOR_CATEGORY = {"paid_traffic": "acquisition", "conversion_rate": "conversion_checkout",
                   "average_order_value": "pricing_merchandising"}


def engine_paths(directory: str | Path):
    from ..analytics.pipeline import SourcePaths
    root = Path(directory)
    return SourcePaths(shopify_orders=str(root / "shopify_orders.csv"),
                       shopify_products=str(root / "shopify_products.csv"),
                       stripe=str(root / "stripe_transactions.csv"),
                       google_ads=str(root / "google_ads.csv"))


def analysis_day(manifest: dict) -> date:
    """Lendemain du dernier jour genere: la derniere semaine generee est la periode analysee."""
    return date.fromisoformat(manifest["config"]["end_date"]) + timedelta(days=1)


def _direction(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    if pct <= -FLAT_BAND:
        return "down"
    if pct >= FLAT_BAND:
        return "up"
    return "flat"


def _wow(report: dict, metric: str) -> Optional[float]:
    wow = report.get("comparisons", {}).get(metric, {}).get("wow", {})
    return wow.get("pct_change") if wow.get("available") else None


def observe(report: dict, check: str, expected: Any, manifest: dict) -> Any:
    """Valeur observee par le moteur pour une attente donnee."""
    root = (report.get("root_causes") or [{}])[0]
    kpis = report.get("kpis", {})
    if check in ("revenue_wow", "orders_wow", "aov_wow"):
        return _direction(_wow(report, check[:-4]))
    if check == "anomaly":
        return expected if expected in {f"{a['metric']}:{a['direction']}" for a in report.get("anomalies", [])} else None
    if check == "no_anomaly":
        return expected if expected not in {a["metric"] for a in report.get("anomalies", [])} else None
    if check == "no_critical_issue":
        return not report.get("critical_issues")
    if check == "primary_factor":
        return root.get("primary_factor") if root.get("available") else None
    if check == "secondary_factor":
        factors = [f for f in root.get("factors", []) if f.get("contribution") is not None]
        return factors[1]["factor"] if len(factors) > 1 else None
    if check == "recommendation_category":
        if not any(i.get("type") == "revenue_decline" for i in report.get("critical_issues", [])):
            return None
        return FACTOR_CATEGORY.get(root.get("primary_factor"))
    if check == "top_product_contributor":
        rows = root.get("product_contributors") or []
        return rows[0]["sku"] if rows else None
    if check == "top_campaign_decline":
        rows = sorted(root.get("campaign_contributors") or [], key=lambda r: r.get("conversions_delta") or 0)
        return rows[0]["name"] if rows else None
    if check in ("kpi_below", "kpi_above"):
        metric, _ = expected
        return (kpis.get(metric) or {}).get("value")
    if check == "new_customer_share_above":
        orders = (kpis.get("orders") or {}).get("value")
        new = (kpis.get("new_customers") or {}).get("value")
        return (new / orders) if orders and new is not None else None
    if check == "insight_present":
        types = {i.get("type") for key in ("critical_issues", "warnings", "opportunities") for i in report.get(key, [])}
        return expected if expected in types else None
    # verites terrain hors du vocabulaire du moteur (angles morts): jamais observables aujourd'hui
    return None


def _matches(check: str, expected: Any, observed: Any, manifest: dict) -> bool:
    if check == "top_product_contributor":
        return observed in {manifest["resolved_products_by_rank"][rank] for rank in expected}
    if check == "top_campaign_decline":
        return observed is not None and expected in observed
    if check == "kpi_below":
        return observed is not None and observed < expected[1]
    if check == "kpi_above":
        return observed is not None and observed > expected[1]
    if check == "new_customer_share_above":
        return observed is not None and observed > expected
    return observed == expected


def evaluate(report: dict, scenario: ScenarioSpec, manifest: dict) -> dict:
    results: List[Dict[str, Any]] = []
    for expectation in scenario.expectations:
        observed = observe(report, expectation.check, expectation.expected, manifest)
        matched = _matches(expectation.check, expectation.expected, observed, manifest)
        outcome = "MATCH" if matched else "MISMATCH"
        satisfied = matched if expectation.status == "MATCH" else not matched
        results.append({"check": expectation.check, "expected": expectation.expected, "observed": observed,
                        "declared": expectation.status, "outcome": outcome, "satisfied": satisfied,
                        "note": expectation.note})
    return {"scenario": scenario.name, "ground_truth": scenario.ground_truth,
            "satisfied": all(r["satisfied"] for r in results), "results": results}


def run_scenario(config, directory: str | Path) -> dict:
    """Genere le scenario, lance le vrai moteur et evalue. Utilise par les tests et les benchmarks."""
    from ..analytics.pipeline import run_analysis
    from .generator import generate_dataset
    from .scenarios import get_scenario
    result = generate_dataset(config, directory)
    report = run_analysis(engine_paths(directory), today=analysis_day(result.manifest))
    return {"manifest": result.manifest, "report": report,
            "evaluation": evaluate(report, get_scenario(config.scenario), result.manifest)}
