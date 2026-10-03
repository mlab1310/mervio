"""Confiance QUALITATIVE et ses criteres (AN-14 / D-081 option C).

Ce module remplace une heuristique numerique par un decompte entier. Il ne contient AUCUN
coefficient, et c'est sa raison d'etre.

Ce qui existait avant:

    raw = 0.35 + 0.5 * min(abs(contribution), 1.0) - quality_penalty
    return max(0.1, min(0.9, raw))

Neuf nombres choisis a la main -- base, pente, trois penalites par facteur, deux bornes, deux
valeurs de repli -- rendus a quatre decimales dans le rapport comme s'ils mesuraient quelque
chose. Ils ne mesuraient rien, et `min(abs(contribution), 1.0)` ECRASAIT l'information la plus
utile: une contribution de 2,16 obtenait la meme confiance maximale qu'une contribution de 1,00,
alors que la premiere signale une decomposition ininterpretable.

Le contrat (roadmap AN-14) nomme quatre criteres: longueur de baseline, couverture des champs,
facteurs observes, part expliquee. Chacun est SATISFAIT, NON SATISFAIT ou NON APPLICABLE, et le
niveau se derive d'un decompte:

    tous les criteres applicables satisfaits  -> high
    aucun satisfait                           -> low
    sinon                                     -> medium

Pas de seuil, pas de ponderation, pas d'arbitrage cache. Un critere NON APPLICABLE ne compte pas:
un critere sans objet ne peut ni soutenir ni affaiblir une conclusion.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

SATISFIED = "satisfied"
NOT_SATISFIED = "not_satisfied"
NOT_APPLICABLE = "not_applicable"

LOW, MEDIUM, HIGH = "low", "medium", "high"
#: ordre croissant de confiance, pour trier sans jamais comparer des chaines
CONFIDENCE_ORDER = (LOW, MEDIUM, HIGH)

#: les quatre criteres nommes par la roadmap AN-14, dans leur ordre d'exposition
BASELINE_SUFFICIENT = "baseline_sufficient"
FIELDS_RELIABLE = "fields_reliable"
NO_UNOBSERVED_COMPETITOR = "no_unobserved_competitor"
SHARE_INTERPRETABLE = "share_interpretable"
CRITERIA_ORDER = (BASELINE_SUFFICIENT, FIELDS_RELIABLE, NO_UNOBSERVED_COMPETITOR,
                  SHARE_INTERPRETABLE)

#: precision a laquelle le rapport EXPOSE une contribution (`CauseFactor.to_dict`).
#: Le critere se juge sur ce qui est expose: sinon un bruit flottant invisible a la lecture
#: ferait basculer un niveau de confiance.
_EXPOSED_DECIMALS = 4


def decomposition_interpretable(contributions: Sequence[Optional[float]]) -> bool:
    """La DECOMPOSITION entiere est-elle lisible comme un partage ?

    Le critere porte sur la decomposition, PAS sur le facteur isole, et c'est essentiel. Quand un
    facteur depasse 100 %, un autre le compense en sens inverse: la somme reste exacte mais AUCUN
    des deux ne decrit une part. Juger facteur par facteur donnerait `high` au compensateur --
    mesure sur `revenue_decline`: order_volume +1.5899 et average_order_value -0.5899, le second
    passant le critere alors qu'il appartient a la meme decomposition degeneree.
    """
    observed = [c for c in contributions if c is not None]
    if not observed:
        return False
    return all(share_interpretable(c) for c in observed)


def share_interpretable(contribution: Optional[float]) -> bool:
    """La part expliquee est-elle lisible comme une part ?

    Une contribution au-dela de 100 % signifie qu'un facteur explique plus que la totalite de la
    variation, ce qui n'arrive que si un autre la compense en sens inverse. La somme reste exacte,
    mais la DECOMPOSITION n'est plus interpretable: c'est le cas mesure sur `healthy_business`,
    ou une variation de +2,69 % produit des contributions de +216 % et -116 %.

    100 % n'est pas un coefficient regle: c'est la borne arithmetique de la notion de part.
    """
    if contribution is None:
        return False
    return abs(round(contribution, _EXPOSED_DECIMALS)) <= 1.0


def derive_level(states: Sequence[str]) -> str:
    """Niveau derive d'un DECOMPTE ENTIER des criteres applicables."""
    applicable = [state for state in states if state != NOT_APPLICABLE]
    if not applicable:
        return LOW
    satisfied = sum(1 for state in applicable if state == SATISFIED)
    if satisfied == len(applicable):
        return HIGH
    if satisfied == 0:
        return LOW
    return MEDIUM


@dataclass(frozen=True)
class Confidence:
    """Confiance qualitative et les criteres qui la produisent, tous exposes."""

    level: str
    criteria: Dict[str, str]

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "criteria": [{"criterion": name, "state": self.criteria[name]}
                         for name in CRITERIA_ORDER if name in self.criteria],
        }


def assess(
    *,
    baseline_periods: int,
    min_baseline_periods: int,
    fields_reliable: Optional[bool],
    unobserved_competitor: Optional[bool],
    contribution: Optional[float],
    share_applicable: bool = True,
    shares_interpretable: Optional[bool] = None,
) -> Confidence:
    """Evalue les quatre criteres et en derive le niveau.

    `None` signifie NON APPLICABLE: le critere n'a pas d'objet pour cet element, et il ne doit
    donc ni le soutenir ni l'affaiblir.
    """
    states: Dict[str, str] = {
        BASELINE_SUFFICIENT: (SATISFIED if baseline_periods >= min_baseline_periods
                              else NOT_SATISFIED),
        FIELDS_RELIABLE: _state(fields_reliable),
        NO_UNOBSERVED_COMPETITOR: _state(None if unobserved_competitor is None
                                         else not unobserved_competitor),
        # propriete de la DECOMPOSITION quand elle est fournie; a defaut, du facteur seul
        SHARE_INTERPRETABLE: (
            NOT_APPLICABLE if not share_applicable
            else _state(shares_interpretable if shares_interpretable is not None
                        else share_interpretable(contribution))),
    }
    return Confidence(derive_level([states[name] for name in CRITERIA_ORDER]), states)


def _state(value: Optional[bool]) -> str:
    if value is None:
        return NOT_APPLICABLE
    return SATISFIED if value else NOT_SATISFIED


def sort_rank(confidence: Optional[Confidence]) -> int:
    """Rang entier pour trier: plus haut = plus confiant. `None` est le plus bas.

    Un rang ENTIER, jamais une chaine: comparer `"high" < "low"` lexicographiquement donnerait
    l'ordre inverse de l'intention.
    """
    if confidence is None:
        return -1
    return CONFIDENCE_ORDER.index(confidence.level)


def levels_present(confidences: List[Optional[Confidence]]) -> List[str]:
    """Niveaux presents, pour les gardes de test et l'exhaustivite des enumerations."""
    return [c.level for c in confidences if c is not None]
