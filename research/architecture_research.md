# Recherche d'architecture — Mission 004.0

Ce document relie les **preuves** (code Mervio existant, dépôts étudiés, mesures) aux options évaluées. Les décisions
sont dans `docs/MISSION_004_0_DECISIONS.md` (ADR-004-001 à 012), l'architecture cible dans
`docs/MISSION_004_0_ARCHITECTURE.md`.

## 1. Point de départ vérifié

| Élément existant | Constat | Conséquence d'architecture |
|---|---|---|
| `src/mervio/analytics` | déterministe ; ENGINE_VERSION 0.1.0 ; aucune dépendance runtime (D-004) | reste une bibliothèque pure, appelée par un service et un worker, jamais réécrite |
| `src/mervio/application/service.py` | `AnalysisRequest` → `analyze_dataset()` → `AnalysisResult` ; ne lève pas (D-022) | point d'entrée naturel du futur worker |
| `src/mervio/ingestion` | connecteurs CSV Shopify, Stripe, Google Ads ; validation pendant la lecture | les futurs connecteurs API produisent les **mêmes entités canoniques** (`domain.models`) |
| `src/mervio/domain` | `Order`, `OrderItem`, `Refund`, `Payment`, `Campaign`, `DailyAdPerformance`, `Dataset`, `DataQualityReport` | noyau du modèle persistant ; contrat du CA D-041 / D-048 |
| `src/mervio/llm` | contrat de contexte 1.0 borné et déterministe ; `validate_response` ; `LLMProvider` + mock ; `explain_report` ne lève pas | frontière LLM déjà construite : il manque un fournisseur réel et la persistance des explications |
| Rapport JSON | ~35 Ko constant, 10² à 10⁶ commandes | unité de persistance et de cache |
| Benchmark | 45 s / 3,7 Go à 1 M de commandes | analyse en tâche de fond, jamais dans une requête |
| `mervio.synthetic` | 17 scénarios, vérité terrain testée contre le moteur | socle des tests de scénarios et de performance |

## 2. Options évaluées

### 2.1 Forme applicative
| Option | Pour | Contre | Verdict |
|---|---|---|---|
| Monolithe modulaire Python (API + worker, même code) + frontend Next.js séparé | réutilise le moteur sans frontière réseau ; une base ; déploiement simple | discipline de modules nécessaire | **retenu** |
| Microservices (ingestion, analytics, LLM, actions) | isolation | réseau, versions, observabilité distribuée, coût d'équipe ; aucune preuve de besoin | rejeté |
| Tout en Next.js (TypeScript) avec réécriture du moteur | une seule pile | réécrit 650+ tests et un moteur validé ; perd le déterminisme acquis | rejeté |
| Moteur appelé en sous-processus CLI par le frontend | rapide à brancher | pas de tenant, pas d'audit, pas de reprise | rejeté (prototype local seulement) |

### 2.2 Persistance
| Option | Verdict | Preuve |
|---|---|---|
| PostgreSQL seul (transactionnel + canonique + rapports JSONB) | **retenu** | produits proches convergent (OCO, ShopFlow) ; volume PME ≪ limites Postgres ; rapport de ~35 Ko |
| + stockage objet pour exports bruts et charges API | **retenu** | immuabilité et hachage des sources (provenance) ; exports de 200 Mo à 1 M de commandes |
| DuckDB en processus | **différé** : option d'agrégation si le benchmark par tenant l'exige | MIT ; aucun serveur |
| ClickHouse | rejeté pour l'instant | justifié pour des milliards d'événements (next-cwv-monitor), pas pour des commandes de PME |
| Entrepôt cloud (BigQuery, Snowflake) | rejeté | coût, dépendance, confidentialité ; aucun besoin mesuré |

### 2.3 Multi-tenancy
| Option | Verdict |
|---|---|
| Base partagée, schéma partagé, `organization_id` + `store_id` sur chaque ligne métier, filtrage obligatoire dans la couche d'accès + Row Level Security PostgreSQL en défense en profondeur | **retenu** |
| Schéma par tenant | rejeté : migrations ×N, pool de connexions |
| Base par tenant | rejeté maintenant : réservé à une demande contractuelle future |

### 2.4 Tâches de fond
| Option | Verdict |
|---|---|
| File dans PostgreSQL (table de jobs + `SELECT … FOR UPDATE SKIP LOCKED`, ou procrastinate, MIT) | **retenu** ; choix de la bibliothèque dans la mission jobs |
| Celery / Dramatiq + Redis ou RabbitMQ | rejeté : composant d'infrastructure en plus ; Dramatiq en LGPL |
| Kafka, bus d'événements | rejeté : aucun flux temps réel requis |

### 2.5 Connecteurs
- **Constat 003.3 :** l'export CSV Shopify ne date pas les remboursements et ne prouve pas la remise partielle.
  L'API Admin GraphQL expose `Refund.processedAt`, `subtotalPriceSet` (après remises, avant retours) et
  `totalRefundedShippingSet`.
- Un connecteur API peut donc **réduire** les limites de D-045 et D-046 sur une boutique de développement, sans
  constituer une preuve marchand.
- **Patrons OCO retenus :**
  - `sync_runs` avec avertissements ;
  - upsert idempotent par clé naturelle ;
  - fuseau de reporting canonique ;
  - seau de dépense non allouée.
- **Écartés :**
  - `singer-io/tap-shopify` (AGPL-3.0) ;
  - `dlt` : étudié, différé tant que 3 connecteurs n'existent pas.

### 2.6 LLM
Le contrat 1.0 existe et est testé (270 tests en Mission 002). Il manque :
1. un fournisseur réel derrière `LLMProvider` ;
2. la persistance de l'explication **avec** l'empreinte du contexte, la version de contrat, le modèle, le résultat du
   validateur ;
3. un budget de coût et de latence.

Rejetés :
- les agents autonomes ;
- les bases vectorielles : aucune recherche documentaire n'est nécessaire, le contexte est déjà structuré et borné.

### 2.7 Observabilité
- **Logs :** journaux structurés JSON avec identifiants de requête, tenant et exécution. C'est nécessaire dès la
  première API.
- **Métriques :** compteurs et durées par tâche.
- **Traces :** OpenTelemetry (Apache-2.0) instrumenté dans l'application, **sans** déployer d'infrastructure de
  tracing distribué tant qu'il n'y a qu'une API et un worker.

### 2.8 Frontend
Next.js App Router + React + primitives accessibles (shadcn/ui sur Radix, MIT). Les patrons Atlas et CWV sont
confirmés :
- composants serveur pour les pages agrégées ;
- filtres dans l'URL ;
- skeletons ;
- `error` par route ;
- entrées validées par schéma.

Le frontend ne calcule aucun KPI : il affiche des champs du rapport persisté.

## 3. Questions ouvertes (non tranchées faute de preuve)

| Question | Ce qui la tranchera |
|---|---|
| Fournisseur d'identité (Auth.js, service OIDC managé, autre) | comparaison coût / SSO / MFA en mission API ; contrainte : le backend valide un jeton, jamais une session implicite |
| procrastinate vs table de jobs maison | spike de 1 jour : reprise, planification, visibilité, tests |
| Hébergement (PaaS conteneurs, VM) | exigences de résidence des données des premiers clients (UE probable) |
| Fuseau de boutique | l'objet `Shop` de l'API Admin exposerait `ianaTimezone` (non vérifié en 004.0) ; décision du modèle de période par boutique en mission connecteurs |
| Agrégats pré-calculés vs rapport complet | usage réel du frontend ; le rapport persisté suffit en v1 |
