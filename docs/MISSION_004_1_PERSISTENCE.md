# Mission 004.1 — Persistance PostgreSQL et isolation des tenants

**Statut :** implémenté sur la branche `mission-004.1`.
**Base :** `master` @ `eb52476` (Mission 004.0 ratifiée).
**Décisions :** `docs/MISSION_004_1_DECISIONS.md` (ADR-004.1-001 à 012).
**Suite :** `docs/MISSION_004_1_HANDOFF.md`.

## 1. Architecture

```
                         CLI (inchangée)                      Chemin SaaS (nouveau)
CSV ──► ingestion ──► Dataset ──► moteur ──► rapport ──► fichiers
                                                          
CSV ──► validation (connecteurs réels) ──► Dataset ──► persistence.snapshots ──► PostgreSQL (instantané scellé)
                                                                                     │
PostgreSQL ──► persistence.snapshots.load_dataset ──► Dataset (== Dataset CSV) ──► MÊME moteur
           ──► rapport ──► persistence.analyses ──► PostgreSQL (octets exacts + jsonb)
```

**Paquets :**

| Paquet | Rôle | Dépendances |
|---|---|---|
| `domain`, `ingestion`, `analytics`, `llm`, `reporting`, `synthetic`, `cli` | existants, inchangés hors ADR-004.1-009 | stdlib ; n'importent jamais `persistence` ni un driver (test AST) |
| `persistence` | tenancy, dépôts, codecs, migrations | psycopg 3 ; SQLAlchemy uniquement dans les migrations |
| `application/persisted_analysis.py` | cas d'usage import → instantané et instantané → rapport | `persistence` + moteur ; **non** importé par `mervio.application` |

**Frontière :**
- PostgreSQL **stocke** : entités canoniques, instantanés, rapports.
- Le moteur **calcule** : aucun KPI en SQL (test AST : `persistence` n'importe aucun module de calcul).
- Aucune API, aucun frontend, aucun LLM de production.

## 2. Schéma

Trois migrations Alembic linéaires :
1. `0001_tenancy` ;
2. `0002_snapshots` ;
3. `0003_analysis_reports`.

17 tables, dont `alembic_version`. Conventions communes :
- `timestamptz` partout ;
- montants en `numeric(19,4)` ;
- RLS `ENABLE` + `FORCE` sur toute table de tenant ;
- clés étrangères composites préfixées par `organization_id`.

### 2.1 Tenancy

| Table | Rôle | PK | Organisation / boutique | Clés étrangères | Unicité | Horodatage | Suppression |
|---|---|---|---|---|---|---|---|
| `organizations` | frontière de sécurité et de facturation | `id uuid` | elle-même (RLS sur `id`) | — | — | `created_at` | RESTRICT depuis appartenances et boutiques |
| `users` | identité plateforme (sujet OIDC opaque, **sans PII**) | `id uuid` | **aucune** (exception ADR-004.1-006) ; pas de RLS | — | `idp_subject` | `created_at` | RESTRICT (appartenances, auteurs) |
| `memberships` | rôle d'un utilisateur dans une organisation | `id uuid` | `organization_id` | organisations, utilisateurs | `(organization_id, user_id)` | `created_at` | seule suppression applicative (hors propriétaire) |
| `stores` | boutique : frontière analytique, devise déclarée | `id uuid` | `organization_id` | organisations | `(organization_id, id)` pour les clés composites | `created_at` | RESTRICT |
| `connections` | origine des données, métadonnées seulement, **jamais de secret** | `id uuid` | `organization_id`, `store_id` | `(organization_id, store_id)` → boutiques | `(organization_id, store_id, id)` | `created_at`, `revoked_at` | révocation par statut ; RESTRICT |

### 2.2 Instantanés et données canoniques

| Table | Rôle | PK | Organisation / boutique | Clés étrangères | Unicité | Suppression |
|---|---|---|---|---|---|---|
| `data_snapshots` | jeu figé produit par un import | `id uuid` | org + boutique | boutique, connexion, `supersedes_snapshot_id` (composites), `created_by` → utilisateurs | `(organization_id, store_id, inputs_sha256)` si `completed` | RESTRICT depuis exécutions et rapports ; CASCADE vers son contenu |
| `snapshot_sources` | un fichier source : type, SHA-256, taille, lignes lues et acceptées ; **pas de nom de fichier** | `id uuid` | org + boutique | instantané (composite) | `(organization_id, snapshot_id, source_kind)` | CASCADE depuis l'instantané |
| `products`, `orders`, `payments`, `refunds`, `campaigns`, `ad_daily_performance` | entités `domain.models` | `(organization_id, snapshot_id, position)` | org + boutique | instantané et source (composites, CASCADE) | `(organization_id, source_record_id, snapshot_id, source)` + référence métier (`sku`, `order_ref`, `campaign_ref`, `campaign_ref+day`) | CASCADE depuis l'instantané ; immuables |
| `order_lines` | `OrderItem` | `(organization_id, snapshot_id, order_position, position)` | org + boutique | commande `(organization_id, snapshot_id, order_position)`, instantané | clé primaire | CASCADE |

**Colonnes métier** : correspondance 1:1 avec `domain.models`.
- `Order` :
  - `order_ref` ← `order_id` ;
  - `customer_ref` ← `customer_id` ;
  - `customer_email`, `created_at`, `currency` ;
  - `subtotal`, `discount`, `shipping`, `tax`, `total` ;
  - `financial_status`, `source`.
- `OrderItem` : `sku`, `title`, `quantity`, `unit_price`, `product_ref`.
- `Payment` : `payment_ref`, `created_at`, `amount`, `fee`, `net`, `status`, `order_ref`, `customer_email`, `source`.
- `Refund` : `refund_ref`, `created_at`, `amount`, `order_ref`, `source`.
- `Product` : `sku`, `product_ref`, `title`, `unit_cogs`.
- `Campaign` : `campaign_ref`, `name`, `channel`.
- `DailyAdPerformance` : `campaign_ref`, `day`, `spend`, `impressions`, `clicks`, `conversions`, `conversion_value`.

**La sémantique 003.3 est transportée, jamais réinterprétée :**
- `subtotal` reste le sous-total après remise, avant port et taxes (D-041) ;
- `net_revenue = subtotal` reste une propriété du domaine ;
- remboursements, commandes annulées ou négatives, dates : tels que produits par les connecteurs.

**Métadonnées de `data_snapshots`** :
- import : `source` (`csv`), `connector`, `connector_version`, `schema_version`, `normalization_version` ;
- cycle de vie : `status`, `ingestion_started_at`, `ingested_at`, `failure_code` ;
- contenu : `inputs_sha256`, `currency`, `source_period_start`, `source_period_end`, `row_count`, `record_counts` ;
- `quality` : `DataQualityReport` sans perte, listes ordonnées ;
- marquage : `synthetic`, `not_for_production` (CHECK : synthétique ⇒ jamais production), `synthetic_manifest` ;
- remplacement : `supersedes_snapshot_id`.

### 2.3 Analyses et rapports

| Table | Rôle | PK | Clés étrangères | Unicité | Immuabilité | Suppression |
|---|---|---|---|---|---|---|
| `analysis_runs` | exécution du moteur sur un instantané **complet** (trigger) | `id uuid` | instantané (composite, RESTRICT), `created_by` | `(organization_id, store_id, snapshot_id, config_sha256, engine_version, label, as_of_date)` si `completed` | seule transition `running` → `completed` / `failed` | RESTRICT |
| `reports` | rapport du moteur | `id uuid` | exécution `(organization_id, store_id, id, snapshot_id)` et instantané (RESTRICT) | `(organization_id, analysis_run_id)` | aucune mise à jour | RESTRICT depuis rien ; purge propriétaire uniquement |

**Colonnes de `analysis_runs`** :
- `engine_version`, `grain` ;
- `as_of_date` : le `today` passé au moteur ; `NULL` = dernier jour de données, comportement CLI ;
- `config` (jsonb) et `config_sha256` ;
- `label`, `started_at`, `finished_at`, `failure_code`.

**Colonnes de `reports`** :
- métadonnées recopiées du rapport : `engine_version`, `generated_at`, `grain`, `period_label`, `period_start`,
  `period_end`, `currency`, `synthetic` ;
- `payload_json` (text, autoritaire) et `payload` (jsonb, projection) ;
- `payload_sha256` et `payload_bytes`, vérifiés par `CHECK`.

## 3. Modèle de tenant

```
Organisation ──┬── Appartenances ── Utilisateurs (owner > admin > analyst > viewer)
               └── Boutiques ──┬── Connexions
                               ├── Instantanés ──┬── Sources (fichiers, SHA-256)
                               │                 └── Données canoniques
                               └── Exécutions d'analyse ── Rapports
```

**Rôles** : matrice de `MISSION_004_0_ARCHITECTURE.md` §8.
- `viewer` : lecture.
- `analyst` : import, analyse, provenance.
- `admin` : connexions, boutiques.
- `owner` : membres.

**Utilisation :**

```python
db = Database.from_env()                                   # refuse superutilisateur / BYPASSRLS
session = TenantSession(db, TenantContext(organization_id, user_id))
result = import_csv_snapshot(session, store_id=..., connection_id=..., request=SnapshotImportRequest(shopify_orders=...))
outcome = analyze_snapshot(session, store_id=..., snapshot_id=result.snapshot.id, today=...)
stored = analyses.get_report(session, store_id, outcome.report.id)   # stored.payload_json == report.json CLI
```

## 4. Modèle RLS

- **Contexte** : chaque transaction commence par
  `set_config('app.organization_id', $1, true), set_config('app.user_id', $2, true)`.
  - `true` : local à la transaction, équivalent de `SET LOCAL`.
  - Paramétré : aucune interpolation.
- **Fonctions** `app_current_organization_id()` et `app_current_user_id()` : absent → `NULL` → aucune ligne.
- **Politiques :**
  - `USING (organization_id = app_current_organization_id()) WITH CHECK (même condition)` sur les 14 tables de tenant
    non racines ;
  - `organizations` : sur `id` ;
  - `memberships` : lecture en plus de ses propres lignes (`user_id = app_current_user_id()`).
- **FORCE ROW LEVEL SECURITY** : le propriétaire des tables y est soumis aussi. Seul un superutilisateur y échappe, et
  l'application refuse un tel rôle.
- **La RLS ne remplace pas l'autorisation applicative** :
  - appartenance et rôle relus en base à chaque transaction ;
  - filtre `organization_id` explicite dans chaque requête.
- **Brèches fermées :**
  - clés étrangères composites (les contrôles de clé étrangère ignorent RLS) ;
  - clés uniques préfixées par l'organisation (les contrôles d'unicité ignorent RLS).

**Preuves** (§10) :
- dépôts avec le rôle réel ;
- dépôts avec un rôle BYPASSRLS : la barrière applicative seule suffit ;
- SQL brut : la RLS seule suffit.

## 5. Frontière de persistance

- **Écriture** : `snapshots.write_snapshot(session, dataset=Dataset, sources, inputs_sha256, …)`.
  - Une transaction ; `INSERT … SELECT FROM unnest(...)` par lots de 5 000.
  - `COPY FROM` n'est pas autorisé par PostgreSQL sur une table sous RLS.
  - Contrôles : ligne rattachée à la bonne commande, clé de produit = SKU, source présente, datetimes UTC naïfs,
    montants représentables.
- **Lecture** : `snapshots.load_dataset(session, store_id, snapshot_id)`.
  - Une requête par table, curseur serveur de 10 000 lignes, `ORDER BY position`, numeric chargé directement en
    float.
  - Contrôle des compteurs scellés ; `compute_customer_index()` comme `load_dataset`.
- **Invariant testé** : `load_dataset(session, …) == analytics.pipeline.load_dataset(paths)`. Égalité de dataclasses,
  ordre compris.

## 6. Instantanés

- Immuables après scellement (triggers, droits). Détails : ADR-004.1-004.
- **Idempotence d'import :** `inputs_sha256` = SHA-256 de
  `{files: {source_kind: sha256}, connector, schema_version, normalization_version, synthetic}`.
- **Réponse à « quel jeu exact a produit ce rapport ? »** :
  `reports.snapshot_id` → `data_snapshots` (versions, empreinte, période, qualité) → `snapshot_sources`
  (SHA-256 et taille de chaque fichier).
- **Refus tracés** : validation (`validation_failed`), lecture (`ingestion_failed`), connexion révoquée, devise
  incompatible, fichier modifié pendant l'import.

## 7. Rapports

- Octets exacts du livrable CLI (ADR-004.1-005).
- Chemin complet :
  1. `analyze_snapshot` réutilise une exécution terminée identique (`reused`) ;
  2. sinon crée l'exécution `running` ;
  3. relit le Dataset, lance `analyze_loaded_dataset`, applique `annotate_report` ;
  4. persiste rapport et fin d'exécution dans une transaction.
- Échec du moteur → exécution `failed` avec `insufficient_data`, `analysis_failed`, `internal_error` ou
  `report_persistence_failed`.
- **Qualité** : le rapport contient `data_quality` et `limitations` tels que produits.

## 8. Provenance

`provenance.trace_report(session, store_id, report_id)` renvoie la chaîne :
rapport (SHA-256) → exécution (moteur, configuration, date de référence) → instantané (empreinte, connecteur,
versions, synthétique) → connexion → fichiers (SHA-256, lignes lues et acceptées).

`provenance.trace_record(session, store_id, snapshot_id, entity=, source=, source_record_id=)` renvoie :
- l'enregistrement d'origine ;
- son fichier source (SHA-256) ;
- sa connexion ;
- son connecteur et sa version de normalisation.

Tous deux exigent le rôle `analyst`.

## 9. Montants, identifiants, suppression, index

- **Montants :** `numeric(19,4)`, aller-retour exact ou refus (ADR-004.1-002).
- **Identifiants :** UUID pour les ressources ; `(organization_id, snapshot_id, position)` pour les lignes ;
  `source_record_id` et référence métier distincts (ADR-004.1-003).
- **Suppression :** RESTRICT partout sauf le contenu d'un instantané ; aucun `DELETE` applicatif sur l'historique
  (ADR-004.1-007).
- **Index :** préfixe tenant ; une seule clé commence par (organisation, instantané) (ADR-004.1-012).

## 10. Tests

160 tests ajoutés : 151 dans `tests/persistence/` et 9 dans `tests/test_persistence_boundaries.py`. Aucun test
existant modifié.

| Fichier | Catégories |
|---|---|
| `test_persistence_migrations.py` | migrations (base vide → head, montée et descente révision par révision, schéma identique quel que soit le chemin), schéma, RLS forcée partout, `store_id`, types monétaires, `timestamptz`, droits du rôle applicatif, index (dont la régression d'import quadratique) |
| `test_persistence_money.py` | précision : refus explicites, aller-retour bit pour bit via PostgreSQL sur 6 000 valeurs, borne numeric |
| `test_persistence_tenancy.py` | autorisation : matrice de rôles, contexte forgé, rôle relu à chaque transaction, refus superutilisateur et BYPASSRLS, identifiants mal formés, contexte local à la transaction |
| `test_persistence_snapshots.py` | dépôts, égalité du Dataset, métadonnées, idempotence, nouveaux fichiers, refus tracés, immuabilité (propriétaire compris), tout ou rien, jeux incohérents refusés, clés étrangères et unicité |
| `test_persistence_reports.py` | **octet pour octet** (5 chemins), projection jsonb, `CHECK` d'empreinte, immuabilité, réutilisation, unicité en base, marqueur synthétique, version et grain, échec tracé |
| `test_persistence_isolation.py` | **tenant croisé** : lectures et écritures A↔B via dépôts, mauvaise boutique, identifiants devinés, organisation forgée, barrière applicative seule (BYPASSRLS), RLS seule en SQL brut, insertion, mise à jour et déplacement croisés, clés étrangères croisées, absence d'oracle d'unicité |
| `test_persistence_provenance.py` | chaîne rapport → fichiers, enregistrement → fichier, colonnes d'identité distinctes, rôle requis |
| `test_persistence_deletion.py` | RESTRICT, purge ordonnée et CASCADE limitée, absence de droit DELETE et TRUNCATE |
| `test_persistence_synthetic.py` | générer → valider → persister → relire → analyser : résultats identiques ; marqueurs jusqu'au rapport ; vérité terrain d'un scénario identique |
| `tests/test_persistence_boundaries.py` (sans base) | frontières AST, zéro dépendance runtime, CLI importable sans persistance, aucun identifiant de connexion commité, interpolations SQL limitées à des constantes revues |

**Stratégie de base de test** (ADR-004.1-008) :
- base et rôles éphémères par session, hôte local exigé ;
- ignorés sans `MERVIO_TEST_ADMIN_DATABASE_URL` ;
- obligatoires avec `MERVIO_REQUIRE_DATABASE_TESTS=1`.

## 11. Développement local

**Installer** (une fois) :

```bash
.venv/bin/pip install -e '.[dev,persistence]'
```

**Démarrer PostgreSQL**, au choix.

Avec Docker :

```bash
cp .env.example .env
```

Renseigner `MERVIO_POSTGRES_PASSWORD` dans `.env`, puis :

```bash
docker compose up -d postgres
```

Sans Docker (PostgreSQL 17 installé, par exemple `brew install postgresql@17`) :

```bash
scripts/dev_postgres.sh start
```

**Lancer les tests** (base et rôles jetables créés puis supprimés). Remplacer l'URL par celle affichée au démarrage ;
avec Docker, ajouter le mot de passe.

```bash
MERVIO_TEST_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres .venv/bin/python -m pytest
```

**Appliquer le schéma à une base de développement.** Avec Docker, `mervio_dev` existe déjà. Sans Docker, la créer :

```bash
/opt/homebrew/opt/postgresql@17/bin/createdb -h 127.0.0.1 -p 55432 -U mervio_admin mervio_dev
```

Puis :

```bash
MERVIO_MIGRATION_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/mervio_dev .venv/bin/python -m mervio.persistence upgrade
```

Vérifier la révision :

```bash
MERVIO_MIGRATION_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/mervio_dev .venv/bin/python -m mervio.persistence current
```

**Remettre à zéro.** Descendre toutes les migrations :

```bash
MERVIO_MIGRATION_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/mervio_dev .venv/bin/python -m mervio.persistence downgrade base
```

Ou détruire le cluster sans Docker :

```bash
scripts/dev_postgres.sh destroy
```

Ou détruire le volume Docker :

```bash
docker compose down -v
```

**Rôle applicatif local** : créer un rôle de connexion membre de `mervio_app`, jamais superutilisateur, puis le
passer dans `MERVIO_DATABASE_URL`.

```bash
/opt/homebrew/opt/postgresql@17/bin/psql -h 127.0.0.1 -p 55432 -U mervio_admin -d mervio_dev -c "CREATE ROLE mervio_app_login LOGIN IN ROLE mervio_app"
```

**Ne jamais commiter** `.env`, mot de passe ou URL de production. `.env.example` ne contient que des variables vides
(test).

## 12. Performance

`benchmarks/persistence_benchmark.py`, `benchmarks/results/persistence_20260915_arm64.json`.
Machine : Apple Silicon (18 cœurs), PostgreSQL 17.11 local, Python 3.11.16.

| Commandes | Lignes canoniques | Lecture CSV | Écriture instantané | Relecture Dataset | Analyse | Rapport écrit / lu | Tables commandes + lignes + paiements |
|---|---|---|---|---|---|---|---|
| 9 968 | 35 508 | 0,23 s | 0,67 s | 0,09 s | 0,08 s | 5 ms / 1 ms | 10 Mo |
| 99 921 | 341 980 | 2,4 s | 6,6 s | 1,1 s | 0,75 s | 5 ms / 1 ms | 112 Mo |
| 999 146 | 3 406 013 | 28 s | 78 s | 17 s | 7,8 s | 8 ms / 1 ms | 1,34 Go |

**Observations :**
- Croissance linéaire.
- Plans de relecture en `Index Scan` sur la clé primaire.
- Dataset relu identique et rapport identique à l'octet aux trois tailles.
- Mémoire de pointe du processus de benchmark (Dataset CSV **et** Dataset relu en mémoire) : 99 Mo, 611 Mo, 4,6 Go.
- Relire un instantané est plus rapide que relire les CSV (17 s contre 28 s à 1 M).
- L'écriture est le poste le plus lourd : 78 s à 1 M, pour une importation unique. À exécuter en job (004.2).
- Aucun N+1 : une requête par table.
- Aucune requête non bornée : les listes ont une limite de 1 à 1 000.
- Tous les accès passent par un index préfixé par le tenant.

## 13. Limites connues

1. **Pas de stockage objet** : seules les empreintes des fichiers sont conservées. Un rejeu depuis les octets bruts
   exige de conserver les fichiers (ADR-004-002, 004.2+).
2. **Instantanés complets** : pas d'import incrémental ; stockage proportionnel au nombre d'imports ; rétention à
   décider (R-21).
3. **Écriture d'un instantané de 1 M de commandes : 78 s**, dans une transaction longue. À déplacer en job.
   Optimisation (`COPY` dans une table temporaire puis `INSERT … SELECT`) seulement sur mesure.
4. **Connexion unique par `Database`**, sans pool (004.3). Non partagée entre threads.
5. **Identité** : `user_id` est la racine de confiance, sans validation OIDC avant 004.3. `users` n'a pas de RLS
   (ADR-004.1-006).
6. **Oracle résiduel** : les UUID de ressources sont des clés globales (ADR-004.1-006) ; inexploitable via
   l'application.
7. **Pas d'audit** (`audit_events`) ni de cycle de vie de job : 004.2.
8. **`as_of_date = NULL`** reproduit le comportement CLI (dernier jour de données) ; l'horloge murale n'intervient pas
   dans la période.
9. `_meta.generated_at` diffère entre deux exécutions : c'est la seule différence possible entre deux rapports d'une
   même analyse (déjà vrai en CLI).
10. **Zéro négatif** (`-0.0`) et montants à plus de 4 décimales sont refusés à l'import. Cas non observé dans les
    connecteurs actuels (la plupart des champs sont normalisés par `or 0.0`).
11. **Tests PostgreSQL ignorés** sans base configurée : la CI doit fournir PostgreSQL et
    `MERVIO_REQUIRE_DATABASE_TESTS=1`.
12. `mervio_app` est un rôle global au cluster, créé par la migration s'il manque : une base de production ne doit pas
    partager son cluster avec une autre application qui utiliserait ce nom.
