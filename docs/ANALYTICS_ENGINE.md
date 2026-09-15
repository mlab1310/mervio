# ANALYTICS ENGINE — méthodes et seuils

Tous les seuils vivent dans `src/mervio/config.py`.

## 1. KPI

CA, commandes, panier moyen, unités, remboursements, taux de remboursement,
dépense pub, clics, taux de conversion payant, nouveaux clients, CAC, ROAS,
COGS, marge brute, frais de paiement, taux de réachat, concentration top 5.

Chaque `Metric` porte `definition`, `formula`, `sources`, `period`,
`data_quality`, `notes`.

### Chiffre d'affaires et signaux

- CA net = Σ `Order.subtotal`, sous-total **après** remises (D-041). Aucune
  soustraction de remise dans l'analytique ; un test structurel l'interdit.
- Un CA négatif ne provient que de montants source négatifs : il est conservé,
  jamais ramené à zéro. KPI `revenue` marqué `incomplete` avec une note,
  avertissements `negative_subtotal` et `negative_period_revenue` (D-043).
- Commandes annulées (`Cancelled at` renseigné, indépendamment du statut
  financier), non encaissées (`pending`, `authorized`, `partially_paid`,
  `voided`, `expired`), à `Subtotal` nul et issues d'un brouillon : **comptées**
  comme toute commande, et signalées avec leur nombre et leur montant
  (`cancelled_orders_counted`, `unsettled_orders_counted`,
  `zero_value_orders_counted`, `draft_orders_counted`). Règle non établie (D-043).

### Zéro vs inconnu

| Situation | Valeur | Raison |
|---|---|---|
| Dépense pub connue, 0 commande | ROAS = `0.0` | fait observé |
| Aucune donnée publicitaire | ROAS / CAC = `None` | rien d'observé |
| Coût produit absent | COGS = `None` | ne jamais supposer 0 |

## 2. Profitabilité

```
CA − COGS − frais de paiement − publicité − remboursements − transport
  = profit de contribution
```

Une composante manquante ⇒ `data_available = false` et
`contribution_profit = null`. Un `partial_contribution_profit` est fourni avec
la liste explicite des coûts inclus et exclus.

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

Chaque anomalie porte son `criterion` en clair et un `assessment`
(`favorable` / `unfavorable` / `neutral`) : une **baisse** des remboursements
est classée en opportunité, pas en alerte.

## 5. Root Cause

```
CA = Clics × Taux de conversion × Panier moyen
contribution(f) = ln(f1/f0) / ln(CA1/CA0)
```

Contributions additives, somme = 1 (testé). Contributions produit et campagne
calculées en additif (`delta_i / delta_total`).

`confidence` est une **heuristique documentée**, pas une probabilité :
`0.35 + 0.5 × |contribution| − pénalité_qualité`, bornée à [0.1, 0.9].

Limite structurelle : les clics ne couvrent que Google Ads. Le trafic organique
et direct n'est pas observé (GA4 non connecté). C'est écrit dans chaque rapport.

## 6. Business Health Score

| Dimension | Poids | Base |
|---|---|---|
| Croissance du CA | 20 % | variation vs période précédente |
| Profitabilité | 25 % | marge de contribution |
| Efficacité marketing | 20 % | ROAS |
| Santé client | 15 % | réachat − pénalité de concentration |
| Santé produit | 10 % | marge brute pondérée |
| Qualité de donnée | 10 % | part des champs fiables |

Chaque dimension : interpolation linéaire entre seuils déclarés (0–100).
Les dimensions non calculables sont **exclues** et les poids renormalisés.

**Deux garde-fous contre un score flatteur :**

- Profitabilité : exclue si le **COGS** manque. Noter une « marge » de 80 % qui
  ignore le coût d'achat récompenserait l'absence de donnée. Sur les fixtures,
  ce garde-fou fait passer le score de **78 à 50** — c'est la bonne valeur.
- Santé produit : exclue si le coût est connu sur moins de 80 % du CA article.

Aucun LLM n'intervient dans ce calcul.

## 7. Insights

`FACT` (données) / `EVIDENCE` (chiffres sourcés) / `HYPOTHESIS` (plausible, non
prouvée) / `RECOMMENDATION` (action).

`estimated_impact` vaut `null` sauf si chiffrable ; quand il est présent,
`estimated_impact_basis` explique d'où il vient. Aucun impact financier inventé.
