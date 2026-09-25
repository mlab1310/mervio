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

**Identité client (004.4.2, D-053)** : `Order.customer_id` est une référence **à clé**, jamais un
e-mail : `c1:` + 128 bits de HMAC-SHA256 de l'e-mail normalisé (minuscules, sans espaces), ou
`g1:` + HMAC de l'identifiant de commande pour un invité (un client par commande, comme avant).
La clé est celle de l'organisation (chemin SaaS, `mervio.persistence.identity_keys`) ou une clé
explicite / éphémère (CLI locale, `--identity-key-file`). L'e-mail est lu par le connecteur Shopify,
transformé, puis oublié ; `Order`, `Payment` et `Customer` ne portent plus aucun e-mail.

**Objets bruts (004.4.4, D-054)** : `raw_objects` porte les octets déposés avant import —
organisation, boutique, clé d'objet générée côté serveur (`org/<org>/store/<store>/raw/<uuid>`),
`sha256`, `byte_size`, `source_kind`, `origin` (`csv_upload`), `state`, `retain_until`, `purged_at`.
C'est **cette ligne, sous RLS**, qui porte la frontière de tenant : ni la clé, ni le magasin. Un
objet d'une autre organisation est donc introuvable, exactement comme un objet qui n'a jamais
existé. Aucun nom de fichier n'y figure. La ligne naît `available` ; la base n'accorde ni `UPDATE`
ni `DELETE` au rôle applicatif, donc `sha256` est figé dès l'insertion. Le domaine complet
`pending → available → purging → purged` est fixé par la contrainte. Depuis 004.4.5 (révision
`0014`), la transition `available → purging` est faite **dans la transaction de l'effacement**, par
la fonction privilégiée, et accompagnée d'un `purge_reason` (`customer_erasure`) qu'une contrainte
lie à l'état. Depuis E4 (révision `0015`), `purging → purged` est faite par une seconde fonction
privilégiée, **après** que les octets ont réellement été détruits — jamais avant : une ligne
`purged` affirme une destruction, et cette affirmation doit être vraie. Seuls `state` et
`purged_at` changent ; l'empreinte, la taille, le type, la clé et les dates restent, comme D-056
l'exige.

**Il n'existe aucune transaction commune à PostgreSQL et au magasin d'objets** (système de
fichiers ou S3), et rien n'en simule une. La sûreté vient de l'**ordre** et de l'**idempotence** :
`ObjectStore.delete` ne se plaint pas d'une clé déjà absente, donc une reprise redétruit puis
finalise. La seule fenêtre d'interruption — octets détruits, ligne encore `purging` — laisse
l'objet **déjà illisible et déjà vide** ; c'est le sens sûr de la fenêtre, et c'est pourquoi
l'ordre inverse est interdit. Un `delete` en échec laisse la ligne `purging` : illisible, jamais
déclarée détruite, et reprenable.

**Effacements client (004.4.5, D-056, révision `0014`)** : `customer_redactions` est la **preuve**
qu'un client a été effacé. Elle ne contient **aucune donnée personnelle** : le `customer_ref`
effacé (un HMAC, jamais un e-mail — la contrainte refuse toute autre forme), le tombstone qui l'a
remplacé (`redacted:<uuid4>`, **aléatoire**, jamais dérivé de l'identité, **distinct par client**
pour ne pas fusionner deux clients dans les agrégats), l'origine, le demandeur, le travail et la
date. `UNIQUE (organization_id, customer_ref)` porte l'idempotence : rejouer un effacement
retrouve le même tombstone au lieu d'en créer un second. Le rôle applicatif n'a que `SELECT` :
la table n'est écrite que par `app_redact_customer`, la fonction `SECURITY DEFINER` étroite de
D-052, déclenchée par un travail `redact_customer` et par rien d'autre.

## Définitions qui engagent les chiffres

**CA avant ajustements** (D-048, anciennement « CA net ») = `Order.subtotal` (D-041).
Dans le modèle normalisé, `subtotal` est le sous-total des articles **après**
remises de commande, avant port et taxes ; `discount` est la remise **déjà
déduite**, gardée à titre informatif et jamais soustraite une seconde fois.
Exclut frais de port et taxes. Le port facturé n'est pas du CA produit ; la TVA
n'appartient pas à l'entreprise. Toute comparaison à Shopify doit utiliser la
même définition.

Chaque connecteur livre cette forme canonique :

| Couche | Contenu |
|---|---|
| Sémantique source Shopify (contrat retenu) | `Subtotal` de l'export commandes = somme des articles après remises de commande, avant port et taxes ; `Total` = `Subtotal + Shipping + Taxes` (taxes éventuellement incluses dans les prix) |
| Normalisation Mervio | `subtotal` ← `Subtotal` sans conversion ; `discount` ← `Discount Amount` ; contrôle arithmétique **partiel** du contrat sur le `Total` : `subtotal_convention_contradiction` (erreur, CA `incomplete`) si un fichier le contredit, `subtotal_contract_unverified` (avertissement, note sur le CA) si des commandes ne permettent pas de conclure (D-046) |
| Définition analytique | CA avant ajustements = Σ `subtotal` ; panier moyen = CA avant ajustements / commandes |
| Niveau de preuve | remise à 100 % : prouvée sur un registre réel déposé publiquement (OH5, 30/30 commandes) ; remise partielle : contrat documentaire (API Admin Shopify), **non observée** — aucun export natif public trouvé (D-046) |

**Avant ajustements.** `Subtotal` n'est pas réduit par un remboursement ni par
une annulation (OH5 : les commandes remboursées gardent leur Subtotal ; API :
`subtotalPriceSet` « before returns »). Le CA Mervio suit le contrat Mervio
ci-dessus. Sa formule est proche du numérateur du panier moyen Shopify
(*gross sales − discounts*) — analogie documentaire, pas identité vérifiée.
Mervio **ne cherche pas à reproduire** les *net sales* Shopify : elles peuvent
différer, notamment par les retours et remboursements, les annulations, les
modifications de commande et d'autres règles ou exclusions propres aux
rapports Shopify (D-044, D-048).

**Périmètre des commandes** (D-044) : toute commande de l'export compte dans
les commandes, le CA et le panier moyen — annulée, non encaissée, issue d'un
brouillon ou à montant nul. Ce choix s'inspire du périmètre publié pour les
rapports Shopify ; les commandes de test et les ventes de cartes cadeaux, que
ces rapports excluent, n'ont pas été vérifiées dans l'export. `Financial Status
= paid` n'est pas une preuve d'encaissement : une commande à 0,00 peut être
« payée » sans transaction.

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
| Profit partiel | part produit du remboursement (base CA), calculée seulement quand l'arithmétique la détermine ; sinon composante indisponible, bornes en note (D-047) |

## Ajouter une source

1. Écrire `ingestion/<source>.py` produisant les entités ci-dessus.
2. Déclarer sa couverture via `quality.set_field(...)`.
3. La brancher dans `pipeline.load_dataset()`.

Aucun fichier de `analytics/` ne doit être modifié.
