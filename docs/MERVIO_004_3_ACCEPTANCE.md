# Mission 004.3 — Final Acceptance

**Date:** 2026-09-17 · **Branch:** `mission-004.3` · **HEAD:** `312c6f2` (= `origin/mission-004.3`)
**Reviewer:** Ingénieur principal
**Révisions :** acceptance initiale (READ-ONLY) ; **004.3.11 — nettoyage documentaire** appliqué (docs uniquement ; aucun changement `src/`, SQL, migration, test ni CI).

---

## 1. Executive Summary

La mission 004.3 (Platform Baseline & Worker Runtime) est **fonctionnellement, opérationnellement et
sécuritairement complète**. Les onze sous-missions 004.3.0 → 004.3.10 sont présentes dans l'historique Git,
chacune avec code et tests ; la suite complète passe **2138 / 2138, 0 échec, 0 ignoré** localement sur
PostgreSQL 17.11 (identique à la CI), garde anti-faux-vert active. La correction de sécurité 0009
(anti-ombrage `pg_temp`) est vérifiée en base : fonctions qualifiées `public.`, SECURITY DEFINER correcte,
EXECUTE au seul `mervio_worker`, RLS forcée sur les 8 tables de tenant, migration réversible à l'octet près.

**Écarts documentaires — CORRIGÉS en 004.3.11 :** les trois écarts LOW identifiés à l'acceptance initiale
sont résolus — (1) `docs/ROADMAP.md` et `docs/TODO.md` remplacés par un renvoi vers `MERVIO_FINAL_ROADMAP.md`
(item de périmètre 9) ; (2) README mis à jour à 2138 tests ; (3) décision dispatcher répercutée
(`DECISIONS.md` D-050 : RLS + identité de service adoptés, option `mervio_dispatcher` SECURITY DEFINER
marquée superseded ; historique 004.2 intact).

**Réserve restante (non bloquante) :** des risques de sécurité résiduels de même classe que 0009 (sévérité
moindre, intra-locataire, **non atteignables** dans le déploiement Docker) restent **documentés et non
corrigés** — leur fermeture est un correctif **code** de suivi, hors du périmètre strictement documentaire
de 004.3.11.

**Décision : RATIFIED WITH DOCUMENTED RESIDUAL RISKS** (voir §10).

---

## 2. Mission-by-Mission Verification

| Mission | Commit | Implementation | Tests | Documentation | Status |
|---|---|---|---|---|---|
| 004.3.0 docs baseline | `860a660` | `docs/MERVIO_FULL_AUDIT.md`, `MERVIO_FINAL_ROADMAP.md` commités | n/a | ✅ | ✅ |
| 004.3.1 CI gates | `a73038b` | `.github/workflows/ci.yml`, `scripts/ci_*` (test gate, migrations round-trip, license) | `test_ci_workflow`, `test_ci_scripts`, `test_ci_migrations` | ✅ | ✅ |
| 004.3.2 lease + fencing | `dc52494` | migration `0006_job_leases`, jeton `(attempts, locked_by)` | `test_jobs_lease`, `test_jobs_concurrency` | ✅ (docstring 0006) | ✅ |
| 004.3.3 service identity + dispatcher | `e2c72af` | migration `0007_service_identity`, `persistence/service.py`, `dispatch.py` | `test_service_identity`, `test_dispatcher`, `test_worker_dispatched` | ✅ (0007 docstring) | ✅ (voir §9 déviation dispatcher) |
| 004.3.4 settings/logging/health | `973ccc1` | `settings.py`, `observability/logging.py`, `redaction.py`, `health.py` | `test_settings`, `test_redaction`, `test_logging_contract`, `test_health` | ✅ | ✅ |
| 004.3.5 real worker process | `d5b4610` | `workers/runtime.py`, `lifecycle.py`, `lease.py`, `handlers.py` | `test_worker_runtime`, `test_worker_lifecycle`, `test_worker_lease_keeper` | ✅ | ✅ |
| 004.3.6 CLI worker/healthcheck | `448c391` | `cli/worker.py` (`mervio worker`, `worker healthcheck`) | `test_cli_worker`, `test_cli_worker_process`, `test_worker_health` | ✅ | ✅ |
| 004.3.7 administration | `4724f57` | migration `0008_admin_audit`, `admin/operations.py`, `cli/admin.py` | `test_cli_admin`, `test_admin_operations`, `test_admin_migration` | ✅ `docs/ADMIN_CLI.md` | ✅ |
| 004.3.8 container | `ec7ce24`, `d3f40e2` | `Dockerfile` (non-root 10001, HEALTHCHECK), `docker-compose.yml`, `docker/postgres/10-mervio-bootstrap.sql`, `scripts/container_smoke.py` | `test_container_artifacts`, `test_container_smoke`, `test_container_bootstrap` | ✅ `docs/CONTAINER.md` | ✅ |
| 004.3.9 measurements | `62d0f59` | `benchmarks/*`, `benchmarks/results/performance_20260917_arm64.json` | `test_benchmark_harness`, `test_benchmark_suite` | ✅ `docs/PERFORMANCE.md` | ✅ |
| 004.3.10 security/documentation | `312c6f2` | migration `0009_qualify_security_functions`, `DECISIONS.md` D-049 | `test_pg_temp_shadowing` (4), migration-chain updated | ✅ | ✅ |
| 004.3.11 acceptance | (ce document) | revue READ-ONLY + rapport | n/a | ce fichier | ✅ |

Aucune fonctionnalité « annoncée mais inexistante » n'a été trouvée. Aucun TODO critique masqué dans le
code de 004.3. Un `docs/TODO.md` obsolète subsiste (voir §8/§9), mais son contenu est hors-périmètre 004.3
(LLM/PDF/OAuth) et n'engage aucun code livré.

---

## 3. Test Results

**Commande exacte :**
```bash
MERVIO_TEST_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres \
MERVIO_REQUIRE_DATABASE_TESTS=1 \
.venv/bin/python -m pytest -p no:cacheprovider -rsx -q
```

| Métrique | Valeur | Preuve |
|---|---|---|
| Collectés | **2138** | `--collect-only` (somme par fichier) = 2138 = `MERVIO_EXPECTED_TESTS` |
| Passed | **2138** | exit code 0 |
| Failed | **0** | exit code 0 |
| Skipped | **0** | `tests/pytest_guard.py` force `TESTS_FAILED` si un seul test est ignoré sous `MERVIO_REQUIRE_DATABASE_TESTS=1` ; exit 0 ⇒ 0 ignoré |
| xfailed | **0** | idem (la garde couvre `xfail`) |

- Environnement : PostgreSQL 17.11 (Homebrew, arm64) — **identique à la version CI** (`postgres:17.11-bookworm`).
- Skips « possibles » (fixtures synthétiques absentes, `TEMPORARY` retiré, pilote absent) sont tous
  **conditionnels et légitimes** ; aucun ne s'est déclenché ici (garde satisfaite). Aucun `@skip`/`@xfail`
  inconditionnel n'existe dans la suite (seuls des exemples de test *dans* `test_pytest_guard.py`).
- Aucun test critique désactivé silencieusement : la garde anti-faux-vert l'interdit par construction.

---

## 4. CI Verification

| Élément | État | Source |
|---|---|---|
| Commit | `312c6f2` | HEAD = `origin/mission-004.3`, arbre propre (vérifié) |
| Workflow jobs | `lint`, `test`, `security`, `docker` | `.github/workflows/ci.yml` |
| lint | ✅ (rapporté) | job `lint` (ubuntu-24.04) |
| test | ✅ (rapporté) | job `test` : Postgres 17.11, `MERVIO_REQUIRE_DATABASE_TESTS=1`, `ci_license_check`, `ci_migrations --postgres-major 17`, `ci_test_gate --expected-tests 2138` |
| security | ✅ (rapporté) | job `security` : gitleaks (historique complet, binaire vérifié) |
| docker | ✅ (rapporté) | job `docker` : `container_smoke.py --image mervio:ci` |
| overall | **SUCCESS** (6m27, rapporté par l'utilisateur) | GitHub Actions |

**Réserve de méthode :** je n'ai pas ré-exécuté GitHub Actions moi-même. Conformément à la règle « ne pas
prétendre que la CI est verte sans exécution réelle », j'établis ici uniquement : (a) `312c6f2` **est** le
HEAD versionné ; (b) `MERVIO_EXPECTED_TESTS=2138` correspond exactement à la collecte locale ; (c) la
**même suite** passe localement 2138/2138/0 sur la **même** version PostgreSQL ; (d) `ci_migrations.py`
round-trip passe localement. Le verdict GREEN pour `312c6f2` est **rapporté par l'utilisateur**, cohérent
avec ces vérifications.

---

## 5. Security Acceptance

**Correction 0009 (`0009_qualify_security_functions`) — vérifiée en base sur une base fraîche montée à head :**

| Contrôle | Résultat |
|---|---|
| `pg_temp` shadowing | Sémantique confirmée : `pg_temp` implicitement premier si non listé. 9 fonctions de sécurité qualifiées `public.` → **0 référence de table non qualifiée** (vérifié sur `prosrc`). |
| public-qualified relations | `app_current_service_id`, `app_service_authorized`, `app_service_organizations`, `app_delegate_rank`, `app_ensure_service_principal`, + 4 gardes trigger : toutes qualifiées. |
| SECURITY DEFINER | `app_ensure_service_principal` : `prosecdef=true`, `config = search_path=pg_catalog, public, pg_temp`. |
| EXECUTE privileges | `mervio_worker:EXECUTE` (+ propriétaire) ; `has_function_privilege('public', …, 'EXECUTE') = false`. |
| owner | propriétaire = rôle de migration (le définisseur), inchangé par `CREATE OR REPLACE`. |
| migration downgrade | Restaure les corps 0007 **à l'octet près** (générés depuis `prosrc`) ; `ci_migrations.py` round-trip (upgrade→downgrade→upgrade) : empreintes identiques. |
| regression tests | `test_pg_temp_shadowing.py` (4) : mécanisme, absence de lecture inter-locataire, non-usurpation du principal (définisseur), non-contournement de la garde d'appartenance. |

**RLS / rôles :**
- RLS **activée ET forcée** (`relrowsecurity` ∧ `relforcerowsecurity`) sur `organizations`, `memberships`,
  `stores`, `connections`, `data_snapshots`, `jobs`, `audit_events`, `service_authorizations`.
- Rôles de groupe `mervio_worker` et `mervio_app` : `NOLOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE`.
- `Database._verify` refuse toute connexion applicative superutilisateur ou BYPASSRLS et impose UTF8.

**Risques résiduels de sécurité — DOCUMENTÉS, NON CORRIGÉS (vérifiés encore présents, pas prétendus fixés) :**
- `0003_analysis_reports.py:67` `FROM data_snapshots s` (non qualifié) — garde d'instantané scellé.
- `0002_snapshots.py:272` `JOIN data_snapshots s` (non qualifié) — garde de scellé.
- `0007_service_identity.py:123` sous-requête `FROM jobs j` **inline** dans la politique `audit_events_service_actor`.

Même classe que 0009, **sévérité moindre** (intégrité intra-locataire / forge d'insert d'audit, **pas** de
lecture inter-locataire), **non reproduits** comme escalade, et **non atteignables** dans le déploiement
Docker (le bootstrap retire `TEMPORARY` à PUBLIC). Recommandé pour un correctif de suivi (hors 004.3).

---

## 6. Operational Acceptance

| Élément | Présent | Preuve |
|---|---|---|
| Worker long-lived | ✅ | `workers/runtime.py` (boucle, états `DRAINING`/`FORCED`) |
| Dispatch | ✅ | `persistence/dispatch.py`, RLS-only (aucun SECURITY DEFINER), `test_dispatcher` |
| Lease | ✅ | `0006_job_leases`, `jobs.claim` `FOR UPDATE SKIP LOCKED` |
| Renewal (heartbeat) | ✅ | `workers/lease.py` (`renew_lease` si `locked_by` correspond), `test_worker_lease_keeper` |
| Fencing | ✅ | jeton `(attempts, locked_by)` ; `attempts` change à chaque prise |
| Stale recovery | ✅ | `recover_stale` périodique (runtime) |
| SIGTERM/SIGINT | ✅ | `runtime.install_signal_handlers` → `request_stop` |
| Draining | ✅ | 1er signal → DRAINING (plus de prise), grâce puis STOPPED(0) |
| Health reporting | ✅ | `observability/health.py` écriture atomique, `MERVIO_WORKER_HEALTH_FILE` |
| Exit codes | ✅ | `EXIT_OK` / `EXIT_FORCED` / `EXIT_INTERNAL` / `EXIT_CONFIG` |
| CLI worker | ✅ | `mervio worker` (`cli/worker.py`) |
| CLI healthcheck | ✅ | `mervio worker healthcheck` (lit le fichier, sans base) |

**Docker :** `Dockerfile` — `USER 10001:10001`, `HEALTHCHECK … CMD ["mervio","worker","healthcheck"]`,
`/run/mervio` créé `-o 10001 -g 10001 -m 0750`. `docker-compose.yml` — `read_only: true`, `cap_drop: ALL`,
`no-new-privileges`, réseau `internal`, `tini` (init), tmpfs `/run/mervio` avec uid/gid explicites, profil
`ops` pour migrate/admin. Bootstrap : `REVOKE ALL ON DATABASE … FROM PUBLIC` (retire CONNECT **et
TEMPORARY** à PUBLIC), rôles de connexion non privilégiés. Smoke test : `scripts/container_smoke.py`
(import CSV + analyse de bout en bout), couvert par `test_container_smoke`.

---

## 7. Performance Acceptance

- `docs/PERFORMANCE.md` (004.3.9) déclare explicitement : **« Ce n'est ni une promesse ni un objectif »**,
  conventions **OBSERVÉ / INFÉRÉ / LIMITE**, aucune optimisation (mesurer d'abord, ADR-004-012).
- Référence reproductible **présente et versionnée** : `benchmarks/results/performance_20260917_arm64.json`
  (86 Ko, exécution complète du 17/09, 21 min 21 s) ; tableaux générés par `benchmarks/perf_report.py`.
- Défauts historiques corrigés : gestionnaire vide étiqueté `infrastructure` et séparé des travaux réels ;
  répétitions déclarées (médiane/p95/p99, `p95_is_max` pour petits échantillons) ; six charges séparées ;
  environnement complet enregistré (OS/CPU/mémoire/Python/PG/commit/état Git/graine/empreinte CSV).
- **Aucun seuil inventé.** Le document mesure ; il ne fixe pas d'objectif — conforme à la consigne. Limite
  honnête : mesures sur une seule machine arm64, pas de cible de production (hors-périmètre 004.3).

---

## 8. Documentation Acceptance

| Doc | État | Remarque |
|---|---|---|
| `MERVIO_FINAL_ROADMAP.md` | ✅ | Définition 004.3 complète ; note « superseded » ajoutée au bullet dispatcher (renvoi D-050) — **corrigé en 004.3.11** |
| `DECISIONS.md` | ✅ | D-001…D-050 ; D-049 documente 0009 ; **D-050 (004.3.11)** documente le dispatcher RLS adopté vs l'option `mervio_dispatcher` SECURITY DEFINER |
| `CONTAINER.md`, `ADMIN_CLI.md`, `PERFORMANCE.md` | ✅ | Cohérents avec le code |
| `ARCHITECTURE.md` (`760edb0`, 14/09), `DATA_MODEL.md` (`e8b4e38`, 15/09) | ⚠️ | Antérieurs aux commits de fonctionnalités 004.3 ; non re-vérifiés en profondeur (staleness possible, BASSE ; hors périmètre du présent nettoyage) |
| `README.md` | ✅ | Ligne 53 mise à jour → « 2138 tests (2138 passés, 0 échec, 0 ignoré avec PostgreSQL) » — **corrigé en 004.3.11** |
| `docs/ROADMAP.md`, `docs/TODO.md` | ✅ | Remplacés par un renvoi vers `MERVIO_FINAL_ROADMAP.md` / `DECISIONS.md` (item de périmètre 9) — **corrigé en 004.3.11** |

Les trois écarts documentaires LOW identifiés à l'acceptance initiale ont été **corrigés en 004.3.11**
(nettoyage documentaire strict : aucun changement `src/`, SQL, migration, test ni CI). `ARCHITECTURE.md` /
`DATA_MODEL.md` restent une staleness possible non traitée (hors périmètre de ce nettoyage ciblé).

---

## 9. Known Residual Risks

### Blockers
**Aucun.** Aucun défaut fonctionnel, de sécurité ou opérationnel bloquant n'a été trouvé ou reproduit.

### Corrigés en 004.3.11 (nettoyage documentaire — étaient les 3 écarts pre-pilot LOW)
1. ✅ **Item de périmètre 9** — `docs/ROADMAP.md` et `docs/TODO.md` remplacés par un renvoi vers
   `MERVIO_FINAL_ROADMAP.md` / `DECISIONS.md`.
2. ✅ **README compte de tests** — ligne 53 mise à jour (2138, 0 échec, 0 ignoré).
3. ✅ **Déviation dispatcher répercutée** — `DECISIONS.md` D-050 documente le dispatcher RLS adopté (identité
   de service + `service_authorizations`, sans `mervio_dispatcher` ni SECURITY DEFINER) et marque l'option
   ADR-004.2-002 comme superseded ; note de renvoi ajoutée au roadmap. L'historique 004.2 n'est pas réécrit.

### Post-pilot risks (sévérité BASSE, non atteignables en Docker) — DOCUMENTÉS, NON CORRIGÉS
4. **Ombrage `pg_temp` résiduel** — gardes d'instantané `0002`/`0003` (`data_snapshots`) et sous-requête
   `FROM jobs` de la politique `audit_events_service_actor` restent non qualifiées (même classe que 0009,
   sévérité moindre, intra-locataire ; retrait de `TEMPORARY` à PUBLIC les neutralise en Docker). Documentés,
   **non corrigés** — à qualifier dans un correctif de suivi.

### Explicitly out-of-scope (NON comptés comme échecs de 004.3)
Frontend produit, API HTTP, Shopify/OAuth, LLM en production, Stripe/billing, actions autonomes,
microservices, Kafka/Redis/Kubernetes, hébergement de production. Tous **explicitement hors-périmètre**
(roadmap 004.3 « Out of scope »). Le `docs/TODO.md` obsolète référence certains de ces éléments (LLM, PDF,
OAuth) : c'est de la dette documentaire, pas une fonctionnalité 004.3 manquante.

---

## 10. Final Decision

## RATIFIED WITH DOCUMENTED RESIDUAL RISKS

**Justification :** le périmètre technique de 004.3 (CI, bail+fencing, identité de service, dispatcher,
settings/logging/health, worker long-lived, CLI, administration, conteneur, mesures, correctif sécurité 0009)
est **présent, testé et vert** — 2138/2138, 0 échec, 0 ignoré, garde anti-faux-vert active, sur PostgreSQL
17.11 (version CI). La correction de sécurité 0009 est **vérifiée en base**, pas seulement documentée. Les
critères d'acceptance techniques du roadmap sont satisfaits. Aucun **blocker** ne subsiste.

Les trois écarts documentaires LOW ont été **corrigés en 004.3.11** (nettoyage documentaire strict). La seule
réserve restante est **un risque de sécurité résiduel de sévérité moindre, documenté et non corrigé, non
atteignable dans le déploiement livré** (ombrage `pg_temp` des gardes `0002`/`0003` et de la sous-requête
`FROM jobs` de la politique d'audit) — d'où le maintien du verdict « WITH DOCUMENTED RESIDUAL RISKS ».

### État des corrections identifiées à l'acceptance initiale

| # | Fichier | Problème | État |
|---|---|---|---|
| 1 | `docs/ROADMAP.md`, `docs/TODO.md` | Non remplacés par un renvoi (item 9) | ✅ **CORRIGÉ 004.3.11** — renvoi vers `MERVIO_FINAL_ROADMAP.md` |
| 2 | `README.md:53` | « 1097 tests » obsolète | ✅ **CORRIGÉ 004.3.11** — 2138, 0 échec, 0 ignoré |
| 3 | `docs/MERVIO_FINAL_ROADMAP.md`, dispatcher | `mervio_dispatcher` SECURITY DEFINER superseded | ✅ **CORRIGÉ 004.3.11** — `DECISIONS.md` D-050 + note de renvoi au roadmap ; historique 004.2 intact |
| 4 | `0002_snapshots.py:272`, `0003_analysis_reports.py:67`, `0007_service_identity.py:123` | Références non qualifiées (résiduel `pg_temp`) | ⏸ **DIFFÉRÉ** — hors périmètre du nettoyage documentaire ; correctif code de suivi (migration `0010`) recommandé |

L'item 4 est un durcissement de **code** (migration), délibérément **hors du périmètre strictement
documentaire** de 004.3.11 ; il reste un risque résiduel documenté (post-pilote, non atteignable en Docker).
Aucun commit, aucun push, aucune fusion, aucun
démarrage de 004.4.

---

*Généré en revue d'acceptance 004.3.11. HEAD `312c6f2` inchangé. Aucune modification de code produit.*
