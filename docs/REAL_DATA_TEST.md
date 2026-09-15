# Tester Mervio avec de vraies données

> **Le moteur n'a encore jamais été exécuté sur un jeu de données réel.**
> Tout ce qui a été validé à ce jour l'a été sur des fixtures synthétiques
> (`data/sample/`). Ce document décrit la procédure ; il ne constitue pas une
> preuve que Mervio fonctionne sur des données réelles.

## 1. Quels exports demander au commerçant

| Source | Chemin dans l'outil | Obligatoire |
|---|---|---|
| **Shopify — commandes** | Admin → Orders → Export → *Orders (all)* → CSV | **Oui** |
| **Shopify — produits** | Admin → Products → Export → CSV | Non, mais sans lui aucune marge |
| **Stripe — paiements** | Dashboard → Payments → Export | Non, mais sans lui aucun frais réel |
| **Google Ads — campagnes** | Campaigns → segmenter par **Jour** → Download CSV | Non, mais sans lui aucun CAC ni ROAS |

Demandez **au moins 12 semaines** d'historique. En dessous, la baseline des
anomalies est trop courte et le YoY est impossible.

## 2. Colonnes nécessaires

**Shopify commandes (obligatoire)** : `Name`, `Created at`, `Lineitem quantity`,
`Lineitem price`.
Fortement recommandées : `Email`, `Currency`, `Subtotal`, `Discount Amount`,
`Shipping`, `Taxes`, `Total`, `Refunded Amount`, `Lineitem sku`, `Lineitem name`.

**Shopify produits** : `SKU`, `Title` obligatoires ; **`Cost per item`** est la
colonne décisive. Sans elle, la profitabilité et la santé produit ne sont pas
notées du tout.

**Stripe** : `id`, `Amount`, `Status` obligatoires ; `Created (UTC)`, `Fee`,
`Net`, `Currency`, `Amount Refunded` recommandées.

**Google Ads** : `Day`, `Campaign`, `Cost` obligatoires ; `Campaign ID`,
`Impressions`, `Clicks`, `Conversions`, `Conv. value` recommandées.

Les colonnes supplémentaires sont ignorées sans erreur. Les séparateurs `,`
`;` `tab` `|` sont détectés automatiquement, ainsi que les encodages UTF-8,
cp1252 et latin-1.

## 3. Anonymiser avant de partager

Mervio n'a besoin d'**aucune** donnée personnelle autre qu'un identifiant
stable de client. Avant d'envoyer un fichier :

- remplacer `Email` par un pseudonyme stable (`client_001`) ; le taux de
  réachat et le CAC continuent de fonctionner, seule l'identité disparaît ;
- supprimer les colonnes d'adresse, téléphone, nom et prénom ;
- supprimer toute colonne de paiement (numéro de carte, IBAN, jeton).

`mervio validate` signale automatiquement les numéros de carte (contrôle de
Luhn), IBAN, clés API et colonnes suspectes. Il indique la **colonne** et le
**nombre d'occurrences**, jamais la valeur.

## 4. Où vivent les données pendant l'analyse

- Les fichiers importés vont dans `data/uploads/<workspace_id>/`, ignoré par
  Git. Un `.gitignore` local y est créé à chaque session.
- `data/sample/` est **réservé aux fixtures synthétiques** ; écrire une donnée
  client à cet endroit lève une `WorkspaceError`.
- Les livrables vont dans le répertoire `--out` (ignoré par Git par défaut).
- Aucune donnée n'est envoyée sur un réseau : le moteur n'a aucune dépendance
  runtime et n'effectue aucun appel sortant.
- `Workspace.cleanup()` supprime les fichiers importés après analyse.

## 5. Lancer l'analyse

```bash
export PYTHONPATH=src

# 1. vérifier les fichiers AVANT toute analyse
python -m mervio.analytics validate \
  --shopify-orders   ~/exports/orders_export.csv \
  --shopify-products ~/exports/products_export.csv \
  --stripe           ~/exports/stripe.csv \
  --google-ads       ~/exports/ads.csv

# 2. lancer l'analyse
python -m mervio.analytics analyze \
  --shopify-orders   ~/exports/orders_export.csv \
  --shopify-products ~/exports/products_export.csv \
  --stripe           ~/exports/stripe.csv \
  --google-ads       ~/exports/ads.csv \
  --out analysis/client_x \
  --label "Client X - semaine 37"
```

Si les sources ne sont pas identifiées, laissez Mervio les détecter :

```bash
python -m mervio.analytics analyze --file a.csv --file b.csv --out analysis/client_x
```

Produit `report.json`, `report.txt` et `data_quality.json`.

## 6. Lire `data_quality.json` avant le rapport

C'est le premier fichier à ouvrir. Ordre de lecture :

1. **`validations[].rejected_rows`** — au-delà de ~2 %, l'export est suspect :
   vérifier avant de commenter le moindre chiffre.
2. **`validations[].sensitive_findings`** — s'il y a quoi que ce soit, demander
   un export nettoyé et supprimer le fichier reçu.
3. **`validations[].currency`** — deux devises différentes ⇒ incident
   `currency_mismatch` de sévérité `error`. Les montants sont agrégés **sans
   conversion** : les totaux sont alors faux et le rapport le déclare. Ne
   présentez pas ces chiffres.
4. **`data_quality.fields`** — `reliable` / `incomplete` / `unavailable` par
   champ. `product_cogs` en `unavailable` signifie qu'aucune marge ne sera
   calculée.
5. **`limitations`** — à lire intégralement avant toute recommandation.

## 7. Limites connues sur données réelles

- **Jamais testé sur un export réel** : c'est la limite principale.
- Dépense publicitaire = Google Ads uniquement ; Meta Ads absent.
- Le trafic est approximé par les clics payants : organique et direct invisibles.
- Le coût de transport réel n'existe pas dans un export Shopify : la
  profitabilité complète reste non calculable sans source supplémentaire.
- Une seule devise par analyse.
- Taux de réachat mesuré à l'intérieur de la période.
- Le format d'export Shopify varie selon l'app d'export utilisée ; un export
  généré par une application tierce peut porter d'autres noms de colonnes.

## 8. Que faire du premier vrai fichier

1. Lancer `validate` et lire chaque incident.
2. Corriger ce qui vient du fichier, corriger **le code** pour ce qui vient du
   moteur, et écrire un test de régression pour chaque cas rencontré.
3. Consigner les écarts de format dans `docs/DECISIONS.md`.
4. Mettre à jour ce document et `PROJECT_STATE.md` avec la date et la
   plateforme du premier test réel.
