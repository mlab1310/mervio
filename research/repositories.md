# Dépôts étudiés — Mission 004.0

**Date :** 15 septembre 2026 · **Méthode :** métadonnées via l'API GitHub, clone `--depth 1` **hors dépôt**
(`~/mervio-validation/research_004_0/clones/`), lecture du README, de la structure, des licences, des tests et
des fichiers clés. Deux démos publiques ont été consultées dans un navigateur (Atlas, Open Commerce Ops).
**Aucune ligne de code externe n'a été copiée dans Mervio.** Les idées retenues ont été réécrites et sont citées.

Le nombre d'étoiles n'est pas un critère de classement : la plupart des dépôts les plus pertinents pour Mervio
ont 0 à 10 étoiles, et plusieurs dépôts très populaires sont hors sujet.

## 1. Grille de notation

Chaque critère est noté de 0 à 3. **Utilité** = utilité pour Mervio, pondérée ×2.

| Dépôt | Pertinence | Qualité code | Récence | Architecture | Tests | Docs | Licence claire | Échelle | UX | Utilité ×2 | Total /33 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Blazity/next-cwv-monitor | 2 | 3 | 3 | 3 | 2 | 3 | 3 | 3 | 3 | 2 | **29** |
| ablancogcr/synthetic-dataset-generator | 3 | 3 | 3 | 2 | 2 | 3 | 3 | 2 | 1 | 2 | **26** |
| bhavipopli/open-commerce-ops | 3 | 2 | 3 | 2 | 0 | 3 | 3 | 1 | 2 | 3 | **25** |
| nikbeznosikov/analytics-dashboard-demo (Atlas) | 2 | 3 | 3 | 2 | 2 | 2 | 0 | 1 | 3 | 2 | **22** |
| G-Schumacher44/ecom_sales_data_generator | 2 | 1 | 2 | 2 | 1 | 2 | 3 | 1 | 0 | 2 | **18** |
| jakubczyz06/ecommerce-data-pipeline | 2 | 1 | 3 | 2 | 0 | 2 | 3 | 1 | 1 | 1 | **17** |
| srujankothuri/shopflow | 2 | 1 | 3 | 1 | 0 | 2 | 0 | 1 | 2 | 2 | **16** |
| lassiecoder/enterprise-dashboard | 1 | 1 | 2 | 1 | 0 | 2 | 0 | 0 | 2 | 0 | **9** |

Décisions possibles : **A** étudier seulement · **B** utiliser comme données de test · **C** adapter des idées ·
**D** réutiliser du code · **E** rejeter.

## 2. Dépôts de départ

### 2.1 ablancogcr/synthetic-dataset-generator
- **URL :** https://github.com/ablancogcr/synthetic-dataset-generator (commit `b7ddadf`, 2026-07-17, 0 étoile)
- **Licence :**
  - code sous MIT (`LICENSE`) ;
  - échantillon de données sous CC0 1.0 (`data/sample_output/LICENSE-DATA`), qui précise ne couvrir que les données.
- **Objet :** générateur Python de datasets e-commerce relationnels de type marketplace (clients, vendeurs, produits,
  commandes, paiements, expéditions, avis). Il propose 4 scénarios et un mode « données sales ».
- **Architecture :**
  - un module par table (`customers.py`, `orders.py`…) ;
  - un rapport `validators.py` de 66 contrôles (schéma, valeurs requises, relations) ;
  - un manifeste `dirty_data_manifest.json` qui donne les compteurs de défauts **sans** identifiant de ligne ;
  - des dépendances numpy, pandas, faker et pydantic ;
  - 42 tests.
- **Motifs utiles :**
  - `stable_seed(root, namespace)` : sha256 → graine par étape, qui rend les étapes indépendantes ;
  - rapport de validation séparé de la génération ;
  - le ZIP n'est produit que si la validation passe ;
  - manifeste de défauts agrégé (bonne pratique de confidentialité).
- **Limites pour Mervio :**
  - schéma marketplace, pas un export Shopify ;
  - les scénarios sont des multiplicateurs sans **attente analytique** (pas de vérité terrain testable) ;
  - dépendances lourdes ;
  - dépôt créé en juillet 2026, sans historique ni communauté.
- **Décision : A + C.** Le motif de graine par espace de noms est réimplémenté en bibliothèque standard
  (`src/mervio/synthetic/rng.py`, source citée). L'échantillon CC0 est utilisable (B), mais ne sert à rien tant
  que son schéma n'est pas Shopify.

### 2.2 G-Schumacher44/ecom_sales_data_generator
- **URL :** https://github.com/G-Schumacher44/ecom_sales_data_generator (`79a44f2`, 2026-01-10) · **Licence :** MIT.
- **Objet :** générateur piloté par YAML pour former au SQL. Il simule paniers et abandons, retours, fidélité
  « méritée » et réactivation, avec un moteur de « messiness ».
- **Motifs utiles :**
  - tunnel paniers → commandes avec abandon ;
  - retours générés en proportion des commandes ;
  - comportement dépendant du canal d'acquisition ;
  - défauts contextuels (raisons de retour biaisées par catégorie).
- **Risques :**
  - `__pycache__` versionnés ;
  - tests dispersés (`src/pytests`, `src/tests`) ;
  - statut « alpha » ;
  - orienté pédagogie SQL.
- **Décision : A + C** : idées de tunnel paniers et de défauts contextuels pour les versions futures du générateur.
  Aucun code repris.

### 2.3 nikbeznosikov/analytics-dashboard-demo (« Atlas »)
- **URL :** https://github.com/nikbeznosikov/analytics-dashboard-demo (`aa6980e`, 2026-05-06) · démo publique consultée.
- **Licence :** **aucun fichier LICENSE** ; GitHub ne détecte aucune licence ; seul le README écrit « MIT ».
  → **RESEARCH ONLY — LICENSE UNCLEAR.**
- **Pile :**
  - Next.js 16 App Router, React 19, TypeScript strict, Tailwind v4 ;
  - Recharts, TanStack Query/Table/Virtual, Radix, nuqs (filtres dans l'URL), zod.
- **Motifs utiles :**
  - route handlers validés par zod, requêtes plafonnées à 1 024 octets ;
  - erreurs `{error, code}` sans trace ;
  - token bucket par IP ;
  - en-têtes de sécurité (CSP, HSTS…) ;
  - filtres synchronisés dans l'URL (lien partageable) ;
  - sous-échantillonnage LTTB déclaré à l'écran (« 31 points → 31 ») ;
  - badge de source « SYNTHETIC » sur chaque carte ;
  - couleur de delta inversée pour le taux de remboursement ;
  - skeletons, `loading.tsx` et `error.tsx` par route ;
  - tests Vitest (49), e2e Playwright avec axe-core ;
  - CI lint + typecheck + tests + e2e.
- **Limites :** lecture seule, données en mémoire, mono-instance, aucune authentification ; KPI sans explication.
- **Décision : A** : patrons UX et API étudiés, aucun code réutilisable faute de licence.

### 2.4 lassiecoder/enterprise-dashboard
- **URL :** https://github.com/lassiecoder/enterprise-dashboard (`5a60014`, 2025-11-24).
- **Licence :** aucune → **RESEARCH ONLY — LICENSE UNCLEAR.**
- **Objet :** gabarit de dashboard Next.js 16 / shadcn (cartes, graphiques, carte Syncfusion, barre latérale droite
  de notifications).
- **Constats :** données statiques, aucun test, aucune API ; dépendance Syncfusion (licence commerciale propre).
- **Décision : A pour la mise en page (barre de notifications), E pour tout le reste.**

### 2.5 Blazity/next-cwv-monitor
- **URL :** https://github.com/Blazity/next-cwv-monitor (`86009a7`, 2026-02-20, 76 étoiles) · **Licence :** MIT.
- **Objet :** RUM Core Web Vitals auto-hébergé pour Next.js (SDK < 5 ko, dashboard, ClickHouse).
- **Architecture :**
  - monorepo `apps/monitor-app`, `apps/client-app` et `packages/client-sdk` ;
  - contrat d'ingestion versionné (`IngestPayloadV1`, paquet `cwv-monitor-contracts`) ;
  - un dossier par cas d'usage côté serveur (`domain/projects/create|list|delete`, `domain/dashboard/regressions`) ;
  - dépôts ClickHouse testés ;
  - migrations SQL numérotées ;
  - ClickHouse : table brute partitionnée par mois, `ORDER BY (project_id, …)` (tenant en tête), TTL de 90 jours,
    agrégats journaliers par vue matérialisée, TTL de 365 jours ;
  - rôles utilisateurs (Better Auth), multi-projets ;
  - 39 fichiers de tests, workflows de release et d'image Docker.
- **Motifs utiles :**
  - contrat d'ingestion versionné et partagé ;
  - clé tenant en tête des index ;
  - brut à durée de vie courte + agrégats persistants ;
  - page « regressions » (proche d'une vue d'anomalies) ;
  - drill-down route → détail avec percentiles.
- **Risques :** ClickHouse est dimensionné pour des milliards d'événements, pas pour des commandes de PME.
- **Décision : A + C** : patrons d'architecture adoptés (ADR-004-004, 004-007, 004-010) ; ClickHouse **rejeté pour
  l'instant** (ADR-004-002).

### 2.6 srujankothuri/shopflow
- **URL :** https://github.com/srujankothuri/shopflow (`281fddd`, 2026-03-03).
- **Licence :** aucune → **RESEARCH ONLY — LICENSE UNCLEAR.**
- **Pile :** Next.js 16, Prisma 7, PostgreSQL (Neon), Auth.js v5 bêta, `json-rules-engine`, Resend.
- **Motifs utiles :**
  - règles SI/ALORS en groupes AND/OR ;
  - modèles de règles (templates) ;
  - `RuleLog` (journal d'exécution) ;
  - rôles ADMIN / MANAGER / VIEWER.
- **Défauts observés (à ne pas reproduire) :**
  - aucune notion d'organisation ni de boutique (mono-tenant) ;
  - montants en `Float` ;
  - la route `POST /api/rules/{id}/execute` vérifie la session **mais pas le rôle** : un VIEWER peut exécuter ;
  - aucun test ;
  - identifiants de démo publiés dans le README.
- **Décision : A** pour le modèle règle → action → journal ; les défauts nourrissent la matrice de sécurité.

## 3. Dépôts additionnels (recherche GitHub)

Recherches effectuées : `shopify analytics dashboard`, `ecommerce analytics dbt`, `duckdb analytics dashboard`,
`synthetic ecommerce data generator`, `rfm cohort analysis customer`, `ecommerce funnel conversion analytics`,
`time series anomaly detection library python`. Les résultats bruts sont dans
`~/mervio-validation/research_004_0/github_search.json` (hors dépôt).

### 3.1 bhavipopli/open-commerce-ops — le plus proche du produit Mervio
- **URL :** https://github.com/bhavipopli/open-commerce-ops (`8ac2e94`, 2026-05-31) · **Licence :** MIT · démo consultée.
- **Objet :** dashboard auto-hébergé Shopify + Google Ads pour petites équipes e-commerce.
- **Architecture :**
  - React + Vite, fonctions serverless, Postgres (Neon) ;
  - worker de synchronisation Shopify ;
  - ingestion Google Ads par **Google Ads Scripts** (évite le jeton développeur) ;
  - tables `sync_runs` avec avertissements.
- **Motifs utiles :**
  - **fuseau de reporting canonique** (`REPORT_TIME_ZONE`), avec conversion des heures locales du compte Ads ;
  - plusieurs boutiques par compte publicitaire ;
  - mapping campagne → boutique (manuel puis automatique) et seau « dépense non allouée » ;
  - avertissement de synchronisation périmée ;
  - rejouabilité idempotente (upsert par compte, campagne, date et heure) ;
  - bandeau d'état des données en tête du dashboard ;
  - feuille de route explicite : OAuth, webhooks, réconciliation planifiée, Bulk Operations.
- **Risques :**
  - stocke nom, email, téléphone et localisation des clients en clair ;
  - affiche un « After-ads profit » (ventes − publicité) présenté comme un profit (contraire à D-008) ;
  - aucun test.
- **Décision : A + C** : fuseau canonique, `sync_runs` et idempotence adoptés (ADR-004-005, 004-008) ; le libellé
  « profit » est un contre-exemple.

### 3.2 jakubczyz06/ecommerce-data-pipeline
- **URL :** https://github.com/jakubczyz06/ecommerce-data-pipeline (`2da7335`, 2026-09-13) · **Licence :** MIT.
- **Objet :** générateur Faker → staging brut → OLTP normalisé → OLAP en étoile → Flask (RFM, risque d'attrition).
- **Constats :** chaîne brut → normalisé → analytique claire ; aucun test ; seuils de segments codés en dur.
- **Décision : A** (confirme la séparation brut / canonique / analytique de l'ADR-004-002).

### 3.3 Bibliothèques et plateformes (métadonnées vérifiées le 15/09/2026)

| Dépôt | Licence (API GitHub) | Dernier push | Rôle évalué | Décision |
|---|---|---|---|---|
| duckdb/duckdb | MIT | 2026-09-15 | analytique en processus | A — candidat évolutif (ADR-004-012) |
| ClickHouse/ClickHouse | Apache-2.0 | 2026-09-15 | analytique à grande échelle | E pour l'instant |
| fastapi/fastapi | MIT | 2026-09-14 | couche API Python | C — retenu (ADR-004-004) |
| sqlalchemy/alembic | MIT | 2026-09-11 | migrations | C — retenu (ADR-004-002) |
| procrastinate-org/procrastinate | MIT | 2026-09-14 | file de tâches PostgreSQL | A — candidat (ADR-004-008) |
| Bogdanp/dramatiq | LGPL-3.0 | 2026-09-14 | tâches (Redis/RabbitMQ) | E (courtier en plus) |
| unionai-oss/pandera | MIT | 2026-09-14 | tests statistiques de données | A — évaluer en 004.x |
| great-expectations/great_expectations (redirigé vers `fivetran/great_expectations`) | Apache-2.0 | 2026-09-15 | qualité de données | A — trop lourd pour le cœur |
| sodadata/soda-core | NOASSERTION | 2026-09-15 | contrats de données | A — licence à vérifier |
| dlt-hub/dlt | Apache-2.0 | 2026-09-14 | chargement de données | A — alternative future aux connecteurs |
| singer-io/tap-shopify | AGPL-3.0 | 2026-09-09 | extraction Shopify | E (AGPL incompatible avec l'embarquement SaaS fermé) |
| Shopify/shopify_python_api | MIT | 2026-08-25 | SDK Admin Shopify | A — à comparer à GraphQL direct |
| facebook/prophet | MIT | 2026-08-27 | prévision saisonnière | A — piste saisonnalité (gap `seasonal_business`) |
| CamDavidsonPilon/lifetimes | MIT | 2024-06-28, **archivé** | CLV | E (archivé) |
| pymc-labs/pymc-marketing | Apache-2.0 | 2026-09-15 | CLV, MMM bayésien | A — hors périmètre 004 |
| yzhao062/pyod | BSD-2-Clause | 2026-09-08 | détection d'anomalies | A — le détecteur actuel suffit |
| unit8co/darts | Apache-2.0 | 2026-09-07 | séries temporelles | A |
| open-telemetry/opentelemetry-python | Apache-2.0 | 2026-09-15 | traces et métriques | C — quand API et worker existeront |
| shadcn-ui/ui | MIT | 2026-09-12 | composants React accessibles | C — base du design system |
| tremorlabs/tremor | Apache-2.0 | 2025-10-10 | composants de dashboard | A (activité ralentie) |
| evidence-dev/evidence | MIT | 2026-09-14 | BI « as code » | A |
| lightdash/lightdash, cube-js/cube, metabase/metabase, PostHog/posthog | NOASSERTION | 2026-09-15 | BI, couche sémantique, analytics produit | A — licence non affirmée par l'API : RESEARCH ONLY |
| apache/superset | Apache-2.0 | 2026-09-15 | BI généraliste | E (Mervio n'est pas un outil BI généraliste) |

`NOASSERTION` signifie que GitHub n'a pas su classer la licence. La licence réelle peut être connue ailleurs
(double licence, par exemple), mais elle n'a pas été vérifiée ici : pas de réutilisation sans vérification.

## 4. Enseignements transverses

1. **Aucun dépôt ne relie « ce qui s'est passé → pourquoi → que faire → agir » avec des preuves.**
   Les dashboards affichent des KPI et des deltas, jamais la décomposition ni l'incertitude. C'est l'espace de Mervio.
2. **Les générateurs publics ne portent pas de vérité terrain testable.** Les scénarios Mervio (attente `MATCH`
   ou `KNOWN_GAP`) sont un apport propre.
3. **Les produits les plus proches de Mervio convergent sur :** Postgres + worker de synchronisation + historique
   des `sync_runs` + fuseau canonique + état de fraîcheur visible.
4. **Les défauts récurrents sont exactement ceux déjà traités par Mervio :** « profit » sans coûts (D-008), PII en
   clair (D-023), autorisation vérifiée à moitié.
