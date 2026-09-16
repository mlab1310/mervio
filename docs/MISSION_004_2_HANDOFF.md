# Mission 004.2 — Handoff (document de départ de Mission 004.3)

## 1. Résumé

Mervio exécute désormais ses imports et ses analyses en **tâches de fond rejouables**, et trace toute écriture dans
un **journal d'audit en ajout seul**.

| | |
|---|---|
| Dépôt | `/Users/mlab/Projects/mervio` |
| Branche | `mission-004.2`, créée depuis `master` @ `1760205` (fusion de 004.1) |
| Environnement | Python 3.11.16 · pytest 9.1.1 · PostgreSQL 17.11 · psycopg 3.3.5 · Alembic 1.20.0 · ENGINE_VERSION 0.1.0 · contrat LLM 1.0 |
| Tests | **1 097 passés** avec PostgreSQL : 816 existants + 281 nouveaux ; 0 échec, 0 ignoré, 0 erreur |
| Tests sans base | 796 passés, 301 ignorés (tests PostgreSQL) |
| Moteur | **aucune formule, aucun seuil, aucune clé de rapport modifiés.** Aucun fichier de `analytics/`, `domain/`, `ingestion/`, `reporting/`, `llm/`, `synthetic/` ou `cli/` n'a été touché |
| Infrastructure ajoutée | **aucune** : pas de Redis, pas de courtier, pas d'ordonnanceur, pas de dépendance nouvelle |

**Le fait central :** le rapport produit par un travail de fond est **identique octet pour octet** à celui du chemin
direct de 004.1, lui-même identique à celui de la CLI. Le moteur déterministe reste la seule source de vérité.

---

## 2. Architecture

```
 utilisateur                     PostgreSQL                      worker
─────────────                ──────────────────              ──────────────
 enqueue() ──────────────────►  jobs (queued)
                                    │
                                    │  claim_next_job()
                                    │  FOR UPDATE SKIP LOCKED
                                    ▼
                                jobs (running) ◄──────────────  transaction 1
                                    │                           (courte : prise)
                                    │
                                    │                           ── hors transaction ──
                                    │                           handler : import ou analyse
                                    │                           (minutes possibles ; aucun
                                    │                            verrou, aucun WAL retenu)
                                    ▼
                     jobs (succeeded | failed | queued) ◄─────  transaction 2
                     + audit_events                             (courte : résultat)
```

**Deux transactions courtes encadrent une exécution longue.** C'est la décision structurante : une analyse d'un
million de commandes ne retient ni verrou ni WAL (elle répond à R-24).

Un arrêt brutal entre les deux laisse un travail `running` ; son bail expire ; un autre worker le reprend.
L'exécution est donc **au moins une fois**, jamais exactement une fois. Rien dans le système ne prétend le
contraire ; c'est l'idempotence de 004.1 qui rend un rejeu inoffensif (§8).

### Modules

| Module | Contenu |
|---|---|
| `persistence/jobs.py` | `JobRecord`, `enqueue_job`, `claim_next_job`, `mark_succeeded`, `mark_failed`, `requeue`, `cancel_job`, `recover_stale_jobs`, `get_job`, `list_jobs`, `queue_statistics`, `purge_terminal_jobs`, `explain_claim`, `backoff_seconds`, `validate_payload` |
| `persistence/audit.py` | `AuditEvent`, `record` (dans une transaction ouverte), `record_event`, `list_events`, `count_events`, `purge_expired_events`. Aucune mise à jour, aucune suppression ordinaire |
| `persistence/retry.py` | `retryable_database_error`, `database_error_code` : la connaissance des SQLSTATE reste dans la persistance |
| `observability/logging.py` | `JsonFormatter`, `EventLogger`, `correlation_scope`, `scrub`, `configure_json_logging` |
| `workers/worker.py` | `Worker` (`run_once`, `run_until_empty`, `run_forever`, `recover_stale`), `enqueue`, `cancel` |
| `workers/handlers.py` | `HandlerRegistry`, `JobContext`, `import_handler`, `analysis_handler`, `purge_handler` |
| `workers/errors.py` | `RetryableJobError`, `PermanentJobError`, `classify` |
| `workers/retention.py` | `RetentionPolicy` et ses planchers |
| `migrations/versions/` | `0004_jobs`, `0005_audit` |

### Frontière conservée

`mervio.workers` **n'importe aucun pilote de base** (test AST). Le worker manipule des dépôts ; aucune requête de
job ou d'audit ne peut naître hors de `mervio.persistence`, donc aucune ne peut oublier le filtre d'organisation.
`mervio.observability` n'importe ni base, ni moteur, ni dépendance externe.

### Registre de gestionnaires

`import → import_handler`, `analysis → analysis_handler`, `purge → purge_handler`. Aucun `if job_type == …` dans le
worker. Un gestionnaire ne calcule rien : l'import passe par `import_csv_snapshot`, l'analyse par
`analyze_snapshot`, tous deux inchangés depuis 004.1 (test AST : `workers` n'importe aucun module de calcul ni
`mervio.llm`).

---

## 3. Schéma

### `jobs` (révision `0004_jobs`)

`id`, `organization_id`, `store_id` (nullable), `job_type` (`import` | `analysis` | `purge`), `status`, `priority`,
`attempts`, `max_attempts`, `idempotency_key`, `correlation_id`, `enqueued_by`, `available_at`, `locked_at`,
`locked_by`, `lease_expires_at`, `started_at`, `finished_at`, `last_error_code`, `last_error`, `payload` (JSONB),
`result` (JSONB), `created_at`, `updated_at`.

**Contraintes** (les invariants sont dans la base, pas seulement dans Python) :
`jobs_lock_consistent` (`running` ⇔ verrou posé), `jobs_finished_consistent` (terminal ⇔ `finished_at`),
`jobs_attempts_bounded`, `jobs_result_only_when_succeeded`, `jobs_started_before_finished`,
`jobs_payload_is_object`, `jobs_tenant_key`, clé étrangère **composite** vers `stores (organization_id, id)`.

**Index** (cinq, chacun justifié par un accès mesuré) :

| Index | Sert | Taille à 1 M |
|---|---|---|
| `jobs_ready_idx (organization_id, priority DESC, created_at, id) WHERE status = 'queued'` | prise du prochain travail | 95 Mo |
| `jobs_lease_idx (organization_id, lease_expires_at) WHERE status = 'running'` | reprise des baux expirés | < 1 Mo |
| `jobs_terminal_idx (organization_id, finished_at) WHERE status IN (…)` | purge | 1,3 Mo |
| `jobs_recent_idx (organization_id, created_at DESC, id)` | historique d'une organisation | 83 Mo |
| `jobs_idempotency_uniq (organization_id, job_type, idempotency_key) WHERE … NOT NULL` | rejeu d'une demande | < 1 Mo |

`available_at` est un **filtre**, pas une clé d'index : la grande majorité des travaux en file sont disponibles, le
premier éligible arrive immédiatement. Mesuré : 3 à 5 blocs lus par prise, à toute profondeur.

### `audit_events` (révision `0005_audit`)

`id`, `organization_id`, `store_id`, `actor_type` (`user` | `system` | `worker`), `actor_id`, `action`,
`resource_type`, `resource_id`, `correlation_id`, `outcome` (`started` | `succeeded` | `failed`), `metadata`
(JSONB), `created_at`. Index : `recent`, `resource`, `correlation`, tous préfixés par `organization_id`.

### RLS

Les deux tables suivent les règles §4 du handoff 004.1 : `organization_id NOT NULL`, RLS `ENABLE` + `FORCE`,
politique `USING` + `WITH CHECK`, clés étrangères composites, clés uniques préfixées par l'organisation, droits
minimaux pour `mervio_app` (colonne par colonne en `UPDATE` ; aucune `UPDATE` du tout sur `audit_events`).

**Deux politiques RESTRICTIVE** s'ajoutent par ET et ne peuvent donc pas être contournées par une autre politique :
- `jobs_purge_terminal_only` : supprimer un travail en file ou en cours est **impossible**, et un travail terminé ne
  devient supprimable qu'après une heure ;
- `audit_events_retention_floor` : rien de plus récent que 30 jours n'est supprimable.

---

## 4. Machine à états

```
                   enqueue
                      │
                      ▼
   cancel ◄──────  QUEUED  ──────► RUNNING ──────► SUCCEEDED
      │               ▲               │
      ▼               │               ├──────────► FAILED        (terminal)
  CANCELLED           └───────────────┘
                   reprise (échec transitoire, tentatives restantes)
                   récupération (bail expiré)
```

Cinq états, pas un de plus. Transitions autorisées, **imposées par le trigger `jobs_guard_transition`** :

| Depuis | Vers | Quand |
|---|---|---|
| `queued` | `running` | prise par un worker |
| `queued` | `cancelled` | annulation explicite |
| `queued` | `queued` | reprogrammation (`available_at`) |
| `running` | `succeeded` | gestionnaire terminé |
| `running` | `failed` | échec définitif, ou tentatives épuisées |
| `running` | `queued` | échec transitoire avec tentatives restantes, ou bail expiré |

Un état terminal est **définitif** : aucune transition ne part de `succeeded`, `failed` ou `cancelled`. Les colonnes
d'identité sont immuables et `attempts` ne décroît jamais — vérifié en SQL brut.

**Pourquoi `failed` n'est pas un état de passage.** La mission décrivait `RUNNING → FAILED → QUEUED`. Un `failed`
transitoire rendrait ambigu le sens du mot : on ne saurait plus distinguer « a échoué une fois » de « a
définitivement échoué ». Le travail retourne donc directement en `queued`, `last_error_code` renseigné, et `failed`
signifie sans ambiguïté terminal — l'état que 004.0 appelait `dead`.

**Pourquoi `cancelled` existe.** C'est le seul moyen de retirer un travail encore en file : le rôle applicatif n'a
pas le droit de supprimer un travail non terminal (politique RESTRICTIVE), et la purge non plus.

---

## 5. Reprises

- **Tentatives bornées** : `max_attempts`, 3 par défaut, 20 au maximum. Incrémenté **à la prise**, pas à l'échec :
  un travail qui fait systématiquement tomber son worker consomme donc ses tentatives, au lieu d'être repris
  indéfiniment.
- **Délai exponentiel déterministe** : 30 s, 60 s, 120 s, 240 s… plafonné à 1 h. Aucune gigue : un travail n'est pris
  que par un worker à la fois. Un test peut donc prédire l'instant de reprise à la seconde.
- **Transitoire ou définitive** :

| Erreur | Verdict |
|---|---|
| `IngestionError`, `InsufficientDataError`, `ImportRejected`, `ConfigurationError` | définitive |
| `NotFound`, `PermissionDenied`, `TenantAccessDenied`, `PayloadRejected` | définitive |
| `FileNotFoundError`, `IsADirectoryError` | définitive |
| violation de contrainte (unicité, clé étrangère, `CHECK`) | définitive |
| SQLSTATE de verrou, sérialisation, ressource, connexion (`40001`, `40P01`, `55P03`, `53xxx`, `57Pxx`, `08xxx`) | transitoire |
| `JobLeaseLost` | ni l'un ni l'autre : le travail est déjà reparti ailleurs, on ne reprogramme rien |
| exception inconnue | transitoire, dans le budget de tentatives |

Une erreur de validation n'est **jamais** rejouée : rejouer trois fois un CSV invalide ne fait que retarder le
diagnostic.

**Tests sans attente réelle.** Aucun test de reprise ne dort : `available_at`, `now=` et l'horloge injectable du
worker (`clock=`) rendent chaque scénario temporel déterministe.

---

## 6. Reprise après panne

```
worker A : claim ──► running, lease_expires_at = T+300s
           ✗ machine perdue

                       T+301s
                          │
  worker B : recover_stale_jobs() ──► la MÊME ligne repasse en queued
                                      attempts conservé
                                      last_error_code = 'lease_expired'
                                      audit : job.recovered (acteur = system)
                          │
  worker B : claim ──► running, attempts + 1
```

- **Aucune perte, aucun doublon** : la même ligne est remise en file (testé : un seul travail en base après
  récupération).
- **Tentatives épuisées** : le travail devient terminalement `failed` plutôt que de tourner en boucle.
- **Le revenant ne peut rien écraser** : seul le détenteur du verrou (`locked_by`) publie un résultat. Un worker A
  ressuscité reçoit `JobLeaseLost` et son résultat est refusé (testé pour le succès comme pour l'échec).
- **Récupérations concurrentes** : cinq superviseurs simultanés remettent le travail en file **une seule fois**
  (`FOR UPDATE SKIP LOCKED` sur la sélection des baux expirés).

---

## 7. Audit

Une ligne répond à sept questions : **QUI** (`actor_type`, `actor_id`), a fait **QUOI** (`action`), sur **QUELLE
RESSOURCE** (`resource_type`, `resource_id`), pour **QUELLE ORGANISATION**, **QUAND**, sous **QUELLE CORRÉLATION**,
avec **QUEL RÉSULTAT** (`outcome`).

**Quinze actions**, choisies pour être opérationnellement utiles, pas pour faire nombre :
`job.enqueued`, `job.claimed`, `job.succeeded`, `job.failed`, `job.requeued`, `job.recovered`, `job.cancelled`,
`import.started`, `import.succeeded`, `import.failed`, `analysis.started`, `analysis.succeeded`, `analysis.failed`,
`purge.started`, `purge.completed`. La liste est une contrainte `CHECK` : en ajouter une est un acte de revue.

**Atomicité.** L'audit n'est jamais écrit « à côté » : chaque événement d'état part dans la **même transaction** que
la transition qu'il décrit, par le crochet `hook` du dépôt. Un travail commis sans sa trace est impossible ; une
trace sans son travail aussi. Testé dans les deux sens : si le crochet d'audit échoue, la transition est annulée.

**Ajout seul :**

| Opération | Rôle applicatif | Propriétaire des tables |
|---|---|---|
| `INSERT` | autorisé | autorisé |
| `UPDATE` | **droit non accordé** | **refusé par trigger** |
| `DELETE` d'une trace récente | **refusé par politique RESTRICTIVE** | **refusé** (RLS forcée) |
| `TRUNCATE` | non accordé | — |

La seule suppression possible est la rétention, au-delà du plancher de 30 jours, et elle est elle-même tracée.
C'est un journal en ajout seul, pas un journal éternel : dire l'inverse serait faux, puisque `audit_events` doit
bien cesser de croître.

---

## 8. Idempotence

Deux niveaux, aucun inventé pour l'occasion :

1. **Mise en file** : `idempotency_key` unique **par organisation et par type**. Rejouer la demande renvoie le
   travail existant. La clé est préfixée par `organization_id` (leçon 004.1) : une violation d'unicité ne peut donc
   pas servir d'oracle d'existence sur le travail d'un autre tenant.
2. **Exécution** : celle de 004.1, réutilisée sans être contournée.
   - import → `inputs_sha256` : un instantané scellé au plus par empreinte ;
   - analyse → `analysis_runs_completed_uniq` : un rapport au plus par exécution.

**Preuve du rejeu après panne** (`test_a_replayed_import_job_keeps_one_snapshot_after_a_simulated_crash`) : le
gestionnaire s'exécute entièrement, le résultat n'est jamais écrit (worker tué), le bail expire, un autre worker
reprend le travail — résultat : `reused`, **un seul instantané**, `attempts = 2`. Même preuve pour l'analyse : un
seul rapport, le même identifiant.

---

## 9. Observabilité

**Logs JSON**, une ligne = un objet, bibliothèque standard uniquement :

```json
{"timestamp": "2026-09-16T04:47:57.320+00:00", "level": "INFO", "service": "worker",
 "event": "job.succeeded", "correlation_id": "…", "job_id": "…", "organization_id": "…",
 "store_id": "…", "job_type": "import", "worker_id": "hôte/pid/…", "attempt": 1, "duration_ms": 1234}
```

**Corrélation.** L'identifiant vit dans un `ContextVar` relu par le **formateur**, pas par chaque appelant. Les
modules existants du moteur entrent donc dans la trace d'une exécution **sans être modifiés**. Une exécution
d'import produit, sous un seul `correlation_id` :

```
job.enqueued → job.claimed → import.started → import.succeeded → job.succeeded
```

Les mêmes cinq événements existent dans l'audit, avec la même corrélation.

**Échec** : `job.failed` ou `job.requeued` portent `error_code`, `error_type`, `attempt`, `max_attempts`,
`retryable`, `duration_ms`.

**Jamais journalisé** : une clé évoquant un mot de passe, un jeton, un secret, une autorisation, un courriel, un
téléphone, un chemin, un contenu brut ou une invite LLM est remplacée par `[redacted]` ; tout chemin absolu est
masqué ; toute valeur est tronquée à 200 caractères ; une exception publie son **type**, jamais son message ni sa
trace. Testé : aucun chemin des fichiers importés n'apparaît dans la sortie d'une exécution réelle.

**Métriques.** Aucun backend (hors périmètre). Les compteurs demandés — `jobs_total`, succès, échecs, reprises,
`job_duration_ms`, `queue_wait_ms`, `attempt_count` — sont dérivables des logs et de l'audit, et
`jobs.queue_statistics` les lit directement en base (par état, par type, plus l'âge du plus vieux travail en file).
Prometheus et OpenTelemetry : 004.3 ou plus tard, quand un collecteur existera (ADR-004.2-005).

---

## 10. Purge

**Ordre imposé** : travaux terminés, **puis** événements d'audit. L'audit d'un travail lui survit donc toujours.

**Ce que la purge ne pourra jamais faire**, et ce n'est pas du code applicatif qui l'en empêche :

| Garde | Niveau |
|---|---|
| `reports`, `analysis_runs`, `data_snapshots`, `snapshot_sources`, lignes canoniques | aucun droit `DELETE` pour `mervio_app` (depuis 004.1) |
| travail en file ou en cours | politique RESTRICTIVE, vraie en SQL brut |
| travail terminé depuis moins d'**1 h** | politique RESTRICTIVE |
| événement d'audit de moins de **30 jours** | politique RESTRICTIVE |
| rétention d'audit plus courte que celle des travaux | refusée par `RetentionPolicy` |

La chaîne rapport → exécution → instantané → source **ne peut pas** être coupée par une purge, même buggée. C'est
vérifié après une purge réelle : le rapport, son exécution, son instantané et son `Dataset` relu sont intacts.

**Défauts opérationnels** — ce ne sont **pas** des politiques métier ni de conformité, et ils doivent être revus avec
le RGPD en 004.3 :

| Catégorie | Défaut |
|---|---|
| travaux `succeeded` | 7 jours |
| travaux `failed` | 30 jours |
| travaux `cancelled` | 7 jours |
| `audit_events` | 365 jours |
| lot par catégorie | 1 000 lignes |

La purge est un travail comme un autre (`purge`), réservé au rôle `owner`, tracé par `purge.started` et
`purge.completed`, répétable sans dommage, et tenant-sûr.

---

## 11. Performance

**Environnement.** Apple Silicon, macOS 26.4 arm64, 18 cœurs, Python 3.11.16, PostgreSQL 17.11 (Homebrew), cluster
local jetable, `fsync` par défaut. Un processus par profondeur. Fichier :
`benchmarks/results/jobs_20260916_arm64.json`.

**Méthode.** Tout ce qui est mesuré passe par le vrai chemin : vraie prise (`FOR UPDATE SKIP LOCKED`), vrais
triggers, vraie RLS, vrai audit, vrai `Worker.run_until_empty`, une connexion PostgreSQL par worker. Seul le
**remplissage** de la file est fait en lot (mêmes colonnes, mêmes contraintes, mêmes triggers) ; le coût d'une mise
en file unitaire est mesuré séparément avec `workers.enqueue`.

| Mesure | 10 k en file | 100 k en file | 1 M en file |
|---|---|---|---|
| mise en file (API réelle, audit compris) | 0,64 ms | 0,64 ms | 0,67 ms |
| remplissage en lot | 0,23 s | 2,3 s | 25,7 s (≈ 39 k/s) |
| **prise, p50** | **0,33 ms** | **0,33 ms** | **0,47 ms** |
| **prise, p95** | **0,44 ms** | **0,43 ms** | **0,68 ms** |
| prise, max | 4,1 ms | 0,64 ms | 1,2 ms |
| plan de la prise | index `jobs_ready_idx` | identique | identique |
| purge | 1 570 lignes/s | 1 610 lignes/s | 1 600 lignes/s |
| table `jobs` | 6,8 Mo | 53 Mo | 486 Mo |
| **mémoire du worker** | **47 Mo** | **48 Mo** | **48 Mo** |

**Le résultat qui compte : la prise ne dépend pas de la profondeur de la file.** De 10 k à 1 M, la latence passe de
0,33 ms à 0,47 ms et le plan ne change pas (accès par index, aucun tri, 3 à 5 blocs lus). La mémoire du worker est
constante : la file n'est jamais chargée en mémoire.

**Contention, 2 000 travaux exécutés à chaque profondeur :**

| Workers | 10 k | 100 k | 1 M |
|---|---|---|---|
| 1 | 962 /s | 922 /s | 724 /s |
| 2 | 1 500 /s | 1 506 /s | 1 404 /s |
| 4 | **1 444 /s** | **1 476 /s** | **1 461 /s** |
| 8 | 962 /s | 954 /s | 965 /s |

Le débit plafonne vers 2 à 4 workers et **redescend à 8**. Ce n'est pas de la contention de verrou — `SKIP LOCKED`
l'exclut, et les tests montrent qu'un verrou tenu ne bloque jamais le worker suivant : c'est le coût du `commit`
(un `fsync` par transaction, deux transactions par travail) sur un cluster local mono-disque. Le point de
saturation est donc l'écriture du WAL, pas la file.

**Deux mesures de latence, pas une moyenne.** À froid, juste après un chargement en lot d'un million de lignes, la
**première** prise peut coûter des centaines de millisecondes : pages d'index absentes du cache et autovacuum encore
actif sur la table fraîchement écrite. Les suivantes reviennent sous la milliseconde. Le benchmark publie donc
`claim_latency_cold_ms` et `claim_latency_ms` séparément, plutôt que de les mélanger en une moyenne trompeuse. Une
campagne a montré un palier isolé à ~38 ms par prise sur la série 100 k, non reproductible ensuite et jamais
accompagné d'un changement de plan (vérifié par `EXPLAIN ANALYZE` : 0,03 ms d'exécution, 4 blocs) : c'est de la
contention d'entrées-sorties de la machine de mesure, pas une propriété de la file.

**Garde anti-régression.** `jobs.explain_claim` expose le plan de **l'instruction réelle** de prise ; le benchmark et
`tests/persistence/test_jobs_query_plans.py` expliquent donc ce qui s'exécute, pas une requête approchante. Les
tests exigent, sur 3 000 lignes : index `jobs_ready_idx`, aucun `Seq Scan`, aucun `Sort`, présence du `LockRows`,
et un coût de `LIMIT 1` inférieur au cinquantième d'un parcours complet.

---

## 12. Sécurité

### Matrice, telle que testée

| Scénario | Attendu | Vérifié |
|---|---|---|
| Tenant A lit le travail de A | autorisé | dépôt |
| Tenant A lit le travail de B | `NotFound` (indiscernable d'inexistant) | dépôt + SQL brut |
| Tenant A prend son travail | autorisé | dépôt |
| Tenant A prend le travail de B | impossible (aucune ligne) | dépôt + SQL brut |
| Tenant A vise le travail de B **par identifiant** | impossible | dépôt |
| Tenant A termine le travail de B | `NotFound` | dépôt |
| Tenant A récupère le bail expiré de B | aucun effet | dépôt |
| Tenant A annule le travail de B | `NotFound` | dépôt |
| Tenant A purge, B intact | oui | dépôt |
| Tenant A écrit un audit pour A | autorisé | dépôt |
| Tenant A écrit un audit pour B | `InsufficientPrivilege` | SQL brut |
| Tenant A lit l'audit de B | vide | dépôt + SQL brut |
| Tenant A met à jour un audit | refusé (droit **et** trigger) | SQL brut, rôle applicatif **et** propriétaire |
| Tenant A supprime un audit récent | refusé (politique RESTRICTIVE) | SQL brut, rôle applicatif **et** propriétaire |
| Tenant A supprime un travail en cours | refusé (politique RESTRICTIVE) | SQL brut |
| Worker traite un travail de A | autorisé | intégration |
| Worker franchit une frontière de tenant | hors de portée de la requête | intégration, 6 workers, 2 tenants |
| Sans contexte de tenant | rien n'est visible | SQL brut |

**Aucune existence étrangère n'est révélée.** Une ressource absente et une ressource d'un autre tenant lèvent la
**même** `NotFound` (règle 004.1). La clé d'idempotence étant préfixée par l'organisation, deux tenants peuvent
utiliser la même clé sans qu'aucune violation d'unicité ne fuite (testé).

**Le worker ne franchit pas de frontière parce qu'il n'y a pas de frontière à franchir** (ADR-004.2-002) : il sert
une liste explicite de `TenantSession`, chaque prise passe par la vérification d'appartenance **et** par RLS. Aucun
rôle `BYPASSRLS`, aucune fonction `SECURITY DEFINER`, aucun privilège nouveau. Le travail d'une organisation non
servie n'est pas « interdit », il est hors de portée de la requête.

**Le rôle est revérifié à l'exécution.** Un travail de purge mis en file par un `owner` échoue si le worker
n'a qu'un rôle `analyst` (testé). L'autorisation n'est jamais héritée du seul fait qu'un travail existe.

**Rôles** (matrice de `MISSION_004_0_ARCHITECTURE.md` §8) :

| Permission | Rôle minimal |
|---|---|
| `RUN_JOBS` (exécuter un travail en file) | `analyst` |
| mettre en file un `import` / une `analysis` | `analyst` |
| `READ_AUDIT` | `admin` |
| `PURGE_DATA`, mettre en file une `purge` | `owner` |

**Aucun secret nulle part.** La charge utile d'un travail refuse toute clé de secret et toute valeur de plus de
4 Ko : elle **localise** une source, elle ne la transporte pas. L'audit et les logs passent par le même filtre de
rédaction.

---

## 13. Tests

```bash
MERVIO_TEST_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres \
MERVIO_REQUIRE_DATABASE_TESTS=1 .venv/bin/python -m pytest
```

```
1097 passed in 99.46s
0 failed · 0 skipped · 0 error
```

Sans base : `796 passed, 301 skipped`.

| Fichier | Tests | Ce qu'il établit |
|---|---|---|
| `tests/test_jobs_unit.py` | 60 | machine à états, reprise, classification des erreurs, registre, rétention, permissions — sans base |
| `tests/test_observability_logging.py` | 34 | forme JSON, corrélation (y compris entre fils d'exécution), rédaction |
| `tests/persistence/test_persistence_jobs.py` | 79 | cycle de vie, idempotence, bail, annulation, purge, isolation dépôt **et** SQL brut, machine à états en base |
| `tests/persistence/test_persistence_audit.py` | 32 | contenu, ajout seul, immuabilité (rôle applicatif **et** propriétaire), isolation, rétention |
| `tests/persistence/test_jobs_worker.py` | 50 | import, analyse, purge, audit corrélé, logs, injection de pannes, boucles, rejeu après crash |
| `tests/persistence/test_jobs_concurrency.py` | 8 | 100 travaux × 10 workers réels, absence de doublon, non-blocage, deux tenants en parallèle |
| `tests/persistence/test_jobs_query_plans.py` | 8 | plans figés sur 3 000 lignes : index, pas de tri, pas de parcours |
| `test_persistence_migrations.py` (étendu) | +8 | RLS forcée, politiques RESTRICTIVE, triggers, index préfixés, clés composites |
| `test_persistence_boundaries.py` (étendu) | +3 | `workers` sans pilote SQL, `observability` sans base ni moteur, `workers` sans calcul analytique |

**Aucun test existant n'a été supprimé ni affaibli.** Les seules modifications portent sur des listes de tables et
de révisions à compléter (`conftest`, migrations) et sur des listes d'autorisation explicitement conçues pour être
étendues sous revue (`test_sql_fstrings_only_interpolate_reviewed_constants`).

**Injection de pannes couverte** : erreur de base transitoire et définitive, source invalide, source absente,
charge utile incomplète ou invalide, moteur en échec, crash après la prise, crash avant le résultat, bail perdu,
exception inconnue, gestionnaire absent, audit indisponible.

---

## 14. Risques

| # | Risque | Statut après 004.2 |
|---|---|---|
| R-01 | Fuite entre tenants | **atténué** aussi pour la file et l'audit (dépôt + SQL brut) ; reste ouvert pour l'API (004.3) |
| R-07 | Analyse ou import lents | **atténué** : exécutés en tâche de fond, hors transaction |
| R-21 | Rétention et suppression | **partiellement traité** : purge des travaux et de l'audit, ordonnée, plancher en base. Suppression d'une organisation et des données client : toujours ouvert |
| R-24 | Transaction d'import longue | **atténué** : aucune transaction longue dans le worker. L'import lui-même reste en une transaction (héritage 004.1) |
| R-25 | Tests PostgreSQL ignorés en CI | inchangé : `MERVIO_REQUIRE_DATABASE_TESTS=1` obligatoire |
| **R-26** (nouveau) | Un worker ne sert que les tenants qu'on lui donne : passer à des milliers d'organisations demande un répartiteur | conçu (ADR-004.2-002), non implémenté ; 004.3 |
| **R-27** (nouveau) | Le diagnostic complet d'un échec n'est que dans le log du worker (la file et l'audit n'en gardent que la forme) | assumé (ADR-004.2-006) ; impose une collecte de logs en production |
| **R-28** (nouveau) | Saturation par `fsync` : deux transactions par travail, débit plafonné vers 4 workers | mesuré, documenté ; regrouper les prises par lot est possible si le besoin apparaît |
| **R-29** (nouveau) | Aucune planification récurrente : rien ne déclenche un import quotidien ni une purge périodique | ouvert ; table `schedules` d'ADR-004-008 non faite |
| **R-30** (nouveau) | Un travail dont le gestionnaire fait systématiquement tomber le worker consomme ses tentatives et finit `failed` | voulu ; évite la boucle infinie, mais un incident d'infrastructure prolongé peut faire échouer des travaux valides |

---

## 15. Non résolu

1. **Planification récurrente** (`schedules`, ADR-004-008) : aucun déclencheur périodique. La purge doit être mise
   en file à la main, par organisation.
2. **Répartiteur multi-tenant** : le schéma est décrit dans ADR-004.2-002 (rôle `mervio_dispatcher`, politique
   `FOR SELECT TO`, fonction `SECURITY DEFINER` ne renvoyant que des métadonnées d'aiguillage). À implémenter avec
   l'identité de service de 004.3.
3. **Enchaînement de travaux** : un import réussi ne déclenche pas l'analyse. Chaque travail est indépendant.
4. **Suppression d'une organisation** et rétention des données client (R-21) : exige une décision écrite sur ce qui
   est conservé, avant toute implémentation.
5. **Stockage objet des fichiers bruts** (hérité de 004.1) : seules les empreintes sont gardées ; la charge utile
   d'un import désigne un chemin local.
6. **Pool de connexions** : une `Database` = une connexion. Le pool arrive avec l'API.
7. **Prometheus / OpenTelemetry** : hors périmètre tant qu'aucun collecteur n'existe.
8. **`LISTEN/NOTIFY`** : le worker interroge la file. Suffisant au débit mesuré ; utile si la latence de prise
   devient un critère.

---

## 16. Règles pour 004.3 et au-delà

- Préserver les 1 097 tests et 0 régression ; tout test modifié pour un changement de contrat est justifié par
  écrit.
- Aucun accès SQL hors de `mervio.persistence` (test AST). `mervio.workers` reste sans pilote de base.
- Toute modification de schéma passe par une **nouvelle** révision Alembic, montée et descente testées. Les
  révisions 0001 à 0005 ne se modifient plus une fois `mission-004.2` fusionnée.
- Toute nouvelle table de tenant respecte les règles §4 du handoff 004.1, et `test_persistence_migrations.py` doit
  être étendu.
- Tout nouvel événement d'audit passe par la contrainte `CHECK` de `0005_audit`, donc par une migration et une
  revue.
- Jamais de connexion applicative superutilisateur ou BYPASSRLS. Un worker ouvre ses transactions via
  `TenantSession`.
- Aucun secret dans une charge utile de travail, dans l'audit ou dans un log.

---

## 17. Mission 004.3 recommandée — API v1

- **Objectif** : exposer la persistance et la file derrière une API HTTP authentifiée.
- **Périmètre suggéré :**
  - FastAPI, identité OIDC (`users.idp_subject` existe depuis 004.1) ;
  - pool de connexions ;
  - `request_id` propagé du client, réutilisé comme `correlation_id` — l'infrastructure de corrélation est prête ;
  - routes de mise en file (import, analyse) et de lecture (travaux, rapports, provenance, audit) ;
  - **404 croisé par route** : la règle « inexistant et interdit sont indiscernables » doit être vérifiée au niveau
    HTTP, pas seulement au niveau dépôt (R-01) ;
  - identité de service pour le worker, puis répartiteur multi-tenant (R-26) ;
  - table `schedules` (R-29).
- **À réutiliser sans les contourner** : `TenantSession` et sa vérification d'appartenance, les codes d'erreur de la
  file, la rédaction des logs et de l'audit.
- **Hors périmètre** : connecteurs API (004.4), frontend (004.5), LLM de production (004.6).

---

## 18. Premières actions de la conversation suivante

1. `git status`, `git log --oneline -10`, branche et fusion de `mission-004.2`.
2. Démarrer PostgreSQL (`scripts/dev_postgres.sh start` ou `docker compose up -d postgres`).
3. Lancer la suite, 1 097 attendus :

   ```bash
   MERVIO_TEST_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres \
   MERVIO_REQUIRE_DATABASE_TESTS=1 .venv/bin/python -m pytest
   ```

4. Lire ce handoff, `docs/MISSION_004_2_DECISIONS.md`, puis `docs/MISSION_004_1_HANDOFF.md` §4 (règles) et
   `docs/MISSION_004_1_PERSISTENCE.md` §2 à §7.
