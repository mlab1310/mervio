# PROJECT STATE

**Mis à jour :** 14 septembre 2026 — fin de la Mission 001.6
**Version moteur :** 0.1.0 · **Tests :** 222 / 222 · **Dépendances runtime :** 0

## Où en est Mervio

Le moteur analytique (Mission 001) est désormais **utilisable par un humain**.
Un opérateur peut prendre quatre CSV, les valider, lancer l'analyse et repartir
avec trois livrables, sans écrire une ligne de Python.

```
IMPORT → VALIDATION → NORMALISATION → ANALYTICS → KPI → PROFITABILITÉ
      → ANOMALIES → ROOT CAUSE → BUSINESS HEALTH → INSIGHTS → LIVRABLES
```

Trois commandes : `validate`, `analyze`, `demo`.

## Build status

| Couche | Statut |
|---|---|
| Domaine (`domain/`) | 🟢 |
| Ingestion CSV (4 sources) | 🟢 |
| Détection de source, séparateur, encodage | 🟢 |
| Validation de fichier | 🟢 |
| Détection de données sensibles | 🟢 |
| Isolation des fichiers importés | 🟢 |
| Service applicatif (`analyze_dataset`) | 🟢 |
| Analytics Engine (KPI → Health Score) | 🟢 |
| Rapports : JSON, TXT, data_quality | 🟢 |
| CLI : analyze / validate / demo | 🟢 |
| Architecture prête pour une API | 🟢 |
| Interface contexte LLM | 🟡 (contrat posé, aucun appel) |
| Couche LLM / AI Analyst | 🔴 (Mission 002) |
| Rapport PDF | 🔴 |
| FastAPI, PostgreSQL, auth, multi-tenant, frontend, billing | 🔴 (volontaire) |
| CI/CD | 🔴 |

## Ce que la Mission 001.5 a ajouté

- **Couche applicative** : `AnalysisRequest` → `analyze_dataset()` →
  `AnalysisResult`. C'est exactement ce qu'un handler FastAPI appellera.
  Le service ne lève jamais : il rapporte un statut et un motif.
- **Validation réelle** : la validation exécute le vrai connecteur. Statut,
  lignes lues / acceptées / rejetées, période, devise, séparateur, incidents.
- **Robustesse des exports réels** : séparateurs `,` `;` tab `|` détectés,
  encodages UTF-8 / cp1252 / latin-1, ligne corrompue rejetée sans faire
  échouer le fichier.
- **Sécurité** : détection de cartes (Luhn), IBAN, clés API et colonnes
  suspectes — colonne et nombre d'occurrences uniquement, jamais la valeur.
  Messages d'erreur expurgés. `data/uploads/` isolé et ignoré par Git.
- **Incohérence de devise** traitée comme une erreur, sans conversion silencieuse.
- **Pseudonymisation** des identifiants clients dans les livrables (D-023) :
  `report.json` exposait les emails en clair, découvert en vérifiant les sorties.
- **Rapport dirigeant** en 15 sections, dont Revenue Analysis et une section
  de traçabilité formule → sources pour chaque indicateur.
- **Réorganisation** `domain/ · ingestion/ · analytics/ · application/ ·
  reporting/ · cli/`, sans modifier une seule règle de calcul.

## Ce qui n'a pas changé

Aucune formule, aucun seuil, aucune décision de la Mission 001 n'a été touché.
Les 132 tests d'origine passent toujours ; le seul modifié est le test CLI,
`--out` désignant désormais un répertoire (D-021).

## Mission 001.6 — test externe de robustesse

Dataset Kaggle `shopify_sales_dataset_ml_eda.csv` reçu et analysé :
**60 000 lignes, 17 colonnes, 2023-01-01 → 2025-06-18, 0 valeur manquante,
0 doublon, aucune devise.** Granularité : une ligne = une commande, un produit
par commande.

**Résultat : format non supporté, et c'est un succès du test.** Zéro colonne en
commun avec un export Shopify natif ; `validate` et `analyze` ont refusé sans
produire le moindre chiffre. Le connecteur Shopify et l'Analytics Engine sont
inchangés.

Vérifié empiriquement sur les 60 000 lignes : `profit = revenue − shipping_cost`
(100 %, écart max 0,0000 €). Cette colonne ignore COGS, frais de paiement,
publicité et remboursements — la mapper afficherait une marge de **98,64 %** et
un score de profitabilité de **100/100**. Elle reste interdite.

Quatre bugs corrigés dans l'**inspecteur** (aucun dans le moteur) : cardinalité,
valeurs manquantes et granularité étaient calculées sur un échantillon de 2 000
lignes puis présentées comme portant sur le fichier entier ; `customer_id`
n'était signalé comme personnel que s'il était textuel. Plus un message CLI
trompeur. Treize tests ajoutés.

Détail : `docs/REAL_DATA_COMPATIBILITY_001_6.md`.

## Limites assumées

- **Le moteur n'a jamais vu les données d'un vrai marchand.** La mission 001.6
  a confronté Mervio à un dataset externe de 60 000 lignes, mais synthétique :
  colonnes dérivées exactes au centième, zéro valeur manquante, zéro doublon.
  Aucun export réel ne ressemble à cela. La limite reste entière.
  Procédure dans `docs/REAL_DATA_TEST.md`.
- Dépense publicitaire = Google Ads uniquement.
- Trafic approximé par les clics payants.
- Coût de transport réel indisponible depuis Shopify.
- Une seule devise par analyse ; aucune conversion.
- Taux de réachat intra-période. Pas de saisonnalité.

## Prochaine étape

1. Passer un **vrai** export Shopify dans `validate` puis `analyze`, et écrire
   un test de régression pour chaque écart rencontré.
2. Mission 002 — couche LLM au-dessus de `build_llm_context()`, avec dataset
   d'évaluation anti-hallucination.
