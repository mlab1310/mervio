"""Catalogue des messages du moteur (AN-19 / D-085).

AN-15 a structure les FAITS. Ici ce sont les TEXTES: les constats du moteur n'ecrivent plus de
prose, ils designent un message du catalogue et fournissent ses parametres.

    <message>_key     identite CONTRACTUELLE, statique, au catalogue
    <message>_params  les valeurs, typees, telles que le moteur les a mesurees
    <message>         le texte RENDU -- une representation derivee, jamais la reference

POURQUOI UN CATALOGUE. Une phrase ecrite au milieu d'un calcul n'a aucune identite: deux
consommateurs du meme constat n'ont que le texte pour le reconnaitre, et `llm/context.py`
fusionnait effectivement les hypotheses PAR LEUR TEXTE. Elle n'est pas relue non plus -- le
champ charge d'enoncer la methode de decomposition publiait le modele d'AVANT AN-02, avec le
facteur que AN-02 avait supprime comme trompeur (D-5).

REGLES (D-085), toutes verifiees par `tests/test_message_catalog.py`:

  * le catalogue est une LITTERALE: aucune entree calculee, aucune boucle, aucune concatenation
    depuis une donnee -- une garde AST le verifie;
  * aucune cle construite depuis une donnee. Les tables `*_KEY` ci-dessous font la
    correspondance valeur de contrat -> cle, explicitement;
  * aucun fallback: une cle inconnue ou un parametre manquant leve `MessageError`. Pas de
    `.get`, pas de texte par defaut, pas de retour de la cle en guise de texte;
  * francais ACCENTUE;
  * le rendu ne CALCULE rien. Les formats (`:.1%`, `:+.1%`) vivent dans le gabarit, donc ils
    sont relus avec lui; les valeurs arrivent telles que le moteur les a mesurees.

CE QUI N'EST PAS ICI. Les 26 cles `evidence.*` d'AN-15 restent des IDENTIFIANTS PURS, sans
entree de catalogue (D-2): une preuve se rend depuis ses champs typés -- `metric_key`, `unit`,
valeurs -- et c'est tout le principe de D-082. Leur donner un gabarit reintroduirait la phrase
comme source de verite.

HORS PERIMETRE (D-1), laisse a G-27 / 004.7: notes de KPI, motifs de comparaison, notes et
limitations de profitabilite, `limitations[]` general, messages d'ingestion, chrome de rendu.
"""
from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple


class MessageError(KeyError):
    """Cle inconnue ou parametre manquant. Jamais un texte de repli."""


@dataclass
class Message:
    """Ce que le moteur produit desormais a la place d'une phrase: une cle et des valeurs.

    Le texte n'est plus porte, il est DERIVE (`text`). Un constat ne peut donc pas publier un
    texte qui contredise sa cle, ni une cle qui ne rende pas le texte publie.
    """

    key: str
    params: Dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return render(self.key, self.params)


MESSAGES: Dict[str, str] = {
    # -- constats: le FAIT ---------------------------------------------------------------
    "finding.revenue_decline.fact":
        "Le CA de {period} est inférieur de {delta_pct_abs:.1%} à la moyenne des périodes "
        "précédentes.",
    "finding.metric_anomaly.fact":
        "{metric} sur {period}: {delta_pct:+.1%} vs baseline.",
    "finding.metric_anomaly.fact_without_delta":
        "{metric}: écart détecté sur {period}.",
    "finding.profitability_unavailable.fact":
        "Le profit de contribution complet n'est pas calculable à partir des données fournies.",
    "finding.low_margin_product.fact":
        "{title} ({sku}) dégage une marge brute de {gross_margin:.1%}.",
    "finding.customer_concentration.fact":
        "Les 5 plus gros clients représentent {share:.1%} du CA de la période.",
    "finding.campaign_opportunity.fact":
        "La campagne {name} a augmenté ses conversions de {conversions_delta:.0f} sur la période.",

    # -- constats: l'HYPOTHESE -----------------------------------------------------------
    "finding.revenue_decline.hypothesis_not_identified":
        "Cause non identifiée à partir des données disponibles.",
    "finding.metric_anomaly.hypothesis_favorable":
        "Écart favorable: comprendre ce qui l'a produit pour tenter de le reproduire.",
    "finding.metric_anomaly.hypothesis_neutral":
        "Écart inhabituel dont le signe n'est ni bon ni mauvais en soi.",
    "finding.metric_anomaly.hypothesis_unfavorable":
        "Variation statistiquement inhabituelle: la cause n'est pas établie par ce moteur.",
    "finding.low_margin_product.hypothesis":
        "Marge faible: prix, coût d'achat ou remises appliquées.",
    "finding.customer_concentration.hypothesis":
        "Dépendance commerciale à un petit nombre de comptes.",
    "finding.campaign_opportunity.hypothesis":
        "Cette campagne absorbe la demande mieux que les autres sur la période.",

    # -- constats: la RECOMMANDATION -----------------------------------------------------
    "finding.revenue_decline.recommendation_not_identified":
        "Vérifier manuellement trafic, checkout et disponibilité produit.",
    "finding.metric_anomaly.recommendation_favorable":
        "Identifier ce qui explique l'amélioration de {metric} avant qu'elle ne se dissipe.",
    "finding.metric_anomaly.recommendation_neutral":
        "Vérifier que la variation de {metric} est intentionnelle.",
    "finding.metric_anomaly.recommendation_unfavorable":
        "Vérifier {metric} dans la source avant toute décision.",
    "finding.profitability_unavailable.recommendation":
        "Renseigner les coûts manquants ({components}) pour obtenir une profitabilité réelle "
        "plutôt qu'un CA.",
    "finding.low_margin_product.recommendation":
        "Réévaluer le prix ou le coût d'achat de cette référence.",
    "finding.customer_concentration.recommendation":
        "Diversifier l'acquisition pour réduire le risque de concentration.",
    "finding.campaign_opportunity.recommendation":
        "Tester une hausse de budget maîtrisée sur cette campagne et mesurer le CPA.",

    # -- recommandation DERIVEE DU FACTEUR primaire (AN-02 / D-075) ----------------------
    "recommendation.factor.order_volume":
        "Chercher d'où vient la demande manquante: le trafic total et la conversion du site ne "
        "sont pas observés, donc la cause n'est pas établie. Connecter une mesure d'audience "
        "pour la trancher.",
    "recommendation.factor.order_volume_with_campaign":
        "Chercher d'où vient la demande manquante: le trafic total et la conversion du site ne "
        "sont pas observés, donc la cause n'est pas établie. Connecter une mesure d'audience "
        "pour la trancher. La baisse de conversions est la plus marquée sur {campaign}.",
    "recommendation.factor.paid_traffic":
        "Vérifier budgets, enchères et taux d'impressions perdues dans Google Ads: la variation "
        "vient du volume de trafic acheté.",
    "recommendation.factor.average_order_value":
        "Analyser le mix produit et le niveau de remise: la variation vient de la valeur moyenne "
        "des commandes, pas du volume.",
    "recommendation.factor.fallback":
        "Vérifier le facteur identifié dans l'outil source avant toute décision.",

    # -- base de l'impact estime ---------------------------------------------------------
    "impact_basis.revenue_vs_baseline":
        "écart de CA observé vs baseline glissante, en devise",
    "impact_basis.campaign_not_quantifiable":
        "impact non chiffrable: aucune donnée de marge par campagne",

    # -- regle de detection (D-007: le critere utilise est toujours ecrit) ---------------
    "anomaly.criterion":
        "|variation| >= {min_material_pct:.0%} (matérialité) ET (|variation| >= "
        "{pct_threshold:.0%} vs moyenne des {baseline_periods} périodes précédentes OU "
        "|z| >= {zscore_threshold})",
    "anomaly.criterion_without_zscore":
        "|variation| >= {min_material_pct:.0%} (matérialité) ET (|variation| >= "
        "{pct_threshold:.0%} vs moyenne des {baseline_periods} périodes précédentes)",

    # -- decomposition de la cause racine ------------------------------------------------
    # D-5: l'enonce precedent decrivait le modele a TROIS facteurs d'avant AN-01/AN-02, avec
    # le facteur que AN-02 a supprime comme trompeur. Formulation ratifiee, non reinventee.
    "root_cause.method":
        "Décomposition du revenu : Revenu = Commandes × Panier moyen. Les commandes sont "
        "ensuite expliquées par les clics payants et les commandes par clic payé.",
    "root_cause.decomposition_impossible":
        "Décomposition impossible: le CA est nul sur au moins une des deux périodes.",
    "root_cause.limitation.paid_clicks_only":
        "Les clics payants ne mesurent que Google Ads: le trafic organique et direct n'est pas "
        "observé.",
    "root_cause.limitation.no_causality":
        "Les contributions décrivent une décomposition arithmétique, pas un lien de causalité "
        "démontré.",
    "root_cause.limitation.verify_in_source":
        "Toute hypothèse doit être vérifiée dans l'outil source avant décision.",

    # -- facteurs: libelles --------------------------------------------------------------
    "factor.order_volume.label": "Volume de commandes",
    "factor.average_order_value.label": "Panier moyen",
    "factor.paid_traffic.label": "Clics payants",
    "factor.organic_traffic.label": "Trafic organique et direct",
    "factor.site_conversion_rate.label": "Taux de conversion du site",

    # -- facteurs: fait observe (message COMPOSE: le libelle est un parametre, D-3) -------
    "factor.observed_fact_vs_baseline":
        "{factor_label} {change:+.1%} sur {period} vs la baseline de {baseline_periods} "
        "période(s)",
    "factor.observed_fact_vs_previous_period":
        "{factor_label} {change:+.1%} sur {period} vs {previous_period}",
    "factor.observed_fact_not_computable": "{factor_label}: non calculable",
    "factor.observed_fact_not_observed": "{factor_label}: non observé",

    # -- facteurs: hypotheses ------------------------------------------------------------
    "factor.order_volume.hypothesis":
        "La variation du CA est associée au NOMBRE de commandes.",
    "factor.average_order_value.hypothesis":
        "La variation du CA est associée à la valeur moyenne des commandes (mix produit, "
        "remises, quantités).",
    "factor.paid_traffic.hypothesis":
        "La variation du volume de commandes est associée au trafic payé acheté.",
    "factor.hypothesis_insufficient_data":
        "Donnée insuffisante pour évaluer ce facteur.",

    # -- enumerations de contrat, rendues en francais ------------------------------------
    "severity.high": "ÉLEVÉE",
    "severity.medium": "MOYENNE",
    "severity.low": "FAIBLE",
    "severity.info": "INFO",
    "data_quality.reliable": "FIABLE",
    "data_quality.incomplete": "INCOMPLET",
    "data_quality.unavailable": "INDISPONIBLE",
    "confidence.low": "faible",
    "confidence.medium": "moyenne",
    "confidence.high": "élevée",
    "criterion_state.satisfied": "oui",
    "criterion_state.not_satisfied": "non",
    "criterion_state.not_applicable": "sans objet",

    # -- etat vide d'une section de constats (D-4) ---------------------------------------
    # Quatre cles, pas un repli: la phrase d'une section ne sert jamais a une autre. Celle de
    # `setup` dit ce que la categorie est -- une donnee a brancher, pas un defaut a detecter.
    "section.critical_issues.empty": "Aucun élément détecté sur les dimensions mesurées.",
    "section.warnings.empty": "Aucun élément détecté sur les dimensions mesurées.",
    "section.setup.empty": "Aucune donnée à connecter sur les dimensions mesurées.",
    "section.opportunities.empty": "Aucun élément détecté sur les dimensions mesurées.",
}

#: valeur de contrat -> cle du catalogue. Tables EXPLICITES: elles remplacent une f-string, et
#: c'est pour cela qu'elles existent (D-085: aucune cle construite depuis une donnee).
FACTOR_LABEL_KEY: Dict[str, str] = {
    "order_volume": "factor.order_volume.label",
    "average_order_value": "factor.average_order_value.label",
    "paid_traffic": "factor.paid_traffic.label",
    "organic_traffic": "factor.organic_traffic.label",
    "site_conversion_rate": "factor.site_conversion_rate.label",
}
FACTOR_HYPOTHESIS_KEY: Dict[str, str] = {
    "order_volume": "factor.order_volume.hypothesis",
    "average_order_value": "factor.average_order_value.hypothesis",
    "paid_traffic": "factor.paid_traffic.hypothesis",
}
FACTOR_RECOMMENDATION_KEY: Dict[str, str] = {
    "order_volume": "recommendation.factor.order_volume",
    "paid_traffic": "recommendation.factor.paid_traffic",
    "average_order_value": "recommendation.factor.average_order_value",
}
SEVERITY_KEY: Dict[str, str] = {
    "high": "severity.high", "medium": "severity.medium",
    "low": "severity.low", "info": "severity.info",
}
DATA_QUALITY_KEY: Dict[str, str] = {
    "reliable": "data_quality.reliable", "incomplete": "data_quality.incomplete",
    "unavailable": "data_quality.unavailable",
}
CONFIDENCE_KEY: Dict[str, str] = {
    "low": "confidence.low", "medium": "confidence.medium", "high": "confidence.high",
}
CRITERION_STATE_KEY: Dict[str, str] = {
    "satisfied": "criterion_state.satisfied", "not_satisfied": "criterion_state.not_satisfied",
    "not_applicable": "criterion_state.not_applicable",
}
SECTION_EMPTY_KEY: Dict[str, str] = {
    "critical_issues": "section.critical_issues.empty",
    "warnings": "section.warnings.empty",
    "setup": "section.setup.empty",
    "opportunities": "section.opportunities.empty",
}

_FORMATTER = string.Formatter()


def required_params(key: str) -> Tuple[str, ...]:
    """Parametres que le gabarit EXIGE, lus dans le gabarit lui-meme.

    Une liste tenue a la main a cote du gabarit divergerait: c'est le gabarit qui decide.
    """
    if key not in MESSAGES:
        raise MessageError(f"cle de message inconnue: {key!r}")
    names = []
    for _literal, field_name, _spec, _conversion in _FORMATTER.parse(MESSAGES[key]):
        if field_name and field_name not in names:
            names.append(field_name)
    return tuple(names)


def render(key: str, params: Optional[Mapping[str, Any]] = None) -> str:
    """Rend un message. Aucun calcul, aucun repli.

    Les parametres EXCEDENTAIRES sont admis: ils sont de la donnee de contrat (`factor`,
    `metric_key`) que le rapport publie a cote du texte, et qu'un gabarit n'a pas a citer.
    Un parametre MANQUANT, lui, leve: un texte a trou ne doit jamais circuler.
    """
    if key not in MESSAGES:
        raise MessageError(f"cle de message inconnue: {key!r}")
    values = dict(params or {})
    missing = [name for name in required_params(key) if name not in values]
    if missing:
        raise MessageError(f"{key}: parametre(s) manquant(s) {', '.join(sorted(missing))}")
    try:
        return MESSAGES[key].format(**values)
    except (ValueError, TypeError) as error:  # format incompatible avec la valeur fournie
        raise MessageError(f"{key}: parametre invalide ({error})") from error


__all__ = ["CONFIDENCE_KEY", "CRITERION_STATE_KEY", "DATA_QUALITY_KEY", "FACTOR_HYPOTHESIS_KEY",
           "FACTOR_LABEL_KEY", "FACTOR_RECOMMENDATION_KEY", "MESSAGES", "Message", "MessageError",
           "SECTION_EMPTY_KEY", "SEVERITY_KEY", "render", "required_params"]
