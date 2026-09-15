# TODO

## P0 — bloquant pour vendre
- [ ] Couche LLM : contexte → explication (mission 002)
- [ ] Export PDF du Business Health Report
- [ ] Tester le moteur sur un **vrai** export Shopify d'une boutique réelle
      (les fixtures sont synthétiques : les vrais exports auront des surprises)

## P1 — important MVP
- [ ] Dataset d'évaluation IA (situations connues → raisonnement attendu)
- [ ] Support multi-devises (aujourd'hui : devise unique par dataset)
- [ ] Connecteur Meta Ads (la dépense pub est structurellement incomplète)
- [ ] Attribution par campagne (le ROAS actuel est global, non attribué)
- [ ] Taux de réachat sur historique complet, pas intra-période
- [ ] Git + CI (pytest à chaque push)

## P2 — non bloquant
- [ ] Grain journalier
- [ ] Détection de saisonnalité (12 semaines ne suffisent pas)
- [ ] Cohortes et LTV
- [ ] Coût de transport réel (source à identifier : transporteur ou saisie manuelle)
- [ ] Décimal pour les montants si besoin comptable

## P3 — plus tard
- [ ] ClickHouse si le volume le justifie
- [ ] Change-point detection
- [ ] Forecasting

## Dette technique connue
- `Dataset.window()` est en O(n) par période : 12 périodes = 12 balayages.
  Sans impact aux volumes actuels, à indexer avant la production.
- `report.py` assemble un dict imbriqué sans schéma formel : passer à Pydantic
  avant d'exposer une API publique.
