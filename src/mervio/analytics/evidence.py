"""Preuves STRUCTUREES (AN-15 / D-082, perimetre etendu par D-090 = A).

Ce module remplace trois `List[str]` de francais preformate -- `Insight.evidence`,
`Anomaly.evidence` et `DimensionScore.evidence` -- par un fait typé.

Ce qui existait avant:

    "revenue observe sur 2026-W37: 24,051.10"
    "attendu (moyenne 8 periodes): 29,569.65"
    "ecart relatif: -18.7%"
    "depense: 749.46 vs 710.56 EUR"
    "CA -23.5% (2026-W37 vs 2026-W36)"

Trois defauts dans ces chaines, et un quatrieme qui les resume:

  * l'API, l'interface et le LLM devaient REPARSER des nombres depuis du francais;
  * l'identite de la grandeur etait perdue -- << CA >> ne dit pas quelle cle KPI, quelle source,
    ni contre quoi la comparaison est faite;
  * << 749.46 vs 710.56 >> melangeait deux valeurs et une devise, et il fallait couper sur
    << vs >> en devinant l'ordre;
  * le LLM recevait un fait mesure et un texte genere sous la MEME forme: une chaine.

Le cas le plus parlant etait `Anomaly.evidence`: l'anomalie portait DEJA `observed_value`,
`expected_value`, `delta`, `delta_pct` et `z_score` en champs typés, et les reformulait en prose.
La preuve etait une reecriture de donnees dont on disposait.

PRINCIPE:  fait structure -> rendu
JAMAIS:    nombre -> chaine de preuve libre -> interpretation LLM

Le texte rendu reste la representation finale (`reporting/`), il n'est plus la source de verite.
Les cles de message preparent AN-19, qui fournira le catalogue de rendu.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

OBSERVED = "observed"
NOT_OBSERVED = "not_observed"

#: contre quoi la comparaison est faite. Enumeration FERMEE: une part de variation vs baseline et
#: vs periode precedente ne sont pas la meme grandeur, et les confondre est le defaut AN-01.
VS_BASELINE = "baseline"
VS_PREVIOUS_PERIOD = "previous_period"
NO_COMPARISON = "none"
COMPARISONS = (VS_BASELINE, VS_PREVIOUS_PERIOD, NO_COMPARISON)

#: unites exposables. Fermee aussi: sans elle, un consommateur devine si 0,18 est un ratio ou un
#: montant.
UNITS = ("currency", "count", "ratio", "score", "days")


@dataclass(frozen=True)
class Evidence:
    """Un fait qui soutient un constat. Aucune de ses valeurs ne vit dans une phrase."""

    #: identifiant stable du fait, unique dans son bloc
    fact_id: str
    #: cle de rendu (AN-19 fournira le catalogue). Technique, jamais du texte libre.
    message_key: str
    #: grandeur d'origine, cle du contrat (`revenue`, `orders_per_paid_click`, ...)
    metric_key: Optional[str] = None
    #: source de donnee qui porte cette grandeur
    source: Optional[str] = None
    observability: str = OBSERVED
    period: Optional[str] = None
    comparison: str = NO_COMPARISON
    unit: Optional[str] = None
    current_value: Optional[float] = None
    previous_value: Optional[float] = None
    delta: Optional[float] = None
    delta_pct: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "message_key": self.message_key,
            "metric_key": self.metric_key,
            "source": self.source,
            "observability": self.observability,
            "period": self.period,
            "comparison": self.comparison,
            "unit": self.unit,
            "current_value": _round(self.current_value),
            "previous_value": _round(self.previous_value),
            "delta": _round(self.delta),
            "delta_pct": round(self.delta_pct, 4) if self.delta_pct is not None else None,
        }


def _round(value: Optional[float]) -> Optional[float]:
    return round(value, 4) if value is not None else None


def observed(
    fact_id: str,
    message_key: str,
    *,
    metric_key: str,
    source: str,
    unit: str,
    period: Optional[str] = None,
    current: Optional[float] = None,
    previous: Optional[float] = None,
    comparison: str = NO_COMPARISON,
    delta_pct: Optional[float] = None,
) -> Evidence:
    """Fait MESURE. `delta` est DERIVE des deux valeurs, jamais fourni a part.

    Le deriver garantit qu'il ne peut pas contredire les valeurs qu'il resume -- c'est exactement
    ce qu'une chaine << 749.46 vs 710.56 >> ne garantissait pas.
    """
    delta = (current - previous) if (current is not None and previous is not None) else None
    if delta_pct is None and delta is not None and previous:
        delta_pct = delta / abs(previous)
    return Evidence(
        fact_id=fact_id, message_key=message_key, metric_key=metric_key, source=source,
        observability=OBSERVED, period=period,
        comparison=comparison if previous is not None else NO_COMPARISON,
        unit=unit, current_value=current, previous_value=previous,
        delta=delta, delta_pct=delta_pct,
    )


def not_observed(fact_id: str, message_key: str, *, metric_key: Optional[str] = None,
                 source: Optional[str] = None) -> Evidence:
    """Fait NON MESURE. Toutes les valeurs restent `null` (meme regle que D-073).

    `0` affirmerait une absence d'effet; une chaine ambigue laisserait croire a une mesure.
    """
    return Evidence(fact_id=fact_id, message_key=message_key, metric_key=metric_key,
                    source=source, observability=NOT_OBSERVED, comparison=NO_COMPARISON)


def to_dicts(items: List[Evidence]) -> List[dict]:
    """Ordre d'EMISSION conserve: il porte la hierarchie semantique des preuves.

    Un tri lexicographique la perdrait, et un ordre d'ensemble la rendrait non deterministe.
    """
    return [item.to_dict() for item in items]


def finding_id(insight_type: str, scope: Optional[str] = None) -> str:
    """Identifiant stable d'un constat (D-082): `{type}` ou `{type}:{scope}`.

    Il existait UNIQUEMENT dans `llm/context.py`, frappe localement, et etait absent du rapport:
    le consommateur fabriquait l'identite de ce qu'il consommait. Elle vient desormais du moteur.
    """
    slug = _slug(insight_type)
    return f"{slug}:{_slug(scope)}" if scope else slug


def _slug(value: str) -> str:
    kept = [char if (char.isalnum() or char in "._-") else "_" for char in str(value).lower()]
    return "".join(kept).strip("_") or "inconnu"


#: parametres de rendu derives d'un fait, pour la couche de presentation (AN-19 les consommera)
def render_params(item: Evidence) -> Dict[str, object]:
    return {key: value for key, value in item.to_dict().items() if value is not None}
