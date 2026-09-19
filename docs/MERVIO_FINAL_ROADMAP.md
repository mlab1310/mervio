# MERVIO — Roadmap définitive jusqu'au SaaS commercialisable

| | |
|---|---|
| Date | 16 septembre 2026 |
| Base | `master` @ `bbe8355` (Merge Mission 004.2) |
| Source de vérité | `docs/MERVIO_FULL_AUDIT.md` (lu intégralement), plus une inspection directe du dépôt |
| Nature | Document d'architecture et de priorisation. **Aucun fichier source, test, migration ou configuration n'a été modifié.** |
| Statut de l'audit | `docs/MERVIO_FULL_AUDIT.md` est **non suivi par Git** (`??`). Il doit être commité avec ce document avant 004.3. |

**Niveaux de preuve utilisés**
- **DÉMONTRÉ** : vérifié dans le code, par un test ou par un fichier de benchmark.
- **PARTIEL** : existe, mais seulement dans un contexte limité (synthétique, bibliothèque sans appelant, local).
- **NON DÉMONTRÉ** : absent, ou jamais exécuté.
- **[EXTERNE]** : dépend d'un fournisseur tiers (Shopify, fournisseur LLM, facturation). À revérifier dans la
  documentation officielle au début de la mission concernée.

---

## 1. Executive Summary

**Où en est Mervio aujourd'hui**
- Un **moteur analytique déterministe** solide, testé, piloté par CLI à partir d'exports CSV.
- Une **couche de persistance multi-tenant** sérieuse :
  - PostgreSQL 17 ;
  - RLS forcée ;
  - instantanés immuables ;
  - rapports persistés identiques octet pour octet ;
  - file de travaux durable ;
  - audit en ajout seul.
- Une **couche LLM** avec garde-fous, qui n'a jamais appelé un vrai modèle.
- **Aucun produit** :
  - pas d'API, pas d'authentification, pas d'interface ;
  - pas de worker lançable comme service ;
  - pas de CI, pas d'hébergement ;
  - pas de connecteur Shopify, pas de facturation.
- **Aucune donnée marchande native** n'a traversé le moteur. La seule donnée réelle est un registre de 106 commandes
  reconstruit depuis un PDF.

**Ce que cette roadmap change par rapport à l'audit**

L'audit reste la source de vérité sur les faits. Trois décisions d'ordonnancement s'en écartent, et chacune est
justifiée en §4.4 :
1. **La protection des données passe avant l'API**, et non en fin de parcours.
   - L'e-mail client est aujourd'hui dans deux colonnes (`customer_email`) **et dans la clé client**
     (`orders.customer_ref`, qui vaut l'e-mail : `ingestion/shopify.py:115`). L'audit ne cite que la première.
   - Le chemin de fichier arbitraire (SEC-01) devient une faille dès la première route d'import.
2. **Le contrat de données est figé avant l'API, sans coder Shopify.**
   - L'audit place le connecteur Shopify avant l'API pour stabiliser le schéma.
   - Cette roadmap stabilise ce schéma par une mission de contrat (identité client, fuseau, objets bruts, type de
     connexion extensible), puis construit l'API et l'interface sur CSV.
   - Résultat : **un produit visible 2 missions plus tôt**, sans retravail d'API.
3. **Le premier marchand réel arrive par CSV, en production, avant Shopify.**
   - L'export natif d'un pilote est exactement ce qui manque pour valider le contrat de revenu (D-046).
   - Shopify arrive ensuite pour supprimer l'export manuel et assurer la synchronisation continue.

**Jalons**

| Jalon | Atteint à la fin de | Contenu |
|---|---|---|
| **M1 — Premier produit visible** | 004.7 | Navigateur local : connexion, organisation, boutique, dépôt CSV, Brief → Problème → Preuves → Recommandation |
| **M2 — Premier marchand réel** | 004.9 | Un pilote se connecte en production, dépose son export Shopify natif, voit son Brief ; le harnais D-046 est passé |
| **M3 — MVP de production** | 004.13 | Installation Shopify, synchronisation quotidienne, explication LLM validée, une semaine sans intervention |
| **M4 — Premier client payant** | 005.1 | Essai, paiement, limites, résiliation, suppression des données, conditions juridiques revues |

**Prochaine mission : 004.3 — Platform Baseline & Worker Runtime.**

---

## 2. Current State

### 2.1 Architecture réelle

```
CSV (Shopify orders/products, Stripe, Google Ads)
  ↓  ingestion/*.py, application/imports.py             DÉMONTRÉ
Dataset canonique (float, datetimes UTC naïfs)           DÉMONTRÉ
  ↓  persistence/snapshots.py (migration 0002)          DÉMONTRÉ — copie complète par import
Instantané scellé, immuable
  ↓  workers/handlers.py (import, analysis, purge)      PARTIEL — bibliothèque, aucun processus
Moteur : KPI → séries → anomalies → cause → santé → insights
  ↓  analytics/*                                        DÉMONTRÉ (v1, angles morts déclarés)
Rapport JSON (octets exacts + jsonb + SHA-256)            DÉMONTRÉ
  ↓  llm/* (contexte 1.0, validateur, mock)             PARTIEL — aucun appelant de production
Explication                                               NON DÉMONTRÉ
  ↓
API / Frontend / Shopify / Actions / Facturation          NON DÉMONTRÉ (absents)
```

### 2.2 Ce qui est démontré par les tests

| Capacité | Preuve |
|---|---|
| Suite complète | 1097 tests passés avec PostgreSQL 17.11, 0 ignoré ; 796 + 301 ignorés sans base |
| Isolation des tenants | RLS `ENABLE` + `FORCE`, clés étrangères composites, tests dépôt + SQL brut |
| Reproductibilité | rapport persisté = rapport CLI, octet pour octet |
| Immuabilité | déclencheurs sur instantanés, lignes canoniques, rapports et audit |
| File de travaux | `FOR UPDATE SKIP LOCKED`, plan pinné, 100 travaux × 10 workers sans doublon, reprise après crash |
| Honnêteté numérique | absent ≠ zéro ; profitabilité exclue sans coûts ; `causality_established = false` |
| Robustesse d'import | formats binaires refusés, dates ambiguës refusées, schéma inconnu refusé |
| Volumétrie | 1 M de commandes : 45 s / 3,7 Go (CSV) ; 4,6 Go RSS (chemin persistant) |
| Garde-fous LLM | chiffres ancrés, causalité refusée, PII et injection détectées — **sur mock** |

### 2.3 Partiellement démontré

| Capacité | Limite |
|---|---|
| Validation sur données réelles | 1 registre reconstruit (OH5, 106 commandes), aucune remise partielle, aucune date de remboursement |
| Couverture analytique | 17 scénarios synthétiques. **Précision :** 10 scénarios sur 17 déclarent au moins un `KNOWN_GAP` (vérifié dans `synthetic/scenarios.py`) ; seuls 7 sont entièrement couverts. Un `KNOWN_GAP` compte comme « réussi » quand le moteur **ne** trouve **pas** la vérité. « 170/170 » ne mesure donc pas une couverture. |
| Worker | `Worker.run_forever` existe ; pas d'entrée processus, pas de SIGTERM, pas de reprise périodique, liste de tenants explicite |
| Journalisation | JSON + corrélation + rédaction ; appelée seulement par un benchmark |
| Débit de file | 1 460 travaux/s mesurés **avec un gestionnaire vide** sur un seul tenant |
| Provenance | empreintes et chaîne complètes ; fichiers bruts non conservés |

### 2.4 Non démontré

- Toute exposition HTTP, et donc l'isolation des tenants **via HTTP**.
- Un vrai modèle LLM, son taux de rejet, sa latence, son coût.
- Un export Shopify natif ; les formats Stripe et Google Ads réels.
- Import et analyse **en tant que travaux** à 100 k et 1 M de commandes.
- Fuseau horaire de la boutique (aucune colonne `stores.timezone`).
- Effacement RGPD, suppression d'organisation, sauvegarde et restauration.
- Déploiement, supervision, alertes.

---

## 3. Critical Gaps

| Gap | Severity | Evidence | Impact | Required Before |
|---|---|---|---|---|
| G-01 E-mail client en clair, y compris **comme clé client** | CRITICAL | `0002_snapshots.py` : `orders.customer_email`, `payments.customer_email` ; `orders.customer_ref` = e-mail (`ingestion/shopify.py:115`) ; lignes immuables, sans `DELETE` applicatif | Effacement RGPD impossible ; `customers/redact` Shopify impossible | 004.6 (API) |
| G-02 Chemin de fichier arbitraire dans la charge d'import | HIGH (latent) | `workers/handlers.py` `import_handler` : `str(path)` ; `validate_payload` ne vérifie que clés et tailles | Lecture de fichiers d'autres tenants dès qu'une route d'import existe | 004.6 |
| G-03 Pseudonymes de rapport réversibles | MEDIUM | `analytics/report.py:15-24` : SHA-256 non salé ; `top_customers` stocké dans des rapports immuables | Ré-identification par liste d'e-mails | 004.6 |
| G-04 Pas de renouvellement de bail | HIGH | `jobs.DEFAULT_LEASE_SECONDS = 300` ; aucune fonction de renouvellement | Double exécution concurrente des travaux longs | 004.6 (imports via API) ; impératif avant Shopify |
| G-05 Worker non déployable | HIGH | aucune entrée processus, aucun SIGTERM, `recover_stale` jamais planifié | Rien ne tourne hors des tests | 004.6 |
| G-06 Liste de tenants explicite, pas d'identité de service | HIGH | `workers/worker.py:7,75` ; ADR-004.2-002 ; `actor_id = user_id` dans `handlers.JobContext.audit` | Une organisation créée par inscription n'est jamais servie ; l'audit attribue le travail à un humain | 004.6 |
| G-07 Aucune CI | HIGH | pas de `.github/` ; tests DB ignorés en silence sans variable (R-25) | Régressions non détectées | toute mission suivante |
| G-08 Décalage de base entre anomalie et cause | HIGH | `anomaly.py` : moyenne des 8 périodes ; `pipeline.py:169,191` : cause vs période précédente | L'explication peut contredire l'anomalie | 004.6 (le rapport devient un contrat d'API) |
| G-09 « Conversion » = toutes commandes / clics payants | HIGH | `kpi.py:181-186` ; `root_cause.py` facteur `conversion_rate` ; recommandation « auditer le checkout » | Baisse organique présentée comme problème de checkout | 004.6 |
| G-10 Preuves en chaînes préformatées, pas d'ID de constat | MEDIUM | `insights.py` `evidence: List[str]` ; rapport non typé | API, UI et LLM doivent reparser des chiffres | 004.6 |
| G-11 Pas de garde saisonnière | HIGH | `anomaly.detect_anomalies` ; scénario `seasonal_business` = `KNOWN_GAP` | Fausse alerte dès la première session | M2 (premier marchand) |
| G-12 Remises confondues avec le panier moyen | MEDIUM | `subtotal` après remise (D-041) ; aucune métrique de taux de remise | Recommandation mal ciblée | M2 |
| G-13 Rétention / attrition absentes | MEDIUM | seul le taux de réachat intra-période existe | Churn invisible | M2 |
| G-14 Fuseau horaire absent | HIGH | `stores` sans `timezone` ; `ingestion/base.py:307-308` convertit en UTC naïf | Périodes différentes de celles du marchand | M2 |
| G-15 Aucune fraîcheur des données | MEDIUM | rien ne compare la dernière donnée à la date du jour | Rapport périmé présenté comme actuel | 004.6 |
| G-16 Aucune validation marchande réelle | CRITICAL | D-046 ; audit §21 | Chiffres potentiellement faux | M2 |
| G-17 Pas d'hébergement, de sauvegardes, de secrets | CRITICAL | audit §18 | Aucune donnée réelle ne peut être accueillie | M2 |
| G-18 Aucun document juridique | CRITICAL ⚖️ | audit §23 | Traitement de données client non couvert | M2 |
| G-19 LLM jamais exécuté, coût non borné | MEDIUM | `llm/` : seul `MockLLMProvider` | Promesse « explication » non tenue | M3 |
| G-20 Connecteur Shopify absent | HIGH | `connections.kind IN ('csv_upload')` ; `data_snapshots.source IN ('csv')` ; `job_type IN ('import','analysis','purge')` | Export manuel ; pas de synchronisation continue | M3 |
| G-21 Pas de planification ni d'enchaînement | HIGH | R-29 ; handoff 004.2 §15 | Pas de produit continu | M3 (enchaînement import→analyse dès 004.4) |
| G-22 Approbations Shopify (`read_all_orders`, données client protégées) | HIGH [EXTERNE] | audit §13 | Profondeur d'historique limitée ; délai inconnu | M3 (démarche à lancer maintenant) |
| G-23 Pas de suppression d'organisation | HIGH | R-21 ; purge limitée aux travaux et à l'audit | Résiliation et `shop/redact` impossibles | M2 |
| G-24 Aucune facturation, aucun plan, aucune limite | HIGH | audit §17 | Impossible de facturer en libre-service | M4 |
| G-25 Mémoire d'analyse (4,6 Go à 1 M) | MEDIUM | `benchmarks/results/persistence_*.json` | OOM avec plusieurs gros travaux | 005.2 (dimensionnement minimal dès 004.9) |
| G-26 Instantanés en copie complète | MEDIUM | ADR-004.1-004 | Croissance de stockage avec synchronisation quotidienne | 005.2 (rétention dès 004.12) |
| G-27 Textes moteur en français sans accents, codés en dur | MEDIUM | `analytics/`, `reporting/` | Interface non localisable | 004.7 |
| G-28 Documentation obsolète | LOW | `ROADMAP.md`, `TODO.md`, `ARCHITECTURE.md`, `DATA_MODEL.md` | Planification trompeuse | 004.3 |

---

## 4. Architecture Decision

### 4.1 Architecture cible (12 à 24 mois)

```
                       ┌──────────────────────────────┐
 Navigateur ─────────► │ web/ — Next.js (lecture,      │   aucun calcul métier en TypeScript
                       │ types générés depuis OpenAPI) │
                       └──────────────┬───────────────┘
                                      │ HTTPS, Bearer OIDC
 Webhooks Shopify ──►  ┌──────────────▼───────────────┐
 (HMAC)                │ mervio api — FastAPI          │   même base de code Python
                       │ projections de rapports,     │   aucun KPI calculé à la demande
                       │ mise en file uniquement      │
                       └──────────────┬───────────────┘
                                      │ TenantSession (RLS)
                       ┌──────────────▼───────────────┐      ┌───────────────────────┐
                       │ PostgreSQL 17 managé (PITR)  │◄────►│ Stockage objet S3     │
                       │ tenants, instantanés,        │      │ CSV, JSONL Shopify,   │
                       │ rapports, jobs, audit,       │      │ clés par org/boutique │
                       │ schedules, explications      │      └──────────▲────────────┘
                       └──────────────▲───────────────┘                 │
                                      │ dispatcher (métadonnées)        │
                       ┌──────────────┴───────────────┐                 │
                       │ mervio worker — même image   │─────────────────┘
                       │ import · sync · analysis ·   │
                       │ explanation · purge · redact │──► Fournisseur LLM (contexte agrégé, sans PII)
                       └──────────────────────────────┘──► Shopify Admin GraphQL
```

- **Deux processus Python** (`mervio api`, `mervio worker`) issus d'**une seule image**.
- **Un frontend** Next.js.
- **Une base** PostgreSQL et **un stockage objet**.
- Aucun autre composant d'infrastructure.

### 4.2 Justification des choix

| Choix | Justification | Condition de réévaluation |
|---|---|---|
| **Monolithe modulaire Python** | Le moteur, la persistance, la file et le LLM partagent le même modèle de domaine et les mêmes garanties d'isolation (`TenantSession`). Les frontières sont déjà imposées par des tests AST (`test_persistence_boundaries.py`). Une équipe réduite ne peut pas porter plusieurs déploiements. | Une équipe par domaine, ou un composant aux besoins de ressources radicalement différents et mesurés. |
| **Next.js** | Déjà décidé (ADR-004-010). Écosystème mature pour un tableau de bord authentifié. Acceptable comme second langage **si** les types viennent d'OpenAPI et si aucune logique métier n'y vit. | Aucune prévue. |
| **PostgreSQL** | RLS forcée, contraintes, déclencheurs d'immuabilité, file et audit dans la même transaction. 1 M de commandes par boutique mesuré. | Partitionnement ou instantanés incrémentaux avant ~1 000 boutiques en synchronisation quotidienne. |
| **Stockage objet** | Déjà décidé (ADR-004-002). Remplace les chemins de fichiers (G-02), accueille les résultats Bulk Shopify, rend la provenance rejouable pendant la durée de rétention. | Aucune. |
| **File PostgreSQL** | Débit mesuré de plusieurs ordres de grandeur au-dessus du besoin. Hérite de la RLS et s'écrit dans la même transaction que l'audit. | Contention soutenue **mesurée**, ou webhooks que PostgreSQL ne peut plus absorber. Aucun des deux n'est observé. |
| **LLM dans le worker** | L'appel est lent, coûteux et faillible : il doit être asynchrone, borné, rejouable et hors du chemin de requête HTTP. Le type de travail `explanation` réutilise baux, reprises et audit. | Aucune. |
| **REST** | Ressources stables (boutiques, exécutions, rapports, constats), cache par ETag sur rapports immuables, contrat OpenAPI testable, génération de types. | Aucune. GraphQL n'apporte rien à un tableau de bord en lecture sur projections fixes. |

### 4.3 Pourquoi pas de microservices

1. **Aucun besoin mesuré.** Débit de file, latence de prise et taille de rapport sont largement dans l'enveloppe.
2. **L'isolation serait affaiblie.** Aujourd'hui, chaque accès passe par `TenantSession`, qui revérifie l'appartenance
   à chaque transaction. Des services séparés multiplieraient les chemins d'authentification de service à service.
3. **La vérité numérique doit rester unique.** Un service « analytics » séparé inviterait à recalculer des KPI
   ailleurs (API, SQL, frontend), ce que le principe fondateur interdit.
4. **Coût opérationnel.** Tracing distribué, versionnement inter-services, déploiements coordonnés : sans valeur
   pour un produit sans client.
5. **La séparation utile existe déjà** : deux processus (API et worker) qui passent à l'échelle indépendamment.

### 4.4 Écarts assumés par rapport à l'ordre de l'audit

| Audit | Cette roadmap | Raison |
|---|---|---|
| Shopify Data Core (004.4) avant l'API | Contrat de données + protection (004.4) avant l'API ; Shopify après le produit visible | Ce qui change le schéma exposé par l'API (identité client, fuseau, objets bruts, type de connexion, référence de secret) est décidable **sans** code Shopify. Les ajouts Shopify ultérieurs (variantes, dates de remboursement, allocations de remise) sont **additifs** et n'altèrent pas les routes. Le produit visible arrive 2 missions plus tôt. |
| Protection des données en 004.10 | 004.4 | G-01 et G-02 doivent être fermés **avant** la première route d'import (règle absolue 16). |
| Durcissement analytique après l'API | Correction du contrat de rapport **avant** l'API (004.5) ; durcissement du signal avant le pilote (004.8) | L'API et l'UI se construisent sur la structure du rapport. Changer cette structure après = retravail. |
| Dispatcher et identité de service dans la mission Shopify Install | 004.3 | Une organisation créée par inscription (004.6) doit être servie sans reconfigurer le worker. |
| Premier marchand réel après Shopify + LLM + Prod | Pilote CSV en production dès 004.9 | La validation D-046 exige justement un export natif. Mieux vaut l'obtenir avant de construire le connecteur. |

---

## 5. FINAL MISSION ROADMAP

### 5.0 Vue d'ensemble

| # | Mission | Priorité | Taille | Dépend de | Jalon |
|---|---|---|---|---|---|
| 004.3 | Platform Baseline & Worker Runtime | P0 | M | — | |
| 004.4 | Data Protection & Safe Ingestion | P0 | M | 004.3 | |
| 004.5 | Report Contract 2.0 & Analytics Correctness | P0 | M | 004.4 | |
| 004.6 | API v1 — Vertical Slice | P0 | L | 004.3, 004.4, 004.5 | |
| 004.7 | Web App v1 — Brief, Problem, Evidence | P1 | M/L | 004.6 | **M1** |
| 004.8 | Analytics Hardening | P1 | M | 004.5 (parallélisable avec 004.6–004.7) | |
| 004.9 | Hosted Environment, Operations & Legal Baseline | P0 (données réelles) | M | 004.6, 004.7, 004.8 | **M2** |
| 004.10 | Production LLM Explanations | P1 | M | 004.5, 004.7, 004.9 | |
| 004.11 | Shopify Connector Core | P1 | L | 004.4, 004.8, 004.9 | |
| 004.12 | Shopify Install, Webhooks & Continuous Sync | P1 | L | 004.6, 004.11 | |
| 004.13 | Pilot Program & Production MVP | P1 | M | 004.10, 004.12 | **M3** |
| 005.0 | Billing & Entitlements | P1 (payant) | M | M3 + décision canal | |
| 005.1 | Self-Serve Account Lifecycle & Engagement | P1 (payant) | M | 005.0 | **M4** |
| 005.2 | Scale Readiness | P3 | L | M4 | |
| 005.3 | Public Shopify App Distribution | P2/P3 | M | M4 [EXTERNE] | |
| 006.0 | Action Layer v1 | P4 | L | M4 + preuve de valeur | |
| 006.x | Connecteurs additionnels (Google Ads API, Meta, GA4…) | P4 | — | M4 | |

**Tailles relatives** (S/M/L) : la capacité de l'équipe est inconnue, aucune date n'est donnée.

### 5.0.1 Sort des anciennes missions

| Ancienne mission | Décision | Nouvelle place |
|---|---|---|
| 004.3 API v1 | **Conservée, renommée, déplacée** : dispatcher et identité de service sortis vers 004.3 ; `schedules` sortis vers 004.12 ; import par stockage objet ajouté ; recommandations suivies ajoutées | 004.6 |
| 004.4 Shopify GraphQL | **Scindée** : contrat de données avancé en 004.4 ; cœur du connecteur en 004.11 ; installation et synchronisation en 004.12 | 004.4 / 004.11 / 004.12 |
| 004.5 Frontend | **Conservée, recentrée** en lecture seule, déplacée | 004.7 |
| 004.6 Production LLM + Action approval | **Scindée** : LLM → 004.10 ; actions → **reportées** en 006.0 (P4). Seuls « fait » / « écarté » sur recommandation restent (004.6–004.7) | 004.10 / 006.0 |
| Audit : Platform Baseline | **Conservée, élargie** (dispatcher, identité de service, CLI de provisionnement, enchaînement minimal) | 004.3 |
| Audit : Production Environment & Data Protection | **Scindée** : protection avancée en 004.4 ; environnement en 004.9 | 004.4 / 004.9 |
| Audit : Analytics Hardening | **Scindée** : contrat de rapport et corrections en 004.5 ; signal en 004.8 ; signaux dépendant de Shopify en 004.11 | 004.5 / 004.8 / 004.11 |
| Audit : Commercial SaaS (005.0) | **Scindée** : facturation (005.0) et cycle de vie du compte (005.1) | 005.0 / 005.1 |
| `docs/ROADMAP.md` : Mission 002 (LLM), 003 (PDF), 004 (FastAPI), 005 (OAuth) | **Supprimées comme plan** (obsolètes) ; le PDF passe en P4 | archivage en 004.3 |
| Nouvelle | **Créée** : Pilot Program & Production MVP ; Scale Readiness ; Public App Distribution | 004.13 / 005.2 / 005.3 |

### 5.0.2 Invariants de toutes les missions

- Les 1097 tests existants passent ; toute modification d'un test est justifiée par écrit (ADR ou décision).
- Les révisions Alembic `0001`–`0005` ne sont plus modifiées. Tout changement de schéma = **nouvelle révision**,
  montée et descente testées, `test_persistence_migrations.py` étendu.
- Aucun SQL hors de `mervio.persistence` ; `mervio.workers` sans pilote de base (tests AST).
- Aucun KPI calculé ailleurs que dans le moteur déterministe (API, SQL, frontend et LLM inclus).
- Toute modification du rapport = **version de contrat de rapport** incrémentée, goldens régénérés **avec
  justification écrite**, contexte LLM adapté.
- Tout nouvel événement d'audit passe par une migration de la contrainte `CHECK` de `0005_audit`.
- Aucun secret, aucune PII, aucun prompt dans les logs, l'audit ou les charges de travail.

---

### Mission 004.3 — Platform Baseline & Worker Runtime

**Priority:** P0

**Objective**
Rendre le système existant vérifié en continu et exécutable comme processus, pour un nombre quelconque
d'organisations.

**Why now**
- Aucune CI : une régression passe inaperçue (R-25).
- Le worker n'est pas lançable et ne sert que des tenants listés à la main (G-05, G-06).
- Le bail fixe rend dangereux tout travail long (G-04).
- Toutes les missions suivantes en dépendent.

**Dependencies**
- Aucune.
- Préalable : commiter `docs/MERVIO_FULL_AUDIT.md` et ce document ; configurer un dépôt distant (la CI en a besoin).

**Scope**
1. **CI** (fournisseur au choix, GitHub Actions par défaut) :
   - service PostgreSQL 17 ;
   - `MERVIO_REQUIRE_DATABASE_TESTS=1` : un test DB ignoré fait échouer le build ;
   - comptage attendu des tests (≥ 1097, 0 ignoré) ;
   - analyse de secrets ; audit des dépendances et des licences ;
   - lint minimal sans reformatage massif.
2. **Entrée processus `mervio worker`** :
   - configuration par variables d'environnement (module `settings` unique, validé au démarrage) ;
   - journalisation JSON configurée au démarrage ; **unification** avec `mervio.logging_config` ;
   - arrêt propre sur SIGTERM/SIGINT : fin du travail courant ou libération du bail ;
   - `recover_stale` exécuté périodiquement ;
   - limite de concurrence par worker (1 par défaut, vu les 4,6 Go à 1 M).
3. **Renouvellement de bail (heartbeat)** :
   - `jobs.renew_lease(session, job, worker_id)` : prolonge `lease_expires_at` seulement si `locked_by` correspond ;
   - fil de heartbeat pendant l'exécution ; perte du bail → arrêt du gestionnaire (`JobLeaseLost`).
4. **Identité de service et dispatcher** (conçus dans ADR-004.2-002) :
   - utilisateur de service par organisation (ou type d'acteur `service`), pour que l'audit n'attribue plus un
     travail à un humain ;
   - rôle `mervio_dispatcher` et fonction `SECURITY DEFINER` qui ne renvoie que des métadonnées d'aiguillage
     (organisation, nombre de travaux prêts) ;
   - le worker découvre les organisations ayant du travail, avec équité (tour de rôle).
   > **Mise à jour 004.3.3 (implémentation réelle) :** cette option `SECURITY DEFINER` / rôle
   > `mervio_dispatcher` **n'a pas été retenue**. Le répartiteur adopté (révision `0007_service_identity`,
   > `src/mervio/persistence/dispatch.py`) repose sur **RLS + identité de service + autorisation explicite
   > par organisation** (`service_authorizations`), **sans fonction `SECURITY DEFINER` ni rôle
   > `mervio_dispatcher`**. Détail et raisons : `docs/DECISIONS.md` (D-050).
5. **Enchaînement minimal** : un import réussi peut demander une analyse (champ `then` dans la charge utile, validé ;
   clé d'idempotence dérivée).
6. **CLI d'administration** (`mervio admin …`) : créer utilisateur, organisation, boutique, connexion CSV ;
   mettre en file ; afficher l'état de la file. Usage opérateur et démo, jamais exposé en HTTP.
7. **Image conteneur** unique (worker + migrations ; API plus tard), utilisateur non root.
8. **`docker compose`** : PostgreSQL + migration + worker.
9. **Documentation** : `ROADMAP.md` et `TODO.md` remplacés par un renvoi vers ce document ; `ARCHITECTURE.md`
   et `DATA_MODEL.md` corrigés (G-28).
10. **Correction triviale** : `pipeline._reconcile_refunds` lit `refund_reconciliation_tolerance` (aucun
    changement de sortie, valeur identique 0,01).

**Out of scope**
- API, stockage objet, Shopify, `schedules`, métriques Prometheus/OpenTelemetry, hébergement.

**Files/components likely affected**
- `src/mervio/workers/worker.py`, `workers/__main__.py` (nouveau), `persistence/jobs.py`,
  `persistence/tenancy.py`, `persistence/audit.py`, `observability/logging.py`, `logging_config.py`,
  `settings.py` (nouveau), `cli/`, `analytics/pipeline.py` (ligne 130).
- `Dockerfile`, `docker-compose.yml`, fichier CI, `docs/*`.

**Database impact**
- Migration `0006` : rôle et fonction dispatcher ; identité de service ; actions d'audit nouvelles
  (`lease_renewed` si audité, `job_chained`).
- Aucune modification des tables existantes au-delà des contraintes `CHECK`.

**API impact** — aucun.

**Security impact**
- Ferme SEC-06 (identité de service) et SEC-08 (bail).
- Le dispatcher ne voit que des métadonnées : aucune donnée métier hors `TenantSession`.
- Analyse de secrets et de dépendances en CI (SEC-11).

**Tests required**
- Heartbeat : un travail plus long que le bail **n'est pas** repris par un autre worker.
- Perte de bail : le gestionnaire s'arrête et aucune écriture tardive n'est acceptée.
- SIGTERM : le travail courant se termine ou redevient `queued` sans attendre l'expiration.
- Dispatcher : 1 000 organisations synthétiques, aucune famine ; le rôle dispatcher ne lit aucune table métier
  (test SQL brut).
- Identité de service : les audits de travaux portent `actor_type = worker` et un identifiant de service.
- Enchaînement : import → analyse, idempotent en cas de rejeu.
- CI : un test DB ignoré fait échouer le build (test du garde-fou).
- Conteneur : import CSV + analyse de bout en bout contre PostgreSQL compose.

**Acceptance criteria**
- CI verte : ≥ 1097 tests + nouveaux, **0 ignoré**.
- `docker compose up` puis `mervio admin` → un rapport persisté produit par le worker conteneurisé, sans code Python
  ad hoc.
- Un travail de 2 × la durée du bail s'exécute **une seule fois** (test).
- Une nouvelle organisation créée après le démarrage du worker est servie sans redémarrage.
- Aucune régression d'octets : rapport persisté = rapport CLI.

**Definition of Done**
- Critères remplis, handoff et ADR rédigés, documentation obsolète corrigée, fusion sur `master` après CI verte.

**What this unlocks**
- Itération sûre ; worker exploitable par l'API ; base du déploiement (004.9).

---

### Mission 004.4 — Data Protection & Safe Ingestion

**Priority:** P0

**Objective**
Rendre le modèle de données compatible avec l'effacement légal **sans perdre la provenance**, remplacer les chemins
de fichiers par des objets appartenant au tenant, et figer le contrat de données que l'API exposera.

**Why now**
- G-01 et G-02 doivent être fermés **avant** la première route HTTP qui accepte des données.
- L'identité client et le fuseau déterminent le schéma que l'API exposera : les fixer avant évite un retravail.
- Aucune donnée réelle n'est encore stockée : la migration est la moins chère possible **maintenant**.

**Dependencies** — 004.3.

**Scope**
1. **ADR « Identité client et PII »** (détail en §11) :
   - la clé client persistée devient `customer_ref = HMAC-SHA256(clé_org, identité_source_normalisée)` ;
   - identité source : identifiant plateforme quand il existe (Shopify GID plus tard), sinon e-mail normalisé
     (CSV) ; **l'e-mail n'est jamais persisté** ;
   - clé de pseudonymisation **par organisation**, dérivée d'une clé maître hors base (variable d'environnement en
     local, KMS en 004.9) ;
   - commandes invitées : `guest:` + HMAC de l'identifiant de commande (comportement actuel conservé, sans donnée
     brute).
     > **Implémenté en 004.4.2 sous la forme `g1:` + 128 bits de HMAC** (et `c1:` pour un client identifié par
     > e-mail) : le préfixe versionné remplace `guest:` ; aucune référence `guest:<commande>` n'est plus produite
     > (D-053, `src/mervio/identity.py`).
2. **Migration** :
   - suppression de `orders.customer_email` et `payments.customer_email` (aucun module analytique ne les lit :
     vérifié) ;
   - réécriture des `customer_ref` existants (données de développement uniquement) ou refus explicite de migrer des
     instantanés non synthétiques ;
   - `stores.timezone` (IANA, obligatoire à terme ; défaut `UTC` explicite et signalé) ;
   - `connections.kind` extensible (`csv_upload` ; `shopify` réservé en 004.11) et `credential_ref` nullable
     (pas encore de secret stocké) ;
   - tables `raw_objects` (organisation, boutique, clé d'objet, SHA-256, taille, type, origine, `retain_until`,
     `purged_at`) et `customer_redactions` (HMAC seulement, date, origine).
3. **Chemin d'effacement contrôlé** :
   - fonction `SECURITY DEFINER` possédée par un rôle dédié `mervio_redactor`, seule autorisée à remplacer un
     `customer_ref` par un jeton de tombstone dans les lignes canoniques ;
   - les déclencheurs d'immuabilité autorisent **uniquement** cette colonne, **uniquement** via cette fonction ;
   - travail `redact_customer` (nouveau type) ; audit obligatoire, sans la valeur effacée.
4. **Suppression d'organisation et de boutique** : travail `purge_organization` / `purge_store` (rôle dédié),
   ordre documenté (objets → lignes canoniques → rapports → connexions → audit conservé selon rétention).
5. **Abstraction de stockage objet** :
   - interface `ObjectStore` : pilote système de fichiers **confiné** (racine configurée, clés générées, jamais de
     chemin fourni par l'appelant) et pilote compatible S3 ;
   - clés `org/<org_id>/store/<store_id>/raw/<uuid>` ;
   - la charge d'import ne contient plus que `raw_object_id` ; `validate_payload` **refuse** toute clé
     `path`/`sources` de chemin ;
   - la lecture passe par `TenantSession` (RLS) puis par le magasin d'objets ;
   - la CLI locale garde ses chemins (hors chemin SaaS).
6. **Politique de rétention v0** (document) : objets bruts 30 jours par défaut, instantanés, rapports, travaux,
   audit — marquée ⚖️ à valider en 004.9.
7. **Contrat de reproductibilité** : la garantie « rapport persisté = rapport CLI » devient « à clé de
   pseudonymisation identique » ; la CLI accepte une clé explicite ou une clé éphémère.

**Out of scope**
- KMS réel, stockage S3 hébergé (004.9) ; jetons OAuth (004.11–004.12) ; webhooks RGPD Shopify (004.12).

**Files/components likely affected**
- `ingestion/shopify.py`, `ingestion/stripe.py`, `domain/models.py`, `persistence/snapshots.py`,
  `persistence/stores.py`, `persistence/jobs.py`, `workers/handlers.py`, `workers/retention.py`,
  `application/persisted_analysis.py`, `storage/` (nouveau), `cli/`.

**Database impact**
- Migration `0007` : colonnes supprimées, `stores.timezone`, `connections.credential_ref`, `raw_objects`,
  `customer_redactions`, rôles `mervio_redactor`/`mervio_purger`, types de travaux et actions d'audit, ajustement
  des déclencheurs.
- Nouvelles tables avec RLS `ENABLE` + `FORCE`, clés composites, clés uniques préfixées.

**API impact**
- Aucun direct ; fixe les ressources `stores`, `uploads/raw_objects`, `imports` de 004.6.

**Security impact**
- Ferme G-01 (SEC-02), G-02 (SEC-01/TD-03) ; ferme G-03 **indirectement** : le pseudonyme de rapport devient un
  hachage d'un HMAC à clé, non réversible sans la clé (le retrait complet des identifiants du rapport est fait en
  004.5).
- Ferme partiellement R-21 (suppression).

**Tests required**
- Aucune colonne e-mail dans le schéma (test de migration) ; aucune chaîne `@` de forme e-mail dans les lignes
  canoniques après import d'un CSV qui en contient.
- `customer_ref` stable pour une même organisation, différent entre deux organisations pour le même e-mail.
- Import par chemin arbitraire refusé (`payload_invalid`) ; objet d'une autre organisation → `NotFound`.
- Traversée de chemin impossible dans le pilote système de fichiers (`..`, liens symboliques, chemins absolus).
- Effacement : les lignes du client sont tombstonées, les KPI agrégés restent identiques, l'audit ne contient pas la
  valeur ; le rôle applicatif ne peut toujours pas modifier les lignes.
- Purge d'organisation : zéro ligne et zéro objet restants ; audit conservé ; autre tenant intact.
- Rapport CLI = rapport persisté à clé identique.

**Acceptance criteria**
- 0 PII directe (e-mail, nom, adresse) persistée par le chemin SaaS.
- Une demande d'effacement client s'exécute de bout en bout comme travail audité.
- Une organisation peut être entièrement supprimée par un travail audité.
- Plus aucun import SaaS ne lit un chemin fourni par l'appelant.
- Tous les tests existants passent ; les goldens concernés sont régénérés avec justification.

**Definition of Done**
- ADR identité/PII, ADR stockage objet, politique de rétention v0, handoff ; CI verte.

**What this unlocks**
- Exposition HTTP sans faille connue de données ; base des webhooks RGPD Shopify ; schéma stable pour l'API.

---

### Mission 004.5 — Report Contract 2.0 & Analytics Correctness

**Priority:** P0

**Objective**
Corriger les défauts qui rendraient une explication contradictoire ou trompeuse, et donner au rapport une structure
typée consommable par l'API, l'interface et le LLM.

**Why now**
- L'API (004.6) et l'interface (004.7) se construisent sur la structure du rapport.
- Un rapport dont la cause contredit l'anomalie ne doit jamais devenir un contrat public.

**Dependencies** — 004.4 (`stores.timezone`, identité client).

**Défauts à corriger** (identifiés, non corrigés par ce document)

| ID | Fichier / fonction | Comportement actuel | Comportement attendu | Impact | Tests nécessaires |
|---|---|---|---|---|---|
| AN-01 | `pipeline.analyze_loaded_dataset` (l. 169, 191) ; `root_cause.analyse_revenue_change` ; `insights.build_insights` | Anomalie vs moyenne des 8 périodes ; décomposition vs **période précédente** ; `estimated_impact` = écart à la baseline | Décomposition vs **la même baseline** : clics, commandes et CA agrégés sur la fenêtre de baseline, puis ratios dérivés (l'identité CA = clics × commandes/clics × panier reste exacte). La comparaison à la période précédente reste disponible, étiquetée séparément. | Explication contradictoire en déclin prolongé | Déclin prolongé où S/S-1 = +2 % et vs baseline = −30 % : contributions calculées vs baseline, somme = 1, texte cohérent |
| AN-02 | `kpi.compute_kpis` (l. 181-186) ; facteur `conversion_rate` ; `insights._recommendation_for` | « Taux de conversion » = toutes commandes / clics payants ; recommandation « auditer le checkout » | Métrique renommée `orders_per_paid_click` (clé de contrat nouvelle, ancienne retirée) ; sans données de trafic total, la décomposition devient **CA = commandes × panier moyen**, avec clics payants en preuve ; facteur `organic_traffic` marqué **non observé** ; aucune recommandation « checkout » sans trafic total observé | Baisse organique présentée comme problème de conversion | `order_volume_drop` : pas de recommandation checkout ; facteur non observé listé |
| AN-05 | `pipeline` ; `last_complete_period` | Aucune fraîcheur exposée | Bloc `data_freshness` : dernière donnée, date d'analyse, retard en jours, statut `fresh`/`stale` (seuil configurable) ; un rapport `stale` le dit en tête | Rapport périmé présenté comme actuel | Export arrêté 40 jours avant `today` → `stale` |
| AN-06 | `periods.py` ; `ingestion/base.py:307-308` | Périodes coupées en UTC | Instants stockés en UTC ; découpage dans le fuseau de la boutique (paramètre de configuration), sûr aux changements d'heure | Semaines différentes de celles du marchand | Commande dimanche 23 h 30 UTC à Paris → semaine suivante ; semaine de changement d'heure |
| AN-14 | constantes de `insights.py` (0,4 ; 0,45 ; 0,6 ; 0,7 ; 0,9) ; `root_cause._confidence` | Confiance numérique fixe par type | Niveau qualitatif `low`/`medium`/`high` dérivé de critères écrits (longueur de baseline, couverture des champs, facteurs observés, part expliquée) ; critères exposés | Fausse précision | Tableau de cas : chaque critère fait bouger le niveau |
| AN-15 | `insights.Insight.evidence: List[str]` | Chaînes préformatées en français | Objets `{metric_key, value, baseline, period, unit, comparison}` + clé de message ; identifiant stable de constat `finding_id` | API, UI et LLM reparsent des chiffres | Chaque nombre du rapport provient d'un champ typé (test de parcours) |
| AN-16 | `insights.py:137` (0,20) et `:152` (0,40, doublon de `HealthConfig.concentration_warning`) ; `health.py` ; `root_cause.py` ; `pipeline.py:130` | Seuils en dur | Seuils dans `AnalyticsConfig`, empreinte de configuration inchangée pour les valeurs par défaut | Règle du projet violée | Configuration modifiée → comportement modifié |
| AN-17 | `report.pseudonymise` ; `top_customers` | Pseudonyme par client dans un rapport immuable | Plus d'identifiant client dans le rapport : rang, part de CA, nombre de commandes | Donnée personnelle dans un artefact non effaçable | Aucune clé `customer_id` dans le rapport |
| AN-18 | `insights` `profitability_unavailable` (sévérité `medium`, catégorie `warning`) | Présent pour presque toute boutique Shopify | Catégorie `setup` (« connecter les coûts pour débloquer »), hors liste des problèmes | Alarme permanente | Boutique sans coûts → aucun problème de profitabilité en tête |
| AN-19 | textes moteur (`analytics/`, `reporting/`) | Français sans accents, figé | Clés de message + paramètres dans le rapport ; `report.txt` rendu à partir d'elles (français accentué) | Interface non localisable (G-27) | Rendu FR identique sémantiquement ; aucune chaîne libre dans les constats |

**Scope**
- Corrections AN-01, AN-02, AN-05, AN-06, AN-14 à AN-19.
- **Contrat de rapport 2.0** : JSON Schema versionné, publié dans le dépôt, validé en test ; `ENGINE_VERSION` 0.2.0.
- **Facteurs non observés** explicites : trafic organique, stock, expédition, coûts historiques.
- Liste classée de **tous** les facteurs, pas seulement le principal.
- Contexte LLM porté au contrat 2.0 (`llm/context.py`) : la suite LLM (mock) passe.
- Scénarios : les attentes changent seulement si la vérité est désormais trouvée, avec justification.

**Out of scope**
- Saisonnalité, remises, rétention, causes produit (004.8) ; signaux Shopify (004.11).

**Files/components likely affected**
- `analytics/{pipeline,kpi,root_cause,insights,anomaly,periods,report,health}.py`, `config.py`,
  `reporting/*`, `llm/context.py`, `llm/response.py`, `synthetic/scenarios.py`, `tests/golden/*`.

**Database impact**
- Aucune migration. Les rapports existants restent lisibles (version 1.x) ; l'API ne servira que la 2.x
  (les rapports 1.x de développement sont à régénérer).

**API impact** — définit le modèle des ressources `reports`, `findings`, `evidence`, `data-quality`.

**Security impact** — AN-17 retire toute donnée client du rapport.

**Tests required** — ceux du tableau, plus validation JSON Schema de tous les goldens et des 17 scénarios,
et identité octet pour octet CLI/persisté maintenue.

**Acceptance criteria**
- Tous les défauts du tableau ont un test qui échouait avant et passe après.
- 100 % des rapports produits (goldens, scénarios) valident le schéma 2.0.
- Aucun nombre dans une chaîne de preuve.
- 7 scénarios entièrement couverts restent couverts ; `order_volume_drop` n'émet plus de recommandation checkout.
- Suite LLM verte sur le contrat 2.0.

**Definition of Done** — ADR « Report Contract 2.0 », table de correspondance 1.x → 2.0, goldens justifiés, CI verte.

**What this unlocks** — un contrat de rapport exposable ; le LLM et l'interface sur une base stable.

---

### Mission 004.6 — API v1 — Vertical Slice

**Priority:** P0

**Objective**
Une API HTTP authentifiée, sûre entre tenants, qui permet le parcours complet
**inscription → boutique → dépôt CSV → travail → rapport → constats → preuves → recommandation**.

**Why now**
- Le frontend en dépend entièrement.
- Les préalables de sécurité (004.3, 004.4) et de contrat (004.5) sont en place.

**Dependencies** — 004.3, 004.4, 004.5.

**Contrat construit à partir du produit (routes minimales)**

| Étape produit | Route | Rôle min. | Notes |
|---|---|---|---|
| Santé | `GET /healthz`, `GET /readyz` | aucun | base joignable, révision de migration = tête |
| Identité | `GET /v1/me` | authentifié | utilisateur, organisations, rôles, boutiques |
| Onboarding | `POST /v1/organizations` | authentifié | crée l'organisation et l'appartenance `owner` ; idempotent |
| Boutique | `POST /v1/stores`, `GET /v1/stores`, `GET/PATCH /v1/stores/{store_id}` | admin / viewer | nom, devise, **fuseau** |
| Données | `POST /v1/stores/{store_id}/uploads` | analyst | multipart, taille bornée, type vérifié → `raw_object` ; renvoie `raw_object_id` |
| Import | `POST /v1/stores/{store_id}/imports` | analyst | `raw_object_id` par source ; `Idempotency-Key` ; `202` + `job_id` ; enchaîne l'analyse |
| Statut | `GET /v1/stores/{store_id}/jobs`, `GET …/jobs/{job_id}` | viewer | progression d'onboarding |
| Instantanés | `GET /v1/stores/{store_id}/snapshots`, `GET …/snapshots/{id}` | viewer | statut, période couverte, qualité ; jamais de lignes brutes |
| Analyse | `POST /v1/stores/{store_id}/analysis-runs` ; `GET …/analysis-runs[/{run_id}]` | analyst / viewer | relance manuelle |
| Brief | `GET /v1/stores/{store_id}/briefs/latest?grain=week` | viewer | projection : résumé, KPI clés, fraîcheur, top constats |
| Rapport | `GET /v1/stores/{store_id}/analysis-runs/{run_id}/report` | viewer | rapport 2.0 intégral, ETag, immuable |
| Problème | `GET /v1/stores/{store_id}/findings?run_id=`, `GET …/findings/{finding_id}` | viewer | constat + preuves typées + facteurs (observés et non observés) + limites + confiance |
| Séries | `GET …/analysis-runs/{run_id}/series/{metric}` | viewer | projection du rapport |
| Qualité | `GET …/analysis-runs/{run_id}/data-quality` | viewer | couverture, avertissements, sources absentes |
| Suite | `GET /v1/stores/{store_id}/recommendations`, `POST …/{id}/status` (`done` / `dismissed` + motif) | viewer / analyst | seule écriture « action » du MVP |
| Audit | `GET /v1/organization/audit-events` | admin | lecture auditée |

**Volontairement absentes** : connexions Shopify et webhooks (004.12), explications (004.10), actions (006.0),
membres et invitations (005.1), facturation (005.0), provenance détaillée (lecture analyste, P2).

**Scope**
- FastAPI + Pydantic v2 comme source du contrat ; OpenAPI 3.1 figé et testé.
- Validation de jetons OIDC (fournisseur choisi par ADR ; critères : hébergement UE, MFA, coût, SDK Next.js).
  `users.idp_subject` existe.
- Pool de connexions compatible avec `set_config(..., true)` (ADR-004.1-006).
- `problem+json` (RFC 9457), codes stables ; pagination par curseur opaque ; filtres en liste blanche.
- `Idempotency-Key` → `jobs.idempotency_key` ; table `idempotency_keys` pour les autres POST.
- Limitation de débit PostgreSQL (seau à jetons) par utilisateur et organisation, plus stricte sur dépôts et imports.
- En-têtes de sécurité, CORS restreint, taille de corps bornée.
- `request_id` → `correlation_id`.
- Table `recommendation_states` (organisation, boutique, `finding_id`, statut, motif, auteur, date).
- Correction SEC-05/TD-27 : `users` lisible seulement pour sa propre ligne (vue ou politique).
- `mervio api` dans la même image ; compose : PostgreSQL + API + worker + stockage objet local compatible S3.
- **Test AST : aucune route ne calcule de KPI** (pas d'import `analytics` dans `api/` hors lecture de schéma).

**Out of scope** — interface, Shopify, LLM, facturation, invitations.

**Files/components likely affected** — `src/mervio/api/` (nouveau), `persistence/*` (curseurs, projections,
`recommendation_states`, `idempotency_keys`, rate limit), `settings.py`, `pyproject.toml` (extra `api`),
`Dockerfile`, `docker-compose.yml`.

**Database impact** — migration `0008` : `recommendation_states`, `idempotency_keys`, `rate_limit_buckets`,
politique `users`, actions d'audit.

**API impact** — création de `/v1`.

**Security impact**
- Premier point d'exposition : isolation HTTP (R-01), autorisation par route, pas d'écho d'entrée, pas de trace.
- Organisation **toujours** dérivée de l'appartenance, jamais d'un paramètre client.

**Tests required**
- **Par route** : succès, validation, non authentifié (401), rôle insuffisant (403 ou 404 selon la règle),
  **accès croisé = 404**, idempotence, pagination.
- Jeton expiré, mauvaise audience, mauvaise signature.
- Dépôt : fichier trop gros, binaire, ZIP, nom de fichier hostile.
- Contrat OpenAPI (instantané comparé).
- Limitation de débit.
- E2E API : inscription → boutique → dépôt → import → rapport → constat → statut de recommandation.
- Performance : lecture de rapport persisté p95 < 300 ms (local, puis staging en 004.9).

**Acceptance criteria**
- 100 % des routes couvertes par la matrice de tests ci-dessus.
- Le parcours E2E API passe sur des données synthétiques **et** OH5.
- Aucune route n'accepte un chemin de fichier ; aucune ne renvoie de PII.
- OpenAPI figé en CI.

**Definition of Done** — ADR IdP, ADR API, handoff, CI verte, compose complet.

**What this unlocks** — le frontend ; plus tard les routes Shopify et d'explication.

---

### Mission 004.7 — Web App v1 — Brief, Problem, Evidence

**Priority:** P1

**Objective**
Une interface web qui montre clairement **WHAT → WHY → EVIDENCE → WHAT NEXT** à partir de rapports persistés.

**Why now** — premier produit visible (M1) ; valide l'architecture de l'information avec des pilotes avant de
construire Shopify.

**Dependencies** — 004.6.

**Écrans minimum**

| Écran | Contenu | Données |
|---|---|---|
| Connexion | via l'IdP | OIDC |
| Onboarding | créer l'organisation, la boutique (devise, **fuseau**) | `POST /v1/organizations`, `/v1/stores` |
| Connexion des données | dépôt CSV guidé (quels exports, où les trouver dans Shopify), état de validation | `uploads`, `imports` |
| Traitement | progression import → analyse, erreurs compréhensibles | `jobs` |
| Brief | période, fraîcheur, KPI clés avec source et qualité, 3 problèmes prioritaires, score de santé avec exclusions | `briefs/latest` |
| Problème | **WHAT** (fait) · **WHY** (facteurs classés, non observés, étiquetés « inférence ») · **EVIDENCE** (chiffres, série, contributeurs produit) · **WHAT NEXT** (recommandation, fait/écarter) · confiance et limites | `findings/{id}`, `series` |
| Qualité des données | couverture, sources manquantes, ce qui se débloque en les ajoutant | `data-quality` |
| État de connexion | dernier import, dernière analyse, fraîcheur | `snapshots`, `jobs` |

**Scope**
- `web/` Next.js, types générés depuis OpenAPI, aucun calcul métier.
- Rendu des clés de message du rapport 2.0 en **français** (anglais préparé, non livré).
- FACT / INFERENCE / RECOMMENDATION visuellement distincts ; badge « synthétique » systématique.
- États vides, de chargement et d'erreur ; responsive ; clavier ; axe.
- Organisation de démonstration synthétique (clairement marquée).
- Zone « explication » réservée : affiche le texte déterministe (contrat D-037) jusqu'en 004.10.
- Compose : `web` ajouté.

**Out of scope** — paramètres complets, membres, facturation, administration, actions, Shopify, e-mails.

**Files/components likely affected** — `web/` (nouveau), CI (build, lint, tests, E2E), compose.

**Database impact** — aucun. **API impact** — ajustements mineurs seulement, par révision de contrat.

**Security impact** — jeton hors `localStorage` (flux recommandé par l'IdP), CSP, aucune donnée sensible en URL.

**Tests required**
- Composants : chaque nombre affiché correspond à un champ d'API (test de correspondance).
- E2E (Playwright) : connexion → onboarding → dépôt → traitement → brief → problème → écarter.
- Accessibilité : axe sans violation critique.
- Accès croisé : URL d'une autre organisation → page « introuvable ».

**Acceptance criteria**
- Le parcours E2E passe sur compose.
- Aucun nombre calculé côté client (revue + test).
- Un utilisateur non technique comprend le problème principal en moins d'une minute (test d'usage avec 3 personnes,
  consigné).

**Definition of Done** — handoff avec captures, CI verte (Python + web).

**What this unlocks** — **M1 — FIRST VISIBLE PRODUCT** ; démonstrations aux pilotes (données synthétiques).

---

### Mission 004.8 — Analytics Hardening

**Priority:** P1

**Objective**
Supprimer les fausses alertes et les mauvaises attributions connues **avant** qu'un marchand réel ne les voie,
avec les données déjà disponibles (sans Shopify API).

**Why now** — la première session réelle (M2) ne doit montrer ni fausse alerte saisonnière ni cause absurde.

**Dependencies** — 004.5. Parallélisable avec 004.6–004.7.

**Défauts et manques traités**

| ID | Fichier / fonction | Actuel | Attendu | Impact | Tests |
|---|---|---|---|---|---|
| AN-04 | `anomaly.detect_anomalies` | Aucune saisonnalité | Avec ≥ 12 mois : contexte a/a de la même période ; écart **attendu saisonnièrement** → rétrogradé en `info` avec explication ; champ `seasonal_context` | Fausse alerte | `seasonal_business` : `KNOWN_GAP` → `MATCH` ; déclin réel pendant une saison haute toujours détecté |
| AN-07 | `kpi` ; `root_cause` | Remise informative ; panier moyen seul | Métrique et série `discount_rate = Σ remise / (Σ sous-total + Σ remise)` ; panier moyen décomposé en panier brut × (1 − taux de remise) ; catégorie de recommandation `discount_policy` | Remise prise pour un problème de panier | `discount_explosion` → `MATCH` |
| AN-08 | `kpi.new_customers` ; `report` | Réachat intra-période | Décomposition commandes nouveaux / récurrents ; part du CA récurrent en série ; taux de réachat à 90 jours sur cohortes **complètes** ; couverture d'identité (invités exclus et comptés) | Attrition invisible | `customer_churn`, `retention_decline` → `MATCH` ou non-observé justifié |
| AN-03 | `insights` (l. 66) | Cause seulement pour une baisse de CA | Décomposition aussi pour une hausse (opportunité) et pour une anomalie de commandes ou de panier sans anomalie de CA | « Qu'est-ce qui a marché ? » sans réponse | Hausse de CA → facteurs classés |
| AN-20 | `root_cause._contributors` | Contributeurs produit additifs, non interprétés | Facteur « concentration produit » : si ≥ X % de la baisse vient de ≤ N produits → cause `product_demand` avec preuves | Effondrement d'un produit non nommé | `product_failure` → `MATCH` |
| AN-21 | `anomaly` | « Nouveau ou persistant ? » inconnu | Indicateur `persisting_since` (même anomalie à la période précédente) | Alerte répétée sans contexte | Deux périodes successives |
| AN-09/10 | — | Marge historique, stock, expédition absents | Facteurs **non observés** explicites, avec ce qui les débloquerait | Silence trompeur | `margin_collapse`, `stockout`, `shipping_problem` : non-observé listé |

**Scope** — tableau ci-dessus ; rapport 2.1 (additif) ; recommandations par catégorie ; scénarios mis à jour ;
**benchmark « scénarios » corrigé** : rapport séparé `MATCH` / `KNOWN_GAP` / faux positifs, plus de total unique.

**Out of scope** — données Shopify (dates de remboursement, allocation des remises par ligne, variantes, stock réel).

**Files/components likely affected** — `analytics/*`, `config.py`, `synthetic/*`, `benchmarks/scenario_robustness.py`,
`llm/context.py` (champs additifs), goldens.

**Database impact** — aucun. **API impact** — champs additifs (rapport 2.1), sans rupture.

**Security impact** — aucun.

**Tests required** — ceux du tableau ; nouveaux scénarios « saison haute + vrai déclin » et « boutique saine
saisonnière » ; non-régression des 7 scénarios couverts ; propriétés (somme des contributions, absence de NaN).

**Acceptance criteria**
- **0 fausse alerte** sur `healthy_store` et `seasonal_business` (5 graines × 2 profils).
- Au moins 4 attentes `KNOWN_GAP` deviennent `MATCH` (saison, remise, produit, rétention).
- Taux de faux positifs publié par scénario.
- Aucune régression sur les 7 scénarios déjà couverts.

**Definition of Done** — ADR, benchmark publié, goldens justifiés, CI verte.

**What this unlocks** — premières sessions réelles crédibles.

---

### Mission 004.9 — Hosted Environment, Operations & Legal Baseline

**Priority:** P0 (avant toute donnée réelle)

**Objective**
Un environnement où des données marchandes peuvent vivre, et le cadre juridique qui l'autorise. Premier pilote CSV.

**Why now** — aucune donnée réelle ne peut rester sur un poste de développeur ; le pilote CSV valide le contrat de
revenu (D-046) avant d'investir dans Shopify.

**Dependencies** — 004.6, 004.7, 004.8. La partie staging peut démarrer dès 004.6.

**Scope**
1. **Hébergement** (UE, ADR) : PaaS de conteneurs ; PostgreSQL managé avec PITR et chiffrement au repos ; stockage
   objet ; KMS/gestionnaire de secrets (clé maître de pseudonymisation, secrets IdP) ; TLS et domaine.
2. **Environnements** : `staging` (synthétique uniquement) et `production` ; configuration séparée ; aucune donnée
   de production en staging.
3. **Livraison continue** : image construite en CI, déploiement staging automatique, production manuelle approuvée ;
   migrations en étape dédiée avec `lock_timeout` ; **runbook de migration**.
4. **Observabilité** : logs centralisés (format unique), suivi d'erreurs sans PII, métriques (profondeur et âge de
   file, échecs par code, durée et mémoire d'analyse, HTTP p95 et 5xx), alertes (travaux `failed`, âge de file,
   5xx, échec de sauvegarde), sonde de disponibilité.
5. **Sauvegardes** : PITR, **exercice de restauration documenté** ; rétention des sauvegardes alignée sur la
   politique de suppression ⚖️.
6. **Dimensionnement** : benchmark import + analyse **en tant que travaux** à 100 k et 1 M ; limites mémoire et
   concurrence des workers fixées à partir de ces mesures.
7. **Rétention** : purge planifiée des objets bruts (`retain_until`) ; purge des travaux et de l'audit (tâche
   périodique du worker, en attendant `schedules`).
8. **Juridique ⚖️** : politique de confidentialité, conditions d'utilisation du pilote, DPA, liste des
   sous-traitants (hébergeur, IdP, stockage) ; registre des traitements ; revue par un professionnel.
9. **Outillage opérateur** : commandes `mervio admin` pour diagnostiquer un tenant (dernier import, dernier échec,
   fraîcheur) sans accès aux lignes canoniques.
10. **Pilote CSV** : procédure d'onboarding, exécution de `scripts/validate_real_export.py` sur l'export du pilote
    **dans l'environnement de production**, écarts documentés et testés.

**Out of scope** — Shopify, LLM, facturation, multi-région, Kubernetes.

**Files/components likely affected** — infrastructure (fichiers de déploiement), `Dockerfile`, CI, `settings.py`,
`workers/retention.py`, `cli/`, `docs/runbooks/*`, `docs/legal/*` (brouillons).

**Database impact** — aucune nouvelle table requise ; éventuel index pour la purge des objets.

**API impact** — aucun ; `readyz` branché sur la plateforme.

**Security impact** — secrets hors image et hors dépôt ; TLS ; accès production restreint et audité ;
revue de sécurité légère (en-têtes, débit, dépendances, OWASP ASVS niveau 1 sur les routes).

**Tests required** — fumée post-déploiement (E2E sur staging) ; alertes déclenchées volontairement en staging ;
restauration ; purge des objets expirés ; performance p95 < 300 ms en staging.

**Acceptance criteria**
- Exercice de restauration réussi et chronométré.
- Aucun secret dans l'image ou le dépôt (analyse CI).
- Alertes reçues en staging pour chaque règle.
- Documents juridiques revus ⚖️ et acceptés par le pilote.
- Export natif du pilote passé dans le harnais D-046 ; chaque écart a une décision écrite et un test.
- Le pilote se connecte, dépose son export et voit son Brief **sans intervention d'un développeur**.

**Definition of Done** — runbooks (déploiement, migration, restauration, incident), handoff, pilote actif.

**What this unlocks** — **M2 — FIRST REAL MERCHANT** (CSV) ; accueil de données réelles pour le LLM et Shopify.

---

### Mission 004.10 — Production LLM Explanations

**Priority:** P1

**Objective**
Une explication en langage business, **validée**, pour chaque rapport :
`constats structurés → LLM → explication validée`. Le LLM n'est jamais source de vérité numérique.

**Why now**
- Complète le parcours WHAT → WHY → EVIDENCE → EXPLANATION → WHAT NEXT pour le pilote actif.
- Dépend seulement du contrat de rapport stable (004.5/004.8) et d'un environnement couvert juridiquement (004.9).
- Plus petite et moins dépendante de tiers que Shopify.

**Dependencies** — 004.5, 004.7, 004.8, 004.9.

**Scope**
- **Fournisseur** : une implémentation réelle derrière `LLMProvider` (un seul fournisseur ; choix par ADR ;
  modèle actuel à la date de la mission [EXTERNE]) ; dépendance ajoutée dans un extra `llm` (D-004 préservé pour le
  moteur).
- **Contrat** :
  - entrée : contexte structuré existant (≤ 64 ko), porté au rapport 2.x, **sans PII** ;
  - sortie : JSON à schéma fermé (sortie structurée du fournisseur quand disponible) ;
  - validation : validateur existant (ancrage des nombres, références, causalité, devise, PII, fuite
    d'instructions) ; rejet total en cas de violation.
- **Interdits vérifiés** : aucun KPI, date, pourcentage, lien causal ou donnée client absent du contexte.
- **Versionnement** : `prompt_version`, `context_contract_version`, `response_contract_version`, modèle, tous
  persistés.
- **Exécution** : type de travail `explanation`, enchaîné après l'analyse (opt-in par organisation) ; délai
  d'expiration réseau ; 1 nouvelle tentative au plus après rejet de validation, sans renvoyer le texte rejeté dans le
  prompt ; erreurs fournisseur transitoires en reprise avec backoff.
- **Budgets** : plafond de jetons par requête ; budget de coût mensuel par organisation ; compteur d'usage ;
  dépassement → explication `unavailable` (code), jamais d'erreur de rapport.
- **Repli** : texte déterministe affiché (D-037) ; l'UI indique la source.
- **Persistance** : table `explanations` (tenant, immuable) : SHA-256 du contexte, versions, modèle, jetons, coût,
  latence, codes de validation, texte validé ; **aucun prompt ni réponse brute dans les logs**.
- **Langue** : français ; règles lexicales du validateur vérifiées pour le français accentué.
- **Évaluation** : harnais hors ligne (7 goldens + 17 scénarios + cas adversariaux) avec enregistreur de réponses ;
  taux de rejet, latence, coût publiés.
- **Confidentialité ⚖️** : conditions de rétention et d'entraînement du fournisseur revues ; fournisseur ajouté à la
  liste des sous-traitants.
- **API/UI** : `GET /v1/stores/{store_id}/analysis-runs/{run_id}/explanation` ; panneau d'explication avec
  références cliquables vers les preuves.

**Out of scope** — agents, outils, base vectorielle, multi-fournisseur, mémoire inter-rapports, génération de
recommandations non référencées.

**Files/components likely affected** — `llm/*`, `workers/handlers.py`, `persistence/explanations.py`, `api/`, `web/`.

**Database impact** — migration : `explanations`, `llm_usage` (ou compteur), type de travail `explanation`, actions
d'audit.

**API impact** — une route de lecture ; option d'organisation (opt-in).

**Security impact** — nouveau sous-traitant ; contexte agrégé uniquement ; clé API dans le gestionnaire de secrets ;
injection via contenu non fiable déjà traitée (canaux séparés).

**Tests required** — suite LLM existante contre le vrai fournisseur **derrière un enregistreur** ; un chiffre
inventé → rejet ; un lien causal → rejet ; budget atteint → `unavailable` ; délai dépassé → `unavailable` ; rapport
inchangé dans tous les cas ; aucun prompt dans les logs (test de capture).

**Acceptance criteria**
- Taux de rejet mesuré et publié ; **≤ 10 %** sur les scénarios (seuil à confirmer par la mesure, sinon ajustement du
  prompt avant activation).
- Coût moyen et p95 par explication mesurés ; budget mensuel par organisation appliqué.
- 0 explication affichée qui n'a pas passé le validateur.
- Explication indisponible → aucune incidence sur le rapport ni sur l'UI (repli visible).

**Definition of Done** — ADR fournisseur, rapport d'évaluation, handoff ; activé pour le pilote avec son accord.

**What this unlocks** — l'expérience complète pour le pilote et pour le MVP.

---

### Mission 004.11 — Shopify Connector Core

**Priority:** P1

**Objective**
Produire des instantanés canoniques à partir de l'API Admin GraphQL d'une boutique de développement, avec une
sémantique correcte et réconciliée.

**Why now** — le pilote CSV a validé le contrat de revenu ; l'export manuel est la principale friction ;
les démarches Shopify (compte partenaire, boutique de dev, demandes de portées) ont été lancées dès maintenant par la
piste business.

**Dependencies** — 004.4 (contrat, objets bruts, identité), 004.8, 004.9 (environnement pour les secrets).

**Contrat de données à établir AVANT tout code** (ADR, [EXTERNE] à revérifier)
- Version d'API épinglée et politique de mise à niveau trimestrielle.
- Portées : `read_orders`, `read_products` ; `read_all_orders` (historique > ~60 jours, approbation requise) ;
  `read_inventory` (stock, optionnel) ; **pas de `read_customers`** si l'identifiant client de la commande suffit.
- Données client protégées : niveau requis pour l'identifiant client seul ; **aucun champ e-mail/nom/adresse
  demandé**.
- Correspondance champ par champ vers le modèle canonique :
  - `subtotal` après remises (D-041) vérifié sur boutique de dev ;
  - remboursements avec **date de traitement** et ventilation par ligne (D-045, D-047) ;
  - allocations de remise par ligne (TD-32) ;
  - variantes et coût par article d'inventaire (TD-23) ;
  - annulation (`cancelledAt`) ;
  - devise boutique vs devise de présentation (ADR) ;
  - fuseau IANA de la boutique → `stores.timezone`.

**Scope**
- Client GraphQL : limitation par coût, reprises sur étranglement, erreurs typées.
- Backfill par Bulk Operation ; résultat JSONL dans le stockage objet (SHA-256, rétention).
- Normaliseur JSONL → modèle canonique (source `shopify`) ; extensions canoniques additives (variante, date de
  remboursement, allocations).
- Secret : table `credentials` chiffrée par enveloppe (clé KMS), référencée par `connections.credential_ref` ;
  jeton d'app personnalisée de la boutique de dev ; jamais journalisé.
- Type de travail `sync` avec heartbeat (004.3) ; enchaînement sync → analyse.
- Moteur : consommation des nouveaux champs (vue remboursements datés au traitement **en plus** de la vue cohorte ;
  marge produit après allocation de remise) ; rapport 2.2 additif.

**Out of scope** — OAuth, webhooks, planification, boutique marchande réelle.

**Files/components likely affected** — `connectors/shopify/` (nouveau), `domain/models.py` (additif),
`persistence/snapshots.py`, `persistence/credentials.py`, `workers/handlers.py`, `analytics/*`.

**Database impact** — migration : `data_snapshots.source` + `shopify`, `connections.kind` + `shopify`,
`credentials`, colonnes canoniques additives, type de travail `sync`.

**API impact** — aucun nouveau point public (déclenchement par CLI admin sur boutique de dev).

**Security impact** — premier secret tiers stocké ; chiffrement par enveloppe ; rotation documentée.

**Tests required** — réponses enregistrées (pas d'appel réseau en CI) ; étranglement ; Bulk en échec, expiré,
partiel ; réconciliation **API ↔ export CSV de la même boutique de dev** ; absence de jeton et d'e-mail dans logs,
audit, instantanés ; fuseau appliqué.

**Acceptance criteria**
- Réconciliation au centime sur « CA avant ajustements », commandes, unités et remboursements (écarts connus
  documentés).
- Au moins une remise partielle et un remboursement partiel observés sur la boutique de dev (documentés comme **non**
  probants pour un marchand).
- Sync de 100 k commandes synthétiques en boutique de dev sans double exécution.

**Definition of Done** — ADR contrat Shopify, handoff, CI verte.

**What this unlocks** — installation marchande et synchronisation continue.

---

### Mission 004.12 — Shopify Install, Webhooks & Continuous Sync

**Priority:** P1

**Objective**
Un marchand connecte sa boutique lui-même et ses données restent à jour sans intervention.

**Why now** — transforme le produit visible en produit utilisable au quotidien.

**Dependencies** — 004.6, 004.11.

**Scope**
- **Installation** (distribution personnalisée d'abord [EXTERNE]) : OAuth, jeton hors ligne chiffré, vérification
  de `state` et HMAC, routes `POST /v1/stores/{id}/connections/shopify` et callback ; état de connexion.
- **Webhooks** (HMAC, idempotents, tolérants au rejeu) :
  - `app/uninstalled` → révocation du jeton, arrêt des travaux ;
  - `customers/data_request` → réponse documentée (Mervio ne stocke pas de données directes) ;
  - `customers/redact` → travail `redact_customer` (004.4) ;
  - `shop/redact` → `purge_store` (004.4) ;
  - `orders/*`, `refunds/create`, `products/update` → **marqueur de synchronisation coalescé**, pas un travail par
    webhook.
- **Planification** : table `schedules` + évaluateur dans le worker (synchronisation quotidienne, purge, rétention).
- **Stratégie d'instantanés** : ADR incrémental vs complet ; copie complète acceptée pour le pilote ; **rétention
  des instantanés** (garder ceux référencés par les N derniers rapports ; les rapports plus anciens gardent leurs
  octets et une provenance marquée « source purgée »).
- **UI** : « Connecter Shopify », progression de la première synchronisation, fraîcheur, erreurs de connexion.

**Out of scope** — distribution publique App Store (005.3), facturation Shopify (005.0).

**Files/components likely affected** — `api/` (install, webhooks), `connectors/shopify/`, `persistence/schedules.py`,
`workers/`, `web/`.

**Database impact** — migration : `schedules`, `sync_markers`, `webhook_deliveries` (déduplication), actions d'audit.

**API impact** — routes de connexion et de webhooks (hors `/v1` authentifié pour les webhooks, protégés par HMAC).

**Security impact** — HMAC obligatoire, fenêtre de rejeu, jeton révoqué à la désinstallation, portées minimales.

**Tests required** — HMAC valide/invalide/rejoué ; désinstallation ; effacement client prouvé ; purge boutique
prouvée ; planification (horloge injectée) ; coalescence de 1 000 webhooks → 1 synchronisation ; dispatcher avec
1 000 organisations.

**Acceptance criteria**
- Installation sur boutique de dev via l'UI → premier rapport **sans action développeur**.
- Resynchronisation quotidienne planifiée pendant 7 jours consécutifs en staging.
- Désinstallation : jeton supprimé, travaux arrêtés, en moins d'une minute.
- `customers/redact` et `shop/redact` prouvés par tests.

**Definition of Done** — ADR instantanés, runbook webhooks, handoff.

**What this unlocks** — données continues pour les pilotes.

---

### Mission 004.13 — Pilot Program & Production MVP

**Priority:** P1

**Objective**
Faire fonctionner Mervio avec 1 à 3 marchands réels connectés à Shopify, sans intervention, et corriger ce que les
données réelles révèlent.

**Why now** — le principe « premier marchand avant de multiplier les fonctionnalités » ; toutes les briques
existent.

**Dependencies** — 004.10, 004.12 ; approbations Shopify obtenues [EXTERNE].

**Scope**
- Installation chez les pilotes (distribution personnalisée).
- Validation D-046 sur les données **API** de chaque pilote, réconciliée avec leur export CSV.
- Corrections analytiques révélées par les pilotes (chaque écart → décision écrite + test).
- Signaux Shopify optionnels si les portées sont accordées : rupture de stock (`read_inventory`), délai
  d'expédition (fulfillment) — P2, sinon « non observé ».
- Boucle de retour : entretiens, suivi de l'usage (recommandations faites/écartées), métriques d'activation.
- Support : canal de contact, runbook d'incident, temps de réponse annoncé.
- Une semaine d'exploitation sans intervention ; revue des alertes et des coûts LLM.

**Out of scope** — facturation, nouveaux connecteurs, actions.

**Files/components likely affected** — selon constats ; `analytics/*`, `connectors/shopify/*`, `docs/pilot/*`.

**Database impact** — selon constats (migrations additives uniquement).

**API impact** — aucun changement de rupture.

**Security impact** — revue des accès opérateur ; test d'intrusion léger externe recommandé avant M4.

**Tests required** — régressions ajoutées pour chaque écart réel ; E2E de production (compte de test) quotidien.

**Acceptance criteria**
- ≥ 1 pilote connecté via Shopify, 7 jours consécutifs de synchronisations et analyses réussies sans intervention.
- 0 fausse alerte reconnue comme telle par le pilote non corrigée.
- Chaque chiffre affiché au pilote a été réconcilié avec sa source.
- Au moins une recommandation marquée « faite » par un pilote.

**Definition of Done** — rapport de pilote (valeur, écarts, décisions), handoff.

**What this unlocks** — **M3 — PRODUCTION MVP** ; base factuelle pour le prix.

---

### Mission 005.0 — Billing & Entitlements

**Priority:** P1 (pour un client payant)

**Objective** — facturer et faire respecter ce qui a été acheté.

**Why now** — M3 prouve la valeur ; construire la facturation avant serait prématuré.

**Dependencies** — M3 ; **décision de canal** : Shopify Billing API (obligatoire en pratique pour une app publique
[EXTERNE]) ou Stripe (vente directe) ; facturation manuelle acceptable pour les tout premiers pilotes payants.

**Scope**
- Tables `plans`, `subscriptions`, `entitlements` (boutiques, fréquence de synchronisation, explications/mois,
  profondeur d'historique).
- Essai (dates, conversion, politique de données à l'expiration).
- Contrôle **côté serveur** des limites (API et worker).
- Webhooks du fournisseur de facturation (signés, idempotents).
- Échec de paiement : période de grâce puis lecture seule (les rapports existants restent lisibles).
- Résiliation : accès jusqu'à la fin de période, rétention, puis `purge_organization`.
- Portail de facturation du fournisseur (pas d'UI de facturation maison).
- Projection de mesure d'usage à partir de `jobs`, `audit_events` et `llm_usage`.

**Out of scope** — montée/descente de gamme complexe, remises, multi-devises de facturation.

**Files/components likely affected** — `billing/` (nouveau), `api/`, `workers/`, `web/`.

**Database impact** — migration : tables de facturation, actions d'audit.

**API impact** — `GET /v1/organization/subscription`, redirection vers le portail, webhooks de facturation.

**Security impact** — aucun moyen de paiement stocké chez Mervio ; secrets de webhook ; séparation des rôles
(seul `owner` gère l'abonnement).

**Tests required** — E2E essai → payant → échec de paiement → lecture seule → résiliation → purge ; limites
dépassées refusées côté serveur ; webhooks rejoués.

**Acceptance criteria** — le cycle complet passe en staging avec le mode test du fournisseur ; aucune limite
contournable par l'API.

**Definition of Done** — ADR canal de facturation, grille de prix validée, handoff.

**What this unlocks** — encaissement en libre-service.

---

### Mission 005.1 — Self-Serve Account Lifecycle & Engagement

**Priority:** P1 (pour un client payant)

**Objective** — un client gère seul son compte, son équipe et ses données, et reçoit la valeur sans se connecter.

**Dependencies** — 005.0.

**Scope**
- Invitations, changement de rôle, retrait de membre, **transfert de propriété** (TD-26, SEC-09).
- Suppression de compte et d'organisation en libre-service (réutilise `purge_organization`), avec délai de grâce.
- Export des données de l'organisation (rapports, constats) ⚖️.
- Brief hebdomadaire par e-mail (fournisseur d'e-mail ajouté aux sous-traitants), désinscription.
- Paramètres minimum : boutique (fuseau, devise), notifications, opt-in LLM.
- Console opérateur : répondre aux 5 questions de l'audit §19 pour n'importe quel tenant, sans lire de données
  canoniques.
- Juridique ⚖️ : conditions commerciales, DPA signable, liste des sous-traitants publiée.

**Database impact** — migration : `invitations`, préférences, actions d'audit.

**API impact** — routes membres, invitations, suppression, export, préférences.

**Security impact** — élévation de privilège testée en base **et** en Python (SEC-04) ; jetons d'invitation à usage
unique et expirants.

**Tests required** — matrice de rôles sur les nouvelles routes ; suppression E2E ; e-mail sans PII client ;
accessibilité des nouveaux écrans.

**Acceptance criteria** — un nouveau client s'inscrit, installe, invite un collègue, paie, reçoit un brief, résilie
et voit ses données supprimées, **sans aide**.

**Definition of Done** — handoff, documents juridiques publiés.

**What this unlocks** — **M4 — FIRST PAYING CUSTOMER** (libre-service).

---

### Mission 005.2 — Scale Readiness

**Priority:** P3

**Objective** — passer de dizaines à milliers de boutiques sans changer d'architecture.

**Dependencies** — M4 et mesures de charge réelles.

**Scope** (déclenché par la mesure, pas par anticipation)
- Instantanés incrémentaux ou partitionnement (TD-08) ; import par étapes avec bascule scellée (TD-20).
- Classes de workers par taille de tenant ; index par période dans `Dataset.window` si mesuré (TD-22).
- Réévaluation DuckDB en processus pour les plus grosses agrégations (ADR-004-002) si analyse > 5 min ou mémoire
  hors budget ; **jamais** de KPI en SQL ou en frontend.
- Porte de régression de performance nocturne (100 k) ; test de charge du dispatcher ; croissance de `jobs` et
  `audit_events` (vacuum, rétention).
- Limitation de débit Shopify multi-boutiques.

**Acceptance criteria** — objectifs chiffrés fixés au démarrage à partir des mesures de production.

**What this unlocks** — croissance.

---

### Mission 005.3 — Public Shopify App Distribution

**Priority:** P2/P3

**Objective** — publication sur l'App Store Shopify si la stratégie de distribution le justifie.

**Scope** [EXTERNE] — exigences de revue, installation gérée/échange de jetons pour app intégrée si requis,
Shopify Billing API, fiche et confidentialité de l'app, conformité données protégées.

**Dependencies** — M4 ; décision business.

---

### Mission 006.0 — Action Layer v1

**Priority:** P4

**Objective** — brouillon d'action → approbation → exécution, pour des actions à faible risque.

**Pourquoi plus tard** — aucune preuve de valeur des recommandations ; exige des portées en écriture Shopify ;
risque élevé ; le MVP se contente de « fait / écarté » et de liens vers l'admin Shopify.

**Préalable** — mesure de l'usage des recommandations en 004.13 et après M4.

### Missions 006.x — Connecteurs additionnels

**Priority:** P4 — Google Ads API (le CSV reste), Meta Ads, GA4 (trafic organique : lèverait AN-02), comptabilité,
autres plateformes. Ordre décidé par la demande des clients payants.

---

## 6. DEPENDENCY GRAPH

```text
                         ┌─────────────────────────────────────────────────┐
  PISTE BUSINESS (dès maintenant, sans code) :                              │
  pilotes · compte partenaire Shopify · boutique de dev · demandes de       │
  portées (read_all_orders, données protégées) · revue juridique ·          │
  choix hébergeur UE / IdP / fournisseur LLM / canal de facturation         │
                         └───────────────┬─────────────────────────────────┘
                                         │ alimente 004.9, 004.10, 004.11, 005.0
004.3 Platform Baseline & Worker Runtime
   ↓
004.4 Data Protection & Safe Ingestion  (contrat de données, objets, effacement)
   ↓
004.5 Report Contract 2.0 & Analytics Correctness
   ↓                                   ↘
004.6 API v1 — Vertical Slice            004.8 Analytics Hardening   (parallélisable)
   ↓                                   ↙
004.7 Web App v1  ──────────────►  M1  FIRST VISIBLE PRODUCT (local)
   ↓
004.9 Hosted Environment, Ops & Legal ─►  M2  FIRST REAL MERCHANT (CSV, production)
   ↓                       ↘
004.10 Production LLM       004.11 Shopify Connector Core   (parallélisables si capacité)
   ↓                          ↓
   │                        004.12 Shopify Install, Webhooks & Sync
   ↓                          ↓
004.13 Pilot Program ◄────────┘ ──────►  M3  PRODUCTION MVP
   ↓
005.0 Billing & Entitlements
   ↓
005.1 Account Lifecycle & Engagement ─►  M4  FIRST PAYING CUSTOMER
   ↓
005.2 Scale Readiness · 005.3 Public App · 006.0 Actions · 006.x Connecteurs
```

**Dépendances techniques explicites**

| Mission | Dépend de | Ce qu'elle consomme |
|---|---|---|
| 004.4 | 004.3 | CI, migrations sous garde, identité de service (purge/effacement audités) |
| 004.5 | 004.4 | `stores.timezone`, identité client à clé |
| 004.6 | 004.3, 004.4, 004.5 | worker multi-tenant, objets bruts, rapport 2.0 |
| 004.7 | 004.6 | OpenAPI figé |
| 004.8 | 004.5 | contrat 2.0 |
| 004.9 | 004.6–004.8 | artefacts déployables, produit crédible |
| 004.10 | 004.5, 004.7, 004.9 | contexte 2.x, panneau UI, secrets et DPA |
| 004.11 | 004.4, 004.8, 004.9 | stockage objet, identité, KMS, heartbeat |
| 004.12 | 004.6, 004.11 | routes, normaliseur, `redact_customer`, `purge_store` |
| 004.13 | 004.10, 004.12 | produit complet |
| 005.0 | M3 | preuve de valeur |
| 005.1 | 005.0 | abonnements (suppression liée à la résiliation) |

---

## 7. FIRST VISIBLE PRODUCT

**Mission : 004.7 — Web App v1** (M1). Environnement : `docker compose up` en local ; staging hébergé en 004.9.

**Ce que je vois**
- Une page de connexion (IdP de développement).
- Un onboarding : nom d'organisation, nom de boutique, devise, fuseau.
- Un écran « Connecter vos données » : dépôt des exports Shopify (commandes, produits), Stripe et Google Ads
  optionnels, avec validation lisible.
- Un écran de traitement : import puis analyse, en direct.
- **Le Brief** : période analysée, fraîcheur, KPI (CA avant ajustements, commandes, panier moyen, remboursements…)
  avec leur source et leur qualité, score de santé avec dimensions exclues, 3 problèmes prioritaires.
- **Un Problème** : le fait (WHAT), les facteurs classés et les facteurs non observés (WHY, marqués « inférence »),
  les chiffres, la série et les produits contributeurs (EVIDENCE), la recommandation (WHAT NEXT), le niveau de
  confiance et les limites.
- La page Qualité des données.

**Ce que je peux faire**
- Créer une organisation et une boutique ; déposer des CSV ; relancer une analyse.
- Naviguer du Brief vers un problème puis vers ses preuves.
- Marquer une recommandation « faite » ou l'écarter avec un motif.
- Ouvrir l'organisation de démonstration synthétique.

**Données utilisées**
- Jeux synthétiques des 17 scénarios (badgés « synthétique »).
- Registre OH5 (hors dépôt, localement).
- **Aucune donnée marchande réelle** (interdit avant 004.9).

**Réel vs simulé**

| Partie | État à M1 |
|---|---|
| Authentification, tenants, RLS, API | **réels** (IdP de développement) |
| Import, instantanés, travaux, analyse, rapports | **réels** (moteur déterministe complet) |
| Correction analytique | contrat 2.0 réel ; durcissement 004.8 réel s'il a été mené en parallèle |
| Explication | **texte déterministe** (pas de LLM) |
| Connexion Shopify | **absente** (dépôt CSV) |
| Hébergement, e-mail, facturation | **absents** |

---

## 8. FIRST REAL MERCHANT

**Deux niveaux, volontairement distincts.**

| Niveau | Mission | Mode |
|---|---|---|
| **M2 — Premier marchand réel (CSV)** | fin de **004.9** | le marchand se connecte en production, dépose son export natif, voit son Brief |
| **Premier marchand connecté à Shopify** | fin de **004.12** ; stabilisé en **004.13 (M3)** | le marchand installe l'app ; synchronisation quotidienne |

**Conditions obligatoires pour M2**
1. 004.3 : CI verte, worker en service, heartbeat, dispatcher.
2. 004.4 : aucune PII directe persistée ; effacement client et purge d'organisation prouvés ; imports par objets.
3. 004.5 et 004.8 : défauts AN-01 à AN-08 corrigés ; 0 fausse alerte sur les scénarios sains et saisonniers.
4. 004.6 : matrice de tests HTTP complète, accès croisé = 404 prouvé par route.
5. 004.9 : production UE, PITR, restauration testée, secrets gérés, alertes actives, runbooks.
6. **Juridique ⚖️** : conditions du pilote, politique de confidentialité et DPA revus et acceptés par le pilote.
7. **D-046** : l'export natif du pilote passe `validate_real_export.py` ; chaque écart a une décision et un test.
8. Fuseau de la boutique renseigné et appliqué.
9. Un contact de support identifié et un runbook d'incident.

**Conditions supplémentaires pour le marchand connecté Shopify**
- 004.11 réconcilié API ↔ CSV ; 004.12 webhooks RGPD prouvés ; portées approuvées [EXTERNE] ;
  synchronisation planifiée stable 7 jours en staging.

---

## 9. FIRST PAYING CUSTOMER

**Libre-service : fin de 005.1 (M4).**
**Exception :** un pilote peut être facturé **manuellement** dès M2/M3 si des conditions commerciales signées
existent ⚖️. C'est une décision business, pas un jalon produit.

| Domaine | Doit être terminé |
|---|---|
| **Produit** | Inscription → installation Shopify → premier Brief sans aide ; synchronisation quotidienne ; explication validée avec repli ; recommandations suivies ; brief hebdomadaire par e-mail |
| **Sécurité** | Matrice HTTP complète en CI ; jetons chiffrés et révoqués à la désinstallation ; identité de service ; en-têtes et limites de débit ; analyses de secrets et dépendances ; test d'intrusion léger ; accès opérateur audité |
| **Données** | Aucune PII directe ; effacement client et suppression d'organisation en libre-service ; politique de rétention publiée ; export des données ; sauvegardes alignées |
| **Analytique** | Contrat de revenu validé sur ≥ 1 marchand réel (API et CSV) ; 0 fausse alerte connue non traitée ; facteurs non observés affichés ; fraîcheur affichée |
| **Infrastructure** | Production UE, PITR, restauration testée, alertes, runbooks, dimensionnement mesuré |
| **Facturation** | Canal décidé ; plans, essai, limites côté serveur, échec de paiement, résiliation → purge |
| **Juridique ⚖️** | Conditions générales, politique de confidentialité, DPA, liste des sous-traitants (hébergeur, IdP, stockage, LLM, e-mail, facturation), mentions de l'app Shopify |
| **Support** | Canal de contact, délai de réponse annoncé, console opérateur, runbook d'incident, page de statut simple |
| **Onboarding** | Guide d'installation, états vides explicites (« coûts manquants : voici ce que cela débloque »), e-mail de bienvenue |

---

## 10. TEST STRATEGY

| Niveau | Portée | Introduit / renforcé |
|---|---|---|
| **Unit** | moteur, domaine, ingestion, LLM, file, journalisation | existant ; chaque mission |
| **Integration** | cas d'usage persistés, worker + PostgreSQL | existant ; 004.3+ |
| **DB** | migrations montée/descente, contraintes, déclencheurs, rôles | existant ; chaque migration |
| **RLS** | dépôt, barrière applicative, SQL brut ; rôles dispatcher, redactor, purger | existant ; 004.3, 004.4 |
| **API** | par route : succès, validation, 401, rôle, **404 croisé**, idempotence, pagination ; contrat OpenAPI | 004.6 |
| **Frontend** | composants, correspondance nombre ↔ champ, accessibilité (axe) | 004.7 |
| **End-to-end** | inscription → dépôt/installation → brief → problème → statut | 004.7 (compose), 004.9 (staging), 004.13 (production, compte de test) |
| **Shopify** | réponses enregistrées, étranglement, Bulk, réconciliation API ↔ CSV, HMAC, désinstallation | 004.11, 004.12 |
| **LLM** | goldens, scénarios, adversarial, enregistreur, budget, délai, taux de rejet | existant (mock) ; 004.10 |
| **Security** | analyse de secrets et dépendances, en-têtes, débit, chemins hostiles, élévation de privilège | 004.3, 004.4, 004.6, 005.1 |
| **GDPR** | aucune PII persistée, effacement client, purge boutique et organisation, logs sans PII | 004.4, 004.12, 005.1 |
| **Performance** | travaux à 100 k/1 M, p95 lecture API, dispatcher 1 000 organisations, régression nocturne | 004.3, 004.6, 004.9, 005.2 |
| **Scénarios** | 17+ scénarios, `MATCH` / `KNOWN_GAP` / faux positifs rapportés séparément | 004.8 |

**Portes bloquantes de fusion (CI)**
1. Tous les tests passent ; **aucun test DB ignoré** (`MERVIO_REQUIRE_DATABASE_TESTS=1`).
2. Nombre de tests ≥ au précédent (sauf suppression justifiée).
3. Tests AST de frontières (pas de SQL hors persistance ; pas de KPI dans l'API, le worker ou le frontend).
4. Goldens inchangés, ou version de contrat incrémentée + justification dans le même changement.
5. Contrat OpenAPI inchangé, ou révision explicite.
6. Montée **et** descente de chaque nouvelle migration.
7. Matrice HTTP complète pour toute nouvelle route.
8. Analyse de secrets propre ; aucune dépendance à vulnérabilité critique ; licences autorisées.
9. Build et tests `web/` (dès 004.7), axe sans violation critique.
10. Aucun faux positif nouveau sur `healthy_store` / `seasonal_business` (dès 004.8).

**Portes de déploiement (non bloquantes pour la fusion)** : E2E staging, fumée post-déploiement, évaluation LLM
(taux de rejet ≤ seuil) avant activation d'un nouveau prompt.

---

## 11. DATA / PRIVACY MODEL

### 11.1 Principes
1. **Minimisation** : Mervio n'a besoin d'aucune donnée directe du client final pour ses KPI. Seule une clé de
   jointure stable est nécessaire (nouveaux clients, réachat, concentration, rétention).
2. **Séparer faits métier et identité** : les montants, dates et produits restent immuables ; seule la référence
   client est effaçable, par un chemin unique et audité.
3. **La provenance garde des empreintes, pas des personnes.**
4. **Tout ce qui est conservé a une durée.**

### 11.2 Modèle cible

| Donnée | Stockage | Protection | Effacement |
|---|---|---|---|
| E-mail, nom, adresse, téléphone du client final | **jamais persistés** (lus en mémoire à l'import CSV pour calculer la clé, puis oubliés ; jamais demandés à Shopify) | — | sans objet |
| `customer_ref` | lignes canoniques | `HMAC-SHA256(clé_org, identité source)` ; clé dérivée d'une clé maître en KMS | tombstone par `redact_customer` (rôle `mervio_redactor`) |
| Liste des effacements | `customer_redactions` | HMAC seulement | conservée (preuve d'exécution) |
| Faits de commande, lignes, produits, remboursements | instantanés immuables | RLS, chiffrement au repos | purge boutique/organisation ; rétention des instantanés |
| Fichiers bruts (CSV, JSONL Shopify) | stockage objet, clé par organisation/boutique | chiffrement serveur, accès via `raw_objects` + RLS | `retain_until` (30 j par défaut ⚖️) puis purge ; purge immédiate sur effacement ou désinstallation |
| Provenance | `snapshot_sources`, `raw_objects` | SHA-256 de fichiers, tailles, comptes | conservée ; l'objet peut être purgé (« source purgée ») |
| Rapports | immuables | **aucun identifiant client** (004.5) | purge avec l'organisation ; rétention |
| Explications LLM | `explanations` | contexte agrégé, validateur PII | purge avec l'organisation |
| Jetons OAuth | `credentials` | chiffrement par enveloppe (KMS) | suppression à la désinstallation |
| Travaux | `jobs` | aucune PII ni secret en charge utile | purge planifiée (existant) |
| Audit | `audit_events` | métadonnées sans PII | rétention 365 j (existant, ⚖️ à confirmer) ; conservé après purge d'organisation pour la durée légale |
| Logs | plateforme | rédaction existante ; jamais de prompt, réponse, jeton, e-mail | rétention de la plateforme (≤ 30 j) |
| Sauvegardes | PITR | chiffrées | expiration alignée sur la politique (fenêtre documentée ⚖️) |

### 11.3 Pseudonymisation
- Le HMAC à clé par organisation **est** une pseudonymisation au sens du RGPD : la donnée reste personnelle tant que
  la clé existe ⚖️. D'où le chemin d'effacement, en plus de la clé.
- Destruction de la clé d'une organisation à sa suppression : les références résiduelles (sauvegardes) deviennent
  inexploitables.
- Pas de pseudonyme client dans les rapports ni dans le contexte LLM.

### 11.4 Compatibilité avec l'immuabilité
- L'immuabilité protège **les faits métier et la reproductibilité**, pas une identité.
- Exception unique : la colonne `customer_ref`, modifiable seulement par la fonction `SECURITY DEFINER` d'effacement,
  auditée. Les déclencheurs restent actifs pour tout le reste.
- Un rapport ancien n'est pas recalculé après effacement : il ne contient déjà aucune donnée client.
- La reproductibilité octet pour octet reste garantie **hors effacement**, et l'écart est documenté.

### 11.5 Rôles de traitement ⚖️
- Mervio est probablement **sous-traitant** pour les données clients des marchands (à confirmer).
- Registre des traitements, liste des sous-traitants, DPA, hébergement UE, procédure de demande d'accès.

---

## 12. PRODUCTION READINESS CHECKLIST

### Security
- [ ] Matrice HTTP par route (401, rôle, 404 croisé, idempotence) en CI
- [ ] OIDC validé (signature, audience, expiration) ; MFA disponible
- [ ] Aucun chemin de fichier accepté ; dépôts bornés et vérifiés
- [ ] Identité de service pour worker et dispatcher
- [ ] Jetons chiffrés (KMS), jamais journalisés, révoqués à la désinstallation
- [ ] HMAC sur tous les webhooks, fenêtre de rejeu
- [ ] En-têtes de sécurité, CORS, limites de débit
- [ ] Analyses de secrets, dépendances, licences
- [ ] Élévation de privilège testée (base et Python)
- [ ] Test d'intrusion léger externe

### Data
- [ ] Aucune PII directe persistée
- [ ] Effacement client, purge boutique, purge organisation prouvés
- [ ] Rétention appliquée (objets, instantanés, travaux, audit, logs)
- [ ] PITR et restauration testée
- [ ] Hébergement UE

### Analytics
- [ ] Contrat de revenu validé sur ≥ 1 marchand réel (CSV et API)
- [ ] Contrat de rapport versionné, schéma validé en CI
- [ ] Fuseau de boutique appliqué ; fraîcheur affichée
- [ ] AN-01 à AN-08 corrigés ; 0 fausse alerte sur scénarios sains et saisonniers
- [ ] Facteurs non observés affichés ; confiance qualitative
- [ ] Taux de faux positifs publié

### API
- [ ] OpenAPI figé et testé
- [ ] `problem+json`, curseurs, idempotence
- [ ] p95 lecture de rapport < 300 ms en staging
- [ ] Aucune route ne calcule de KPI (test AST)

### Worker
- [ ] Processus avec SIGTERM, heartbeat, reprise périodique
- [ ] Dispatcher multi-tenant équitable
- [ ] Planification et enchaînement
- [ ] Limites mémoire/concurrence issues des benchmarks de travaux réels

### Frontend
- [ ] WHAT / WHY / EVIDENCE / WHAT NEXT distincts ; synthétique badgé
- [ ] Aucun calcul client ; correspondance nombre ↔ champ testée
- [ ] États vides, chargement, erreur ; axe sans violation critique
- [ ] E2E en CI

### LLM
- [ ] Fournisseur réel derrière `LLMProvider`, délai d'expiration
- [ ] Validation totale ; taux de rejet mesuré ≤ seuil
- [ ] Budget par requête et par organisation ; opt-in
- [ ] Repli déterministe visible
- [ ] Aucun prompt ni réponse dans les logs ; conditions du fournisseur revues ⚖️

### Infrastructure
- [ ] Staging et production séparés
- [ ] Déploiement automatisé ; migrations avec `lock_timeout` et runbook
- [ ] Secrets en gestionnaire ; TLS
- [ ] Image non root, unique pour API et worker

### Monitoring
- [ ] Logs centralisés au format unique, corrélés
- [ ] Suivi d'erreurs sans PII
- [ ] Métriques : file, échecs, durée/mémoire d'analyse, HTTP, LLM, synchronisations
- [ ] Alertes testées ; sonde de disponibilité
- [ ] Console opérateur répondant aux 5 questions de l'audit §19

### Billing
- [ ] Canal décidé (Shopify Billing / Stripe / manuel)
- [ ] Plans, essai, limites côté serveur
- [ ] Échec de paiement → lecture seule sans perte
- [ ] Résiliation → rétention → purge

### Legal ⚖️
- [ ] Conditions générales
- [ ] Politique de confidentialité
- [ ] DPA et liste des sous-traitants
- [ ] Registre des traitements
- [ ] Mentions et obligations de l'app Shopify

### Customer onboarding
- [ ] Inscription et installation sans aide
- [ ] Guide des exports CSV (repli) et de l'installation
- [ ] Premier Brief en quelques minutes, avec fraîcheur et limites
- [ ] Canal de support et délai annoncé
- [ ] E-mail de bienvenue et brief hebdomadaire

---

## 13. RISKS

| Catégorie | Risque | Probabilité | Impact | Atténuation | Mission |
|---|---|---|---|---|---|
| **Technical** | Mémoire d'analyse (4,6 Go à 1 M) → OOM | moyenne | élevé | concurrence 1 par worker ; benchmarks de travaux réels ; classes de workers | 004.3, 004.9, 005.2 |
| Technical | Le contrat de rapport 2.0 casse goldens et contexte LLM | élevée | moyen | une seule rupture majeure, table de correspondance, contexte LLM migré dans la même mission | 004.5 |
| Technical | Croissance des instantanés en synchronisation quotidienne | élevée | moyen | rétention (004.12), incrémental (005.2) | 004.12, 005.2 |
| Technical | Deuxième pile (Next.js) portée par une petite équipe | moyenne | moyen | types générés, aucune logique métier en TS, écrans minimum | 004.7 |
| **Security** | Fuite entre tenants via HTTP (R-01) | faible | critique | matrice 404 par route, organisation dérivée de l'appartenance | 004.6 |
| Security | Chemin de fichier arbitraire (SEC-01) | certaine si non traité | critique | objets par tenant avant toute route | 004.4 |
| Security | Élévation de privilège dans l'organisation (SEC-04) | faible | élevé | contrôle Python + tests base ; politique renforcée | 004.6, 005.1 |
| Security | Fuite de jeton Shopify | faible | critique | enveloppe KMS, jamais journalisé, révocation | 004.11, 004.12 |
| **Data** | Contrat de revenu faux sur données réelles (R-15, D-046) | moyenne | critique | harnais sur export pilote avant tout chiffre montré | 004.9, 004.13 |
| Data | Effacement RGPD impossible (SEC-02) | certaine aujourd'hui | critique | modèle HMAC + chemin d'effacement | 004.4 |
| Data | Données synthétiques prises pour une validation (R-05) | moyenne | élevé | badges, rapport de scénarios séparé | 004.7, 004.8 |
| **Shopify** | Délai ou refus d'approbation `read_all_orders` / données protégées | moyenne | élevé | demandes lancées maintenant ; pas de champ client direct ; repli CSV conservé | piste business |
| Shopify | Changements de version d'API trimestriels | certaine | moyen | version épinglée, suite de réponses enregistrées | 004.11 |
| Shopify | Obligations de distribution publique / facturation Shopify | moyenne | élevé | distribution personnalisée d'abord ; décision de canal avant 005.0 | 005.0, 005.3 |
| **LLM** | Taux de rejet élevé → explication rarement disponible | moyenne | moyen | mesure avant activation ; repli déterministe | 004.10 |
| LLM | Coût non borné (R-13) | moyenne | moyen | budgets par requête et organisation, opt-in | 004.10 |
| LLM | Sortie hors contrat en production (R-12) | faible | élevé | validation totale, rejet complet | 004.10 |
| **Product** | Aucune recherche utilisateur ; adéquation au marché inconnue | élevée | critique | démonstrations dès M1 ; pilote dès M2 ; mesure d'usage des recommandations | 004.7, 004.9, 004.13 |
| Product | Profitabilité indisponible pour presque tous | certaine | moyen | présentée comme « à débloquer », pas comme alarme | 004.5 |
| Product | Fausse alerte dans la première session | moyenne sans 004.8 | élevé | 004.8 avant M2 | 004.8 |
| **Commercial** | Prix et canal de facturation non décidés | certaine | élevé | décision pendant 004.13 à partir des pilotes | 005.0 |
| Commercial | Juridique non prêt au moment du pilote | moyenne | critique | revue lancée maintenant | 004.9 |
| **Operational** | Capacité de l'équipe / facteur bus inconnus | inconnue | élevé | missions bornées, handoffs, runbooks, CI | toutes |
| Operational | Diagnostic seulement dans les logs du worker (R-27) | élevée | moyen | logs centralisés, console opérateur | 004.9, 005.1 |
| Operational | Migration en production bloquante | faible | élevé | `lock_timeout`, runbook, migrations additives | 004.9 |

---

## 14. WHAT NOT TO BUILD YET

| Ne pas construire | Pourquoi | Condition pour reconsidérer |
|---|---|---|
| **Microservices** | Aucun besoin mesuré ; affaiblirait l'isolation et la vérité numérique unique (§4.3) | Équipes multiples ou profil de ressources incompatible, mesuré |
| **Kafka / courtier de messages** | File PostgreSQL mesurée largement au-dessus du besoin ; webhooks coalescés | Contention soutenue mesurée sur `jobs` |
| **Redis** | Débit, cache et limitation de débit tiennent dans PostgreSQL à cette échelle ; rapports immuables cachés par ETag | Latence p95 hors objectif due à la base, mesurée |
| **Kubernetes** | Un PaaS de conteneurs suffit pour 2 processus + 1 frontend | Contraintes d'hébergement non satisfaites par un PaaS |
| **Architecture événementielle complexe / event sourcing** | L'audit et les instantanés couvrent la traçabilité | — |
| **Entrepôt analytique séparé / KPI en SQL / dbt** | Le moteur Python est la seule source de vérité ; 1 M mesuré | Critère ADR-004-002 (analyse > 5 min ou mémoire hors budget) → DuckDB **en processus** d'abord |
| **Agents autonomes, outils LLM, base vectorielle, RAG** | Le LLM ne fait qu'expliquer des faits structurés | Jamais pour les calculs ; à réévaluer pour un assistant conversationnel post-M4 |
| **Orchestration multi-fournisseurs LLM** | Un fournisseur derrière `LLMProvider` suffit ; l'abstraction permet de changer | Panne ou coût mesurés rendant un second fournisseur nécessaire |
| **Exécution d'actions (écriture Shopify)** | Valeur non prouvée, risque élevé, portées en écriture | Usage mesuré des recommandations après M4 (006.0) |
| **Fonctions BI étendues** (constructeur de tableaux, requêtes libres, exports multiples) | Le produit est un diagnostic priorisé, pas un outil BI | Demande récurrente de clients payants |
| **Application mobile** | Le brief par e-mail et le web responsive couvrent l'usage | Demande mesurée |
| **Rapport PDF** | Non requis pour le MVP | Demande client (P4) |
| **Connecteurs Google Ads / Meta / GA4 / comptabilité en API** | Shopify seul suffit au MVP ; le CSV Ads reste | Demande des clients payants (006.x) |
| **Multi-devises avec conversion** | Une devise par boutique ; mélange refusé (existant) | Marchands multi-devises en pilote |
| **SSO/SAML, multi-organisation avancée, exports d'audit** | Besoins entreprise | Premier client entreprise |
| **Prévisions, LTV, cohortes avancées** | Le socle de rétention 004.8 suffit | Après M4 |
| **Localisation complète (EN et autres)** | Préparée par les clés de message (004.5) ; FR d'abord | Premier marché non francophone |
| **Seuils configurables par tenant dans l'UI** | Configuration centralisée suffit ; risque d'incohérence | Demande client |
| **Prometheus/OpenTelemetry auto-hébergés** | Métriques de la plateforme suffisent | Besoin de tracing non couvert |
| **LISTEN/NOTIFY** | L'interrogation de la file suffit au débit mesuré | Latence de prise devenue critère |

---

## 15. FINAL RECOMMENDED SEQUENCE

```text
NOW
  │  commiter l'audit et cette roadmap ; configurer un dépôt distant
  │  lancer la piste business : pilotes, compte partenaire Shopify, boutique de dev,
  │  demandes de portées, revue juridique, choix hébergeur UE / IdP / fournisseur LLM
  ↓
Mission 004.3 — Platform Baseline & Worker Runtime
  │  PASSAGE : CI verte (0 test DB ignoré) ; worker conteneurisé produit un rapport via `mervio admin` ;
  │  travail 2× plus long que le bail exécuté une seule fois ; nouvelle organisation servie sans redémarrage
  ↓
Mission 004.4 — Data Protection & Safe Ingestion
  │  PASSAGE : 0 PII directe persistée ; effacement client et purge d'organisation prouvés ;
  │  aucun import SaaS par chemin ; `stores.timezone` présent
  ↓
Mission 004.5 — Report Contract 2.0 & Analytics Correctness
  │  PASSAGE : AN-01, 02, 05, 06, 14–19 corrigés avec tests ; 100 % des rapports valident le schéma 2.0 ;
  │  aucun nombre dans une chaîne de preuve ; suite LLM verte
  ↓
Mission 004.6 — API v1 — Vertical Slice          (004.8 en parallèle si capacité)
  │  PASSAGE : matrice HTTP complète ; E2E API synthétique + OH5 ; OpenAPI figé ; p95 < 300 ms local
  ↓
Mission 004.7 — Web App v1
  │  PASSAGE : E2E navigateur sur compose ; axe sans violation critique ; aucun calcul client
  ↓
★ FIRST VISIBLE PRODUCT (M1)
  ↓
Mission 004.8 — Analytics Hardening
  │  PASSAGE : 0 fausse alerte sur scénarios sains et saisonniers ; ≥ 4 KNOWN_GAP → MATCH ;
  │  taux de faux positifs publié
  ↓
Mission 004.9 — Hosted Environment, Operations & Legal Baseline
  │  PASSAGE : restauration testée ; alertes vérifiées ; juridique revu ⚖️ ;
  │  export natif du pilote validé (D-046) ; pilote autonome sur son Brief
  ↓
★ FIRST REAL MERCHANT (M2 — CSV, production)
  ↓
Mission 004.10 — Production LLM Explanations      (004.11 en parallèle si capacité)
  │  PASSAGE : taux de rejet ≤ seuil mesuré ; budget appliqué ; repli prouvé ; aucun prompt journalisé
  ↓
Mission 004.11 — Shopify Connector Core
  │  PASSAGE : réconciliation API ↔ CSV au centime sur la boutique de dev ; aucun jeton/e-mail persisté en clair
  ↓
Mission 004.12 — Shopify Install, Webhooks & Continuous Sync
  │  PASSAGE : installation → premier rapport sans développeur ; 7 jours de synchronisation planifiée ;
  │  webhooks RGPD prouvés ; désinstallation < 1 min
  ↓
Mission 004.13 — Pilot Program & Production MVP
  │  PASSAGE : ≥ 1 pilote Shopify, 7 jours sans intervention ; chiffres réconciliés ;
  │  ≥ 1 recommandation marquée « faite » ; décision de prix et de canal de facturation
  ↓
★ PRODUCTION MVP (M3)
  ↓
Mission 005.0 — Billing & Entitlements
  │  PASSAGE : essai → payant → échec → lecture seule → résiliation → purge en E2E ; limites côté serveur
  ↓
Mission 005.1 — Self-Serve Account Lifecycle & Engagement
  │  PASSAGE : un client inconnu réalise le cycle complet sans aide ; documents juridiques publiés ⚖️
  ↓
★ FIRST PAYING CUSTOMER (M4)
  ↓
SCALE — 005.2 Scale Readiness · 005.3 Public App · 006.0 Actions · 006.x Connecteurs
     PASSAGE : déclenché par des mesures de production (charge, mémoire, stockage, demande client),
     jamais par anticipation
```

**Règle d'arrêt** : aucune mission ne commence tant que les critères de passage de la précédente ne sont pas
démontrés en CI (ou, pour les critères d'exploitation, consignés dans le handoff).
