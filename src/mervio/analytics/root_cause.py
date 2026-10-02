"""Root Cause Engine v1 (deterministe).

Methode: decomposition multiplicative du CA.

    Revenue = Clics payants x Taux de conversion x Panier moyen

La contribution de chaque facteur a la variation du CA est mesuree en
log-ratio, ce qui donne des contributions additives dont la somme vaut 1:

    contribution(f) = ln(f1/f0) / ln(R1/R0)

Les contributions produit et campagne sont additives (delta_i / delta_total).

Le moteur produit des HYPOTHESES, jamais des causalites prouvees: une
correlation d'agregat ne demontre pas un mecanisme.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..domain.models import Dataset
from .kpi import campaign_performance, product_performance, total_clicks, total_revenue
from .periods import Period


@dataclass
class CauseFactor:
    factor: str
    observed_fact: str
    supporting_metric: str
    current_value: Optional[float]
    previous_value: Optional[float]
    factor_change_pct: Optional[float]
    contribution: Optional[float]  # part de la variation expliquee (0..1)
    confidence: float
    hypothesis: str

    def to_dict(self) -> dict:
        return {
            "factor": self.factor, "observed_fact": self.observed_fact,
            "supporting_metric": self.supporting_metric,
            "current_value": round(self.current_value, 4) if self.current_value is not None else None,
            "previous_value": round(self.previous_value, 4) if self.previous_value is not None else None,
            "factor_change_pct": round(self.factor_change_pct, 4) if self.factor_change_pct is not None else None,
            "contribution": round(self.contribution, 4) if self.contribution is not None else None,
            "confidence": round(self.confidence, 2), "hypothesis": self.hypothesis,
        }


@dataclass
class RootCauseAnalysis:
    target_metric: str
    period: str
    previous_period: str
    observed_change_pct: Optional[float]
    observed_change_absolute: Optional[float]
    method: str
    #: labels des periodes de baseline, identiques a ceux de l'anomalie (AN-01)
    baseline_periods: List[str] = field(default_factory=list)
    #: niveau de reference: moyenne du CA sur la baseline. Egale EXACTEMENT
    #: `anomaly.expected_value` -- c'est l'objet meme d'AN-01.
    baseline_revenue: Optional[float] = None
    #: comparaison a la periode precedente, CONSERVEE mais etiquetee a part et SANS
    #: contribution: une contribution rattachee a un autre referentiel serait un melange.
    change_vs_previous_period: Dict[str, Optional[float]] = field(default_factory=dict)
    factors: List[CauseFactor] = field(default_factory=list)
    product_contributors: List[dict] = field(default_factory=list)
    campaign_contributors: List[dict] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    available: bool = True

    def primary_factor(self) -> Optional[CauseFactor]:
        ranked = [f for f in self.factors if f.contribution is not None]
        return max(ranked, key=lambda f: abs(f.contribution)) if ranked else None

    def to_dict(self) -> dict:
        primary = self.primary_factor()
        return {
            "target_metric": self.target_metric, "period": self.period,
            "previous_period": self.previous_period,
            "observed_change_pct": round(self.observed_change_pct, 4) if self.observed_change_pct is not None else None,
            "observed_change_absolute": round(self.observed_change_absolute, 2) if self.observed_change_absolute is not None else None,
            "method": self.method, "available": self.available,
            "baseline_periods": list(self.baseline_periods),
            "baseline_revenue": round(self.baseline_revenue, 2) if self.baseline_revenue is not None else None,
            "change_vs_previous_period": dict(self.change_vs_previous_period),
            "primary_factor": primary.factor if primary else None,
            "factors": [f.to_dict() for f in self.factors],
            "product_contributors": self.product_contributors,
            "campaign_contributors": self.campaign_contributors,
            "limitations": self.limitations,
        }


def _pct(current: float, previous: float) -> Optional[float]:
    return ((current - previous) / abs(previous)) if previous else None


def _confidence(contribution: Optional[float], quality_penalty: float) -> float:
    """Heuristique documentee, PAS une probabilite statistique.

    Base 0.35, +0.5 proportionnel a la part de variation expliquee, moins une
    penalite de qualite de donnee. Bornee a [0.1, 0.9].
    """
    if contribution is None:
        return 0.1
    raw = 0.35 + 0.5 * min(abs(contribution), 1.0) - quality_penalty
    return max(0.1, min(0.9, raw))


def analyse_revenue_change(
    full: Dataset, current: Period, previous: Period,
    baseline: Optional[Sequence[Period]] = None,
) -> RootCauseAnalysis:
    """Decompose la variation du CA contre la MEME baseline que l'anomalie (AN-01).

    `baseline`: periodes de reference, telles que declarees par le detecteur
    (`anomaly.baseline_points`). Absent, la baseline vaut `[previous]`: cas degenere n = 1, qui
    reproduit EXACTEMENT le comportement anterieur a AN-01 (somme(R)/1 = R0, somme(O)/somme(C)
    = cvr0, somme(R)/somme(O) = aov0).

    Les agregats construisent les RATIOS; le niveau de reference est leur MOYENNE par periode.
    Prendre somme(CA) sans diviser par n fausserait ln(R1/R0) d'un facteur ln n.
    """
    windows = [full.window(p.start, p.end) for p in (baseline or [previous])]
    cur_ds = full.window(current.start, current.end)
    prev_ds = full.window(previous.start, previous.end)

    n = len(windows)
    sum_revenue = sum(total_revenue(w) for w in windows)
    sum_clicks = sum(total_clicks(w) for w in windows)
    sum_orders = sum(len(w.orders) for w in windows)

    r1 = total_revenue(cur_ds)
    r0 = sum_revenue / n if n else 0.0
    prev_revenue = total_revenue(prev_ds)
    analysis = RootCauseAnalysis(
        target_metric="revenue", period=current.label, previous_period=previous.label,
        observed_change_pct=_pct(r1, r0), observed_change_absolute=r1 - r0,
        method="decomposition multiplicative Revenue = Clics x Taux de conversion x Panier moyen "
               "contre la MEME baseline que l'anomalie (agregats sur la fenetre, puis ratios "
               "derives); contributions en log-ratio; contributions produit/campagne additives",
        baseline_periods=[p.label for p in (baseline or [previous])],
        baseline_revenue=r0 if n else None,
        change_vs_previous_period={
            "previous_period": previous.label,
            "current_revenue": round(r1, 2),
            "previous_revenue": round(prev_revenue, 2),
            "delta": round(r1 - prev_revenue, 2),
            "pct": round(_pct(r1, prev_revenue), 4) if _pct(r1, prev_revenue) is not None else None,
        },
    )
    if r0 <= 0 or r1 <= 0:
        analysis.available = False
        analysis.limitations.append(
            "Decomposition impossible: le CA est nul sur au moins une des deux periodes."
        )
        return analysis

    c1 = total_clicks(cur_ds)
    o1 = len(cur_ds.orders)
    # RATIOS DE SOMMES, pas moyennes de ratios: seule forme qui preserve l'identite
    # C x cvr x aov = somme(R)/n = r0
    c0 = sum_clicks / n if n else 0.0
    cvr1 = (o1 / c1) if c1 else None
    cvr0 = (sum_orders / sum_clicks) if sum_clicks else None
    aov1 = (r1 / o1) if o1 else None
    aov0 = (sum_revenue / sum_orders) if sum_orders else None

    log_total = math.log(r1 / r0)
    factors_spec = [
        ("paid_traffic", "Clics payants (proxy de trafic)", "clicks", c1, c0, 0.15,
         "La variation du CA est associee au volume de trafic paye achete."),
        ("conversion_rate", "Taux de conversion sur trafic paye", "paid_conversion_rate", cvr1, cvr0, 0.20,
         "La variation du CA est associee a la capacite du site a convertir le trafic "
         "(parcours d'achat, prix, disponibilite, checkout)."),
        ("average_order_value", "Panier moyen", "aov", aov1, aov0, 0.05,
         "La variation du CA est associee a la valeur moyenne des commandes "
         "(mix produit, remises, quantites)."),
    ]

    for key, label, metric, v1, v0, penalty, hypothesis in factors_spec:
        if not v1 or not v0 or v1 <= 0 or v0 <= 0:
            analysis.factors.append(CauseFactor(
                key, f"{label}: non calculable", metric, v1, v0, None, None, 0.1,
                "Donnee insuffisante pour evaluer ce facteur.",
            ))
            continue
        change = _pct(v1, v0)
        contribution = math.log(v1 / v0) / log_total if log_total else None
        analysis.factors.append(CauseFactor(
            factor=key,
            observed_fact=(f"{label} {change:+.1%} sur {current.label} vs la baseline de "
                           f"{n} periode(s)" if n > 1 else
                           f"{label} {change:+.1%} entre {previous.label} et {current.label}"),
            supporting_metric=metric, current_value=v1, previous_value=v0,
            factor_change_pct=change, contribution=contribution,
            confidence=_confidence(contribution, penalty), hypothesis=hypothesis,
        ))

    analysis.factors.sort(key=lambda f: abs(f.contribution) if f.contribution is not None else -1, reverse=True)
    analysis.product_contributors = _contributors(
        product_performance(cur_ds), _mean_performance(windows, "sku"), "sku", "title", r1 - r0
    )
    analysis.campaign_contributors = _campaign_contributors(cur_ds, prev_ds)
    analysis.limitations = [
        "Les clics payants ne mesurent que Google Ads: le trafic organique et direct n'est pas observe.",
        "Les contributions decrivent une decomposition arithmetique, pas un lien de causalite demontre.",
        "Toute hypothese doit etre verifiee dans l'outil source avant decision.",
    ]
    return analysis


def _mean_performance(windows: Sequence[Dataset], key: str) -> List[dict]:
    """Performance produit MOYENNE PAR PERIODE sur la fenetre de baseline (AN-01).

    Necessaire, pas cosmetique: la contribution d'un produit divise par `r1 - r0`, ou `r0` est
    desormais la moyenne de baseline. Garder une reference produit sur la seule periode
    precedente ferait que la somme des contributions ne vaudrait plus 1 -- exactement le melange
    de referentiels qu'AN-01 supprime.
    """
    n = len(windows)
    if not n:
        return []
    totals: Dict[str, dict] = {}
    for window in windows:
        for row in product_performance(window):
            entry = totals.setdefault(row[key], dict(row, revenue=0.0))
            entry["revenue"] += row["revenue"]
    for entry in totals.values():
        entry["revenue"] /= n
    return sorted(totals.values(), key=lambda e: (-e["revenue"], e[key]))


def _contributors(current: List[dict], previous: List[dict], key: str, label: str, total_delta: float) -> List[dict]:
    prev_index = {row[key]: row for row in previous}
    rows = []
    for row in current:
        before = prev_index.get(row[key], {}).get("revenue", 0.0)
        delta = row["revenue"] - before
        rows.append({
            key: row[key], "label": row.get(label, row[key]),
            "current_revenue": round(row["revenue"], 2), "previous_revenue": round(before, 2),
            "delta": round(delta, 2),
            "contribution": round(delta / total_delta, 4) if total_delta else None,
        })
    for row in previous:
        if row[key] not in {r[key] for r in rows}:
            rows.append({
                key: row[key], "label": row.get(label, row[key]),
                "current_revenue": 0.0, "previous_revenue": round(row["revenue"], 2),
                "delta": round(-row["revenue"], 2),
                "contribution": round(-row["revenue"] / total_delta, 4) if total_delta else None,
            })
    # cle TOTALE (F-01): `abs(delta)` seul admet des egalites, et `list.sort` etant stable
    # l'ordre d'entree deciderait alors du classement. `key` est la colonne unique ("sku").
    rows.sort(key=lambda r: (-abs(r["delta"]), r[key]))
    return rows[:5]


def _campaign_contributors(cur_ds: Dataset, prev_ds: Dataset) -> List[dict]:
    current = {c["campaign_id"]: c for c in campaign_performance(cur_ds)}
    previous = {c["campaign_id"]: c for c in campaign_performance(prev_ds)}
    rows = []
    # DEFENSE 1 (F-01): l'ordre d'iteration d'un ensemble depend de PYTHONHASHSEED. La cle de tri
    # ci-dessous suffirait aujourd'hui, mais laisser la source en place la ferait reapparaitre au
    # prochain tri ajoute sur une cle non totale.
    for cid in sorted(set(current) | set(previous)):
        cur = current.get(cid, {})
        prev = previous.get(cid, {})
        name = cur.get("name") or prev.get("name") or cid
        cur_conv, prev_conv = cur.get("conversions", 0.0), prev.get("conversions", 0.0)
        cur_spend, prev_spend = cur.get("spend", 0.0), prev.get("spend", 0.0)
        rows.append({
            "campaign_id": cid, "name": name,
            "spend_current": round(cur_spend, 2), "spend_previous": round(prev_spend, 2),
            "spend_change_pct": round(_pct(cur_spend, prev_spend), 4) if _pct(cur_spend, prev_spend) is not None else None,
            "conversions_current": round(cur_conv, 2), "conversions_previous": round(prev_conv, 2),
            "conversions_change_pct": round(_pct(cur_conv, prev_conv), 4) if _pct(cur_conv, prev_conv) is not None else None,
            "cvr_current": round(cur["cvr"], 5) if cur.get("cvr") is not None else None,
            "cvr_previous": round(prev["cvr"], 5) if prev.get("cvr") is not None else None,
            "conversions_delta": round(cur_conv - prev_conv, 2),
        })
    # DEFENSE 2 (F-01): cle TOTALE. `campaign_id` est unique par construction (cle du dict de
    # `campaign_performance`), contrairement a `name` -- qui retombe sur `campaign_id` quand la
    # campagne est inconnue, n'est contraint par aucune unicite, vient d'une source non fiable et
    # est tronque a 300 caracteres cote LLM.
    rows.sort(key=lambda r: (r["conversions_delta"], r["campaign_id"]))
    return rows
