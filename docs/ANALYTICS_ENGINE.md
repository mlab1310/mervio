# ANALYTICS ENGINE — méthodes et seuils

Tous les seuils vivent dans `src/mervio/config.py`.

La **forme** du rapport produit par ce moteur est un contrat à part :
[`REPORT_CONTRACT.md`](REPORT_CONTRACT.md), version **2.0**, définie formellement par
[`schemas/report/2.0.json`](../schemas/report/2.0.json). Ce document-ci décrit les **méthodes** ;
celui-là décrit ce qui sort.

## 1. KPI

Les **dix-sept** KPI du contrat : CA, commandes, panier moyen, unités, remboursements, taux de
remboursement, dépense pub, **clics payants**, **commandes par clic payé**, nouveaux clients,
CAC, ROAS, COGS, marge brute, frais de paiement, taux de réachat, concentration top 5.

`orders_per_paid_click` s'appelait `conversion_rate` et `paid_clicks` s'appelait `clicks` : le
nom disait « taux de conversion » là où la mesure est « commandes **totales** / clics
**payants** », qui baisse aussi quand le trafic organique chute (AN-02).

Chaque `Metric` porte `definition`, `formula`, `sources`, `period`,
`data_quality`, `notes`.

### Chiffre d'affaires et signaux

- CA avant ajustements (libellé D-048) = Σ `Order.subtotal`, sous-total
  **après** remises (D-041) : ni les remboursements, ni les annulations, ni les
  modifications ne le réduisent (D-044). Formule proche du numérateur du panier
  moyen Shopify (analogie documentaire, pas identité vérifiée) ; Mervio ne
  cherche pas à reproduire les *net sales* Shopify, qui peuvent différer. Le KPI
  porte cette définition en note jusqu'au contexte LLM. Aucune soustraction de
  remise dans l'analytique ; un test structurel l'interdit.
- Contrôle **partiel** du contrat du Subtotal (D-046) : contradiction ⇒ KPI
  `revenue` `incomplete` avec note ; commandes non vérifiables (Total absent,
  non reconstructible, compatible avec les deux lectures) ⇒ note, qualité
  inchangée.
- Un CA négatif ne provient que de montants source négatifs : il est conservé,
  jamais ramené à zéro. KPI `revenue` marqué `incomplete` avec une note,
  avertissements `negative_subtotal` et `negative_period_revenue` (D-043).
- Commandes annulées (`Cancelled at` renseigné, indépendamment du statut
  financier), non encaissées (`pending`, `authorized`, `partially_paid`,
  `voided`, `expired`), à `Subtotal` nul et issues d'un brouillon : **comptées**
  dans commandes, CA et panier moyen (D-044), et signalées avec leur nombre et
  leur montant (`cancelled_orders_counted`, `unsettled_orders_counted`,
  `zero_value_orders_counted`, `draft_orders_counted`). Le signal d'annulation
  isole les commandes annulées à montant positif et sans aucun remboursement :
  une cause possible, parmi d'autres, d'écart avec les *net sales* Shopify.

### Remboursements (D-045)

| KPI | Formule | Date | Base |
|---|---|---|---|
| `refunds` | Σ `Refunded Amount` (Shopify) | création de la commande (cohorte) | montant facturé, port et taxes éventuels compris |
| `refund_rate` | `refunds / Σ order.total` | idem | idem ; `incomplete` si `missing_order_total`, `unavailable` si Σ Total = 0 |

L'export ne date pas les remboursements : un KPI mensuel se lit
« remboursements des commandes du mois », pas « remboursements traités dans le
mois ». Aucun remboursement n'est déduit du CA.

### Zéro vs inconnu

| Situation | Valeur | Raison |
|---|---|---|
| Dépense pub connue, 0 commande | ROAS = `0.0` | fait observé |
| Aucune donnée publicitaire | ROAS / CAC = `None` | rien d'observé |
| Coût produit absent | COGS = `None` | ne jamais supposer 0 |

## 2. Profitabilité

```
CA avant ajustements − COGS − frais de paiement − publicité
  − part produit des remboursements − transport = profit de contribution
```

Une composante manquante ⇒ `data_available = false` et
`contribution_profit = null`. Un `partial_contribution_profit` est fourni avec
la liste explicite des coûts inclus et exclus, et seulement si au moins un coût
est connu.

Les remboursements entrent sur la **base du CA** (D-047), jamais pour leur
montant port et taxes compris : part produit calculée quand l'arithmétique la
détermine (aucun remboursement, remboursement intégral, commande sans port ni
taxes, Subtotal nul), sinon composante indisponible avec ses bornes en note.
`refunds` est alors une composante critique : la dimension profitabilité du
score est exclue, comme sans COGS.

Le coût de transport réel est **toujours** manquant depuis un export Shopify :
l'export contient le port *facturé au client*, pas le coût transporteur.

## 3. Temps

Semaines ISO (lundi→dimanche) ou mois. Seules les périodes **closes** sont
analysées. Une comparaison indisponible porte sa raison, jamais un zéro.
YoY nécessite ≥ 12 mois d'historique.

## 4. Anomalies

Condition : `|variation| >= 8 %` (matérialité) **ET**
(`|variation| >= 15 %` **OU** `|z| >= 2.0`).

Le plancher de matérialité est **nécessaire**. Sur une baseline très stable,
un écart de 4 % peut donner z = 3 sans aucune portée business : le z-score peut
*aggraver* une anomalie, jamais en *créer* une sous le plancher.

Baseline : moyenne glissante des 8 périodes précédentes, z-score à partir de 4.
Sévérité : `high` si `|var| >= 30 %` ou `|z| >= 3`, sinon `medium`.

Chaque anomalie porte un `assessment` (`favorable` / `unfavorable` / `neutral`) : une
**baisse** des remboursements est classée en opportunité, pas en alerte. Elle porte aussi le
critère **effectivement appliqué** (D-007), non plus comme phrase assemblée mais comme
`criterion_key` + `criterion_params` (AN-19) : le seuil de z-score n'est publié **que** s'il a
servi à la règle — le publier sans cela laissait croire qu'il s'était appliqué.

## 5. Root Cause — deux couches (AN-01, AN-02)

```
couche A   CA = Commandes × Panier moyen                        additive, somme des parts = 1
couche B   Commandes = Clics payants × Commandes par clic payé  explications, NON additives
contribution(f) = ln(f1/f0) / ln(CA1/CA0)
```

L'énoncé antérieur — `CA = Clics × Taux de conversion × Panier moyen` — décrivait le modèle à
**trois** facteurs et citait le facteur qu'AN-02 a supprimé comme trompeur. Une `contribution`
n'existe qu'en couche A : c'est une part de la variation du **CA**, et la couche B explique la
variation du **volume**. Les mélanger dans un seul champ est le défaut qu'AN-01 supprime.

**La baseline est partagée** (AN-01) : la décomposition se mesure contre la **même** moyenne
glissante que l'anomalie, déclarée dans `baseline_periods` et `baseline_revenue`. La comparaison
à la période précédente reste publiée, étiquetée à part et **sans** contribution. Conséquence
mesurée : sur un déclin prolongé, S/S−1 peut valoir +2 % quand l'écart à la baseline vaut −30 %.

**Les angles morts sont déclarés, pas comblés** (D-073) : `organic_traffic` et
`site_conversion_rate` figurent dans les facteurs avec `observability: not_observed`, **tous**
leurs champs chiffrés à `null` et la source manquante nommée. `contribution: 0.0` affirmerait une
absence d'effet. Les clics ne couvrent que Google Ads ; aucune recommandation « auditer le
checkout » n'est émise sans trafic total observé (D-075).

`confidence` n'est plus un nombre (AN-14 / D-081 option C) : `low` / `medium` / `high`, dérivés
d'un **décompte entier** de quatre critères — longueur de baseline, couverture des champs,
facteur concurrent non observé, part interprétable — tous exposés, `not_applicable` compris.
Aucun coefficient, aucun seuil caché. Le critère de part signale la décomposition **dégénérée** :
au-delà de 100 %, un facteur explique plus que la totalité de la variation et un autre le
compense en sens inverse — la somme reste exacte, la décomposition n'est plus lisible.

## 6. Business Health Score

| Dimension | Poids | Base |
|---|---|---|
| Croissance du CA | 20 % | variation vs période précédente |
| Profitabilité | 25 % | marge de contribution |
| Efficacité marketing | 20 % | ROAS |
| Santé client | 15 % | réachat − pénalité de concentration |
| Santé produit | 10 % | marge brute pondérée |
| Qualité de donnée | 10 % | part des champs fiables |

Chaque dimension : interpolation linéaire entre seuils déclarés (0–100). Les dimensions non
calculables sont **exclues** et listées, les poids renormalisés — jamais approximées. Les six
dimensions sont publiées, exclusions comprises, et le barème appliqué l'est avec elles sous la
forme `[[seuil, score], ...]` (D-094). `data_quality` est la **seule** à ne porter aucun barème :
sa note est une application linéaire de la part de champs fiables, et `HealthConfig` ne définit
aucune échelle pour elle.

**Deux garde-fous contre un score flatteur :**

- Profitabilité : exclue si le **COGS** manque. Noter une « marge » de 80 % qui
  ignore le coût d'achat récompenserait l'absence de donnée. Sur les fixtures,
  ce garde-fou fait passer le score de **78 à 50** — c'est la bonne valeur.
  Même exclusion si la part produit des remboursements n'est pas déterminable
  (D-047).
- Santé produit : exclue si le coût est connu sur moins de 80 % du CA article.

Aucun LLM n'intervient dans ce calcul.

## 7. Insights

`FACT` (données) / `EVIDENCE` (chiffres sourcés) / `HYPOTHESIS` (plausible, non
prouvée) / `RECOMMENDATION` (action).

`estimated_impact` vaut `null` sauf si chiffrable ; quand il est présent,
`estimated_impact_basis` explique d'où il vient. Aucun impact financier inventé.

### Quatre sections, dans l'ordre de lecture (AN-18 / D-084)

`critical_issues` → `warnings` → `setup` → `opportunities`. Le rapport publie **une liste par
catégorie** ; l'ordre des clés est l'ordre de lecture, et il survit à la sérialisation.

`setup` est la nouveauté : une donnée **à brancher**, pas un problème détecté.
`profitability_unavailable` y est passé de `medium`/`warning` à `info`/`setup` — il était émis
par **construction** (un export Shopify porte le port facturé, jamais le coût transporteur),
donc permanent sur toute boutique alimentée par CSV, donc muet, et il occupait la section où le
marchand cherche ce qui va mal cette semaine.

### Preuve structurée (AN-15 / D-082)

`Insight.evidence`, `Anomaly.evidence` et `DimensionScore.evidence` ne portent plus de français
préformaté mais des faits typés : `fact_id`, `metric_key`, `source`, `observability`, `period`,
`comparison`, `unit`, valeurs et deltas. Le cas le plus parlant était l'anomalie : elle portait
**déjà** `observed_value`, `expected_value`, `delta`, `delta_pct` et `z_score` en champs typés, et
les reformulait en prose. Chaque constat porte en plus un `finding_id` stable — il n'existait que
dans `llm/context.py`, frappé localement : le consommateur fabriquait l'identité de ce qu'il
consommait.

### Textes : un catalogue, pas de la prose (AN-19 / D-085)

Un constat désigne un message de `analytics/messages.py` et fournit ses paramètres :
`<nom>` (texte rendu), `<nom>_key`, `<nom>_params`. Le catalogue est une **littérale** — aucune
entrée calculée, aucune clé construite depuis une donnée, aucun repli : une clé inconnue ou un
paramètre manquant lève. Le texte est dérivé, donc il ne peut pas contredire sa clé. Les clés
`evidence.*` restent des identifiants purs, **sans** entrée de catalogue (D-2).

## 8. Fraîcheur de la donnée (AN-05 / D-079)

Le bloc `data_freshness` porte la dernière donnée, la date d'analyse, le retard en jours, le
seuil appliqué, le statut (`fresh` / `stale` / `unknown`) et le détail **par source**. Choisir
quelle période analyser et mesurer le retard sont deux questions **disjointes** : un export
arrêté 40 jours plus tôt produit une période parfaitement close, analysée, et pourtant périmée.
Un rapport `stale` le dit **en tête** du résumé — un lecteur qui s'arrête à la première phrase
doit savoir que les chiffres qui suivent sont vieux. `analysis_date` est le paramètre `today`,
jamais l'horloge murale.

## 9. Périodes et fuseau (AN-06)

Les instants restent stockés en UTC ; le **découpage** se fait dans le fuseau de configuration
(défaut `UTC`, qui reproduit exactement le comportement antérieur). Les confondre est le défaut
AN-06 : en UTC strict, une commande passée dimanche à 23 h 30 UTC tombe dans la semaine
**suivante** du marchand parisien, parce qu'il y est déjà lundi. Conséquence à asserter et non à
corriger : une semaine de changement d'heure fait 167 ou 169 heures.
