# Mission 001.6 — Test externe de robustesse (dataset Kaggle)

**Date :** 14 septembre 2026
**Dataset :** `shopify_sales_dataset_ml_eda.csv` (6 445 227 octets, md5 `093f8de2c93d2f8a56b6f8609ba33092`)
**Source :** https://www.kaggle.com/datasets/aliiihussain/shopify-sales-dataset-for-ml-and-eda

> **Ce n'est pas le premier test sur données réelles d'un marchand Shopify.**
> C'est un test de robustesse face à un format externe. Le dataset est
> synthétique : ses colonnes dérivées sont exactes au centième près, ce qu'aucun
> export réel ne fait jamais.

---

## 1. Inspection

| Propriété | Valeur |
|---|---|
| Lignes | **60 000** |
| Colonnes | **17** |
| Séparateur | `,` |
| Encodage | UTF-8 |
| Période | **2023-01-01 → 2025-06-18** (899 jours, 900 dates distinctes) |
| Valeurs manquantes | **0** sur 1 020 000 cellules |
| Lignes en double | **0** |
| Devise | **aucune colonne devise** |
| Granularité | **une ligne = une commande**, `order_id` unique 60 000 / 60 000 |
| Produits par commande | exactement **1** (min = max = 1) |

Cardinalités (fichier entier) : `customer_id` 31 154 · `product_id` 6 998 ·
`product_category` 7 · `customer_country` 7 · `traffic_source` 5 ·
`payment_method` 5 · `discount_percent` 8 · `quantity` 5 (1 à 5).

Colonne potentiellement personnelle : `customer_id` (pseudonyme numérique).
Aucun secret détecté (carte, IBAN, clé API).

## 2. Mapping vers le connecteur Shopify

**Zéro colonne en commun avec un export Shopify natif.** Les quatre colonnes
obligatoires (`Name`, `Created at`, `Lineitem quantity`, `Lineitem price`) sont
toutes absentes. Aucun champ n'est donc en catégorie 1.

| Colonne dataset | Champ Mervio | Catégorie | Remarque |
|---|---|---|---|
| `order_id` | `Name` | 2 — après transformation | renommage simple |
| `order_date` | `Created at` | 2 | date pure, sans heure ni fuseau |
| `customer_id` | clé client | 2 | Mervio utilise l'email comme clé ; un pseudonyme convient |
| `product_id` | `Lineitem sku` | 2 | |
| `quantity` | `Lineitem quantity` | 2 | renommage simple |
| `product_price` | `Lineitem price` | 2 | prix catalogue, avant remise |
| `discount_percent` | `Discount Amount` | 2 | dérivable : `product_price × quantity × pct/100` |
| `discounted_price` | — | 4 — **ambigu** | redondant avec `product_price` + `discount_percent` ; choisir la mauvaise colonne change le CA |
| `revenue` | `Subtotal` | 4 — **ambigu** | précalculé ; à recalculer plutôt qu'à importer |
| `shipping_cost` | `Shipping` | 4 — **dangereux** | `Shipping` côté Shopify = port **facturé au client** (recette). Ici c'est un **coût**. Mapper l'un dans l'autre inverse le signe économique. |
| `is_returned` | `Refunded Amount` | 4 — **dangereux** | booléen, pas un montant. Mapper supposerait un remboursement intégral. |
| `profit` | `contribution_profit` | 4 — **interdit** | voir section 3 |
| `product_category` | — | 3 — absent du modèle | |
| `customer_country` | — | 3 | |
| `traffic_source` | — | 3 | attribution par commande ; sans rapport avec les clics Google Ads |
| `payment_method` | — | 3 | aucun **montant** de frais |
| `rating` | — | 3 | |

### Formules internes vérifiées empiriquement (60 000 lignes)

| Hypothèse | Vérifiée sur | Écart max |
|---|---|---|
| `discounted_price = product_price × (1 − discount_percent/100)` | **100,00 %** | 0,0050 |
| `revenue = quantity × discounted_price` | **100,00 %** | 0,0000 |
| `profit = revenue − shipping_cost` | **100,00 %** | 0,0000 |

## 3. Point critique : la colonne `profit`

Votre hypothèse est **confirmée à 100 %, écart maximal 0,0000 €** :

```
profit = revenue − shipping_cost
```

Cette définition ne déduit **ni COGS, ni frais de paiement, ni publicité, ni
remboursements**. La définition Mervio en déduit les cinq (`REQUIRED_COMPONENTS`).

**Conséquence chiffrée si l'on mappait `profit` :**

| | |
|---|---|
| CA total du dataset | 59 645 935,58 |
| Marge portée par la colonne `profit` | **98,64 %** |
| Score de la dimension profitabilité | **100/100** (seuil Mervio : ≥ 25 % → 100) |

Le Business Health Score serait gonflé par une « marge » qui ignore le coût
d'achat. C'est exactement le défaut corrigé en mission 001 (D-008), qui avait
fait passer le score de nos fixtures de 78 à 50. **`profit` ne doit jamais
alimenter la profitabilité Mervio.** Test de régression : `test_external_schema_rejection.py`.

## 4. Compatibilité des dimensions

| Dimension | Statut | Raison |
|---|---|---|
| Revenue | **PARTIAL** | recalculable via `quantity × discounted_price` (vérifié 100 %), mais hors taxes et sans devise |
| Orders | **SUPPORTED** | `order_id` unique, 60 000 commandes |
| AOV | **SUPPORTED** | 994,10 (CA / commandes) |
| Refund / return rate | **PARTIAL** | taux de retour en **nombre** : 14,81 %. Mervio mesure un taux en **euros**. Le CA et le profit ne sont pas réduits pour les commandes retournées : impact financier nul dans le fichier. Métrique différente, pas équivalente. |
| Customer analysis | **PARTIAL** | 31 154 clients, 57,2 % avec plus d'une commande. Pas d'acquisition (aucun coût média) donc pas de CAC. |
| Product analysis | **PARTIAL** | CA et volumes par produit possibles ; **aucune marge** faute de COGS → dimension exclue (D-009) |
| Country analysis | **UNSUPPORTED** | aucune entité pays dans le modèle normalisé |
| Traffic-source analysis | **UNSUPPORTED** | aucune entité source de trafic ; l'attribution par commande n'est pas le modèle Campaign de Mervio |
| Discount analysis | **PARTIAL** | `discount_percent` exploitable (8 paliers) ; Mervio porte la remise au niveau commande |
| Shipping analysis | **UNSUPPORTED** | Mervio n'a pas d'analyse transport. `shipping_cost` comblerait la composante manquante de la profitabilité, mais le COGS reste absent : sans lui, rien ne se débloque. |
| Rating / expérience client | **UNSUPPORTED** | aucune entité satisfaction |

**Business Health Score : 3 dimensions sur 6 calculables.** Les trois exclues
sont profitabilité (25 %), santé produit (10 %) et efficacité marketing (20 %),
soit **55 % du poids total**.

## 5. Résultat validate / analyze

| Commande | Code retour | Résultat |
|---|---|---|
| `validate --file <dataset>` | **2** | aucune signature de colonnes reconnue |
| `validate --shopify-orders <dataset>` | **1** | `Colonnes obligatoires manquantes: Created at, Lineitem price, Lineitem quantity, Name` |
| `analyze --file <dataset>` | **2** | refusé avant analyse |
| `analyze --shopify-orders <dataset>` | **1** | analyse rejetée |

**Aucun répertoire de sortie n'a été créé. Aucun chiffre n'a été produit.**

C'est le résultat attendu et c'est un **succès du test** : confronté à un
fichier plausible portant le mot « shopify » dans son nom, le moteur a refusé
au lieu de deviner.

## 6. Bugs découverts

Aucun dans le moteur analytique. Trois dans l'inspecteur, tous du même genre —
des statistiques calculées sur un échantillon de 2 000 lignes et présentées
comme portant sur le fichier entier — plus une incohérence de détection :

| # | Bug | Effet observé sur les 60 000 lignes |
|---|---|---|
| 1 | Cardinalité calculée sur l'échantillon | `order_id` affichait « 2000 distinct » au lieu de 60 000 ; la granularité était juste par chance |
| 2 | Taux de valeurs manquantes sur l'échantillon | invisible ici (0 manquant), mais faux par construction |
| 3 | Granularité comparée à la taille de l'échantillon | même cause |
| 4 | `customer_id` signalé si texte, pas si numérique | la colonne réelle étant numérique, elle n'était pas signalée comme personnelle |

Corrigés : passage complet sur le fichier pour les comptages (avec plafond de
suivi à 200 000 valeurs distinctes), et hints d'identifiants applicables quel
que soit le type. Les comptages correspondent désormais exactement à une
vérification indépendante sous pandas.

Un cinquième défaut, d'ergonomie : `validate --file` répondait « aucun fichier
fourni » alors qu'un fichier avait bien été donné mais non reconnu. Le message
indique maintenant la vraie cause et oriente vers `inspect`.

## 7. Décision finale

**Format externe non supporté en l'état. Aucune adaptation du connecteur
Shopify.**

Ce dataset n'est pas un export Shopify : c'est une table analytique à plat,
une ligne par commande, un produit par commande, colonnes dérivées
précalculées. Le connecteur Shopify attend une ligne par article avec les
champs de commande sur la première ligne du bloc.

Si ce format devait être supporté un jour, ce serait par un **connecteur
distinct** (option B de D-025), jamais par des alias ajoutés au connecteur
Shopify. Et ce connecteur n'aurait d'intérêt que s'il existait un besoin
client : ici, le dataset ne peut alimenter que 45 % du poids du score.

Ce qui a été gardé de ce test : la garantie de refus est désormais figée par
huit tests de régression construits sur l'en-tête réelle, et l'inspecteur est
fiable sur un fichier de 60 000 lignes.
