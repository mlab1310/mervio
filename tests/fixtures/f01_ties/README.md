# Jeu `f01_ties` — egalites de `conversions_delta` (F-01)

Fixture **versionnee volontairement**, construite pour un seul usage : prouver F-01.

## Pourquoi elle existe

`data/sample` ne contient **aucune egalite** de `conversions_delta` — ses deltas de campagne sont
`-44, -6, -4, +1`. Le test multi-processus de `tests/persistence/test_persistence_reports.py` ne peut
donc pas detecter F-01, et sa docstring le disait. Sans jeu avec egalites, tout test de determinisme
sur ce defaut est **vide de sens** : il passerait avant comme apres la correction.

## Construction — deterministe, sans alea

12 semaines completes, grain hebdomadaire, du **lundi 2026-06-22** au **dimanche 2026-09-13**.
Avec `today=2026-09-14`, la derniere periode complete est `2026-W37` (courante) et `2026-W36` la
precedente.

| Grandeur | Baseline (11 semaines) | Semaine courante (2026-W37) |
|---|---|---|
| Commandes | 38, 41, 39, 42, 40, 41, 39, 40, 41, 39, 40 | **20** |
| Panier moyen | 100,00 EUR constant | 100,00 EUR constant |
| Clics payants | 400 / semaine, plat | 400, plat |

Panier constant et clics plats : les facteurs `average_order_value` et `paid_traffic` restent
neutres, et la cause racine tombe donc sur `conversion_rate`. C'est **necessaire** : c'est le seul
chemin qui traverse `_recommendation_for("conversion_rate")`, l'occurrence la plus grave de F-01,
celle qui fait varier une phrase destinee au marchand. La variation legere des commandes de baseline
evite un ecart-type nul, sans quoi le detecteur d'anomalie n'aurait pas de z-score.

### Les deux egalites, voulues

Conversions Google Ads par campagne :

| Campagne | 2026-W36 | 2026-W37 | `conversions_delta` |
|---|---|---|---|
| `CMP-A` Search - Alpha | 20 | 10 | **−10** |
| `CMP-B` Search - Bravo | 15 | 5 | **−10** |
| `CMP-C` Shopping - Charlie | 12 | 4 | −8 |
| `CMP-D` Display - Delta | 8 | 11 | **+3** |
| `CMP-E` Video - Echo | 6 | 9 | **+3** |

- **Egalite sur le plus negatif** (`CMP-A` / `CMP-B`) : rend `campaign_contributors[0]` ambigu, donc
  le suffixe de la recommandation (`insights.py`, « La degradation est la plus marquee sur … »).
- **Egalite sur le plus positif** (`CMP-D` / `CMP-E`) : rend `max(..., key=conversions_delta)`
  ambigu, donc la campagne citee par le constat `campaign_opportunity`.

## Defaut mesure avant correction

A `7580ad4`, sur cinq graines, l'ordre et la prose variaient :

```
PYTHONHASHSEED=0   ordre [CMP-A, CMP-B, …]   « … la plus marquee sur Search - Alpha. »
PYTHONHASHSEED=1   ordre [CMP-B, CMP-A, …]   « … la plus marquee sur Search - Bravo. »
PYTHONHASHSEED=2   ordre [CMP-A, CMP-B, …]   « … la plus marquee sur Search - Alpha. »
PYTHONHASHSEED=7   ordre [CMP-B, CMP-A, …]   « … la plus marquee sur Search - Bravo. »
PYTHONHASHSEED=42  ordre [CMP-B, CMP-A, …]   « … la plus marquee sur Search - Bravo. »
```

## Ce que la fixture ne couvre pas

Aucune donnee Stripe : la profitabilite complete reste indisponible et le constat
`profitability_unavailable` est emis. C'est sans effet sur F-01, et volontaire — la fixture reste
minimale.

Ces fichiers ne contiennent **aucune donnee reelle** : adresses `clientN@example.com`, SKU `TIE-00x`,
noms de campagne fictifs (D-015, D-046).

## Verification

`tests/test_determinism_f01.py::test_the_fixture_really_contains_tied_conversion_deltas` echoue si
le jeu perd ses egalites. C'est une garde explicite : sans elle, les autres tests du module
deviendraient silencieusement vides de sens.
