# Mission 004.1 — Handoff (document de départ de Mission 004.2)

## 1. État

| | |
|---|---|
| Dépôt | `/Users/mlab/Projects/mervio` |
| Branche | `mission-004.1`, créée depuis `master` @ `eb52476` (fusion de 004.0) |
| Point de départ de 004.2 | `master` après fusion de `mission-004.1` ; à défaut, la pointe de `mission-004.1` (hash dans le rapport final de 004.1) |
| Environnement | Python 3.11.16 · pytest 9.1.1 · PostgreSQL 17.11 · psycopg 3.3.5 · Alembic 1.20.0 · ENGINE_VERSION 0.1.0 · contrat LLM 1.0 |
| Tests | **816 passés** avec PostgreSQL : 656 existants + 160 nouveaux ; 0 échec, 0 ignoré, 0 erreur |
| Tests sans base | 697 passés, 119 ignorés (tests PostgreSQL, voir §5) |
| Moteur | aucune formule, aucun seuil, aucune clé de rapport modifiés ; deux extractions sans changement de comportement (ADR-004.1-009) ; contrats 003.3 et LLM 1.0 intacts |

## 2. Livré

**Code** (`src/mervio/persistence/`, extra optionnel `persistence`) :

| Module | Contenu |
|---|---|
| `database.py` | `Database` : connexion vérifiée (refus superutilisateur, BYPASSRLS, non UTF-8), transactions avec contexte `set_config(..., true)` |
| `tenancy.py` | `TenantContext`, `TenantSession` (appartenance et rôle relus à chaque transaction), `Role`, `Permission`, provisionnement : `ensure_user`, `create_organization`, `add_member`, `remove_member`, `organizations_of` |
| `stores.py` | boutiques et connexions CSV (création, lecture, liste, révocation) |
| `snapshots.py` | `write_snapshot`, `record_failed_snapshot`, `load_dataset`, `get_snapshot`, `list_snapshots`, `find_completed_snapshot`, `snapshot_sources` |
| `analyses.py` | `start_run`, `complete_run`, `fail_run`, `get_run`, `list_runs`, `get_report`, `get_report_for_run`, `latest_report`, `find_completed_run` |
| `provenance.py` | `trace_report`, `trace_record` |
| `money.py`, `codec.py` | montants exacts, qualité sans perte, sérialisation du rapport, empreintes |
| `migrate.py`, `__main__.py`, `migrations/` | Alembic sans `alembic.ini` : `python -m mervio.persistence upgrade|downgrade|current|heads` ; révisions `0001_tenancy`, `0002_snapshots`, `0003_analysis_reports` |

**Autres fichiers :**
- **Cas d'usage** : `src/mervio/application/persisted_analysis.py`.
  - `import_csv_snapshot` : validation par les connecteurs réels → instantané.
  - `analyze_snapshot` : instantané → moteur inchangé → rapport persisté.
- **Moteur** :
  - `analytics.pipeline.analyze_loaded_dataset` (extrait de `run_analysis`) ;
  - `application.service.annotate_report` (extrait d'`analyze_dataset`).
- **Local** : `docker-compose.yml` (postgres:17), `scripts/dev_postgres.sh` (sans Docker), `.env.example`
  (variables vides), `.gitignore` (`.pgdata/`, `!.env.example`).
- **Tests** : `tests/persistence/` (151), `tests/test_persistence_boundaries.py` (9).
- **Benchmark** : `benchmarks/persistence_benchmark.py`, `benchmarks/results/persistence_20260915_arm64.json`.
- **Documentation** :
  - `docs/MISSION_004_1_PERSISTENCE.md` ;
  - `docs/MISSION_004_1_DECISIONS.md` (ADR-004.1-001 à 012) ;
  - ce handoff ;
  - licences ajoutées à `research/licensing_matrix.md` ;
  - README et PROJECT_STATE mis à jour.

## 3. Faits établis

1. **Rapport persisté identique octet pour octet au livrable CLI**, sur 5 chemins :
   - rapport CLI persisté puis relu ;
   - chaîne SaaS complète sur jeu synthétique ;
   - `data/sample` ;
   - sources partielles ;
   - grain mensuel.

   Également identique à 10 k, 100 k et 1 M de commandes (benchmark). Seule `_meta.generated_at` est figée dans le
   test, valeur non déterministe par nature.
2. **`Dataset` relu == `Dataset` CSV**, ordre compris. Complément nécessaire : une mutation d'un ulp sur un montant
   non utilisé par le rapport ne change pas ses octets (vérifié).
3. **Isolation prouvée à trois niveaux** : dépôts, barrière applicative seule (BYPASSRLS), RLS seule (SQL brut).
   Deux brèches découvertes et fermées par construction :
   - contrôles de clé étrangère hors RLS → clés composites ;
   - contrôles d'unicité hors RLS (oracle d'existence) → clés préfixées par l'organisation.
4. **Import quadratique trouvé et corrigé** : le plan générique du contrôle de clé étrangère choisissait un index
   secondaire (5,9 s → 0,64 s pour 10 k). Régression testée sur le catalogue.
5. **Performance** : linéaire jusqu'à 1 M de commandes.
   - Écriture 78 s, relecture 17 s (les CSV : 28 s), analyse 7,8 s.
   - Rapport écrit en 8 ms, lu en 1 ms.
6. **La vérité terrain d'un scénario synthétique** (`revenue_drop`) s'évalue à l'identique depuis le rapport persisté.

## 4. Règles pour 004.2 et au-delà

- Préserver les 816 tests et 0 régression ; tout test modifié pour un changement de contrat est justifié par écrit.
- Aucun accès SQL hors de `mervio.persistence` (test AST). Aucun KPI en SQL.
- Toute modification de schéma passe par une **nouvelle** révision Alembic, montée et descente testées. Les révisions
  0001 à 0003 ne se modifient plus une fois `mission-004.1` fusionnée.
- Toute nouvelle table de tenant respecte les règles suivantes, et `test_persistence_migrations.py` doit être étendu :
  - `organization_id NOT NULL`, et `store_id` si donnée de boutique ;
  - RLS `ENABLE` + `FORCE` et politique `USING` + `WITH CHECK` ;
  - clés étrangères composites ;
  - clés uniques préfixées par `organization_id` ;
  - aucun index secondaire concurrent de la clé d'accès principale ;
  - droits minimaux pour `mervio_app`.
- Jamais de connexion applicative superutilisateur ou BYPASSRLS. Les jobs ouvrent leurs transactions via
  `TenantSession`.
- Aucune donnée synthétique en production : `data_snapshots_synthetic_never_production` et le marqueur du rapport
  sont des invariants.

## 5. CI à mettre en place

- Service PostgreSQL 17.
- `pip install -e '.[dev,persistence]'`.
- Variables :
  - `MERVIO_TEST_ADMIN_DATABASE_URL` : rôle capable de `CREATE DATABASE` et `CREATE ROLE ... BYPASSRLS`, donc
    superutilisateur du **conteneur de CI** uniquement ;
  - `MERVIO_REQUIRE_DATABASE_TESTS=1` : sans elle, 119 tests seraient ignorés.
- Scan de secrets. Vérification de licences à l'ajout de dépendance (R-11).

## 6. Risques

| # | Risque | Statut après 004.1 |
|---|---|---|
| R-01 | Fuite entre tenants | **atténué** en persistance (3 niveaux de preuve) ; reste ouvert pour l'API (004.3 : 404 croisé par route) |
| R-17 | Complexité excessive | atténué : PostgreSQL seul, pas d'ORM, pas de service ajouté |
| R-18 | Dérive documentaire | corrigée : README et PROJECT_STATE à 816 |
| R-21 | Rétention et suppression | ouvert : purge ordonnée non implémentée ; instantanés complets cumulatifs |
| R-07 | Analyse ou import lents | import 1 M = 78 s en transaction unique → à exécuter en job (004.2) |
| R-11 | Dépendance copyleft | psycopg LGPL-3.0 adopté non modifié, côté serveur ; repli pg8000 documenté |
| **R-23** (nouveau) | Divergence future entre `domain.models` et le schéma canonique | `Dataset ==` après aller-retour testé ; tout champ ajouté au domaine casse ce test tant que le schéma ne suit pas |
| **R-24** (nouveau) | Transaction d'import longue (1 M : 78 s) : verrous et WAL | job dédié, lots, supervision de durée (004.2) |
| **R-25** (nouveau) | Tests PostgreSQL silencieusement ignorés en CI | `MERVIO_REQUIRE_DATABASE_TESTS=1` obligatoire |

## 7. Conditions non résolues (à porter par les missions suivantes)

1. Stockage objet des fichiers bruts (seules les empreintes sont gardées) : 004.2 ou 004.4.
2. `audit_events` append-only et `sync_runs` / `jobs` : 004.2.
3. Politique de rétention et purge d'organisation ou d'instantané par job tracé : 004.2 et R-21.
4. Pool de connexions et identité OIDC (`users.idp_subject`) : 004.3.
5. Identifiant opaque exposable pour une ligne canonique, si l'API l'exige : 004.3.
6. Import incrémental et upsert par clé de source pour les connecteurs API : ADR requis en 004.4.

## 8. Mission 004.2 recommandée — Jobs d'arrière-plan et audit

- **Objectif** : exécuter import et analyse en tâches de fond rejouables, et tracer toute écriture.
- **Périmètre :**
  - spike table maison vs procrastinate (ADR-004-008) ;
  - `jobs` avec `organization_id`, `store_id`, `kind`, `payload` sans secret, `status`, `attempts`, `run_after`,
    verrou, `idempotency_key`, `last_error_code` ;
  - `SELECT … FOR UPDATE SKIP LOCKED` ;
  - worker ouvrant une `TenantSession` ;
  - jobs `import_snapshot` (autour de `import_csv_snapshot`) et `analysis` (autour de `analyze_snapshot`) ;
  - `audit_events` append-only (aucun `UPDATE` ni `DELETE` pour `mervio_app`, RLS) ;
  - logs JSON corrélés (ADR-004-009) ;
  - job de purge ordonnée : rapport → exécution → instantané.
- **Réutiliser**, sans les contourner :
  - idempotence d'import (`inputs_sha256`) et d'analyse (`analysis_runs_completed_uniq`) ;
  - cycle `running` → `completed` / `failed` de `analysis_runs` ;
  - instantané `failed` tracé.
- **Critères d'acceptation :**
  - un job interrompu reprend sans double écriture : un instantané scellé au plus par empreinte, un rapport au plus
    par exécution ;
  - même clé d'idempotence → un seul run ;
  - un job d'une organisation ne lit ni n'écrit jamais une autre (test RLS avec contexte du job) ;
  - import et analyse de 1 M de commandes en job, avec durée et mémoire mesurées ;
  - aucun secret ni PII dans les logs et l'audit (test) ;
  - 816+ tests, 0 régression.
- **Hors périmètre** : API publique (004.3), connecteurs API (004.4), frontend (004.5), LLM de production (004.6).

## 9. Premières actions de la conversation suivante

1. `git status`, `git log --oneline -10`, branche et fusion de `mission-004.1`.
2. Démarrer PostgreSQL (`scripts/dev_postgres.sh start` ou `docker compose up -d postgres`).
3. Lancer la suite, 816 attendus :

   ```bash
   MERVIO_TEST_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres .venv/bin/python -m pytest
   ```

4. Lire ce handoff, `docs/MISSION_004_1_DECISIONS.md`, puis `docs/MISSION_004_1_PERSISTENCE.md` §2 à §7.
