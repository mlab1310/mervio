# MERVIO 004.4.4 — ACCEPTANCE

**Date :** 2026-09-24 · **Mission :** 004.4.4 — Frontière d'import sans chemin : contrat `ObjectStore`,
objets bruts et rétention v0 (D-054, D-055, D-061)

## Status

**ACCEPTED WITH RESIDUALS**

Revue formelle sur le SHA figé, après trois régressions trouvées par la CI et corrigées avant
acceptation. Les résiduels listés en §9 sont explicitement acceptés par le périmètre et les
décisions ; aucun n'est une exigence non tenue.

## Baseline

`aed66b0` — `perf(jobs): robust dispatcher probe and claim plans under RLS (D-060)`, dernier commit
antérieur à 004.4.4 et dernière CI entièrement verte avant la mission (run #12).
`2f482c9` — `docs(d060): ratify dispatcher probe decision`, ratification documentaire de D-060, hors
périmètre de la présente mission.

## Final commit

`252daa2eb62625218a6851e08ad2dfb104421845` — `fix(container): prepare named volumes for image user`.
Branche `mission-004.4`, identique à `origin/mission-004.4` au moment de la revue ; arbre de travail
propre.

Périmètre complet de la mission (`2f482c9..252daa2`) : **58 fichiers, +2761 −139**.

| Catégorie | Fichiers |
|---|---|
| `tests/` | 26 (+1425 −76) |
| `src/` (code produit) | 15 (+1115 −48) |
| `docs/` + `README.md` | 7 (+103 −3) |
| Infrastructure (`Dockerfile`, `docker-compose.yml`, `ci.yml`, `pyproject.toml`, verrous, `benchmarks/`) | 7 |
| `scripts/` (outils CI) | 3 |

Commits de la mission, dans l'ordre :

| SHA | Objet |
|---|---|
| `5dc36c4` | `feat(storage)` — contrat `ObjectStore` et trois pilotes |
| `02561f6` | `feat(persistence)` — objets bruts par locataire et révision `0013` |
| `8c44d0c` | `feat(import)` — fermeture de la frontière d'import sur les identifiants d'objets |
| `0916281` | `docs(004.4.4)` — frontière d'import et contrat de rétention |
| `f700f38` | `test(container)` — le worker peut écrire le scratch dont l'import a besoin |
| `c61e5e0` | `fix(004.4.4)` — chemin local hostile et configuration du smoke (BUG #1, BUG #2) |
| `252daa2` | `fix(container)` — préparation des volumes nommés pour l'utilisateur de l'image (BUG #3) |

## CI evidence

| Champ | Valeur |
|---|---|
| Workflow | `ci` (`.github/workflows/ci.yml`) |
| Run | **#15** |
| Run ID | `36035708864` |
| SHA de tête | `252daa2eb62625218a6851e08ad2dfb104421845` |
| Conclusion | **`success`** — 4 jobs sur 4 |
| `lint` | ✅ `All checks passed!` (ruff `E9,F63,F7,F82`) + `compileall` sur `src tests scripts benchmarks` |
| `test` | ✅ 13 étapes sur 13 |
| `security` | ✅ gitleaks `no leaks found` (historique complet, binaire vérifié par SHA-256) ; `pip-audit` `No known vulnerabilities found` × 2 |
| `docker` | ✅ build + smoke conteneurisé complet + absence de résidu |

**Tests**

```
MERVIO_EXPECTED_TESTS: 2468
ci_test_gate: 2468 passed, 0 failed, 0 errors, 0 skipped (attendus: 2468)
ci_test_gate: porte franchie
```

La porte relit le rapport JUnit brut, exige l'égalité exacte du nombre, **zéro ignoré**, et
l'exécution réelle des modules obligatoires (isolation, RLS, migrations, file, audit, worker).

**Migrations**

```
ci_migrations: PostgreSQL 17 joignable
ci_migrations: upgrade head ok
ci_migrations: downgrade base ok
ci_migrations: second upgrade head ok
ci_migrations: porte franchie
```

Base neuve, rôle propriétaire non superutilisateur, empreinte de catalogue identique après descente
puis remontée.

**Licences et dépendances** — `ci_license_check: 32 distributions, porte franchie` ;
`pip check: No broken requirements found`.

**Docker — smoke 12 étapes sur 12**

```
container_smoke: image: mervio:ci (deja construite)
container_smoke: compose: fichier valide; postgres: demarrage et bootstrap
container_smoke: worker: refus d'un schema non migre (aucune migration implicite)
container_smoke: migrate: upgrade head (etape explicite)
container_smoke: worker: refus d'un superutilisateur; admin: refus d'un mauvais mot de passe
container_smoke: worker: demarrage, healthcheck, identite de service
container_smoke: demo: provisionnement, import, analyse, rapport, audit
container_smoke: isolation: autre organisation, RLS applicative et worker
container_smoke: worker: SIGTERM et arret propre
container_smoke: fuites: sorties et logs de tous les conteneurs
container_smoke: nettoyage: conteneurs, reseaux et volumes du projet
container_smoke: smoke test reussi
```

L'étape suivante du workflow, `No container, volume or network left behind`, est également passée :
aucun conteneur, volume ni réseau étiqueté du projet ne subsiste.

---

## 1. Scope

Mission 004.4 items **5** (abstraction de stockage objet) et **6** (politique de rétention v0), plus
la table `raw_objects` de l'item **2**, tels qu'arbitrés par :

- **D-054** — frontière d'import sûre : le worker ne reçoit jamais de chemin ;
- **D-055** — `ObjectStore` : trois pilotes, une suite de contrat, S3 testé avec `moto` ;
- **D-061** — signatures, confinement, matérialisation worker et intégrité en deux temps.

**Hors périmètre**, par décision : KMS réel et stockage S3 hébergé (004.9) ; jetons OAuth
(004.11–004.12) ; webhooks RGPD Shopify (004.12) ; effacement client (004.4.5) ; purge d'organisation
(004.4.6) ; enchaînement import → analyse (004.6, D-059).

## 2. Exigences

Chaque exigence porte : implémentation, preuve, tests associés, preuve CI, statut, résiduel.

### R1 — Interface `ObjectStore`, trois pilotes, une suite de contrat unique (D-055)

- **Implémentation** — `src/mervio/storage/` : `base.py` (interface, `PutResult`, `copy_and_digest`),
  `memory.py`, `filesystem.py`, `s3.py`. `boto3` importé à la demande dans `build_object_store`
  (`storage/__init__.py`), extra `[s3]`.
- **Preuve** — une seule fixture paramétrée pilote les trois :
  `@pytest.fixture(params=["memory", "filesystem", "s3"])` (`tests/test_object_store_contract.py`) ;
  le pilote S3 est exercé par `moto` **en processus**, sans compte AWS ni réseau.
- **Tests** — `test_object_store_contract.py` : 73.
- **Preuve CI** — job `test` vert ; porte de comptage exact franchie.
- **Statut** — **PASS**
- **Résiduel** — écarts `moto` / fournisseur réel : accepté, reporté à 004.9 (D-055).

### R2 — Confinement du pilote système de fichiers, quatre couches (D-061 Q4)

- **Implémentation** — `storage/filesystem.py` : validation de la clé par expression régulière ;
  `Path.resolve()` puis contrôle d'appartenance à la racine résolue ; refus explicite d'un lien
  symbolique sur la cible ; permissions 0700 / 0600 indépendantes de l'umask.
- **Preuve** — les quatre couches sont énoncées dans le docstring du module et exercées séparément.
- **Tests** — `test_object_store_traversal.py` : 18 (clés hostiles, lien sur la cible, parent lié
  sortant, parent lié restant confiné, racine absolue, permissions, répertoire à la place d'un objet).
- **Preuve CI** — job `test` vert.
- **Statut** — **PASS**
- **Résiduel** — TOCTOU et absence d'`O_NOFOLLOW` : assumés et documentés (D-061 Q4).

### R3 — Clés générées côté serveur

- **Implémentation** — `build_object_key(org, store)` → `org/<org_id>/store/<store_id>/raw/<uuid4>`
  (`storage/base.py`). L'appelant ne contrôle aucun segment.
- **Preuve** — `upload_object` construit la clé depuis `session.organization_id` et le `store_id`
  validé, jamais depuis une entrée.
- **Tests** — `test_object_store_contract.py`, `test_object_store_traversal.py`.
- **Preuve CI** — job `test` vert.
- **Statut** — **PASS** — aucun résiduel.

### R4 — Table `raw_objects`, RLS, révision `0013`

- **Implémentation** — `0013_raw_objects.py` : `ENABLE` + `FORCE ROW LEVEL SECURITY` ;
  `retain_until timestamptz NOT NULL` ; `UNIQUE (organization_id, store_id, id)` et
  `UNIQUE (organization_id, object_key)` ; domaine d'état `pending → available → purging → purged`.
- **Preuve** — `GRANT SELECT, INSERT` **seulement** : ni `UPDATE` ni `DELETE`, conforme à la politique
  de rétention v0 et prérequis de 004.4.5 / 004.4.6.
- **Tests** — `test_migration_0013.py` : 7 ; `test_raw_objects.py` : 29.
- **Preuve CI** — porte migrations franchie (montée, descente, remontée, empreinte identique).
- **Statut** — **PASS** — aucun résiduel.

### R5 — Vocabulaire d'audit `object.uploaded` / `raw_object` (D-061 Q10)

- **Implémentation** — ajout aux listes `CHECK` d'`audit_events` par le mécanisme `_replace_audit`
  établi par `0008`, avec restauration exacte à la descente.
- **Preuve** — la porte migrations exige un schéma identique après `downgrade base` puis
  `upgrade head` : la restauration est prouvée, non déclarée.
- **Tests** — `test_migration_0013.py`.
- **Preuve CI** — `ci_migrations: porte franchie`.
- **Statut** — **PASS** — aucun résiduel.

### R6 — Frontière d'import : aucun chemin ne survit jusqu'au worker (D-054)

- **Implémentation** — `validate_payload` refuse toute **clé** et toute **valeur** de chemin,
  récursivement et à travers les listes (`persistence/jobs.py`, `_reject_paths`).
- **Preuve** — les drapeaux de source de `enqueue-import` sont désormais `type=_uuid` dans le
  parseur (`cli/admin.py`) : un chemin ne peut plus être saisi.
- **Tests** — `test_import_boundary.py` : 17 ; `test_a_path_can_never_be_passed_as_a_raw_object`
  (3 paramètres) dans `test_admin_operations.py`.
- **Preuve CI** — job `test` vert ; smoke conteneur 12/12.
- **Statut** — **PASS** — aucun résiduel.

### R7 — `object upload` puis `enqueue-import`, un identifiant par source

- **Implémentation** — `mervio admin object upload --store --kind --file` lit le fichier local **en
  flux** (mémoire bornée) ; `enqueue-import` ne reçoit que des UUID, **un par source** (roadmap 004.6
  l. 588), ce qui préserve les instantanés multi-sources.
- **Preuve** — le smoke exige quatre `object.uploaded` **avant** `job.enqueued`
  (`EXPECTED_ADMIN_ACTIONS`, `scripts/container_smoke.py`).
- **Tests** — `test_admin_operations.py` : 69 ; `test_cli_admin.py` : 88.
- **Preuve CI** — `container_smoke: demo: provisionnement, import, analyse, rapport, audit` ✅.
- **Statut** — **PASS** — aucun résiduel.

### R8 — Résolution de l'objet sous `TenantSession` (RLS)

- **Implémentation** — l'objet est résolu sous session locataire ; un objet d'une autre organisation
  et un objet inexistant échouent **à l'identique** (aucune fuite d'existence).
- **Preuve** — la ligne `raw_objects` sous RLS, et non la clé ni le magasin, porte la frontière
  locataire (`docs/DATA_MODEL.md`).
- **Tests** — `test_import_boundary.py`, `test_raw_objects.py`.
- **Preuve CI** — job `test` vert ; `container_smoke: isolation: autre organisation, RLS applicative
  et worker` ✅.
- **Statut** — **PASS** — aucun résiduel.

### R9 — Matérialisation par le worker, pile d'ingestion inchangée (D-061 Q1)

- **Implémentation** — le worker écrit les octets résolus dans un temporaire qu'il crée lui-même
  (0600, supprimé en `finally`), puis appelle la pile d'ingestion **inchangée**
  (`workers/handlers.py`).
- **Preuve** — le diff de la mission ne touche aucun fichier de `ingestion/`, `analytics/` ni
  `persistence/codec.py`. Le chemin fabriqué n'apparaît ni en charge utile, ni en audit, ni en log.
- **Tests** — `test_jobs_worker.py`, `test_worker_dispatched.py` ; `test_container_artifacts.py`
  prouve que le tmpfs `/tmp` du worker est inscriptible par l'utilisateur de l'image.
- **Preuve CI** — smoke 12/12 : import réel réussi dans un conteneur `read_only`.
- **Statut** — **PASS** — aucun résiduel. La distinction « chemin d'appelant interdit / chemin
  fabriqué par le worker autorisé » est un arbitrage explicite de D-061, non une dérogation.

### R10 — Intégrité en deux temps N1 + N2 (D-061 Q2)

- **Implémentation** — **N1** : après matérialisation, `sha256(temporaire)` est confronté à
  `raw_objects.sha256` ; une divergence est **permanente** (`object_checksum_mismatch`) et **aucun
  parseur n'est appelé** (`workers/handlers.py`). **N2** : la garde `source_changed_during_import` de
  `persisted_analysis.py` est conservée telle quelle.
- **Preuve** — aucun `UPDATE` n'est accordé sur `raw_objects` (R4) : la valeur de référence est
  inaltérable après insertion.
- **Tests** — `test_import_boundary.py`, `test_jobs_worker.py`.
- **Preuve CI** — job `test` vert.
- **Statut** — **PASS** — aucun résiduel.

### R11 — Contrat `ObjectStore` : `put` / `open`, sans garantie de non-écrasement

- **Implémentation** — `put(key, source) -> PutResult(sha256, byte_size)` et `open(key)` contextuel.
  Pas d'`ObjectAlreadyExists`, ni `stat()`, ni `delete()`.
- **Preuve** — l'unicité est portée par les clés `uuid4` **et** par
  `UNIQUE (organization_id, object_key)` en base.
- **Tests** — `test_object_store_contract.py` : 73, identiques pour les trois pilotes.
- **Preuve CI** — job `test` vert.
- **Statut** — **PASS**
- **Résiduel** — absence de garantie de non-écrasement : accepté et motivé par D-061. Sur S3 la seule
  garantie réelle serait une écriture conditionnelle, que D-055 reporte à 004.9. Retirer la propriété
  des trois pilotes préserve l'exigence centrale de D-055 — une interface identique partout — plutôt
  que de déclarer tenue une garantie qui ne le serait pas.

### R12 — La CLI analytique locale garde ses chemins

- **Implémentation** — `python -m mervio.analytics` inchangé ; il ne passe ni par la file ni par le
  worker.
- **Preuve** — aucun fichier `analytics/` ni `ingestion/` dans le diff de la mission.
- **Tests** — `test_inspector.py`, `test_cli_admin.py` (`inspect --file`), suite analytique complète.
- **Preuve CI** — job `test` vert.
- **Statut** — **PASS** — aucun résiduel.

### R13 — Politique de rétention v0 (item 6)

- **Implémentation** — `docs/RETENTION_POLICY_V0.md` ; `retain_until NOT NULL` calculé à l'insertion
  depuis `RetentionPolicy.raw_objects_days`, constante Python testable comme les autres durées du
  dépôt.
- **Preuve** — le document énonce lui-même, en toutes lettres, ce qui n'est **pas** fait.
- **Tests** — `test_raw_objects.py` ; `workers/retention.py` et ses tests.
- **Preuve CI** — job `test` vert.
- **Statut** — **PASS**. Le livrable de l'item 6 est un **document de contrat**, pas une exécution.
- **Résiduel** — aucune expiration n'est exécutée, rien ne lit `retain_until` ; les octets orphelins
  après un dépôt interrompu subsistent, illisibles faute de ligne `available`. Acceptés et reportés à
  004.9 par D-054 et par le document. Les durées marquées ⚖️ restent à valider juridiquement.

### R14 — Extra `[s3]`, verrou et porte de licences

- **Implémentation** — extra `[s3]` ; `moto` en dépendance de test ; `certifi` (MPL-2.0) inscrit dans
  `REVIEWED` de `scripts/ci_license_check.py`, avec `ALIASES`/`REVIEWED` pour `s3transfer`,
  `python-dateutil` et `cffi`.
- **Preuve** — aucune modification de la politique `PERMISSIVE` : le mécanisme de revue est celui déjà
  employé pour `psycopg` (LGPL-3.0, ADR-004.1-001).
- **Tests** — `test_requirements_lock.py` : 36 ; `test_ci_scripts.py` : 52.
- **Preuve CI** — `ci_license_check: 32 distributions, porte franchie`.
- **Statut** — **PASS**
- **Résiduel** — `certifi` reste MPL-2.0 dans le verrou de **test** ⚖️ : accepté, motivé par écrit
  (dépendance de test uniquement, absente de `requirements-runtime.lock` et de l'image).

### R15 — Conteneur : volume d'objets partagé, worker en lecture seule

- **Implémentation** — volume nommé `mervio_objects` ; `admin` en lecture-écriture, `worker` en
  **`:ro`** ; répertoire préparé dans l'image pour l'utilisateur 10001 (`Dockerfile`).
- **Preuve** — le smoke a été migré vers « upload puis mise en file » **sans assouplissement** :
  aucune exception, aucun chemin réintroduit.
- **Tests** — `test_container_artifacts.py` : 84 (dont le test générique des volumes nommés) ;
  `test_container_smoke.py` : 63.
- **Preuve CI** — job `docker` ✅ : build + smoke 12/12 + aucun résidu.
- **Statut** — **PASS** — aucun résiduel.

## 3. Régressions trouvées par la CI et corrigées

Les trois défauts ci-dessous ont été introduits par 004.4.4 et détectés par la CI avant acceptation.
Aucun n'a exigé d'assouplir un contrat : le correctif a chaque fois porté sur l'appelant ou sur
l'image, jamais sur `storage/` ni sur `settings.py`.

### BUG #1 — Traversée de chemin local au dépôt d'objet

- **Symptôme (CI #13, `f700f38`)** — `test_a_hostile_local_path_is_refused_at_upload[/srv/../etc/passwd]`
  → `KeyError: 'code'`. Sur Linux, `/srv` existe : `/srv/../etc/passwd` désigne `/etc/passwd`, que
  `is_file()` acceptait. Le dépôt aboutissait et le test recevait un payload de succès. Invisible en
  local, où `/srv` n'existe pas sur macOS.
- **Correction** (`c61e5e0`) — `validate_local_source` délègue le jugement de **forme** à
  `validate_source_path`, règle déjà présente dans le module : absolue, normalisée, sans `..`, sans
  caractère de contrôle, longueur bornée. `is_file()` ne juge plus que la lisibilité, **après** la
  règle. `expanduser()` retiré : `~` ne passe jamais `os.path.isabs`.
- **Preuve de non-régression** — comparaison différentielle sur 52 entrées contre la fonction
  d'origine reconstruite : **0 régression**, 2 durcissements (`/usr/../etc/passwd`, `/../etc/passwd`),
  et suppression d'un chemin de crash de l'ancienne version (`~root/x` levait une `PermissionError`
  nue au lieu d'un refus).
- **Tests** — 20 cas, dont 10 prouvant que le refus **n'accède pas** au système de fichiers
  (`Path.is_file` remplacé par un échec) et un cas `existing_dir/../target` dont la cible existe,
  créée par le test, sans aucun fichier système.
- **Statut** — **PASS** (CI #15, job `test` vert).

### BUG #2 — `MERVIO_OBJECT_STORE_ROOT` absent des workers ad-hoc du smoke

- **Symptôme (CI #13)** — étape 5/12 : `worker.identity_refused` attendu, absent. 004.4.4 rend le
  magasin obligatoire dès que `import` figure dans `JOB_TYPES`, ce qui est le défaut ; les workers
  ad-hoc du smoke, lancés hors compose, mouraient en `worker.config_invalid` — avec le **même code de
  sortie 2**, ce qui masquait la substitution jusqu'au second assert.
- **Correction** (`c61e5e0`) — la variable est transmise aux **deux** workers ad-hoc, sur la racine
  même du compose. Le contrat n'a pas été assoupli : `settings.py` est intact.
- **Tests** — 5, dont un qui rejoue `WorkerSettings.from_env` sur l'environnement reconstruit du
  conteneur et vérifie qu'en retirant la variable la seule plainte est `MERVIO_OBJECT_STORE_ROOT`.
- **Statut** — **PASS** (CI #15 : `worker: refus d'un superutilisateur…` ✅, puis `worker.ready` et
  `healthy`).

### BUG #3 — Volume nommé `/var/lib/mervio/objects` non préparé pour l'UID 10001

- **Symptôme (CI #14, `c61e5e0`)** — étape 7/12 : `demo provision` →
  `{'code': 'PermissionError', 'message': 'erreur interne'}`. Docker donne au point de montage d'un
  volume nommé la propriété du répertoire homonyme **de l'image** ; absent du `install -d`, il le
  créait `root:root`, et `mkdir .../objects/org` échouait en `EACCES`. `mervio_data` fonctionnait
  parce que le `Dockerfile` le créait, lui : l'asymétrie était invisible car le montage était vérifié
  et sa viabilité non.
- **Correction** (`252daa2`) — un chemin ajouté à la ligne `install -d` existante,
  `10001:10001 / 0750`, identique à ses voisins. **Aucun code produit** :
  `FilesystemObjectStore` se comportait comme spécifié ; le corriger là aurait masqué une erreur de
  déploiement.
- **Tests** — 1 test générique qui **parcourt** les montages de volumes nommés du compose au lieu d'en
  lister les noms. Il exige que le chemin figure dans les répertoires créés par le `Dockerfile`, qu'il
  appartienne à `10001:10001` et qu'il soit inscriptible ; il refuse aussi bien un répertoire absent
  qu'un répertoire présent mais mal possédé, et un garde interdit qu'il devienne vide. Tout volume
  nommé ajouté plus tard passe par la même règle.
- **Statut** — **PASS** (CI #15 : étape 7 ✅, puis les cinq étapes suivantes, jamais exécutées
  auparavant sur 004.4.4).

## 4. Security evidence

| Contrôle | Résultat |
|---|---|
| gitleaks (historique complet, binaire vérifié par SHA-256) | ✅ `no leaks found` |
| `pip-audit` `requirements.lock` | ✅ `No known vulnerabilities found` |
| `pip-audit` `requirements-build.lock` | ✅ `No known vulnerabilities found` |
| Porte de licences | ✅ `32 distributions, porte franchie` |
| `pip check` | ✅ `No broken requirements found` |
| Élévation de privilège introduite par les correctifs | Aucune. Le correctif BUG #3 **réduit** la surface : `root:root 0755` devient `10001:10001 0750` |
| `GRANT` / `SECURITY DEFINER` introduits par les correctifs | Aucun. Le `GRANT SELECT, INSERT` de `0013` est un livrable prévu, sans `UPDATE` ni `DELETE` |
| RLS modifiée par les correctifs | Aucune — `src/mervio/persistence/` intact sur `f700f38..252daa2` |

Frontières vérifiées : `validate_payload` refuse clés et valeurs de chemin récursivement (R6) ;
confinement filesystem en quatre couches (R2) ; résolution de l'objet sous RLS avec échec identique
pour un objet absent et pour un objet d'un autre locataire (R8) ; intégrité N1 contre une valeur
figée à l'insertion (R10).

## 5. Migration evidence

Révision **`0013_raw_objects`**, seule nouvelle révision de la mission.

- `ENABLE` + `FORCE ROW LEVEL SECURITY` sur `raw_objects` ;
- clés composites `UNIQUE (organization_id, store_id, id)` et `UNIQUE (organization_id, object_key)` ;
- `retain_until timestamptz NOT NULL` ;
- vocabulaire d'audit `object.uploaded` / `raw_object` ajouté par `_replace`, restauré exactement à la
  descente ;
- privilèges : `GRANT SELECT, INSERT` au rôle applicatif, rien d'autre.

Preuve CI : montée, descente complète et seconde montée sur base neuve, avec empreinte de catalogue
identique (colonnes, contraintes, index, déclencheurs, politiques RLS, fonctions, droits, RLS forcée).

## 6. Test evidence

**2468** tests, **0 échec, 0 erreur, 0 ignoré**, porte de comptage exact franchie sur le SHA final.

Couverture spécifique à la mission :

| Fichier | Tests |
|---|---|
| `tests/test_cli_admin.py` | 88 |
| `tests/test_container_artifacts.py` | 84 |
| `tests/test_object_store_contract.py` | 73 |
| `tests/persistence/test_admin_operations.py` | 69 |
| `tests/test_container_smoke.py` | 63 |
| `tests/persistence/test_raw_objects.py` | 29 |
| `tests/test_object_store_traversal.py` | 18 |
| `tests/persistence/test_import_boundary.py` | 17 |
| `tests/persistence/test_migration_0013.py` | 7 |

Le compteur est passé de 2450 à **2468** au fil de la mission : 2451 (`f700f38`), 2467 (`c61e5e0`),
2468 (`252daa2`). Chaque valeur a été mesurée avant d'être inscrite.

## 7. Docker evidence

- `docker build --tag mervio:ci .` — ✅ image construite à partir d'une base épinglée par digest et de
  dépendances verrouillées.
- `python scripts/container_smoke.py --image mervio:ci --skip-build` — ✅ **12 étapes sur 12**, jusqu'à
  `smoke test reussi` (liste complète en tête de document).
- `No container, volume or network left behind` — ✅ aucun conteneur, volume ni réseau étiqueté du
  projet ne subsiste après exécution.

Les cinq étapes jamais atteintes avant CI #15 — isolation RLS applicative et worker, arrêt SIGTERM,
contrôle des fuites sur les sorties et logs de tous les conteneurs, nettoyage, verdict final — sont
désormais exécutées et vertes.

Le smoke conteneur **n'a pas pu être exécuté localement** : aucun runtime conteneur n'est disponible
sur la machine de développement. La validation bout en bout du chemin conteneurisé repose donc
exclusivement sur le job `docker` de la CI.

## 8. Historique CI de la mission

| Run | SHA | `lint` | `test` | `security` | `docker` | Smoke |
|---|---|---|---|---|---|---|
| #12 | `aed66b0` | ✅ | ✅ | ✅ | ✅ | 12/12 (antérieur à 004.4.4) |
| #13 | `f700f38` | ✅ | ❌ | ✅ | ❌ | 4/12 |
| #14 | `c61e5e0` | ✅ | ✅ | ✅ | ❌ | 6/12 |
| **#15** | **`252daa2`** | ✅ | ✅ | ✅ | ✅ | **12/12** |

## 9. Résiduels acceptés

Prévus et motivés par le périmètre ou une décision. Ce ne sont pas des exigences non tenues.

| # | Résiduel | Origine | Traitement |
|---|---|---|---|
| 1 | Objets orphelins après un dépôt interrompu | D-054, D-061 | Ramassage en **004.9** |
| 2 | Aucune expiration exécutée ; rien ne lit `retain_until` | `RETENTION_POLICY_V0.md` | Expiration en **004.9** |
| 3 | `put` sans garantie de non-écrasement ; pas d'`ObjectAlreadyExists`, ni `stat()`, ni `delete()` | D-055, D-061 | Écriture conditionnelle en **004.9** |
| 4 | TOCTOU du pilote filesystem ; `O_NOFOLLOW` non introduit | D-061 Q4 | Assumé et documenté |
| 5 | Écarts de comportement entre `moto` et le fournisseur S3 réel | D-055 | Validation contre le fournisseur en **004.9** |
| 6 | `certifi` MPL-2.0 dans le verrou de test ⚖️ | D-061 | Motivé par écrit ; dépendance de test, absente de l'image |
| 7 | Durées de rétention marquées ⚖️ | Roadmap §11, §13 | Validation juridique en 004.9 |
| 8 | Lien symbolique local suivi au dépôt | Arbitré dans D-061 le 24/09/2026 | Comportement conservé ; dette future si une politique plus stricte est souhaitée |

Sur le point 8 : la CLI accepte un chemin local dont la **forme** est canonique même lorsqu'il désigne
un lien symbolique. Cela porte exclusivement sur la sélection d'un fichier local par l'opérateur, lue
sous son identité — il peut de toute façon nommer directement n'importe lequel de ses fichiers. Ce
n'est pas une brèche du confinement `ObjectStore` côté serveur, qui porte sur la clé d'objet générée
côté serveur et refuse explicitement un lien sur la cible. La frontière import/travail reste entière :
aucune référence de fichier local ne la franchit, et le worker ne reçoit qu'un `raw_object_id`.

## 10. Hors périmètre

KMS réel et stockage S3 hébergé (004.9) ; jetons OAuth (004.11–004.12) ; webhooks RGPD Shopify
(004.12) ; effacement client (004.4.5) ; purge d'organisation (004.4.6) ; enchaînement import →
analyse (004.6, D-059).

**Dette future** — la machine à états `pending → available → purging → purged` n'existe qu'en domaine
de contrainte : 004.4.4 ne produit que `available`.

## 11. Verdict

**ACCEPTED WITH RESIDUALS**

Les quinze exigences sont **PASS**, sans **PARTIAL** ni **FAIL**. Les huit résiduels sont acceptés par
le périmètre et les décisions ; aucun ne constitue une exigence non tenue. Les trois régressions
introduites par la mission ont été trouvées par la CI, corrigées sans assouplir aucun contrat, et
couvertes par des tests qui échouent sur le défaut d'origine.

| | |
|---|---|
| SHA final | `252daa2eb62625218a6851e08ad2dfb104421845` |
| Branche | `mission-004.4`, identique à `origin/mission-004.4` |
| CI | Run #15, `36035708864`, `success`, 4 jobs sur 4 |
| Tests | 2468 passés, 0 échec, 0 erreur, 0 ignoré |
| Migrations | montée / descente / remontée, empreinte identique |
| Docker | build ✅, smoke 12/12 ✅, aucun résidu ✅ |

## 12. Actions suivantes

1. **Porter les résiduels 1, 2, 3 et 5 dans le périmètre de 004.9**, afin qu'ils y soient repris
   explicitement plutôt que redécouverts. Ils sont aujourd'hui traçables depuis D-054, D-055, D-061 et
   `RETENTION_POLICY_V0.md`.
2. **Prérequis 004.4.5 et 004.4.6** : `raw_objects` n'accorde ni `UPDATE` ni `DELETE`. L'effacement
   client et la purge d'organisation devront introduire ces privilèges par migration, sous fonction
   dédiée, conformément à D-052 et D-056.
3. **Étendre la discipline de vérification des montages** : le test générique des volumes nommés
   couvre désormais la propriété des points de montage. Tout nouveau mécanisme de montage introduit
   ultérieurement mérite son pendant, comme les `tmpfs` et les volumes nommés ont le leur.
