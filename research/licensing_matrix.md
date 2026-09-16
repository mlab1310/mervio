# Matrice de licences, propriété intellectuelle et provenance — Mission 004.0

**Règle :**
- une licence non établie avec certitude signifie **aucune réutilisation** : `RESEARCH ONLY — LICENSE UNCLEAR` ;
- aucune donnée externe n'entre en production ;
- tout jeu de benchmark importé porte un manifeste de provenance (source, licence, SHA-256, date, transformations).

Ce document n'est pas un avis juridique. La colonne « commercial » ne reprend **que** ce que la licence ou la
source affirme explicitement.

**Légende :**
- Attrib. = attribution requise ;
- Redistrib. = conditions de redistribution ;
- Modif. = conditions sur les œuvres modifiées.

## 1. Code

| Ressource | Licence code | Licence données | Commercial | Attrib. | Redistrib. | Modif. | Incertitude | Décision Mervio |
|---|---|---|---|---|---|---|---|---|
| ablancogcr/synthetic-dataset-generator | MIT (fichier LICENSE) | CC0 1.0 (échantillon seulement, `LICENSE-DATA`) | code : autorisé par MIT ; données : CC0 l'autorise explicitement | MIT : conserver la notice | MIT : notice | libre | faible | Idée réimplémentée et citée ; aucun code ni donnée repris |
| G-Schumacher44/ecom_sales_data_generator | MIT | aucune donnée publiée | autorisé par MIT | notice | notice | libre | faible | Étude seulement |
| nikbeznosikov/analytics-dashboard-demo | **aucun fichier** ; README dit « MIT » | données synthétiques générées | non établi | — | — | — | **élevée** | **RESEARCH ONLY — LICENSE UNCLEAR** |
| lassiecoder/enterprise-dashboard | **aucune** | aucune | non établi | — | — | — | **élevée** | **RESEARCH ONLY — LICENSE UNCLEAR** |
| Blazity/next-cwv-monitor | MIT | aucune | autorisé par MIT | notice | notice | libre | faible | Patrons d'architecture seulement |
| srujankothuri/shopflow | **aucune** | aucune | non établi | — | — | — | **élevée** | **RESEARCH ONLY — LICENSE UNCLEAR** |
| bhavipopli/open-commerce-ops | MIT | données de démo générées | autorisé par MIT | notice | notice | libre | faible | Patrons seulement |
| jakubczyz06/ecommerce-data-pipeline | MIT | données générées | autorisé par MIT | notice | notice | libre | faible | Étude seulement |
| duckdb, fastapi, alembic, procrastinate, shadcn/ui, prophet, shopify_python_api | MIT | — | autorisé | notice | notice | libre | faible | Candidats de dépendance, à revalider à l'adoption |
| ClickHouse, dlt, opentelemetry-python, great_expectations, pymc-marketing, darts, superset, tremor | Apache-2.0 | — | autorisé | notice + NOTICE | notice + NOTICE | indiquer les changements | faible | Étude ; candidats futurs |
| pyod | BSD-2-Clause | — | autorisé | notice | notice | libre | faible | Étude |
| dramatiq | LGPL-3.0 | — | conditions LGPL | oui | oui | copyleft faible | moyenne | Rejeté (courtier supplémentaire) |
| singer-io/tap-shopify | **AGPL-3.0** | — | copyleft réseau | oui | oui | **copyleft fort, y compris en service** | faible | **Rejeté** pour intégration |
| **psycopg 3.3 + psycopg-binary 3.3** (adopté 004.1) | **LGPL-3.0-only** (métadonnées PyPI) ; la roue binaire embarque libpq (PostgreSQL License) et OpenSSL (Apache-2.0) | — | autorisé sous conditions LGPL : bibliothèque non modifiée, importée dynamiquement, installée comme distribution séparée ; usage serveur (SaaS), aucune redistribution du produit | notice | si Mervio redistribuait un binaire : fournir la licence et permettre le remplacement de la bibliothèque | copyleft faible : toute modification de psycopg elle-même serait LGPL | moyenne | **Adopté** comme driver PostgreSQL (ADR-004.1-001) : **jamais modifié ni vendu, jamais copié dans le dépôt**. Réévaluer si Mervio distribue un jour un logiciel installable (pg8000, BSD-3, en repli documenté) |
| **alembic 1.20** (adopté 004.1) | MIT | — | autorisé | notice | notice | libre | faible | **Adopté** (migrations, ADR-004-002 et ADR-004.1-001) |
| **SQLAlchemy 2.0** (dépendance d'alembic) | MIT | — | autorisé | notice | notice | libre | faible | Transitif ; utilisé **uniquement** par les migrations (test AST) |
| Mako 1.4, MarkupSafe 3.0, typing_extensions 4.16 (transitifs d'alembic / SQLAlchemy / psycopg) | MIT ; BSD-3-Clause ; PSF-2.0 | — | autorisé | notice | notice | libre | faible | Transitifs, non importés par Mervio |
| PostgreSQL 17 (serveur, image `postgres:17`) | PostgreSQL License | — | autorisé | notice | notice | libre | faible | **Adopté** (ADR-004-002) ; service, pas une dépendance Python |
| soda-core, lightdash, cube, metabase, posthog | **NOASSERTION** (API GitHub) | — | non établi ici | — | — | — | **élevée** | **RESEARCH ONLY — LICENSE UNCLEAR** tant que non vérifié |

## 2. Données

| Ressource | Licence | Nature | Commercial | Attrib. | Incertitude | Stockage | Décision Mervio |
|---|---|---|---|---|---|---|---|
| UCI Online Retail II (DOI 10.24432/C5CG6D) | **CC BY 4.0** (page UCI) | réelle : détaillant en ligne britannique, 1 067 371 lignes, 2009-12-01 → 2011-12-09, GBP, annulations (factures `C…`), CustomerID | autorisé par CC BY 4.0 | **oui** : citer Chen, D. (2012), UCI ML Repository | faible | `~/mervio-validation/external_004_0/online_retail_ii.zip`, SHA-256 `572e36277c2390fbfde10664750731e0a86f55e33470d91919085f0408e67bfb`, hors Git | **B** — candidat de benchmark de volume réel ; format XLSX non Shopify : un adaptateur de recherche distinct serait nécessaire (D-025), jamais d'alias dans le connecteur |
| Olist — Brazilian E-Commerce Public Dataset | **CC BY-NC-SA 4.0** (métadonnées Kaggle) | réelle, anonymisée, marketplace | **non** (NC) | oui | faible | non téléchargé | **A** — RESEARCH ONLY : NC incompatible avec un produit commercial |
| RetailRocket recommender dataset | **CC BY-NC-SA 4.0** (métadonnées Kaggle) | réelle, valeurs hachées, événements | **non** (NC) | oui | faible | non téléchargé | **A** — RESEARCH ONLY |
| REES46 eCommerce behavior (multi-category store) | Kaggle : « Data files © Original Authors » ; page REES46 : usage libre avec citation | réelle, événements | **contradictoire** | oui | **élevée** | non téléchargé | **RESEARCH ONLY — LICENSE UNCLEAR** |
| theLook eCommerce (BigQuery `thelook_ecommerce`) | décrit comme synthétique « pour découverte, tests et évaluation » ; aucune licence formelle trouvée | synthétique | non établi | — | **élevée** | non téléchargé | **RESEARCH ONLY — LICENSE UNCLEAR** |
| GA4 obfuscated sample ecommerce (BigQuery) | conditions non trouvées lors de la recherche | réelle obfusquée, Google Merchandise Store, nov. 2020 → janv. 2021 | non établi | — | **élevée** | non téléchargé | **RESEARCH ONLY — LICENSE UNCLEAR** |
| Échantillon ablancogcr (`ecommerce_baseline_10000_seed42`) | CC0 1.0 | synthétique marketplace | autorisé | non requise | faible | clone hors dépôt | **B possible**, inutile tant que le schéma n'est pas Shopify |
| Kaggle « Shopify » (`shopify_sales_dataset_ml_eda`) | non réévalué (Mission 001.6) | synthétique à l'origine | — | — | — | hors dépôt | **E** — format non supporté (D-028) |
| OH5 (TTAB 92078800) | pièce publique déposée | réelle, un marchand, rendu PDF | — | — | contient des PII | `~/mervio-validation/`, hors Git | Validation de sémantique uniquement (003.x), jamais dans Git |
| `mervio.synthetic` (ce dépôt) | code du projet | synthétique, déterministe | propriété Mervio | — | aucune | `data/synthetic/`, `data/benchmark/` ignorés ; `data/scenarios/*.json` versionnés | **Seule source de fixtures versionnées** avec `data/sample/` |

## 3. Contrôles appliqués à Mission 004.0

- Aucun fichier de données externe n'a été ajouté au dépôt (`git status` et contrôle de taille, gate final).
- Les clones externes restent sous `~/mervio-validation/research_004_0/clones/`.
- La seule idée reprise d'un dépôt externe (graine par espace de noms) vient d'un dépôt MIT ; elle est réécrite et
  sa source est citée dans `src/mervio/synthetic/rng.py`.
- Les données générées portent `synthetic: true`, `not_for_production: true`, des emails en
  `@customers.synthetic.invalid` (TLD réservé) et un SHA-256 par fichier.
