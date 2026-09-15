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

**CA net** = `subtotal - discount`.
Exclut frais de port et taxes. Le port facturé n'est pas du CA produit ; la TVA
n'appartient pas à l'entreprise. Toute comparaison à Shopify doit utiliser la
même définition.

**`unit_cogs` est `Optional`.** Un produit sans coût renseigné a `None`, jamais
`0.0`. Cette distinction est la raison pour laquelle le moteur peut dire
« marge non calculable » au lieu d'afficher une marge de 100 %.

**`customer_first_order`** est calculé sur l'historique **complet** et conservé
lors des découpages temporels. Sans cela, tout client d'une fenêtre semblerait
« nouveau » et le CAC serait faux.

**Remboursements** : Shopify fait foi (rattachés à la commande). Les
remboursements Stripe sont conservés séparément ; un écart > 1 % est signalé
dans `data_quality.issues` plutôt que corrigé en silence.

## Ajouter une source

1. Écrire `ingestion/<source>.py` produisant les entités ci-dessus.
2. Déclarer sa couverture via `quality.set_field(...)`.
3. La brancher dans `pipeline.load_dataset()`.

Aucun fichier de `analytics/` ne doit être modifié.
