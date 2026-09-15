# DATA MODEL

Modèle interne normalisé (`src/mervio/models.py`), indépendant des sources.

## Entités

| Entité | Champs clés | Source |
|---|---|---|
| `Product` | `sku`, `title`, `unit_cogs` (**Optional**) | Shopify products |
| `OrderItem` | `sku`, `quantity`, `unit_price`, `line_revenue` | Shopify orders |
| `Order` | `order_id`, `customer_id`, `created_at`, `subtotal`, `discount`, `shipping`, `tax`, `total`, `items` | Shopify orders |
| `Payment` | `payment_id`, `amount`, `fee`, `net`, `status` | Stripe |
| `Refund` | `refund_id`, `amount`, `order_id`, `source` | Shopify + Stripe |
| `Campaign` | `campaign_id`, `name`, `channel` | Google Ads |
| `DailyAdPerformance` | `day`, `spend`, `impressions`, `clicks`, `conversions`, `conversion_value` | Google Ads |
| `Customer` | dérivée : `orders_count`, `revenue`, `first_order_at` | calculée |
| `Dataset` | conteneur + `quality` + `customer_first_order` | — |

## Définitions qui engagent les chiffres

**CA net** = `Order.subtotal` (D-041).
Dans le modèle normalisé, `subtotal` est le sous-total des articles **après**
remises de commande, avant port et taxes ; `discount` est la remise **déjà
déduite**, gardée à titre informatif et jamais soustraite une seconde fois.
Exclut frais de port et taxes. Le port facturé n'est pas du CA produit ; la TVA
n'appartient pas à l'entreprise. Toute comparaison à Shopify doit utiliser la
même définition.

Chaque connecteur livre cette forme canonique :

| Couche | Contenu |
|---|---|
| Sémantique source Shopify | `Subtotal` de l'export commandes = somme des articles après remises de commande, avant port et taxes ; `Total` = `Subtotal + Shipping + Taxes` (taxes éventuellement incluses dans les prix) |
| Normalisation Mervio | `subtotal` ← `Subtotal` sans conversion ; `discount` ← `Discount Amount` ; contrôle arithmétique du contrat sur le `Total`, erreur `subtotal_convention_contradiction` si un fichier le contredit |
| Définition analytique | CA net = Σ `subtotal` ; panier moyen = CA net / commandes |
| Niveau de preuve | remise à 100 % : prouvée sur un registre réel déposé publiquement (OH5, 30/30 commandes) ; remise partielle : contrat documentaire (API Admin Shopify), non observée sur données réelles |

La valeur des lignes (`OrderItem.line_revenue` = quantité × prix) reste brute :
elle ne porte pas la remise de commande.

**Dates** (D-042). Formats acceptés : ISO 8601 (avec ou sans heure, secondes,
fuseau, normalisé en UTC) et dates à barres `J/M/AAAA` ou `M/J/AAAA` avec heure
optionnelle `H:MM` ou `H:MM:SS`. L'ordre jour/mois d'une date à barres n'est
jamais deviné : chaque connecteur l'établit **par fichier** à partir des seules
valeurs discriminantes (une composante > 12). Colonne sans valeur
discriminante : lignes rejetées (`ambiguous_date_order`). Deux ordres dans le
même fichier : dates à barres rejetées (`date_order_conflict`). Une date à
barres n'a pas de fuseau : elle est traitée comme déjà en UTC. Années à deux
chiffres, `AAAA/MM/JJ` et mois en lettres ne sont pas acceptés.

**`unit_cogs` est `Optional`.** Un produit sans coût renseigné a `None`, jamais
`0.0`. Cette distinction est la raison pour laquelle le moteur peut dire
« marge non calculable » au lieu d'afficher une marge de 100 %.

**`customer_first_order`** est calculé sur l'historique **complet** et conservé
lors des découpages temporels. Sans cela, tout client d'une fenêtre semblerait
« nouveau » et le CAC serait faux.

**Remboursements** : Shopify fait foi (rattachés à la commande). Les
remboursements Stripe sont conservés séparément ; un écart > 1 % est signalé
dans `data_quality.issues` plutôt que corrigé en silence. Un remboursement
n'est jamais déduit du CA : il est rapporté à part. Le `Refunded Amount`
Shopify peut inclure taxes et port alors que le CA ne les inclut pas (observé
sur OH5 : 3 remboursements sur 3 égaux au `Total`) ; le taux de remboursement le
déclare. L'export commandes ne contient pas de date de remboursement : les
remboursements sont datés à la création de la commande (non vérifiable).

## Ajouter une source

1. Écrire `ingestion/<source>.py` produisant les entités ci-dessus.
2. Déclarer sa couverture via `quality.set_field(...)`.
3. La brancher dans `pipeline.load_dataset()`.

Aucun fichier de `analytics/` ne doit être modifié.
