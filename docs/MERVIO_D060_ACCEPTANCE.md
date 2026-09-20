# MERVIO D-060 — ACCEPTANCE & RATIFICATION

**Date :** 2026-09-20 · **Décision :** D-060 — Sonde du dispatcher et prise : plans robustes sous RLS
(révision `0012`)

Ce lot n'est pas une sous-mission numérotée : `docs/DECISIONS.md` le désigne comme
« durcissement du dispatcher (après ratification de 004.4.2) ». Il est ratifié ici selon la même
procédure documentaire que 004.3, 004.4.1 et 004.4.2.

> **Nomenclature.** Le label « D1 » qui figure dans le corps de D-060 désigne l'**option de conception
> retenue à l'intérieur de cette décision**, et non la première étape d'une séquence de missions
> « D1 → D2 ». Il n'existe aucun « D2 » dans le dépôt ; la présente ratification n'en crée aucun et
> n'ouvre aucune décision `D-061`.

## Status

**RATIFIED / CLOSED**

## Baseline

`673ca4f` — `docs(acceptance): ratify mission 004.4.2` (migrations `0001` à `0011` inchangées ;
`0012_dispatch_probe_plan` est la seule nouvelle révision).

## Final commit

`aed66b03afa7f61a7e9d554162fe1db378d045de` —
`perf(jobs): robust dispatcher probe and claim plans under RLS (D-060)`
(12 fichiers : 2 ajoutés, 10 modifiés ; +809 −18). Branche `mission-004.4`, identique à
`origin/mission-004.4` au moment de la ratification.

## CI evidence

| Champ | Valeur |
|---|---|
| Workflow | `ci` (`.github/workflows/ci.yml`) |
| Run | #12 |
| Run ID | `35452058203` |
| SHA | `aed66b03afa7f61a7e9d554162fe1db378d045de` |
| Result | `completed / success` — jobs `lint`, `test`, `security`, `docker` : `success` |

**Portée de la preuve.** Deux sources de nature différente se rejoignent dans ce document. Cette
distinction porte sur la **provenance**, pas sur la validité : rien ici ne met en doute la preuve
fournie par le projet.

*Vérifié indépendamment pendant cette passe documentaire, directement dans le dépôt :*

- branche `mission-004.4` ;
- HEAD `aed66b03afa7f61a7e9d554162fe1db378d045de` ;
- HEAD identique à `origin/mission-004.4` ;
- `git show --stat` du commit final : **12 fichiers**, **+809 −18**, dont **2 ajoutés** ;
- `MERVIO_EXPECTED_TESTS: "2277"` dans `.github/workflows/ci.yml` ;
- état de l'arbre de travail (propre avant l'édition de cette ratification) ;
- nature **strictement documentaire** des modifications non commitées en cours : seuls
  `docs/DECISIONS.md` et le présent fichier.

*Preuve historique fournie par le projet pour la ratification, reprise telle quelle et non
re-vérifiée ici :*

- run CI **#12**, identifiant `35452058203`, résultat `success` ;
- 2277 tests passés, 0 ignoré ;
- `lint` : succès ;
- `security` : succès ;
- build Docker : succès ;
- smoke Docker : succès ;
- migration montée / descente / remontée : succès.

Cette passe documentaire **n'a pas relancé la CI**, **n'a pas appelé l'API GitHub** et **n'a pas
inspecté les logs du run historique**. Aucun fichier de code, de test, de migration, de dépendance ni
de CI n'a été touché pour l'établir.

## Acceptance gates

| Gate | Résultat | Étape CI / preuve |
|---|---|---|
| Tests | ✅ 2277 passés | job `test` : suite complète, PostgreSQL 17.11 épinglé, `MERVIO_REQUIRE_DATABASE_TESTS=1` |
| 0 échec · 0 ignoré | ✅ | `scripts/ci_test_gate.py --expected-tests 2277` : compte exact, aucun échec, erreur, test ignoré ou xfail toléré |
| Lint | ✅ | job `lint` |
| Migrations (montée / descente / remontée) | ✅ | `scripts/ci_migrations.py --postgres-major 17` : même schéma après l'aller-retour `0012` |
| Security | ✅ | job `security` : `pip-audit --strict` sur les verrous, `gitleaks` sur l'historique complet |
| Docker build | ✅ | job `docker` : image de base épinglée, dépendances verrouillées |
| Docker smoke | ✅ | `scripts/container_smoke.py`, vrais conteneurs, nettoyage vérifié |

## Scope delivered

D-060 a durci **les plans** de la sonde du dispatcher et de la prise sous RLS PostgreSQL. La
sémantique de la file n'a pas changé.

| Fichier | Changement |
|---|---|
| `src/mervio/persistence/migrations/versions/0012_dispatch_probe_plan.py` | **nouvelle révision** : `ALTER POLICY jobs_service_dispatch` (contexte évalué une fois, sémantique identique) et reconstruction de `jobs_ready_idx` en `(organization_id, priority DESC, created_at, id, available_at) WHERE status = 'queued'` (même préfixe, donc même ordre de prise) |
| `src/mervio/persistence/dispatch.py` | `_READY_SQL` et `_EXPIRED_SQL` durcis : horloge évaluée une fois (`(SELECT COALESCE(%s::timestamptz, now()))`, motif InitPlan) ; contrat de la sonde documenté comme **test d'existence par organisation** |
| `src/mervio/persistence/jobs.py` | `_CLAIM_SQL` durci : même motif d'horloge InitPlan sur `available_at` |
| `tests/persistence/test_dispatch_probe_plan.py` | **nouveau module** (+644 lignes) : propriétés de plan sans nom d'index, prise sur base jamais analysée (100 000 et 1 000 000 de travaux), travaux différés en tête, invariance du travail choisi, équivalence des politiques dans tous les contextes, aller-retour exact de la révision |
| `tests/persistence/test_dispatcher.py` | l'assertion de `test_the_dispatch_probe_cost_does_not_grow_with_queue_depth` passe du **nom** d'index (`jobs_ready_idx`) à la **propriété de plan** (index ordonné, aucun bitmap, aucun tri de `jobs`), assortie d'une borne de blocs ; raison consignée dans le docstring ; **aucun test retiré** |
| `tests/persistence/test_persistence_migrations.py` | `0012` ajoutée à la chaîne des révisions attendues |
| `benchmarks/jobs_benchmark.py` | `ANALYZE` exécuté via l'URL de maintenance du propriétaire ; **aucun privilège ajouté** au rôle applicatif |
| `.github/workflows/ci.yml` | `MERVIO_EXPECTED_TESTS` : 2253 → 2277 |
| `README.md` | compte de tests : 2253 → 2277 |
| `docs/DECISIONS.md`, `docs/PERFORMANCE.md`, `benchmarks/README.md` | consignation de la décision, de la cause mesurée et de la correction du banc |

Ce que D-060 n'a **pas** touché, et qui reste vrai après ratification :

- la politique **RESTRICTIVE** `jobs_service_authorized`, frontière d'isolation, est inchangée ;
- aucune fonction, aucun rôle, aucun privilège, aucun `SECURITY DEFINER` ajouté ou modifié ;
- aucun réglage du planificateur ;
- contrat de prise inchangé : mêmes prédicats, `ORDER BY priority DESC, created_at ASC, id ASC`,
  `FOR UPDATE SKIP LOCKED`, `LIMIT 1` ; machine à états inchangée ; ordre des organisations toujours
  le tour de rôle par identifiant (D-050).

## Accepted residual risks

Acceptés et documentés, **non résolus** par la ratification :

| # | Constat |
|---|---|
| 1 | Le coût des travaux différés reste **linéaire**, mais il est désormais payé **dans l'index** concerné plutôt que par une lecture de table par travail. |
| 2 | `recover_stale` conserve sa limitation existante à `now()` : la reprise des baux expirés dans une organisation n'est pas couverte par le motif d'horloge InitPlan. |
| 3 | L'index de la file (`jobs_ready_idx`) **croît d'environ 14,6 %**. Le corps de D-060 en donnait une estimation *ex ante* de « +12 à 14 % » ; la valeur mesurée est celle retenue ici. |
| 4 | Le coût de mise en file augmente d'environ **5,6 %**. |
| 5 | En production, sur une table volumineuse en service, la reconstruction d'index doit passer par `CREATE INDEX CONCURRENTLY` sous un autre nom, `DROP INDEX CONCURRENTLY` de l'ancien, puis renommage — et non par la reconstruction en transaction de la révision, acceptable ici uniquement parce qu'aucune donnée marchande réelle n'est stockée. |
| 6 | Le test de stabilité à **20 tirages** (`test_the_ready_probe_plan_is_stable_whatever_the_physical_order`, `MERVIO_DISPATCH_PLAN_DRAWS`, 20 par défaut) est **probabiliste** : il échantillonne des ordres physiques aléatoires. Le comportement déterministe (travail choisi, ordre, `SKIP LOCKED`, équivalence des politiques, aller-retour de migration) est couvert par d'autres tests du même module. |
| 7 | Les résultats de benchmark **historiques** ont été mesurés **sans statistiques à jour** (l'`ANALYZE` du banc était ignoré sous le rôle applicatif) et **n'ont pas été régénérés**. Le fait est consigné dans `benchmarks/README.md` ; `docs/PERFORMANCE.md` conserve ces chiffres comme référence historique de 004.3.9. |

**Provenance des valeurs des points 3 et 4.** Ces valeurs proviennent de la campagne de validation
ayant accompagné la ratification. Elles ne sont **actuellement corroborées par aucun artefact du
dépôt** : `docs/PERFORMANCE.md` et `benchmarks/README.md` ne les portent pas, et le corps de D-060 ne
donne que l'estimation *ex ante* « +12 à 14 % ». Elles sont conservées telles quelles, sans
revendication de vérification indépendante ni de reproductibilité à partir du dépôt en l'état.

Le succès de la CI ne referme aucun de ces sept points.

## Out of scope

La ratification de D-060 **n'implique l'implémentation d'aucun** des éléments suivants, qui restent
entiers et non livrés :

- `ObjectStore` et ses pilotes (D-055) ;
- table `raw_objects` ;
- frontière d'import sans chemin / `raw_object_id` (D-054) ;
- stockage S3 (pilote comme hébergement) ;
- rétention, expiration `retain_until`, ramassage des objets orphelins ;
- validation IANA du fuseau ;
- correction de **F-01** (ordre non déterministe de `campaign_contributors`,
  `docs/MISSION_004_4_2_HARDENING.md`) ;
- effacement client, tombstones, purge d'organisation ou de boutique, destruction du sel ;
- toute mission ultérieure (004.4.3, 004.4.4, 004.4.5, 004.4.6, 004.5 et au-delà).

D-060 a livré **uniquement** le durcissement de plans décrit ci-dessus.

## Closure

D-060 est **formellement ratifiée et close** sur la base de son implémentation
(`aed66b03afa7f61a7e9d554162fe1db378d045de`) et des preuves de validation consignées ci-dessus :
2277 tests passés, 0 ignoré, lint, sécurité, build et smoke Docker, aller-retour de migration, run CI
#12 (`35452058203`) au vert.

## Final verdict

**RATIFIED / CLOSED**
