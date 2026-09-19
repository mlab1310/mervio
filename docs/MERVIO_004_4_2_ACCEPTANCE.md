# MERVIO 004.4.2 — ACCEPTANCE & RATIFICATION

**Date :** 2026-09-18 · **Mission :** 004.4.2 — Identité client à clé et retrait de l'e-mail persisté
(D-053, D-057, D-058)

## Status

**RATIFIED / CLOSED**

Revues : audit post-implémentation indépendant en lecture seule, durcissement avant ratification
(`docs/MISSION_004_4_2_HARDENING.md`), puis preuve CI sur le SHA figé.

## Baseline

`3150b91` — `docs(acceptance): ratify mission 004.4.1` (migrations `0001` à `0010` inchangées octet pour
octet ; `0011_identity_pii_schema` est la seule nouvelle révision).

## Final commit

`dfe1975e1c72a83272fcb7c0b65e64fc1930bc28` — `feat(privacy): implement tenant-scoped identity and safe ingestion`
(63 fichiers : 7 ajoutés, 56 modifiés ; +2324 −168). Branche `mission-004.4`, identique à
`origin/mission-004.4` au moment de la ratification.

## CI evidence

| Champ | Valeur |
|---|---|
| Workflow | `ci` (`.github/workflows/ci.yml`) |
| Run | #10 |
| Run ID | `35417529854` |
| Event | `push` |
| SHA | `dfe1975e1c72a83272fcb7c0b65e64fc1930bc28` (run et chacun des 4 jobs) |
| Attempt | 1 (aucune relance ; aucun `continue-on-error` ni retry dans le workflow) |
| Result | `completed / success` — jobs `lint`, `test`, `security`, `docker` : `success` |

**Portée de la preuve.** Le statut du run, des jobs et de chaque étape a été lu via l'API publique GitHub.
Le **contenu textuel des logs n'a pas été accessible** (téléchargement des logs refusé sans authentification,
HTTP 403). Les conclusions ci-dessous reposent donc sur le succès des étapes et sur la sémantique des scripts
qu'elles exécutent au SHA figé (chacun échoue sur toute attente non tenue), pas sur la lecture des logs.

## Acceptance gates

| Gate | Résultat | Étape CI / preuve |
|---|---|---|
| Tests | ✅ 2253/2253 | job `test` : suite complète, PostgreSQL 17.11 épinglé, `MERVIO_REQUIRE_DATABASE_TESTS=1` |
| 0 fail · 0 error · 0 skip | ✅ | `scripts/ci_test_gate.py --expected-tests 2253` : compte exact, aucun échec, erreur, test ignoré ou xfail toléré, modules obligatoires exécutés |
| Lint | ✅ | job `lint` : `compileall` + `ruff check --select E9,F63,F7,F82` |
| Migrations | ✅ | `scripts/ci_migrations.py --postgres-major 17` : montée, descente, remontée, même schéma |
| Security (tests) | ✅ | modules obligatoires de la porte (isolation, tenancy, service identity, migrations, audit…) |
| RLS | ✅ | tests RLS de `organization_identity_keys` et matrice d'isolation existante |
| `pg_temp` | ✅ | `test_pg_temp_residual.py` + tests d'ombrage de `identity_keys.py` (F-04) |
| pip-audit | ✅ | job `security` : `requirements.lock` et `requirements-build.lock`, `--strict` |
| gitleaks | ✅ | job `security` : historique complet, binaire 8.30.1 vérifié par somme de contrôle |
| Docker build | ✅ | job `docker` : image de base épinglée, dépendances verrouillées |
| Docker smoke | ✅ | `scripts/container_smoke.py`, vrais conteneurs : amorçage, refus d'un schéma non migré, migration au head, refus des connexions privilégiées |
| Worker | ✅ | processus non-root (uid vérifié), identité de service ; sans `MERVIO_IDENTITY_MASTER_KEY` : refus de configuration (code 2) nommant la variable, aucun principal enregistré |
| Healthcheck | ✅ | `HEALTHCHECK` de l'image et `mervio worker healthcheck --ready` |
| Import · analysis · report | ✅ | étape `demonstrate` du smoke : import, analyse, rapport en base, audit attendu |
| Isolation | ✅ | étape `check_isolation` du smoke : organisation étrangère invisible, RLS applicative et worker |
| Cleanup | ✅ | `cleanup` du smoke puis « No container, volume or network left behind » |

Le test `test_a_graceful_stop_lets_the_current_job_finish_and_takes_no_other` (échec intermittent sous
charge en local, 1 sur 4 suites complètes ; 20/20 en isolation) fait partie du job `test`, vert au premier
essai.

## Scope delivered

- **Identité client à clé par organisation (D-053)** : `customer_ref` = `c1:` + 128 bits de
  HMAC-SHA256 de l'e-mail normalisé (espaces retirés, minuscules), ou `g1:` + HMAC de l'identifiant de
  commande pour un invité ; `guest:` n'est plus produit (`src/mervio/identity.py`).
- **Clés d'identité d'organisation** : sel de 32 octets dans `organization_identity_keys` (RLS forcée,
  SELECT/INSERT seulement, déclencheur n'autorisant que « sel présent → détruit »), créé à la première
  utilisation (`src/mervio/persistence/identity_keys.py`) ; clé d'organisation =
  `HMAC-SHA256(clé maître, "mervio:identity:v1:" ‖ organization_id ‖ ":" ‖ sel)`, jamais persistée.
- **Clé maître** : `MERVIO_IDENTITY_MASTER_KEY` (ou `_FILE`), hors base, jamais journalisée ; empreinte
  non secrète `master_key_id` stockée ; discordance ou sel détruit → refus `identity_key_unavailable`.
- **Retrait de l'e-mail persisté (révision `0011`)** : `orders.customer_email` et
  `payments.customer_email` supprimés ; contrainte `orders_customer_ref_keyed` (`NOT VALID`) ; refus de
  migrer en présence d'un instantané non synthétique (D-057) ; descente refusée dès qu'une clé existe.
- **Modes d'identité CLI (D-058)** : `--identity-key-file` (déterministe) ou `--ephemeral-identity-key`
  (défaut, pseudonymes non liables).
- **Provenance** : `NORMALIZATION_VERSION = "mervio-normalization/2"`, indépendant d'`ENGINE_VERSION` ;
  identifiant non secret de la clé dans `inputs_sha256` ; provenance de clé (`organization`, `explicit`,
  `ephemeral`) et `write_snapshot` n'acceptant qu'un jeu calculé avec la clé de l'organisation de la
  session (F-08).
- **Frontière d'import (limitée à cette mission)** : `import.started` n'est audité qu'après une clé
  d'identité utilisable, avant tout accès fichier (F-02). La frontière d'import par `raw_object_id` /
  ObjectStore (D-054, D-055) **n'est pas** livrée ici.
- **Schéma boutique et connexion** : `stores.timezone` (défaut `'UTC'`) et `stores.timezone_source`
  (`default` / `explicit`) ; `connections.credential_ref` nullable, interdit sur `csv_upload`, aucun secret.
- **Protections PII** : aucun e-mail dans `Order`, `Payment`, `Customer`, les lignes canoniques, les
  rapports, les travaux, l'audit ou les logs ; Stripe ne lit plus l'e-mail ; rédaction des clés dans les logs.
- **Durcissement sécurité** : SQL d'`identity_keys.py` qualifié `public.` (F-04) ; aucune nouvelle
  fonction `SECURITY DEFINER`, aucun nouveau rôle.
- **Worker** : clé maître obligatoire dès que le worker traite des imports (settings, compose, smoke).
- **Tests et CI** : 2234 → 2253 tests ; porte à compte exact mise à jour.

## Residual risks

Acceptés et documentés, **non résolus** :

| ID | Sévérité | Constat | Référence |
|---|---|---|---|
| F-01 | MEDIUM, préexistant, hors périmètre | L'identité d'octets rapport CLI / persisté n'est pas garantie entre processus lorsque `campaign_contributors` contient des égalités de `conversions_delta` (ordre dépendant de `PYTHONHASHSEED`, `analytics/root_cause.py`, non modifié). Le test multi-processus ajouté ne couvre que `data/sample`, sans égalité. | `docs/MISSION_004_4_2_HARDENING.md` (tâche de correction), `docs/DECISIONS.md` (D-058, statut) |
| F-03 | MEDIUM | Le premier `master_key_id` inscrit devient définitif pour une organisation ; une mauvaise clé initiale peut bloquer ses imports ; aucune procédure de reprise n'est implémentée. Comportement fail-closed voulu ; ne jamais résoudre par un `UPDATE` de `master_key_id`. | `docs/DECISIONS.md` (D-053, statut), `docs/CONTAINER.md` |

Autres constats consignés (INFO) : F-07 (normalisation e-mail sans NFC ni alias), F-09 (anciens instantanés
synthétiques : référence = e-mail synthétique, accepté par D-057).

## Out of scope

- Frontière d'import sans chemin, `raw_object_id`, ObjectStore et pilote S3 (D-054, D-055 : 004.4.4).
- Effacement client, objets bruts, tombstones `redacted:` (D-056 : 004.4.5) ; seul le format est accepté par la contrainte.
- Purge d'organisation et destruction du sel (D-051 : 004.4.6) et fonctions privilégiées d'effacement et de purge (D-052 : 004.4.5 / 004.4.6) ; seule la transition « sel présent → détruit » est autorisée par le déclencheur.
- Validation IANA du fuseau et son usage (004.4.3 / 004.5) ; `connections.kind` reste `csv_upload`.
- KMS, rotation et réassignation de la clé maître (004.9) ; jetons OAuth / Shopify (004.11–004.12).
- Contrat de rapport 2.0 et retrait des identifiants client du rapport (004.5).
- Correction de F-01 (mission dédiée).

## Final verdict

**RATIFIED / CLOSED**

CI GREEN on the frozen final SHA `dfe1975e1c72a83272fcb7c0b65e64fc1930bc28` (run #10, ID `35417529854`).
