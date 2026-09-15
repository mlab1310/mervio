# Mission 004.0 — Architecture cible de Mervio SaaS

**Statut :** proposition validée par le gate 004.0, **non implémentée** (hormis `mervio.synthetic` et les
benchmarks). **Base :** `e8b4e38` (Mission 003.3 ratifiée), branche `mission-004.0`.
**Décisions :** `docs/MISSION_004_0_DECISIONS.md` · **Preuves :** `research/`.

## 1. Principe directeur

```
DONNÉES → FAITS VÉRIFIÉS → ANOMALIES → CAUSES → EXPLICATION → RECOMMANDATION → ACTION
 source    moteur          moteur      moteur    LLM (encadré)   moteur + LLM      humain approuve
```

- Le **moteur déterministe** possède la vérité numérique. Aucune couche (API, frontend, LLM) ne recalcule un KPI.
- Chaque maillon est **persisté** et **relié** au précédent : une recommandation pointe vers un constat, qui pointe
  vers des métriques, qui pointent vers une exécution d'analyse, qui pointe vers un instantané de données, qui
  pointe vers des exports ou des appels source.
- L'interface montre la chaîne ; l'API la transporte ; la base la conserve ; le LLM l'explique sans la fabriquer.

## 2. Vue d'ensemble — monolithe modulaire (ADR-004-001)

```
┌──────────────────────┐   HTTPS/JSON (OpenAPI v1)   ┌────────────────────────────────────────────┐
│ web/ Next.js         │ ──────────────────────────► │ mervio.api (FastAPI)                       │
│ - rendu serveur      │ ◄────────────────────────── │  auth ─ tenancy ─ routes ─ problem+json    │
│ - aucun calcul KPI   │                             └───────────────┬────────────────────────────┘
└──────────────────────┘                                             │ appels Python (même codebase)
                                                                     ▼
            ┌────────────────────────────────────────────────────────────────────────────────┐
            │ couche applicative : cas d'usage (import, sync, analyse, explication, action)  │
            ├───────────────┬───────────────┬───────────────┬───────────────┬────────────────┤
            │ connectors    │ ingestion     │ analytics     │ llm           │ actions        │
            │ (API sources) │ (CSV, exist.) │ (moteur 0.1.0)│ (contrat 1.0) │ (approbation)  │
            ├───────────────┴───────────────┴───────────────┴───────────────┴────────────────┤
            │ persistence : dépôts tenant-scopés (PostgreSQL) · object store · audit        │
            └────────────────────────────────────────────────────────────────────────────────┘
                                                                     ▲
┌──────────────────────┐   jobs (table PostgreSQL)                   │
│ mervio.worker        │ ────────────────────────────────────────────┘
│ sync · analyse · LLM │   même code, processus séparé, planificateur intégré
└──────────────────────┘
      PostgreSQL (transactions, canonique, rapports, jobs, audit) · stockage objet (exports bruts)
```

Deux processus Python issus du même paquet (API, worker), un frontend, une base, un stockage objet.
Aucune file externe, aucun microservice, aucun Kubernetes.

## 3. Structure de dépôt (ADR-004-001)

**Existant conservé tel quel.** On ajoute des paquets à côté ; aucune réorganisation mécanique.

```
mervio/
├── src/mervio/
│   ├── domain/         existant — entités canoniques, qualité
│   ├── ingestion/      existant — connecteurs CSV (Shopify, Stripe, Google Ads)
│   ├── analytics/      existant — moteur déterministe (indépendant, testable seul)
│   ├── application/    existant — service d'analyse, validation de fichiers
│   ├── llm/            existant — contrat 1.0, validation, fournisseurs
│   ├── reporting/      existant — livrables texte et JSON
│   ├── cli/            existant
│   ├── synthetic/      NOUVEAU 004.0 — générateur, scénarios, évaluation
│   ├── persistence/    004.1 — modèles SQL, dépôts tenant-scopés, migrations Alembic
│   ├── jobs/           004.2 — file PostgreSQL, worker, planification
│   ├── api/            004.3 — FastAPI, schémas Pydantic, auth, erreurs
│   ├── connectors/     004.4 — connecteurs API (Shopify GraphQL d'abord)
│   └── actions/        004.6 — recommandations actionnables, approbation, exécution
├── web/                004.5 — Next.js (package.json propre, types générés depuis OpenAPI)
├── research/ · docs/ · benchmarks/ · scripts/
├── data/  sample/ (versionné) · scenarios/ (versionné) · synthetic/ benchmark/ external/ (ignorés)
└── tests/  existant + contract/ + api/ + e2e/ au fil des missions
```

**Règles de dépendance, vérifiables par test AST comme `test_the_analytics_engine_never_imports_the_generator` :**
- `analytics`, `domain` et `ingestion` n'importent ni `api`, ni `persistence`, ni `jobs`, ni `connectors`,
  ni `synthetic` ;
- `llm` n'importe pas `analytics` au-delà du rapport sérialisé ;
- `api` n'importe jamais `analytics` directement : il lit des rapports persistés via `persistence` et déclenche des
  jobs via `application`.

## 4. Architecture de l'information (Partie H)

Dérivée du benchmark UX (`research/ux_benchmark.md`) : la navigation suit les questions, pas les tables.

### 4.1 Navigation primaire

| Entrée | Question | Contenu |
|---|---|---|
| **Briefing** (accueil) | Que s'est-il passé ? | période close, 1 à 3 constats prioritaires, KPI de soutien avec qualité et source, fraîcheur des données |
| **Problèmes** | Pourquoi ? | liste des constats (critique / avertissement / opportunité), détail = faits → preuves → hypothèse → limites |
| **À décider** | Que faire ? | recommandations ouvertes, rejetées, converties en action ; badge de compteur |
| **Actions** | Laisse-moi agir | brouillons, approuvées, exécutées, échouées ; journal |
| **Explorer** | vérifier soi-même | CA, commandes, produits, clients (pseudonymisés), marketing : séries et tableaux, drill-down |
| **Données** | puis-je faire confiance ? | connexions, synchronisations, imports, qualité, provenance, sources absentes |
| Réglages | — | organisation, membres et rôles, boutiques, fuseau et devise, facturation (plus tard) |

### 4.2 Modèle global
- **Sélecteur de boutique** dans l'en-tête : il fixe `store_id` dans l'URL (`/s/{store_id}/…`). Le serveur vérifie
  l'appartenance à chaque requête.
- **Modèle de période :**
  - les analyses portent sur des **périodes closes** (semaine ISO ou mois, `AnalyticsConfig.grain`) et sur la
    période précédente ;
  - Explorer accepte des plages libres, marquées « exploration, hors analyse » ;
  - fuseau : celui de la boutique quand le connecteur le fournit, sinon UTC affiché explicitement (limite actuelle).
- **Filtres :** synchronisés dans l'URL (partageables).
- **Notifications :** en v1, un compteur « À décider » et un email hebdomadaire de Briefing (opt-in). Pas de flux
  temps réel.
- **Approbation d'action :**
  - brouillon → approbation par un rôle `admin` ou `owner` → exécution ;
  - chaque transition est un événement d'audit ;
  - une action ne s'exécute jamais sur la seule sortie d'un LLM.

### 4.3 Chemins de drill-down
```
Briefing ─► Problème ─► Preuve (métrique) ─► Série / tableau Explorer ─► Provenance (run, instantané, source)
Problème ─► Recommandation ─► Action ─► Journal d'audit
Données ─► Synchronisation ─► Avertissements ─► KPI touchés
```

## 5. Système de composants (Partie I)

Base : primitives accessibles (shadcn/ui sur Radix, MIT). Chaque composant affiche un champ du rapport ou de l'API,
jamais un calcul local.

| Composant | Données | Règle |
|---|---|---|
| `KPICard` | `Metric` (valeur, unité, période, qualité, notes) | affiche `data_quality` ; `unavailable` = « non calculable » + raison, jamais 0 |
| `TrendIndicator` | `comparison.pct_change`, sens souhaitable | flèche + signe + texte ; couleur selon `DESIRABLE_DIRECTION` |
| `InsightCard` | constat (`category`, `fact`) | étiquette **FACT** |
| `AnomalyCard` | anomalie (observé, attendu, critère, z, sévérité, `assessment`) | critère lisible ; « favorable » ou « défavorable » |
| `RootCauseCard` | facteurs (contribution, confiance, `causality_established=false`) | étiquette **INFERENCE** ; « contribution arithmétique, pas une cause démontrée » |
| `RecommendationCard` | recommandation + `source_insight` + impact ou « non chiffrable » | étiquette **RECOMMENDATION** ; lien vers le constat |
| `ActionCard` | action, statut, approbateur, résultat | étiquette **ACTION** ; boutons selon le rôle |
| `ConfidenceBadge` | `confidence` ∈ [0,1 ; 0,9] | libellé qualitatif (faible / moyenne / élevée) + formule en infobulle ; jamais « % de certitude » (D-012) |
| `DataFreshnessIndicator` | dernière sync réussie, avertissements, sources absentes | orange si sync périmée ; lien vers Données |
| `SourceBadge` | `sources` de la métrique, `synthetic` du manifeste | Shopify · Stripe · Google Ads · **SYNTHÉTIQUE** |
| `EvidencePanel` | `evidence[]`, contributeurs produit et campagne, limites | toujours présent sous une inférence |
| `DateRangePicker` | périodes closes + plage d'exploration | les deux modes sont visuellement distincts |
| `FilterBar` | boutique, canal, catégorie | état dans l'URL |
| `Chart` | séries (≤ 500 points) | sous-échantillonnage déclaré ; tableau accessible équivalent |
| `DataTable` | listes paginées par curseur | virtualisation au-delà de 200 lignes |
| `EmptyState` / `LoadingState` / `ErrorState` | — | l'état vide dit quelle source connecter et ce qu'elle débloque ; erreurs avec `request_id` |

**Exemple d'écran Problème**, chaque chiffre provenant d'un champ du rapport :

```
CA avant ajustements  ↓ 18 %                                     FACT        [Shopify · fiable]
POURQUOI ?
  Commandes ↓ 23 %  ·  Panier moyen ↑ 6 %                        FACT
  Cause principale probable : baisse du trafic payé              INFERENCE   [confiance moyenne]
  Preuves : 3 produits = 61 % de la baisse · clics payants ↓ 19 % · conversion stable
  Limites : trafic organique non observé
QUE FAIRE ?
  Revoir l'acquisition sur le groupe de produits X               RECOMMENDATION
  [Créer une action]                                             ACTION → approbation requise
```

## 6. Frontière API (Partie J, ADR-004-004)

### 6.1 Conventions
- **Version :** préfixe `/v1`. **Contrats :** modèles Pydantic v2 = source de vérité → OpenAPI 3.1 → types
  TypeScript générés pour `web/` ; test de contrat bloquant sur toute modification.
- **Authentification :** `Authorization: Bearer <jeton>` émis par le fournisseur d'identité, validé par l'API
  (signature, audience, expiration). Aucune session implicite par cookie tiers.
- **Tenant :**
  - l'organisation vient de l'appartenance de l'utilisateur, jamais d'un paramètre client ;
  - `store_id` est dans le chemin et vérifié contre l'organisation ;
  - toute ressource d'une autre organisation renvoie `404`, pas `403`, pour ne pas révéler son existence.
- **Rôles :** `owner` > `admin` > `analyst` > `viewer` (matrice en §8).
- **Idempotence :** en-tête `Idempotency-Key` obligatoire sur les POST créant un job ou une action ; clé conservée
  24 h par (organisation, route, clé) ; même clé + corps différent → `409 idempotency_conflict`.
- **Erreurs :** `application/problem+json` (RFC 9457) : `{type, title, status, code, detail, request_id}`.
  Pas de trace, pas d'écho brut d'entrée. Codes stables (`store_not_found`, `insufficient_role`,
  `validation_failed`, `rate_limited`, `run_not_ready`, `action_not_approved`, …).
- **Pagination :** curseur opaque (`?cursor=`, `?limit=` ≤ 200, défaut 50) ; réponse `{items, next_cursor}`.
- **Filtres et tri :** liste blanche par ressource (`?status=`, `?category=`, `?sort=-created_at`) ; tout autre
  paramètre → `400`.
- **Cache :**
  - rapports et exécutions terminées immuables : `ETag` = `run_id` + version de contrat,
    `Cache-Control: private, max-age=300` ;
  - listes et statuts : `no-store`.
- **Limitation de débit :** par utilisateur et par organisation (jeton), plus stricte sur imports, syncs et
  explications LLM.
- **Audit :** toute écriture et toute lecture de provenance ou d'export produisent un `audit_event`.

### 6.2 Ressources

Toutes les routes sous `/v1/stores/{store_id}` exigent l'appartenance à l'organisation de la boutique.
Colonnes :
- « Idem. » = idempotence ;
- « Pag./filtres » = pagination et filtres.

| Méthode et route | Objet | Rôle min. | Idem. | Pag./filtres | Cache | Audit |
|---|---|---|---|---|---|---|
| `GET /v1/me` | utilisateur, organisation, rôles, boutiques | viewer | — | — | no-store | non |
| `GET /v1/stores` | boutiques de l'organisation | viewer | — | curseur | no-store | non |
| `GET /v1/stores/{store_id}` | boutique (devise, fuseau, statut) | viewer | — | — | private 60 s | non |
| `POST /v1/stores/{store_id}/imports` | dépôt de CSV (chemin existant) → stockage objet → job de sync | analyst | **oui** | — | — | **oui** |
| `POST /v1/stores/{store_id}/connections` | créer une connexion (type, portée demandée) → URL d'autorisation | admin | **oui** | — | — | **oui** |
| `GET /v1/stores/{store_id}/connections` | connexions et état (jamais de secret) | viewer | — | curseur ; `status` | no-store | non |
| `DELETE /v1/stores/{store_id}/connections/{connection_id}` | révoquer (suppression des jetons, conservation de l'historique) | admin | naturelle | — | — | **oui** |
| `POST /v1/stores/{store_id}/connections/{connection_id}/sync-runs` | lancer une synchronisation → `202` + `sync_run_id` | analyst | **oui** | — | — | **oui** |
| `GET /v1/stores/{store_id}/sync-runs` | historique | viewer | — | curseur ; `status`, `connection_id` ; tri `-started_at` | no-store | non |
| `GET /v1/stores/{store_id}/sync-runs/{sync_run_id}` | statut, compteurs, avertissements, instantané produit | viewer | — | — | private si terminé | non |
| `POST /v1/stores/{store_id}/analysis-runs` | analyser un instantané (`grain`, `snapshot_id?`) → `202` | analyst | **oui** | — | — | **oui** |
| `GET /v1/stores/{store_id}/analysis-runs` | historique des analyses | viewer | — | curseur ; `status`, `grain` | no-store | non |
| `GET /v1/stores/{store_id}/analysis-runs/{run_id}` | statut, versions (moteur, contrat), instantané source | viewer | — | — | ETag si terminé | non |
| `GET /v1/stores/{store_id}/reports/latest?grain=week` | dernier rapport terminé (redirection `303` vers le run) | viewer | — | — | no-store | non |
| `GET /v1/stores/{store_id}/analysis-runs/{run_id}/report` | rapport complet du moteur, tel que produit (~35 Ko) | viewer | — | — | **ETag, immuable** | non |
| `GET /v1/stores/{store_id}/analysis-runs/{run_id}/series/{metric}` | projection d'une série du rapport | viewer | — | — | ETag | non |
| `GET /v1/stores/{store_id}/findings` | constats du run | viewer | — | curseur ; `run_id`, `category`, `severity` | ETag par run | non |
| `GET /v1/stores/{store_id}/findings/{finding_id}` | constat + preuves + facteurs + limites + provenance | viewer | — | — | ETag | non |
| `GET /v1/stores/{store_id}/anomalies` | anomalies du run | viewer | — | curseur ; `run_id`, `metric`, `assessment` | ETag par run | non |
| `GET /v1/stores/{store_id}/recommendations` | recommandations et leur statut | viewer | — | curseur ; `status`, `run_id` | no-store | non |
| `POST /v1/stores/{store_id}/recommendations/{id}/dismiss` | rejeter avec motif | analyst | naturelle | — | — | **oui** |
| `POST /v1/stores/{store_id}/analysis-runs/{run_id}/explanations` | demander une explication LLM → `202` | analyst | **oui** | — | — | **oui** |
| `GET /v1/stores/{store_id}/explanations/{explanation_id}` | explication validée ou `unavailable` + code | viewer | — | — | ETag si terminée | non |
| `POST /v1/stores/{store_id}/actions` | brouillon d'action depuis une recommandation | analyst | **oui** | — | — | **oui** |
| `GET /v1/stores/{store_id}/actions` | actions | viewer | — | curseur ; `status` | no-store | non |
| `POST /v1/stores/{store_id}/actions/{action_id}/approve` | approuver (motif facultatif) | **admin** | naturelle | — | — | **oui** |
| `POST /v1/stores/{store_id}/actions/{action_id}/execute` | exécuter une action approuvée → `202` ; `409 action_not_approved` sinon | **admin** | **oui** | — | — | **oui** |
| `GET /v1/stores/{store_id}/data-quality?run_id=` | qualité, avertissements, sources absentes | viewer | — | — | ETag par run | non |
| `GET /v1/stores/{store_id}/provenance/{kind}/{id}` | chaîne de provenance (§10) | analyst | — | — | ETag | **oui** |
| `GET /v1/organization/audit-events` | journal d'audit | admin | — | curseur ; `actor`, `action`, `from`, `to` | no-store | **oui** (lecture) |
| `GET /healthz`, `GET /readyz` | vivacité, base et migrations | aucun | — | — | no-store | non |

**Choix structurant :** aucune route « analytics » ne calcule à la demande. Les vues (`series`, `findings`,
`anomalies`) sont des **projections d'un rapport persisté et immuable**. L'analyse de 1 M de commandes prend 45 s ;
un rapport persisté se lit en millisecondes.

### 6.3 Contrats typés (extrait indicatif)

```python
class Problem(BaseModel):                       # RFC 9457
    type: str; title: str; status: int; code: str; detail: str | None = None; request_id: str

class Page(BaseModel, Generic[T]):
    items: list[T]; next_cursor: str | None

class AnalysisRunCreate(BaseModel):
    grain: Literal["week", "month"] = "week"
    snapshot_id: UUID | None = None             # défaut: dernier instantané validé

class AnalysisRun(BaseModel):
    id: UUID; store_id: UUID
    status: Literal["queued", "running", "completed", "failed"]
    grain: Literal["week", "month"]; period_label: str | None
    snapshot_id: UUID; engine_version: str; report_contract: str
    error_code: str | None; created_at: datetime; finished_at: datetime | None

class MetricView(BaseModel):                    # recopie d'un Metric du rapport, jamais recalculé
    key: str; label: str; value: float | None
    unit: Literal["currency", "count", "ratio", "percent"]
    data_quality: Literal["reliable", "incomplete", "unavailable"]
    period: str; sources: list[str]; notes: list[str]

class FindingDetail(BaseModel):
    id: str; run_id: UUID; category: Literal["critical_issue", "warning", "opportunity"]
    fact: str; evidence: list[str]; hypothesis: str | None
    root_cause: RootCauseView | None; limitations: list[str]; provenance: ProvenanceRef

class ActionView(BaseModel):
    id: UUID; recommendation_id: str; kind: str
    status: Literal["draft", "approved", "executing", "executed", "failed", "cancelled"]
    created_by: UUID; approved_by: UUID | None; audit_event_ids: list[UUID]
```

## 7. Multi-tenancy (Partie K, ADR-004-003)

```
Plateforme ─► Organisation ─► Membres (rôles) ─► Boutiques ─► Connexions ─► Instantanés ─► Analyses ─► Constats ─► Recommandations ─► Actions
```

| Clé | Portée | Présente sur |
|---|---|---|
| `organization_id` | frontière de sécurité et de facturation | **toutes** les tables métier, audit et jobs compris |
| `store_id` | frontière analytique (devise, fuseau, données) | toute donnée de boutique : connexions, instantanés, commandes, runs, constats, actions |
| `user_id` | auteur et approbateur | memberships, actions, audit |
| `connection_id` | origine d'une donnée | sync runs, fichiers bruts, entités canoniques (provenance) |

**Garanties :**
1. **Couche d'accès :** chaque dépôt prend un `TenantContext(organization_id, user_id, roles)` obligatoire ; aucune
   méthode de requête sans contexte. Test AST : aucune requête SQL hors de `persistence/`.
2. **Row Level Security PostgreSQL** en défense en profondeur :
   - `SET LOCAL app.organization_id` par transaction ;
   - politiques `USING (organization_id = current_setting(...)::uuid)` ;
   - rôle applicatif sans `BYPASSRLS`.
3. **Jobs :** chaque job porte `organization_id` et `store_id` ; le worker ouvre sa transaction avec le même contexte.
4. **Clés d'index :** `organization_id` en tête des index composites (motif next-cwv-monitor).
5. **Tests obligatoires :** pour chaque route, un utilisateur de l'organisation B reçoit `404` sur une ressource de
   A ; test RLS direct en SQL.
6. **Jamais** de filtrage de tenant côté frontend.

## 8. Authentification et autorisation (ADR-004-003, ADR-004-004)

- **Identité :** fournisseur OIDC (choix en 004.3). Mervio ne stocke aucun mot de passe. MFA exigible pour
  `owner` et `admin`.
- **Jetons :** courte durée côté API ; le frontend les obtient côté serveur, en cookies `HttpOnly`, `Secure`,
  `SameSite=Lax`, avec protection CSRF sur les mutations issues de formulaires.

**Matrice de rôles :**

| Capacité | viewer | analyst | admin | owner |
|---|---|---|---|---|
| lire Briefing, Problèmes, rapports, Explorer | ✓ | ✓ | ✓ | ✓ |
| importer, lancer sync, analyse, explication, créer brouillon d'action, rejeter recommandation | | ✓ | ✓ | ✓ |
| lire la provenance détaillée | | ✓ | ✓ | ✓ |
| gérer les connexions, approuver et exécuter les actions, lire l'audit | | | ✓ | ✓ |
| membres, rôles, suppression des données, facturation | | | | ✓ |

## 9. Persistance (Partie L, ADR-004-002)

| Stockage | Contenu | Règles |
|---|---|---|
| **PostgreSQL** | organisations, membres, boutiques, connexions (métadonnées), `sync_runs`, `snapshots`, entités canoniques (`orders`, `order_lines`, `refunds`, `payments`, `campaigns`, `ad_days`, `products`), `analysis_runs`, `reports` (JSONB immuable), `findings`, `recommendations`, `explanations`, `actions`, `jobs`, `audit_events`, `idempotency_keys` | montants en `numeric(14,2)` (jamais `float` en base) ; horodatages `timestamptz` UTC ; `organization_id` + `store_id` partout ; entités canoniques partitionnables par `store_id` plus tard |
| **Stockage objet** | exports CSV importés, charges brutes d'API (JSONL compressé), rapports d'export volumineux | objets immuables adressés par SHA-256 ; préfixe `org/{organization_id}/store/{store_id}/` ; chiffrement serveur ; durée de rétention par politique |
| **Secrets** | jetons OAuth des connecteurs | gestionnaire de secrets de l'hébergeur ou chiffrement par enveloppe (clé hors base) ; jamais en clair en base ni dans les logs |
| **Analytique** | aucune base dédiée | le moteur lit un instantané canonique en mémoire (mesuré jusqu'à 1 M) ; DuckDB en processus étudié **si** un tenant dépasse le budget mémoire du worker |

**Instantané (`snapshot`) :** ensemble figé d'entités canoniques pour une boutique à un instant, produit par une ou
plusieurs synchronisations. Une analyse porte toujours sur un instantané identifié : le rejouer produit le même
rapport (déterminisme D-033).

**Migrations :**
- Alembic, révisions linéaires, testées en CI : montée depuis une base vide, puis descente d'une révision ;
- aucune migration destructive sans révision de sauvegarde ;
- ajout de colonnes nullable → backfill par job → contrainte.

**Passage depuis l'existant :** les connecteurs CSV restent la première source (route `imports`). Ils remplissent
les mêmes tables canoniques que les futurs connecteurs API.

## 10. Ingestion et connecteurs (Partie M, ADR-004-005)

```
Source (API / CSV) ─► brut (stockage objet, haché) ─► normalisé (entités canoniques) ─► validé (DataQualityReport)
                   ─► instantané canonique (PostgreSQL) ─► moteur (Dataset)
```

**Interface d'un connecteur, contrat du paquet `connectors` :**

| Méthode | Rôle |
|---|---|
| `authenticate(grant) -> CredentialRef` | échange OAuth ; retourne une référence de secret, jamais le jeton |
| `validate_connection(ref) -> ConnectionHealth` | portées, accès en lecture, fuseau et devise de la boutique |
| `discover_schema(ref) -> SourceSchema` | version d'API, champs disponibles ; signalement des dérives |
| `fetch(ref, cursor, window) -> RawPage` | page brute + curseur ; bornes de débit respectées |
| `paginate(...)` | itérateur de pages, reprise sur curseur |
| `normalize(raw) -> CanonicalBatch` | vers `domain.models` ; aucune règle métier implicite ; conventions de montant documentées (D-041) |
| `validate(batch, quality)` | mêmes signaux que les connecteurs CSV (`subtotal_convention_contradiction`, `subtotal_contract_unverified`, `missing_order_total`, …) |
| `store(batch, snapshot)` | upsert idempotent par clé naturelle (`store_id`, `source`, `source_record_id`) |
| `record_provenance(run, pages)` | SHA-256 des pages, compteurs, avertissements dans `sync_runs` |

**Ordre recommandé :**
1. **Shopify Admin GraphQL** (commandes, remboursements avec `processedAt`, `subtotalPriceSet`, fuseau).
   Réduit les limites D-045 et D-046, validé d'abord sur une boutique de développement.
2. **Stripe.**
3. **Google Ads.** Option API ou script d'export comme OCO. Mapping campagne → boutique et seau non alloué.
4. **Meta Ads.**

Le moteur ne voit jamais un format fournisseur.

## 11. Provenance (Partie N, ADR-004-007)

**Champs canoniques** sur chaque entité importée :
- `source` (shopify, stripe…) ;
- `source_record_id` ;
- `source_updated_at` ;
- `connection_id` ;
- `sync_run_id` ;
- `raw_object_sha256` ;
- `schema_version` (version d'API ou de format) ;
- `normalization_version` (version du connecteur).

**Chaîne :**
```
Recommandation ─ recommendation.source_insight ─► Constat (finding_id)
Constat ─ evidence/metric keys ─► Métrique du rapport (report.kpis[key], anomalies[i], root_causes[0])
Rapport ─ analysis_run: engine_version, report_contract, config_hash ─► Instantané (snapshot_id, content_sha256)
Instantané ─ snapshot_members ─► Sync runs (connector, schema_version, normalization_version)
Sync run ─ raw objects (sha256) ─► Source d'origine (shop domain, endpoint, fenêtre, horodatage)
Explication LLM ─ context_sha256, contract_version, prompt_version, model, validator issues ─► Rapport
```

Pas de lignage colonne par colonne : le grain est l'exécution. Pour une commande donnée, la provenance remonte à sa
page brute (`source_record_id` + `raw_object_sha256`). C'est suffisant pour l'audit, sans surconstruire.

## 12. Tâches de fond (ADR-004-008)

- **Table `jobs` :**
  - `id`, `organization_id`, `store_id`, `kind`, `payload` (JSONB sans secret) ;
  - `status` (queued, running, succeeded, failed, dead), `attempts`, `run_after`, `locked_by`, `locked_at` ;
  - `idempotency_key`, `last_error_code`.
- **Worker :**
  - `SELECT … FOR UPDATE SKIP LOCKED` ;
  - délai maximal par type ;
  - reprise exponentielle bornée pour les erreurs transitoires uniquement, comme D-036 ;
  - `dead` après N essais, visible dans Données.
- **Types :** `sync`, `analysis`, `explanation`, `action_execute`, `scheduled_briefing`.
- **Planification :** table `schedules` (cron simple par boutique : sync quotidienne, analyse le lundi 06:00 dans le
  fuseau de la boutique), évaluée par le worker. Aucun ordonnanceur externe.
- **Enchaînement :** `sync` réussie → instantané validé → `analysis` → (si activé) `explanation`. Chaque étape est
  un job distinct, rejouable.

## 13. Frontière LLM de production (Partie O, ADR-004-006)

Construite sur l'existant (`src/mervio/llm`, contrat 1.0), sans l'élargir.

| Type | Définition |
|---|---|
| **LLMInput** | le contexte `build_llm_context(report)` contrat 1.0 : `facts`, `findings`, `evidence`, `hypotheses`, `recommendations`, `root_causes`, `limitations`, `unavailable_metrics`, `data_quality`, `business_health`, `profitability`, `analysis_period`, `currency`, `engine_version`, `contract_version`, `untrusted_content` + politique ; plus `business_context` optionnel (secteur, objectifs déclarés), traité comme **donnée non fiable** (D-034) |
| **LLMOutput** | `BusinessExplanation` validée par `validate_response` : `summary`, `facts`, `explanations`, `hypotheses`, `recommendations`, `limitations`, chacun avec `refs` ; `health_score` recopié du contexte (`health_score_source = analytics_engine`) |
| **LLMError** | codes existants : `provider_timeout`, `provider_unavailable`, `provider_rate_limited`, `provider_authentication_failed`, `provider_refusal`, `provider_invalid_response`, `provider_output_too_large`, `provider_error` + rejet du validateur (codes `issues`) → explication `unavailable`, rapport intact (D-037) |
| **LLMConfidence** | **jamais** auto-déclarée par le modèle : dérivée des `confidence` du moteur, de `data_quality` et des `limitations` du contexte ; affichée par `ConfidenceBadge` |
| **LLMProvenance** | métadonnées existantes (`provider`, `model`, `prompt_version`, `contract_version`, `response_schema_version`, `request_fingerprint`, `attempts`, `latency_ms`, `usage`) + `analysis_run_id`, `context_sha256`, `validator_issue_codes`, `created_at` |

**Interdits, garantis par le validateur et par test :**
- chiffre non ancré dans un champ numérique ;
- métrique indisponible chiffrée ;
- profit partiel présenté comme profit ;
- causalité affirmée ;
- avertissement de qualité ignoré ;
- fuite d'instructions.

**Production :**
- un fournisseur réel derrière `LLMProvider`, appelé **uniquement par le worker** ;
- délai et tentatives bornés ;
- budget de coût par organisation ;
- ni prompt ni réponse brute dans les logs (D-037) ;
- explication persistée avec sa provenance ;
- pas d'agent autonome, pas de base vectorielle.

## 14. Observabilité (Partie P, ADR-004-009)

- **Logs JSON :**
  - champs `ts`, `level`, `service` (api, worker), `event` ;
  - `request_id` (propagé du frontend, sinon généré), `organization_id`, `store_id`, `user_id` (identifiant opaque) ;
  - `job_id`, `sync_run_id`, `analysis_run_id`, `connector`, `duration_ms`, `error_code`.
- **Métriques :**
  - requêtes par route (compte, latence p50/p95, codes) ;
  - jobs (en file, âge, durée, échecs par type) ;
  - échecs de connecteur par code ;
  - échecs LLM par code et rejets du validateur ;
  - incidents qualité par `kind` (`subtotal_convention_contradiction`, `currency_mismatch`…) ;
  - durée et mémoire d'analyse par taille d'instantané (suivi du benchmark en production).
- **Traces :** instrumentation OpenTelemetry dans API et worker, export vers le backend de l'hébergeur quand il
  existe. Pas d'infrastructure de tracing dédiée.
- **Alertes initiales :** taux de 5xx, jobs `dead`, âge de la file, syncs en échec répétées, rejets du validateur
  LLM anormaux.
- **Jamais journalisé :** mots de passe, secrets et clés API, jetons OAuth et d'accès, en-têtes `Authorization`,
  prompts et réponses LLM, emails, noms, adresses, téléphones, lignes de commande brutes, valeurs de cellules de
  fichiers importés.
  - Le moteur applique déjà `redact()` (D-016) et ne cite jamais une valeur sensible (D-017).
  - Un test vérifie ces exclusions sur les logs de l'API.

## 15. Sécurité (Partie R)

Architecture de sécurité initiale. **Aucune certification de conformité n'est revendiquée.**

| Domaine | Mesure |
|---|---|
| Authentification | OIDC externe ; MFA exigible pour admin et owner ; jetons courts ; révocation à la suppression de membre |
| Autorisation | matrice de rôles vérifiée côté serveur à **chaque** route de mutation (contre-exemple ShopFlow) ; test par route |
| Isolation tenant | §7 : couche d'accès + RLS + tests croisés |
| Secrets | gestionnaire de secrets ; variables d'environnement limitées au démarrage ; aucun secret dans le dépôt (scan en CI) |
| Jetons OAuth | portées minimales en lecture ; stockés chiffrés par enveloppe ; rotation et révocation ; supprimés à la déconnexion |
| Chiffrement | TLS partout ; chiffrement au repos base et stockage objet (hébergeur) ; sauvegardes chiffrées |
| Base | rôle applicatif sans superutilisateur ni `BYPASSRLS` ; migrations via un rôle distinct ; réseau privé |
| Débit | limites par jeton et par organisation ; plus strictes sur imports, syncs et LLM ; token bucket (motif Atlas) |
| Webhooks | vérification HMAC de la signature fournisseur, horodatage et rejeu refusé, traitement asynchrone idempotent |
| CSRF | cookies `SameSite=Lax` + jeton CSRF sur les mutations du frontend ; API en Bearer non sensible au CSRF |
| Entrées | schémas Pydantic stricts ; taille de requête et de fichier plafonnée ; formats refusés comme aujourd'hui (D-039) ; CSV traités comme données non fiables |
| En-têtes | CSP, HSTS, X-Content-Type-Options, Referrer-Policy, frame-ancestors (motif Atlas) |
| Audit | `audit_events` append-only : acteur, action, ressource, organisation, `request_id`, horodatage ; pas de contenu sensible |
| Rétention et suppression | exports bruts : durée configurable (défaut à décider avec les premiers clients) ; suppression d'organisation = purge base + objets + révocation des jetons, avec job traçable ; pseudonymisation des clients dans les livrables (D-023) |
| Moindre privilège | worker et API avec des identifiants distincts ; accès production restreint et journalisé |
| Données synthétiques et externes | jamais en production ; manifeste `not_for_production` vérifiable |

## 16. Déploiement initial (Partie S, ADR-004-001)

| Élément | Choix initial | Rejeté pour l'instant |
|---|---|---|
| Frontend | Next.js sur un hébergeur managé ou en conteneur | — |
| API | conteneur Python (FastAPI + serveur ASGI), 2 instances minimum | microservices |
| Worker | même image, commande différente, 1 à N instances | Celery et courtier |
| Base | PostgreSQL managé, sauvegardes PITR | base analytique séparée |
| Stockage | stockage objet compatible S3 | — |
| LLM | fournisseur derrière `LLMProvider` | agents, base vectorielle |
| Observabilité | logs JSON + métriques de l'hébergeur ; SDK OpenTelemetry | infrastructure de tracing dédiée |
| Orchestration | PaaS de conteneurs (une région, UE probable) | Kubernetes, Kafka |
| Environnements | local (docker compose : Postgres + stockage compatible S3), staging, production | — |

## 17. Stratégie de test (Partie U)

**Invariant de toutes les missions 004.x :**
- les 623 tests historiques et les 33 tests de 004.0 passent ;
- 0 régression ;
- un test modifié l'est parce qu'un contrat change volontairement, avec justification écrite (ADR ou décision) ;
- aucun test n'est affaibli pour passer.

```
                     e2e (Playwright + axe)            ~10  parcours Briefing → Problème → Action
                  sécurité (tenant croisé, rôles)      ~1 par route
               performance (benchmarks/, seuils)       tailles 10k/100k en CI nocturne
            API (routes, erreurs, pagination)          ~5 par route
         contrat (OpenAPI figé, rapport, LLM 1.0)      snapshots versionnés
      connecteurs (réponses enregistrées, sans réseau) par connecteur
   scénarios (mervio.synthetic, vérité terrain)        17 aujourd'hui
intégration (Postgres réel en conteneur, migrations)   par dépôt
unitaires (moteur, domaine, LLM, générateur)           656 aujourd'hui
```

| Niveau | Règle |
|---|---|
| Unitaires | stdlib, rapides ; le moteur reste testable sans base ni réseau |
| Intégration | vraie base PostgreSQL éphémère ; RLS testée en SQL |
| Contrat | OpenAPI généré comparé à un fichier versionné ; rapport moteur et contexte LLM déjà figés par goldens |
| Connecteurs | cassettes de réponses **synthétiques ou de boutique de développement**, jamais de données marchand |
| Scénarios | `tests/test_synthetic_scenarios.py` ; tout nouveau facteur de cause racine ajoute ou met à jour un scénario |
| API | chaque route : succès, validation, 404 tenant croisé, rôle insuffisant, idempotence |
| LLM | contrat 1.0, suite adversariale existante, fournisseur réel testé derrière un mock enregistré |
| Performance | `benchmarks/run_benchmark.py` ; alerte si `run_analysis` à 100k régresse de plus de 25 % |
| Sécurité | secrets (scan), en-têtes, débit, webhooks signés, logs sans PII |
| E2E | parcours principaux avec données synthétiques marquées |
