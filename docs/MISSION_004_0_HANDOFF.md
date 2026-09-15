# Mission 004.0 — Handoff

## 1. État

| | |
|---|---|
| Dépôt | `/Users/mlab/Projects/mervio` (racine canonique ; `~/mervio-validation/source/` n'est pas un dépôt Git, c'est le dossier de preuves OH5) |
| Branche | `mission-004.0` (tag de départ `mission-004.0-start` sur `e8b4e38`) |
| Baseline | Python 3.11.16 · pytest 9.1.1 · ENGINE_VERSION 0.1.0 · 623 tests passés · arbre propre · Mission 003.3 ratifiée |
| Tests après 004.0 | **656 passés**, 0 échec, 0 ignoré, 0 erreur (+33) |
| Moteur analytique | **inchangé** : aucun fichier existant de `src/mervio` modifié ; contrats 003.3 (D-041 à D-048), rapport et contexte LLM 1.0 intacts |

## 2. Livré

**Code et outils (nouveau, testé) :**
- `src/mervio/synthetic/` :
  - `rng`, `profiles` (2 profils), `scenarios` (17 scénarios avec vérité terrain), `generator` (écriture en flux,
    manifeste SHA-256), `validation` (relue depuis le disque), `evaluation` (confrontation au rapport public du
    moteur) ;
  - stdlib uniquement.
- `scripts/generate_data.py` : équivalent de `mervio generate-data`. La sous-commande CLI est différée pour ne pas
  modifier la CLI validée.
- `benchmarks/run_benchmark.py`, `benchmarks/scenario_robustness.py` et les résultats du 15/09/2026.
- `data/scenarios/*.json` (17 définitions versionnées), READMEs de `data/`, `.gitignore` des gros volumes.
- `tests/test_synthetic_generator.py` (16 tests) et `tests/test_synthetic_scenarios.py` (17 tests).

**Recherche :** `research/README.md`, `repositories.md`, `datasets.md`, `licensing_matrix.md`, `ux_benchmark.md`,
`performance_benchmark.md`, `architecture_research.md`.

**Architecture :** `docs/MISSION_004_0_ARCHITECTURE.md`, `MISSION_004_0_DECISIONS.md` (ADR-004-001 à 012),
`MISSION_004_0_RISKS.md` (R-01 à R-22).

**Hors dépôt :**
- `~/mervio-validation/research_004_0/` : clones, résultats de recherche GitHub ;
- `~/mervio-validation/external_004_0/online_retail_ii.zip` : UCI, CC BY 4.0, SHA-256 consigné.

## 3. Faits établis par la mission

1. Aucun dépôt ni jeu public ne fournit la chaîne « quoi → pourquoi → que faire → agir » avec preuves ; aucun
   générateur public ne fournit de vérité terrain testable.
2. Seul jeu externe réel commercialement utilisable trouvé : UCI Online Retail II (CC BY 4.0), non converti.
3. Le moteur est quasi linéaire :
   - 100 000 commandes : 3,6 s et 515 Mo ;
   - 1 M de commandes : 45 s et 3,7 Go ;
   - rapport constant autour de 35 Ko ;
   - goulots : ingestion ~60 %, séries et comparaisons ~38 % (`window()` répété).
4. Le moteur retrouve la vérité terrain de 16 scénarios sur leurs attentes `MATCH`. Angles morts documentés en
   `KNOWN_GAP` :
   - trafic organique ;
   - attrition et rétention ;
   - remise vs panier moyen ;
   - évolution de marge ;
   - effondrement produit en tant que cause ;
   - rupture de stock ;
   - expédition ;
   - saisonnalité (fausse alerte sur une décrue saisonnière normale).
   Robustesse : 170/170.

## 4. Règles pour Mission 004.x

- Préserver les 656 tests, 0 régression ; tout test modifié pour un changement de contrat est justifié par écrit.
- Aucun calcul de KPI hors `analytics` ; API et frontend lisent des rapports persistés.
- Toute nouvelle infrastructure (base, file, service) exige un ADR fondé sur une mesure.
- Aucune donnée synthétique ou externe en production ; aucune PII dans Git.
- Toute dépendance ajoutée passe par la matrice de licences.

## 5. Missions recommandées

### Mission 004.1 — Fondation de persistance et d'isolation tenant
- **Objectif :** stocker organisations, boutiques, instantanés canoniques et rapports dans PostgreSQL avec une
  isolation prouvée.
- **Périmètre :**
  - paquet `persistence` : SQLAlchemy Core ou ORM, Alembic ;
  - tables : organisations, memberships, stores, snapshots, entités canoniques, analysis_runs, reports (JSONB),
    audit_events ;
  - `TenantContext` obligatoire, RLS, import d'un jeu CSV existant vers un instantané ;
  - analyse d'un instantané via `analyze_dataset` et persistance du rapport ;
  - docker compose local (Postgres) ;
  - correction du nombre de tests dans le README.
- **Dépendances :** ADR-004-002, 004-003, 004-007.
- **Risques :** R-01, R-17 ; divergence entre entités persistées et `domain.models`.
- **Critères d'acceptation :**
  - instantané importé → rapport persisté **identique octet pour octet** au rapport CLI sur le même jeu synthétique ;
  - tests RLS : lecture croisée impossible en SQL et via les dépôts ;
  - migrations montée et descente testées ;
  - montants en `numeric` ;
  - 656+ tests, 0 régression.

### Mission 004.2 — Jobs, exécutions et audit
- **Objectif :** exécuter imports, analyses et planifications en tâche de fond, rejouables et tracées.
- **Périmètre :**
  - spike d'une journée (table maison vs procrastinate) puis implémentation ;
  - `jobs`, `schedules`, cycle de vie `sync_runs` et `analysis_runs`, idempotence, reprise bornée, état `dead` ;
  - audit append-only ;
  - logs JSON corrélés (ADR-004-009) ;
  - benchmark d'analyse en job à 100k et 1M.
- **Dépendances :** 004.1.
- **Risques :** R-07, R-21.
- **Critères d'acceptation :**
  - un job interrompu reprend sans double écriture ;
  - même `Idempotency-Key` → un seul run ;
  - analyse de 1 M de commandes en job < 60 s et < 4 Go sur la machine de référence, ou écart expliqué ;
  - aucun secret ni PII dans les logs (test).

### Mission 004.3 — Frontière API v1 (lecture + déclenchements)
- **Objectif :** exposer la chaîne rapport → constats → anomalies → recommandations via un contrat typé.
- **Périmètre :**
  - FastAPI ;
  - routes §6.2 sauf connecteurs OAuth et exécution d'action ;
  - validation de jetons OIDC (choix du fournisseur), matrice de rôles ;
  - erreurs RFC 9457, pagination par curseur, ETag, limitation de débit ;
  - OpenAPI versionné ;
  - `imports` CSV comme source.
- **Dépendances :** 004.1, 004.2, ADR-004-004.
- **Risques :** R-01, R-02, R-09.
- **Critères d'acceptation :**
  - chaque route testée : succès, validation, 404 tenant croisé, rôle insuffisant, idempotence ;
  - test de contrat OpenAPI bloquant ;
  - aucune route ne calcule un KPI ;
  - p95 < 300 ms sur la lecture d'un rapport persisté.

### Mission 004.4 — Connecteur Shopify Admin GraphQL
- **Objectif :** remplacer l'export CSV par une synchronisation API et réduire les limites D-045 et D-046.
- **Périmètre :**
  - interface de connecteur (ADR-004-005) ;
  - OAuth sur une **boutique de développement**, jetons chiffrés ;
  - commandes, lignes, remises, remboursements (`processedAt`), fuseau, devise ;
  - Bulk Operations pour l'historique ;
  - provenance ;
  - réponses enregistrées pour les tests.
- **Dépendances :** 004.1 à 004.3.
- **Risques :** R-03, R-14, R-15 ; changements de version d'API.
- **Critères d'acceptation :**
  - instantané API et export CSV d'une même boutique de développement rapprochés au centime sur le CA avant
    ajustements ;
  - date de remboursement réelle disponible et documentée ;
  - remise partielle observée sur boutique de développement (**pas une preuve marchand**, D-046 reste à jour) ;
  - aucun jeton dans les logs ;
  - décision écrite sur le fuseau de période.

### Mission 004.5 — Frontend : Briefing → Problème → Preuves (lecture seule)
- **Objectif :** rendre la chaîne visible sans calcul côté client.
- **Périmètre :**
  - `web/` Next.js ;
  - navigation §4 : Briefing, Problèmes, Données, Explorer minimal ;
  - composants §5 : KPICard, TrendIndicator, InsightCard, AnomalyCard, RootCauseCard, EvidencePanel, ConfidenceBadge,
    DataFreshnessIndicator, SourceBadge, états vides, chargement et erreur ;
  - types générés depuis OpenAPI ;
  - données synthétiques marquées.
- **Dépendances :** 004.3.
- **Risques :** R-04, R-16.
- **Critères d'acceptation :**
  - chaque chiffre affiché correspond à un champ du rapport (test) ;
  - FACT, INFERENCE, RECOMMENDATION et ACTION distingués ;
  - Core Web Vitals p75 dans les budgets sur staging ;
  - axe-core sans violation critique ;
  - clavier complet ;
  - thème clair et sombre.

### Mission 004.6 — LLM de production, recommandations et approbation d'actions
- **Objectif :** expliquer le rapport avec un fournisseur réel et permettre d'agir sous contrôle humain.
- **Périmètre :**
  - fournisseur réel derrière `LLMProvider` (worker), persistance des explications et de leur provenance, budget
    par organisation ;
  - modèle d'action : brouillon → approbation admin → exécution d'actions **à faible risque** d'abord (export,
    notification, tâche), journal d'audit ;
  - aucune action d'écriture chez un fournisseur tiers sans nouvel ADR.
- **Dépendances :** 004.2, 004.3 (004.5 pour l'interface).
- **Risques :** R-12, R-13, R-02.
- **Critères d'acceptation :**
  - suite LLM existante verte avec le fournisseur réel derrière un enregistreur ;
  - taux de rejet du validateur mesuré sur les 17 scénarios synthétiques ;
  - aucune action exécutable sans approbation d'un rôle autorisé (test) ;
  - explication indisponible sans effet sur le rapport ;
  - aucun prompt ni réponse dans les logs.

## 6. Premières actions de la conversation suivante

1. `git status`, `git log --oneline -5` sur `mission-004.0`, puis `.venv/bin/python -m pytest` (656 attendus).
2. Lire ce handoff, puis `docs/MISSION_004_0_DECISIONS.md` et `docs/MISSION_004_0_ARCHITECTURE.md` §6 à §9.
3. Décider de la fusion de `mission-004.0` dans `master` (non effectuée).
4. Démarrer 004.1 par un ADR si un choix s'écarte d'ADR-004-002 ou 004-003.
