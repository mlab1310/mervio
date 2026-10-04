"""Root Cause Engine v1 (deterministe).

Methode: decomposition du CA en DEUX COUCHES (AN-01, AN-02).

    couche A   Revenue = Commandes x Panier moyen          (additive, somme des parts = 1)
    couche B   Commandes = Clics payants x Commandes par clic paye   (explications, non additives)

L'enonce << Revenue = Clics payants x Taux de conversion x Panier moyen >> decrivait le modele
a TROIS facteurs d'AVANT AN-01/AN-02, et citait un facteur -- le taux de conversion -- que
AN-02 a supprime parce qu'il confondait une chute organique avec un probleme de checkout. Le
texte publie vient desormais du catalogue (`root_cause.method`, AN-19 / D-5).

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

from ..config import AnomalyConfig, RootCauseConfig
from .confidence import Confidence, assess, decomposition_interpretable
from ..domain.models import Dataset
from .kpi import campaign_performance, product_performance, total_clicks, total_revenue
from .messages import FACTOR_HYPOTHESIS_KEY, FACTOR_LABEL_KEY, Message, render
from .periods import Period


#: couche A: identite arithmetique du CA (Revenue = Commandes x Panier moyen), ADDITIVE
LAYER_REVENUE = "a"
#: couche B: explications du VOLUME DE COMMANDES, NON additives
LAYER_ORDER_VOLUME = "b"
OBSERVED = "observed"
NOT_OBSERVED = "not_observed"
#: source qui manque pour observer le trafic total et la conversion du site
_MISSING_SESSIONS = "web_analytics_sessions"


def _label(factor: str) -> str:
    """Libelle francais d'un facteur, LU AU CATALOGUE via une table explicite (AN-19).

    Il entre ensuite dans `observed_fact` comme parametre: le message compose n'a donc pas
    besoin d'une cle par facteur, et le libelle reste au catalogue, en un seul endroit.
    """
    return render(FACTOR_LABEL_KEY[factor])


@dataclass
class CauseFactor:
    factor: str
    #: AN-19 / D-085: cle + parametres. Message COMPOSE: le libelle du facteur est un
    #: PARAMETRE resolu depuis `FACTOR_LABEL_KEY`, ce qui evite de creer les vingt cles
    #: facteur x variante qu'une cle par combinaison aurait exigees (D-3).
    observed_fact: Message
    supporting_metric: Optional[str]
    current_value: Optional[float]
    previous_value: Optional[float]
    factor_change_pct: Optional[float]
    #: part de la variation du CA. UNIQUEMENT pour la couche A: une contribution est une part de
    #: la variation du CA, et la couche B explique la variation du VOLUME. Les melanger dans un
    #: seul champ serait le melange de referentiels qu'AN-01 supprime.
    contribution: Optional[float]
    confidence: Optional[Confidence]
    hypothesis: Optional[Message]
    layer: str = LAYER_REVENUE
    observability: str = OBSERVED
    #: couche B: grandeur que ce facteur explique ("order_volume")
    explains: Optional[str] = None
    #: couche B observee: part de la variation de `explains` imputable a ce facteur
    explains_share: Optional[float] = None
    #: facteur NON observe: source qui manque. Obligatoire dans ce cas (D-073).
    missing_source: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "factor": self.factor, "layer": self.layer,
            "observability": self.observability,
            "observed_fact": self.observed_fact.text,
            "observed_fact_key": self.observed_fact.key,
            "observed_fact_params": dict(self.observed_fact.params),
            "supporting_metric": self.supporting_metric,
            "current_value": round(self.current_value, 4) if self.current_value is not None else None,
            "previous_value": round(self.previous_value, 4) if self.previous_value is not None else None,
            "factor_change_pct": round(self.factor_change_pct, 4) if self.factor_change_pct is not None else None,
            "contribution": round(self.contribution, 4) if self.contribution is not None else None,
            "confidence": self.confidence.to_dict() if self.confidence is not None else None,
            "hypothesis": self.hypothesis.text if self.hypothesis is not None else None,
            "hypothesis_key": self.hypothesis.key if self.hypothesis is not None else None,
            "hypothesis_params": (dict(self.hypothesis.params)
                                  if self.hypothesis is not None else None),
            "explains": self.explains,
            "explains_share": round(self.explains_share, 4) if self.explains_share is not None else None,
            "missing_source": self.missing_source,
        }

    @property
    def sort_key(self):
        """Cle TOTALE: observes d'abord, couche A avant couche B, puis |part|, puis le nom.

        `factor` est unique, donc la cle ne peut pas admettre d'egalite. Avant AN-02,
        `abs(contribution)` seul laissait l'ordre d'entree decider a egalite (mesure sur
        `f01_ties`: `paid_traffic` et `average_order_value` tous deux a -0.0).
        """
        magnitude = self.contribution if self.contribution is not None else self.explains_share
        return (
            0 if self.observability == OBSERVED else 1,
            0 if self.layer == LAYER_REVENUE else 1,
            -abs(magnitude) if magnitude is not None else 0.0,
            self.factor,
        )


@dataclass
class RootCauseAnalysis:
    target_metric: str
    period: str
    previous_period: str
    observed_change_pct: Optional[float]
    observed_change_absolute: Optional[float]
    #: AN-19 / D-5: l'enonce de la methode vient du catalogue. Le texte precedent decrivait le
    #: modele a TROIS facteurs d'avant AN-01/AN-02, avec le facteur qu'AN-02 a supprime.
    method: Message
    #: labels des periodes de baseline, identiques a ceux de l'anomalie (AN-01)
    baseline_periods: List[str] = field(default_factory=list)
    #: niveau de reference: moyenne du CA sur la baseline. Egale EXACTEMENT
    #: `anomaly.expected_value` -- c'est l'objet meme d'AN-01.
    baseline_revenue: Optional[float] = None
    #: comparaison a la periode precedente, CONSERVEE mais etiquetee a part et SANS
    #: contribution: une contribution rattachee a un autre referentiel serait un melange.
    change_vs_previous_period: Dict[str, Optional[float]] = field(default_factory=dict)
    #: commandes TOTALES / clics PAYANTS: une preuve, jamais un taux de conversion du site
    orders_per_paid_click: Dict[str, object] = field(default_factory=dict)
    #: seuil de primaute de la couche B EFFECTIVEMENT applique (D-089). Enregistre pour que le
    #: rapport soit auditable: sans lui, `to_dict()` retomberait sur le defaut et le seuil
    #: configure n'atteindrait jamais le rapport.
    #: valeur par defaut LUE dans la configuration: un litteral ici serait un duplicata
    #: du seuil D-089, et deux proprietaires pour un seuil divergent toujours.
    primacy_share: float = field(
        default_factory=lambda: RootCauseConfig().paid_traffic_primacy_share)
    factors: List[CauseFactor] = field(default_factory=list)
    product_contributors: List[dict] = field(default_factory=list)
    campaign_contributors: List[dict] = field(default_factory=list)
    limitations: List[Message] = field(default_factory=list)
    available: bool = True

    def primary_factor(self, cfg: Optional[RootCauseConfig] = None) -> Optional[CauseFactor]:
        """Facteur primaire. Jamais un facteur NON observe (D-073).

        Couche A d'abord: elle porte l'identite du CA, et sa dominance est exacte, sans seuil.
        Puis, si le volume de commandes domine le CA ET que les clics payants expliquent la part
        dominante de ce volume, la cause nommee devient `paid_traffic` -- parce que le volume est
        le SYMPTOME et le trafic paye l'explication mesurable.

        Le seuil vient de `RootCauseConfig` (D-089, MEASUREMENT REQUIRED): il n'est pas une
        constante, et il ne doit pas etre calibre pour faire passer un test.
        """
        threshold = cfg.paid_traffic_primacy_share if cfg is not None else self.primacy_share
        layer_a = [f for f in self.factors
                   if f.layer == LAYER_REVENUE and f.observability == OBSERVED
                   and f.contribution is not None]
        if not layer_a:
            return None
        # cle TOTALE: `factor` est unique, donc aucune egalite possible
        dominant = min(layer_a, key=lambda f: (-abs(f.contribution), f.factor))
        if dominant.factor != "order_volume":
            return dominant
        explanations = [f for f in self.factors
                        if f.layer == LAYER_ORDER_VOLUME and f.observability == OBSERVED
                        and f.explains_share is not None]
        if not explanations:
            return dominant
        best = min(explanations, key=lambda f: (-abs(f.explains_share), f.factor))
        if abs(best.explains_share) >= threshold:
            return best
        return dominant

    def to_dict(self) -> dict:
        primary = self.primary_factor()
        return {
            "target_metric": self.target_metric, "period": self.period,
            "previous_period": self.previous_period,
            "observed_change_pct": round(self.observed_change_pct, 4) if self.observed_change_pct is not None else None,
            "observed_change_absolute": round(self.observed_change_absolute, 2) if self.observed_change_absolute is not None else None,
            "method": self.method.text, "method_key": self.method.key,
            "available": self.available,
            "baseline_periods": list(self.baseline_periods),
            "baseline_revenue": round(self.baseline_revenue, 2) if self.baseline_revenue is not None else None,
            "change_vs_previous_period": dict(self.change_vs_previous_period),
            "orders_per_paid_click": dict(self.orders_per_paid_click),
            "primacy_share": self.primacy_share,
            "primary_factor": primary.factor if primary else None,
            "factors": [f.to_dict() for f in self.factors],
            "product_contributors": self.product_contributors,
            "campaign_contributors": self.campaign_contributors,
            # AN-19: le texte reste, la cle s'ajoute a cote (forme additive, D-3)
            "limitations": [item.text for item in self.limitations],
            "limitation_keys": [item.key for item in self.limitations],
        }


def _pct(current: float, previous: float) -> Optional[float]:
    return ((current - previous) / abs(previous)) if previous else None


def _fields_reliable(dataset: Dataset) -> Optional[bool]:
    """Les champs qui alimentent la decomposition sont-ils DECLARES fiables ?

    Lit le rapport qualite, qui est deja la source de verite sur la couverture (D-047). Aucun
    seuil nouveau: le statut est celui que l'ingestion a inscrit.
    """
    fields = getattr(dataset.quality, "fields", None)
    if not fields:
        return None
    relevant = [f for key, f in fields.items()
                if key in ("order_total", "order_created_at", "ad_clicks", "ad_spend")]
    if not relevant:
        return None
    return all(getattr(f.status, "value", f.status) == "reliable" for f in relevant)


def analyse_revenue_change(
    full: Dataset, current: Period, previous: Period,
    baseline: Optional[Sequence[Period]] = None,
    cfg: Optional[RootCauseConfig] = None,
    cfg_anomaly: Optional[AnomalyConfig] = None,
) -> RootCauseAnalysis:
    """Decompose la variation du CA contre la MEME baseline que l'anomalie (AN-01).

    `baseline`: periodes de reference, telles que declarees par le detecteur
    (`anomaly.baseline_points`). Absent, la baseline vaut `[previous]`: cas degenere n = 1, qui
    reproduit EXACTEMENT le comportement anterieur a AN-01 (somme(R)/1 = R0, somme(O)/somme(C)
    = cvr0, somme(R)/somme(O) = aov0).

    Les agregats construisent les RATIOS; le niveau de reference est leur MOYENNE par periode.
    Prendre somme(CA) sans diviser par n fausserait ln(R1/R0) d'un facteur ln n.
    """
    cfg = cfg or RootCauseConfig()
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
        method=Message("root_cause.method"),
        baseline_periods=[p.label for p in (baseline or [previous])],
        baseline_revenue=r0 if n else None,
        primacy_share=cfg.paid_traffic_primacy_share,
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
        analysis.limitations.append(Message("root_cause.decomposition_impossible"))
        return analysis

    c1 = total_clicks(cur_ds)
    o1 = len(cur_ds.orders)
    # RATIOS DE SOMMES, pas moyennes de ratios (AN-01)
    c0 = sum_clicks / n if n else 0.0
    o0 = sum_orders / n if n else 0.0
    aov1 = (r1 / o1) if o1 else None
    aov0 = (sum_revenue / sum_orders) if sum_orders else None
    opc1 = (o1 / c1) if c1 else None
    opc0 = (sum_orders / sum_clicks) if sum_clicks else None

    log_total = math.log(r1 / r0)
    # Criteres d'AN-14, evalues UNE fois pour cette analyse.
    #   baseline: la meme exigence que le detecteur d'anomalie (D-007), pas un nouveau seuil
    #   champs:   la qualite DECLAREE des champs qui alimentent la decomposition
    min_baseline = cfg_anomaly.min_baseline_periods if cfg_anomaly else n
    fields_reliable = _fields_reliable(full)

    def _observed(key, label, metric, v1, v0, hypothesis, layer, explains=None,
                  unobserved_competitor=None):
        """Facteur OBSERVE. Contribution en couche A, part explicative en couche B.

        AN-14: plus aucune penalite numerique. La confiance est derivee des quatre criteres du
        contrat, et la part expliquee en est UN -- au lieu d'etre ecrasee par
        `min(abs(contribution), 1.0)`.
        """
        if not v1 or not v0 or v1 <= 0 or v0 <= 0:
            return CauseFactor(
                factor=key,
                observed_fact=Message("factor.observed_fact_not_computable",
                                      {"factor": key, "factor_label": label}),
                supporting_metric=metric,
                current_value=v1, previous_value=v0, factor_change_pct=None, contribution=None,
                confidence=assess(baseline_periods=n, min_baseline_periods=min_baseline,
                                  fields_reliable=fields_reliable,
                                  unobserved_competitor=unobserved_competitor,
                                  contribution=None,
                                  shares_interpretable=shares_interpretable),
                hypothesis=Message("factor.hypothesis_insufficient_data"),
                layer=layer, explains=explains,
            )
        change = _pct(v1, v0)
        share = math.log(v1 / v0) / log_total if log_total else None
        if layer == LAYER_REVENUE:
            contribution, explains_share = share, None
        else:
            # couche B: part de la variation du VOLUME DE COMMANDES, pas du CA.
            # Identite exacte: Commandes = Clics payants x Commandes par clic paye.
            log_orders = math.log(o1 / o0) if (o1 and o0) else None
            contribution = None
            explains_share = (math.log(v1 / v0) / log_orders) if log_orders else None
        magnitude = contribution if contribution is not None else explains_share
        # B-1: le parametre publie porte la precision du CONTRAT (4 decimales, comme
        # `factors[].factor_change_pct`). `change` lui-meme reste entier: c'est lui qui
        # alimente `factor_change_pct` et les criteres de confiance.
        published_change = round(change, 4)
        observed_fact = (
            Message("factor.observed_fact_vs_baseline",
                    {"factor": key, "factor_label": label, "change": published_change,
                     "period": current.label, "baseline_periods": n})
            if n > 1 else
            Message("factor.observed_fact_vs_previous_period",
                    {"factor": key, "factor_label": label, "change": published_change,
                     "period": current.label, "previous_period": previous.label}))
        return CauseFactor(
            factor=key, observed_fact=observed_fact,
            supporting_metric=metric, current_value=v1, previous_value=v0,
            factor_change_pct=change, contribution=contribution,
            confidence=assess(baseline_periods=n, min_baseline_periods=min_baseline,
                              fields_reliable=fields_reliable,
                              unobserved_competitor=unobserved_competitor,
                              contribution=magnitude,
                              shares_interpretable=shares_interpretable),
            hypothesis=hypothesis, layer=layer, explains=explains, explains_share=explains_share,
        )

    def _unobserved(key, label, explains):
        """Facteur NON observe (D-073).

        Tous les champs chiffres et interpretatifs sont null: `contribution: 0.0` AFFIRMERAIT une
        absence d'effet, la meme faute que << CAC = 0 >>. L'angle mort est DECLARE, jamais comble.
        """
        return CauseFactor(
            factor=key,
            observed_fact=Message("factor.observed_fact_not_observed",
                                  {"factor": key, "factor_label": label}),
            supporting_metric=None,
            current_value=None, previous_value=None, factor_change_pct=None, contribution=None,
            confidence=None, hypothesis=None, layer=LAYER_ORDER_VOLUME,
            observability=NOT_OBSERVED, explains=explains, explains_share=None,
            missing_source=_MISSING_SESSIONS,
        )

    # AN-14: le critere de part expliquee porte sur la DECOMPOSITION. Elle est construite en
    # deux passes: les contributions de couche A d'abord, puis la confiance qui les juge
    # ensemble. Un facteur ne peut pas etre juge interpretable isolement quand un autre de la
    # meme decomposition depasse 100 %.
    layer_a_shares = [
        math.log(v1 / v0) / log_total if (v1 and v0 and v1 > 0 and v0 > 0 and log_total) else None
        for v1, v0 in ((float(o1), o0), (aov1, aov0))
    ]
    shares_interpretable = decomposition_interpretable(layer_a_shares)

    # COUCHE A -- identite arithmetique du CA, additive, somme des contributions = 1
    # `order_volume` a DEUX concurrents non observes: le trafic organique et la conversion du
    # site expliquent le meme volume et ne sont pas mesures (lecon d'AN-02). Sa dominance est
    # donc confondue, et le critere le dit.
    analysis.factors.append(_observed(
        "order_volume", _label("order_volume"), "orders", float(o1), o0,
        Message(FACTOR_HYPOTHESIS_KEY["order_volume"]), LAYER_REVENUE,
        unobserved_competitor=True))
    analysis.factors.append(_observed(
        "average_order_value", _label("average_order_value"), "aov", aov1, aov0,
        Message(FACTOR_HYPOTHESIS_KEY["average_order_value"]), LAYER_REVENUE,
        unobserved_competitor=False))

    # COUCHE B -- explications du volume de commandes, NON additives
    analysis.factors.append(_observed(
        "paid_traffic", _label("paid_traffic"), "paid_clicks", float(c1), c0,
        Message(FACTOR_HYPOTHESIS_KEY["paid_traffic"]),
        LAYER_ORDER_VOLUME, explains="order_volume", unobserved_competitor=True))
    analysis.factors.append(_unobserved(
        "organic_traffic", _label("organic_traffic"), "order_volume"))
    analysis.factors.append(_unobserved(
        "site_conversion_rate", _label("site_conversion_rate"), "order_volume"))

    analysis.orders_per_paid_click = {
        "current": round(opc1, 6) if opc1 is not None else None,
        "baseline": round(opc0, 6) if opc0 is not None else None,
        "note": "commandes TOTALES rapportees aux SEULS clics payants: une preuve, pas un taux "
                "de conversion du site. Le trafic organique et direct n'est pas observe.",
    }

    # cle TOTALE (AN-02): observes avant non observes, couche A avant couche B,
    # puis |part| decroissante, puis le nom du facteur -- qui est unique.
    analysis.factors.sort(key=lambda f: f.sort_key)
    analysis.product_contributors = _contributors(
        product_performance(cur_ds), _mean_performance(windows, "sku"), "sku", "title", r1 - r0
    )
    analysis.campaign_contributors = _campaign_contributors(cur_ds, prev_ds)
    analysis.limitations = [
        Message("root_cause.limitation.paid_clicks_only"),
        Message("root_cause.limitation.no_causality"),
        Message("root_cause.limitation.verify_in_source"),
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
