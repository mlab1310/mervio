# Mission 004.4 — Handoff (document de départ de Mission 004.5)

> **Objet.** Ce document permet de reprendre Mervio **sans aucun contexte conversationnel**. Il
> couvre l'intégralité de la mission 004.4 — *Data Protection & Safe Ingestion* — de sa première
> sous-mission à sa clôture, et il énonce les résiduels **sans les masquer**. Les arbitrages sont
> journalisés dans [`DECISIONS.md`](DECISIONS.md) ; la feuille de route de référence est
> [`MERVIO_FINAL_ROADMAP.md`](MERVIO_FINAL_ROADMAP.md).

## 1. Baseline

| | |
|---|---|
| Dépôt | `/Users/mlab/Projects/mervio` |
| Branche | `mission-004.4` — identique à `origin/mission-004.4` |
| Commit final | `e1a68efa8d591f7058eba1235297a7d934e9567f` — `feat(persistence): finalize tenant purge recovery` |
| Arbre de travail | propre |
| Tête de migration | **`0016_tenant_purge`** |
| CI | **run #32, `36793763636`, `success`** — 4 jobs sur 4 (`lint`, `test`, `security`, `docker`) |
| Tests | **2982 passés · 0 ignoré · 0 échec · 0 erreur** (`MERVIO_EXPECTED_TESTS = 2982`, égalité **exacte** imposée par la CI) |
| Environnement | Python 3.11.16 · pytest 9.1.1 · PostgreSQL 17.11 (épinglé par digest) · psycopg 3.3.5 · Alembic 1.20.0 |
| Moteur | **ENGINE_VERSION 0.1.0 · contrat LLM 1.0 — inchangés.** Aucune formule, aucun seuil, aucune clé de rapport modifiés par 004.4 |

**Le fait central de 004.4 :** une organisation peut désormais être **entièrement détruite** par un
travail audité, un client peut être **effacé** de bout en bout, et **aucun import du chemin SaaS ne
lit plus un chemin fourni par l'appelant**. La frontière d'isolation locative est, et reste, la
ligne sous RLS forcée — jamais un module Python, jamais un privilège élevé.

## 2. Chronologie

| Sous-mission | Révision(s) | Décisions | Commit final | Acceptance |
|---|---|---|---|---|
| **004.4.1** — hygiène anti-`pg_temp`, gardes résiduelles | `0009`, `0010` | D-049 | `7ac1d9a` — `fix(security): qualify residual tenant guards` | [`MERVIO_004_4_1_ACCEPTANCE.md`](MERVIO_004_4_1_ACCEPTANCE.md) |
| **004.4.2** — identité client à clé, retrait de l'e-mail persisté | `0011` | D-053, D-057 | `dfe1975` — `feat(privacy): implement tenant-scoped identity and safe ingestion` | [`MERVIO_004_4_2_ACCEPTANCE.md`](MERVIO_004_4_2_ACCEPTANCE.md) |
| **004.4.3** — plans du répartiteur sous RLS | `0012` | D-060, D-050 | `aed66b0` — `perf(jobs): robust dispatcher probe and claim plans under RLS` | [`MERVIO_D060_ACCEPTANCE.md`](MERVIO_D060_ACCEPTANCE.md) |
| **004.4.4** — frontière d'import sans chemin, `ObjectStore`, `raw_objects` | `0013` | D-054, D-055, D-061 | `252daa2` — `fix(container): prepare named volumes for image user` | [`MERVIO_004_4_4_ACCEPTANCE.md`](MERVIO_004_4_4_ACCEPTANCE.md) |
| **004.4.5** — effacement client : désignation, concurrence, destruction réelle | `0014`, `0015` | D-062, D-063, D-064, achèvement de D-056 | `720404d` — `fix(004.4.5): assert ratified customer erasure audit event` | [`MERVIO_004_4_5_ACCEPTANCE.md`](MERVIO_004_4_5_ACCEPTANCE.md) |
| **004.4.6** — purge de boutique et d'organisation, reprise | `0016` | D-065 à D-069, D-071 ; **D-070 ouverte** | `e1a68ef` — `feat(persistence): finalize tenant purge recovery` | [`MERVIO_004_4_6_ACCEPTANCE.md`](MERVIO_004_4_6_ACCEPTANCE.md) |

**Protocole observé, et à conserver :** toute décision d'architecture est **ratifiée par un commit
documentaire avant** son implémentation (D-062, D-063, D-064, D-065, D-066 l'ont été ainsi).
**Exception assumée de 004.4.6** : D-067, D-068, D-069 et D-071 sont nées *pendant* l'implémentation,
d'une revue de concurrence qui a mesuré un défaut réel ; elles sont ratifiées **avec** leur code et
leurs tests, et leur journalisation dans `DECISIONS.md` a été faite par la clôture documentaire de
la mission. Si ce cas se reproduit, le journaliser **dans le commit qui l'introduit**.

Documents d'appui conservés : [`MERVIO_004_4_5_E5_RACE.md`](MERVIO_004_4_5_E5_RACE.md) (campagne de
course D-063, statut **CLOS**), [`MISSION_004_4_2_HARDENING.md`](MISSION_004_4_2_HARDENING.md),
[`RETENTION_POLICY_V0.md`](RETENTION_POLICY_V0.md).

## 3. Architecture actuelle

```
  CLI admin (opérateur)              PostgreSQL 17                       worker
 ─────────────────────         ─────────────────────────         ──────────────────────
  mervio admin …                organizations / stores             claim_next_job()
     │  TenantSession             memberships / users                 │
     │  (RLS, rôle app)          service_authorizations              │ identité de service
     ├─ object upload ─────────►  raw_objects  ◄────────────────────  │ ObjectStore (lecture)
     ├─ job enqueue-* ─────────►  jobs (queued)  ───────────────────► │ gestionnaires
     ├─ org show / list        data_snapshots / canonical rows        │
     └─ org reconcile-purges     analysis_runs / reports              │ fonctions SECURITY DEFINER
                                 audit_events (ajout seul)            │ (purge, effacement)
                                 organization_identity_keys           │
                                 customer_redactions                  │
```

### 3.1 Tenancy

`organizations`, `stores`, `memberships`, `users`. **RLS `ENABLE` + `FORCE`** sur toutes les tables
de locataire. Un `organization_id` **n'accorde rien** : chaque opération relit en base
l'appartenance **et** le rôle de l'acteur. Rôles : `owner` > `admin` > `analyst` > `viewer`.
`Database` **refuse** toute connexion superutilisateur ou `BYPASSRLS`.

Depuis `0016` : `status` (`active`/`purging`/`purged`) et `purged_at` sur `organizations` **et**
`stores`, **sans aucun `GRANT UPDATE`** au rôle applicatif, et exclues de son `GRANT INSERT`.

### 3.2 Identité et PII (`0011`, D-053, D-057)

`customer_ref` = préfixe versionné + 128 bits de HMAC-SHA256 : `c1:` depuis l'e-mail normalisé,
`g1:` depuis l'identifiant de commande pour un invité. **L'e-mail n'est jamais persisté.** Clé par
organisation (`organization_identity_keys`), dérivée d'une clé maître **hors base**
(`MERVIO_IDENTITY_MASTER_KEY`). Le sel est **destructible, jamais supprimable** : le déclencheur de
`0011` interdit le `DELETE`, fige `master_key_id` et rend immuable une ligne déjà détruite.

**Le worker détient la clé maître ; `admin` ne la reçoit à aucun moment** et n'accepte **aucune**
identité directe (D-062).

### 3.3 Instantanés et rapports

`data_snapshots` (scellés), `snapshot_sources`, lignes canoniques, `analysis_runs`, `reports`.
Immuabilité par déclencheur : un `DELETE` direct sur une canonique d'un instantané **scellé** est
refusé ; la suppression passe par `data_snapshots` et son CASCADE. FK auto-référente `supersedes`
en RESTRICT → un `DELETE` **unique** couvrant toute la portée, jamais par lots.

**Garantie de reproductibilité (roadmap 004.4 item 7) :** rapport persisté = rapport CLI, **à clé de
pseudonymisation identique**. Vérifié par `tests/persistence/test_persistence_reports.py` et
`test_jobs_worker.py`. L'écart introduit par un effacement est documenté, non masqué.

### 3.4 Jobs (`0004`, `0006`, `0012`, `0014`, `0016`)

Six types : `import`, `analysis`, `purge` (**rétention**), `redact_customer`, `purge_store`,
`purge_organization`. **Ne pas confondre `purge` et les purges tenant** : la rétention supprime des
travaux terminés et des événements expirés **sans toucher** aux instantanés, exécutions ni rapports ;
la purge tenant détruit **exactement cela** et **conserve** l'audit (D-051). Sémantiques opposées.

Baux (`0006`), reprises à backoff borné (`BACKOFF_CAP_SECONDS = 3600`), jeton d'exclusion
`attempts || ':' || locked_by`, `max_attempts BETWEEN 1 AND 20` (défaut 3). Répartiteur : **RLS +
identité de service + autorisation par organisation**, **aucune** fonction `SECURITY DEFINER`,
**aucun** rôle `mervio_dispatcher` (D-050 ; l'option de 004.2 est *superseded*).

### 3.5 Magasin d'objets (`storage/`, D-055)

Interface `ObjectStore` : `put` / `open` / `delete`, trois pilotes (mémoire, filesystem confiné,
S3), **une seule** suite de contrat. Clés `org/<org>/store/<store>/raw/<uuid>`, **générées côté
serveur**. `storage/` ne dépend d'aucune autre couche et **n'est pas une frontière de sécurité** —
la ligne `raw_objects` sous RLS l'est.

### 3.6 Objets bruts (`0013`, D-054, D-061)

`raw_objects` : organisation, boutique, clé, `sha256`, `byte_size`, `source_kind`, `origin`,
`state`, `retain_until`, `purged_at`, `purge_reason`. **Aucun nom de fichier n'y figure.** Le rôle
applicatif n'a que `GRANT SELECT, INSERT` : **ni `UPDATE`, ni `DELETE`** — `sha256` est donc figé dès
l'insertion, et le cycle de vie `pending → available → purging → purged` n'est écrit que par des
fonctions privilégiées. La charge d'un import ne porte que des `raw_object_id` ; `validate_payload`
**refuse** toute clé de chemin.

### 3.7 Effacement client (`0014`, `0015`, D-056, D-062, D-064)

`customer_redactions` est la **preuve** d'un effacement et ne contient **aucune donnée
personnelle** : le `customer_ref` effacé, le tombstone aléatoire `redacted:<uuid4>` qui l'a
remplacé, l'origine, le demandeur, le travail, la date. `UNIQUE (organization_id, customer_ref)`
porte l'idempotence. Écrite **uniquement** par `app_redact_customer`.

**Rejeu à l'import** : un client effacé le **reste**. Dans la transaction de `write_snapshot`, entre
dérivation et écriture, la référence est remplacée par le tombstone **déjà enregistré**.

**Destruction physique** : `ObjectStore.delete` **après** le tombstone, jamais avant ; la ligne
reste `purging` si le magasin échoue — illisible, non déclarée détruite, **reprenable**.

### 3.8 Purge tenant (`0016`, D-051, D-065 à D-069)

**Huit** fonctions `SECURITY DEFINER` — `app_close_organization`, `app_close_store`,
`app_destroy_identity_key`, `app_purge_finalize_raw_object`, `app_purge_tenant_data`,
`app_tombstone_organization_stores`, `app_tombstone_organization`, `app_tombstone_store` — plus
**un** helper `app_purge_guard`, **`SECURITY INVOKER`**, `REVOKE ALL FROM PUBLIC`, **aucun `GRANT`** :
il porte la batterie D-052 commune aux huit **sans ajouter de surface privilégiée**.

`app_redact_customer` et `app_finalize_raw_object_purge` conservent leur batterie **inlinée**
(D-065 Q2) : **trois** corps à maintenir équivalents.

Quatre actions d'audit : `store.purge_started`, `store.purged`, `organization.purge_started`,
`organization.purged`. **Jamais** réutilisées depuis `purge.started` / `purge.completed`, qui
appartiennent à la rétention.

**Descente : les huit définisseurs AVANT le helper.** PostgreSQL n'enregistre aucun `pg_depend`
entre fonctions plpgsql — l'ordre inverse réussirait en laissant huit fonctions pointant dans le
vide. Un test verrouille cet ordre.

### 3.9 Audit

`audit_events`, **en ajout seul**, écrit **dans la transaction du changement**. Toute action nouvelle
passe par une **migration de la contrainte `CHECK` de `0005_audit`** — invariant de projet, sans
exception. Jamais de secret, de PII ni de prompt dans l'audit, les logs ou les charges de travail.

### 3.10 Concurrence D-063

Verrou consultatif PostgreSQL d'organisation, **pris dans la transaction** : import →
`pg_advisory_xact_lock_shared(clé_org)` ; effacement et purge → `pg_advisory_xact_lock(clé_org)`.
`persistence/concurrency.py` **refuse** (`LockOutsideTransaction`) un verrou demandé hors
transaction — sans ce refus, `autocommit=True` en ferait un **no-op silencieux**.

**Propriété à comprendre avant de toucher à la purge :** le verrou est un verrou **de
transaction**, et chaque opération privilégiée est **sa propre** transaction. Ce qui sérialise les
étapes d'une purge entre elles est le **STATUT** (`purging`), **pas** le verrou.

## 4. Tête de migration

**`0016_tenant_purge`** (revises `0015_raw_object_purge`).

```
0001 tenancy          0007 service_identity        0013 raw_objects
0002 snapshots        0008 admin_audit             0014 customer_erasure
0003 analysis_reports 0009 qualify_security_fn     0015 raw_object_purge
0004 jobs             0010 qualify_residual_sec    0016 tenant_purge  ← tête
0005 audit            0011 identity_pii_schema
0006 job_leases       0012 dispatch_probe_plan
```

Règles : `0001`–`0005` **ne sont plus modifiées** ; tout changement de schéma = **nouvelle
révision**, montée **et** descente testées, `test_persistence_migrations.py` étendu ; la CI exerce
`up → down → up` sur base neuve à chaque exécution.

## 5. Cycle de vie d'une purge

```
active ──(app_close_organization)──► purging ──(app_tombstone_organization)──► purged
                                        │                                         │
                                        │  fenêtre D-066 OUVERTE                   │ refermée
                                        │  (plancher de rétention levé)            │
                                        │  entrée fermée, SAUF reprise             │ TERMINAL
```

Ordre exécuté par `make_purge_organization_handler`, et il est **normatif** :

1. clôture → `purging`, travaux `queued` annulés, objets rendus illisibles, audit `*.purge_started` ;
2. **le sel** (D-065 Q4, D-053, D-069) — ici seulement, et seulement si l'organisation est `purging` ;
3. octets détruits, puis lignes `raw_objects` retirées ;
4. données : rapports → exécutions → instantanés (CASCADE) → connexions → preuves d'effacement →
   **travaux** (dans la fenêtre D-066) ;
5. boutiques tombstonées, une par une ;
6. pierre tombale de l'organisation → `purged`, audit `organization.purged` — **l'exception D-066 se
   referme ici**.

**Deux propriétés d'ordre à ne jamais inverser.** *Le sel avant les données* : les références client
encore présentes deviennent définitivement non réversibles **avant** d'être supprimées. *Le nettoyage
des travaux avant la pierre tombale* : après `purged`, la fenêtre D-066 est refermée et l'étape 10
de D-051 redevient impossible — **silencieusement**, un `DELETE` sans effet ne levant rien.

`purge_store` suit la même forme sans toucher **ni** au sel **ni** aux `customer_redactions`, qui
sont à l'échelle de l'organisation. L'organisation reste vivante.

## 6. Reprise d'une purge figée

**Commande :** `mervio admin org reconcile-purges --as <owner> [--org UUID] [--execute] [--limit N]`.
**Simulation par défaut** ; sans `--execute`, **rien n'est écrit**. Documentée dans
[`ADMIN_CLI.md`](ADMIN_CLI.md).

**Pourquoi dans `admin` et pas dans le worker.** `0007` (SEC-06) impose `jobs_service_no_insert`,
RESTRICTIVE `WITH CHECK (false)` sur `mervio_worker` : **un worker ne crée jamais un travail**.
L'autoriser aurait exigé d'amender une frontière de sécurité ratifiée. La reprise passe donc par la
**mise en file nominale**, sous l'identité d'un humain `owner` — **sans `SET ROLE`, sans
`SECURITY DEFINER`, sans insertion directe**. Un test de forme le verrouille sur le code réel.

**Garde anti-boucle D + B + A, délibérément hors base** — une garde en base créerait un nouveau
dead-end, alors qu'ici le chemin manuel reste ouvert quoi qu'elle décide :

| | Règle | Valeur |
|---|---|---|
| **D** | fail-closed : `55006` est le **seul** code transitoire ; tout autre arrête la reprise | `{sqlstate_55006}` |
| **B** | plafond d'échecs, origine comprise → au plus **deux** reprises automatiques | 3 |
| **A** | délai minimal depuis le dernier `finished_at` | 3600 s (`BACKOFF_CAP_SECONDS`) |

L'**ordre** d'évaluation compte : une cause déterministe arrête **avant** que le plafond ne soit
consommé.

**Chemin manuel :** `mervio admin job enqueue-purge-organization` — aucune garde anti-boucle ne s'y
applique, et le déclencheur l'autorise par l'exception de D-068 (`jobs` seule,
`purge_organization` seul, `purging` seul).

**Il n'existe aucune option `--force`**, nulle part. `purged` est **terminal** : rien n'y entre, pas
même une purge, et aucune commande ne propose de l'outrepasser.

## 7. Invariants de sécurité — à ne pas éroder

1. **RLS `ENABLE` + `FORCE`** sur toutes les tables de locataire. `SECURITY DEFINER` **ne contourne
   pas la RLS** : le propriétaire du schéma y est soumis, donc chaque fonction privilégiée reste
   confinée à une organisation **par PostgreSQL**, pas par une condition Python.
2. **Le worker ne crée jamais un travail** (SEC-06, `0007`). Non amendé par 004.4.6.
3. **Le worker est strictement plus contraint**, jamais moins : `jobs_service_purge_by_owner`
   (RESTRICTIVE, `TO mervio_worker`, rang délégué ≥ 3) s'ajoute **par ET** aux politiques existantes.
4. **Aucun `SET ROLE` dans Mervio** — zéro occurrence dans `src/`, vérifié. D-052 le range parmi les
   options **rejetées**, et un test de forme l'asserte sur le chemin de reprise. Ne pas en introduire.
5. **Aucun `BYPASSRLS`, aucun superutilisateur** : `Database` refuse la connexion au premier usage.
6. **Les colonnes de cycle de vie sont hors de portée du rôle applicatif.** `status` et `purged_at`
   n'ont **aucun `GRANT UPDATE`** et sont exclues du `GRANT INSERT` de colonne. **Attention** :
   `REVOKE INSERT (colonne)` est **sans effet** si `INSERT` a été accordé au niveau **table** —
   `0016` révoque la table puis réaccorde colonne par colonne. Toute révision future qui accorderait
   `UPDATE` sur ces colonnes **briserait silencieusement D-066 et D-069**.
7. **Aucun GUC n'est une autorisation.** `app.*` porte un **contexte**, jamais un droit : il est
   positionnable par n'importe quel rôle. Ne jamais ancrer une garde dessus.
8. **Relations qualifiées `public.` et `SET search_path` explicite** dans tout corps de fonction
   (D-049). L'inventaire de `test_pg_temp_residual.py` couvre désormais **lectures ET cibles
   d'écriture** (`FROM|JOIN|USING|UPDATE|INSERT INTO`) — **résiduel F-1 fermé en 004.4.6**.
9. **Inventaire du privilège** : toute fonction `SECURITY DEFINER` **autre** que celles attendues
   fait échouer `test_the_hygiene_changes_no_privilege_and_adds_no_definer`.
10. **D-063 : un seul verrou, une seule primitive.** Ne pas ajouter de portée, ne pas ajouter de
    seconde primitive de concurrence.
11. **Aucun SQL hors de `mervio.persistence`** ; `mervio.workers` sans pilote de base (tests AST).
12. **Aucun KPI calculé ailleurs que dans le moteur déterministe** (API, SQL, frontend, LLM inclus).

## 8. Résiduels connus — documentés, non masqués

### Portés par 004.4.6

1. **La détection d'une purge figée est entièrement manuelle** ⚖️ — **limite principale**. Rien n'est
   périodique, rien n'alerte, rien ne relance. Une organisation peut rester `purging`, **sel
   détruit**, indéfiniment, tant que personne ne lance `reconcile-purges`.
2. **Historique supprimé → `no_requester`** : la fenêtre D-066 permet de supprimer la purge
   d'origine pendant `purging` ; l'organisation reste alors `purging` **sans demandeur
   identifiable**, et la réconciliation refuse. Seul le chemin manuel reste ouvert.
3. **D-070 non implémentée** : aucune trace durable d'une reprise **refusée**.
4. **`max_attempts = 7` n'est dominant qu'aux valeurs par défaut** de bail ; aucune valeur admise par
   le schéma (≤ 20) ne couvre `MERVIO_JOB_LEASE_SECONDS` au maximum (86400).
5. **Trois batteries de gardes D-052** à maintenir équivalentes : risque de dérive réduit, non annulé.
6. **La dépendance d'ordre de D-066 n'est protégée que par des tests** (test 4).
7. **`60505e1` et `790c8db` n'ont pas de CI propre** : la mission est validée **en bloc**, à sa tête.
8. **Les purges tenant ne sont pas exercées en conteneurs** : `container_smoke.py` n'a pas été
   étendu. La destruction est prouvée sur PostgreSQL réel, **pas** en runtime Docker.
9. Élargissement réel du plancher de rétention pendant `purging` ⚖️ ; clôture interrompue sans
   expiration ; annulation commitée irréversible ; purge de boutique refusée pendant une autre purge
   de la même organisation ; `memberships` et `service_authorizations` révoquées conservées après
   purge ; coût d'évaluation de la politique non mesuré.

### Hérités, inchangés

10. **004.9** : expiration de `retain_until` (**personne ne le lit**), ramassage des orphelins,
    écriture conditionnelle S3 (`IfNoneMatch`), politique IAM `s3:DeleteObject` — donc
    `delete_capability()` de S3 rend `undetermined` et le worker démarre en journalisant un
    avertissement, par **honnêteté** plutôt que par un drapeau déclaratif —, non-écrasement de `put`,
    KMS réel.
11. **TOCTOU du pilote filesystem réduit, non annulé** : `O_NOFOLLOW` couvre la lecture et le
    **dernier** composant ; `put` et `delete` résolvent toujours avant d'agir.
12. **Compromission du worker → destruction ou écrasement des octets de tous les locataires du
    volume partagé** ⚖️ — accepté par D-064 : D-056 **exige** cette capacité, et l'isolation locative
    n'en dépend pas.
13. **La résolution d'identité n'est pas auditée par le produit** (D-062) : qui peut exécuter un
    processus dans le conteneur `worker` peut ré-identifier. Restreindre `docker exec` en conséquence.
14. **Compteurs `raw_objects_*` non observables par la CLI** : masquage **volontaire** de `scrub()`,
    verrouillé par un test de garde.
15. **S3 n'est exercé que par `moto`** : aucun bucket réel, aucune politique IAM vérifiée.
16. **Durées à valider juridiquement** ⚖️ : audit 365 j, objets bruts 30 j, rôles de traitement
    (**004.9**, roadmap §11.5 et §13).

### Dette documentaire, hors périmètre de cette clôture

17. **`docs/ARCHITECTURE.md`** décrit l'arborescence de l'ère 001/002 : ni `persistence/`, ni
    `admin/`, ni `workers/`, ni `observability/`, et il renvoie à `ROADMAP.md`, lui-même déclaré
    obsolète. **`docs/PROJECT_STATE.md`** est daté du 16 septembre 2026 et annonce 1097 tests.
    Les deux sont **sciemment laissés inchangés** : leur remise à niveau est un chantier à part
    entière, qui ne relève pas de la clôture de 004.4.

## 9. Prochaine mission

**La roadmap prévoit 004.5 — *Report Contract 2.0 & Analytics Correctness* (P0).** Ses dépendances
déclarées — `stores.timezone` et l'identité client — sont livrées depuis 004.4.2 / `0011`. Périmètre
entièrement écrit : onze défauts tabulés (AN-01, AN-02, AN-05, AN-06, AN-14 à AN-19), JSON Schema de
rapport versionné, `ENGINE_VERSION` 0.2.0, **aucune migration**.

**Cette mission documentaire ferme d'abord la Definition of Done de 004.4**, qui exigeait
nommément « ADR identité/PII, ADR stockage objet, politique de rétention v0, **handoff** ; CI
verte » — le handoff est le présent document. **004.5 n'est pas commencée**, et rien de ce qui
précède ne l'entame.

### Premières actions recommandées pour la conversation suivante

1. Vérifier la baseline : `git status --short`, `git rev-parse HEAD` vs `origin/mission-004.4`.
2. Lire [`MERVIO_004_4_6_ACCEPTANCE.md`](MERVIO_004_4_6_ACCEPTANCE.md) §I, puis D-065 à D-071 dans
   [`DECISIONS.md`](DECISIONS.md).
3. Décider du sort de **D-070** : l'arbitrer, ou la reporter avec une échéance nommée. Toute
   implémentation exige une révision `0017` (contrainte `CHECK` de `0005_audit`) et une ratification
   documentaire **préalable**.
4. Ne pas toucher à la purge tenant sans avoir lu §3.10 et §5 : le statut, non le verrou, sérialise
   les étapes, et deux propriétés d'ordre ne sont protégées que par des tests.
5. Pour 004.5, se rappeler que toute modification du rapport impose une **version de contrat**
   incrémentée, des goldens régénérés **avec justification écrite**, et un contexte LLM adapté.
