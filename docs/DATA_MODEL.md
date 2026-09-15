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
| Niveau de preuve | remise à 100 % : prouvée sur un registre réel déposé publiquement (OH5, 30/30 commandes) ; remise partielle : contrat documentaire (API Admin Shopify), **non observée** — aucun export natif public trouvé (D-046) |

**Avant ajustements.** `Subtotal` n'est pas réduit par un remboursement ni par
une annulation (OH5 : les commandes remboursées gardent leur Subtotal ; API :
`subtotalPriceSet` « before returns »). Le CA Mervio correspond donc au
numérateur du panier moyen Shopify (*gross sales − discounts*), pas aux
*net sales* Shopify, qui déduisent annulations et retours (D-044).

**Périmètre des commandes** (D-044) : toute commande de l'export compte dans
les commandes, le CA et le panier moyen — annulée, non encaissée, issue d'un
brouillon ou à montant nul. C'est le périmètre publié par Shopify pour ses
rapports. `Financial Status = paid` n'est pas une preuve d'encaissement : une
commande à 0,00 peut être « payée » sans transaction.

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
**Fuseau (limite, Mission 003.3) :** les périodes sont découpées en UTC ;
l'export ne déclare pas le fuseau de la boutique et l'aide Shopify ne le
précise pas. Une commande passée près de minuit peut tomber dans la période
voisine de celle des rapports Shopify. Aucune configuration de fuseau n'existe.

**`unit_cogs` est `Optional`.** Un produit sans coût renseigné a `None`, jamais
`0.0`. Cette distinction est la raison pour laquelle le moteur peut dire
« marge non calculable » au lieu d'afficher une marge de 100 %.

**`customer_first_order`** est calculé sur l'historique **complet** et conservé
lors des découpages temporels. Sans cela, tout client d'une fenêtre semblerait
« nouveau » et le CAC serait faux.

**Remboursements** (D-006, D-045) : Shopify fait foi (rattachés à la commande).
Les remboursements Stripe sont conservés séparément ; un écart > 1 % est signalé
dans `data_quality.issues` plutôt que corrigé en silence. Un remboursement
n'est jamais déduit du CA : il est rapporté à part.

| Élément | Contrat |
|---|---|
| Montant | `Refunded Amount`, cumul de la commande, port et taxes éventuels compris (OH5 : 3/3 égaux au `Total`) |
| Date | création de la commande : l'export ne contient aucune date de remboursement. Lecture en cohorte |
| Taux | `Σ Refunded Amount / Σ Total` des commandes de la période, même devise ; `incomplete` si des `Total` manquent (`missing_order_total`), `unavailable` sans montant facturé |
| Incohérence | remboursement > `Total` : conservé, signalé `refund_exceeds_order_total` |

## Ajouter une source

1. Écrire `ingestion/<source>.py` produisant les entités ci-dessus.
2. Déclarer sa couverture via `quality.set_field(...)`.
3. La brancher dans `pipeline.load_dataset()`.

Aucun fichier de `analytics/` ne doit être modifié.
