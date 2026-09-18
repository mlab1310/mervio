# Mervio 004.4.1 Acceptance

**Date :** 2026-09-18 · **Statut :** RATIFIED
**Revue :** audit post-implémentation indépendant, en lecture seule (verdict : GO FOR 004.4.1 ACCEPTANCE / RATIFICATION).

## Mission

004.4.1 — Security Hygiene D-049 + Architecture Decisions D-051 → D-059

## Baseline

`c3563f2` (ratification de la mission 004.3)

## Final Commit

`7ac1d9a428909c03f434a8bc0c2ed9af7b0999f2` — `fix(security): qualify residual tenant guards`

## Branch

`mission-004.4` (identique à `origin/mission-004.4` au moment de la ratification)

## Scope

Implémenté dans `7ac1d9a` (6 fichiers, +576 −4) :

| Fichier | Changement |
|---|---|
| `src/mervio/persistence/migrations/versions/0010_qualify_residual_security.py` | nouvelle révision, seul fichier `src/` modifié |
| `tests/persistence/test_pg_temp_residual.py` | 9 nouveaux tests (attaques `pg_temp` réelles, inventaire du catalogue, privilèges, aller-retour 0009 ↔ 0010) |
| `tests/persistence/test_persistence_migrations.py` | 0010 ajoutée à la chaîne des révisions attendues ; aucun test retiré ni affaibli |
| `docs/DECISIONS.md` | statut de D-049 (risque résiduel fermé, diagnostic corrigé) ; décisions D-051 à D-059 (issues de la revue d'architecture 004.4.0) |
| `.github/workflows/ci.yml` | `MERVIO_EXPECTED_TESTS` : 2138 → 2147 (porte à compte exact) |
| `README.md` | compte de tests : 2138 → 2147 |

Les décisions D-051 à D-059 sont **des décisions enregistrées, pas des fonctionnalités implémentées**. Chacune indique la sous-mission qui l'implémentera (004.4.2 à 004.4.6, ou 004.6 pour D-059).

## Security

**D-049 — risque résiduel `pg_temp` fermé.** PostgreSQL cherche d'abord dans `pg_temp` tout nom de relation non qualifié, sauf si `pg_temp` est nommé explicitement dans `search_path`. Un corps de fonction est réinterprété à chaque exécution. Deux gardes lisaient encore une table par un nom nu :

| Objet | Avant (0009) | Après (0010) | Attaque reproduite à 0009 |
|---|---|---|---|
| `analysis_runs_require_sealed_snapshot()` (0003) | `FROM data_snapshots` | `FROM public.data_snapshots` | une table temporaire `data_snapshots` qui déclare `completed` un instantané encore en ingestion permettait d'ouvrir une analyse sur une entrée non scellée |
| `canonical_rows_guard_sealed()` (0002) | `JOIN data_snapshots` | `JOIN public.data_snapshots` | une table temporaire qui déclare `ingesting` un instantané scellé permettait d'y ajouter une commande |

- **`changed_rows`** reste non qualifié : c'est la table de transition du déclencheur. PostgreSQL la résout avant les tables du catalogue, donc avant `pg_temp`. Un test montre qu'une table temporaire `changed_rows` ne la masque pas.
- **Politique `audit_events_service_actor` (0007).** Elle n'était **pas** vulnérable, et c'est démontré :
  - l'expression d'une politique est stockée déjà résolue ; à 0009, `pg_policy.polwithcheck` référence directement l'identifiant interne de `public.jobs` ;
  - une table temporaire `jobs` forgée ne permet pas de forger `on_behalf_of`, avec un témoin qui prouve que le refus vient bien de cette politique.

  0010 la réécrit avec `public.jobs` par cohérence de source. L'arbre stocké est identique avant et après.
- **Inventaire.** Un test interdit toute lecture (`FROM`/`JOIN`) d'une relation non qualifiée dans une fonction du schéma `public`.
- **`search_path`.** Aucun `SET` ajouté aux fonctions de déclencheur, comme dans la stratégie de 0009. La seule fonction `SECURITY DEFINER` (`app_ensure_service_principal`) garde `search_path=pg_catalog, public, pg_temp`.
- **RLS et FORCE RLS.** Inchangées. Toutes les tables de tenant restent en RLS activée et forcée ; seules `users` (SEC-05, préexistant) et `alembic_version` en sont dépourvues, comme à 0009. 90 politiques avant et après.
- **Privilèges.** 0009 et 0010 ont été comparés en base :
  - une seule fonction `SECURITY DEFINER`, que PUBLIC ne peut pas exécuter ;
  - droits de `mervio_app`, `mervio_worker` et PUBLIC sur les tables et les colonnes identiques ;
  - `mervio_app` et `mervio_worker` restent sans superutilisateur, sans `BYPASSRLS` et sans connexion directe.

  **0010 ne crée aucun privilège supplémentaire.**
- **Isolation des tenants.** Les attaques sont exécutées sous les vrais rôles `app` et `worker`, avec le tenant A comme contexte et un humain du tenant B comme victime. Aucune table temporaire forgée ne contourne le scellement des instantanés, le contrôle de l'acteur d'audit ni la RLS.

## Migration

`0010_qualify_residual_security` (révise `0009_qualify_security_functions`)

| Transition | Vérification |
|---|---|
| 0009 → 0010 | les deux attaques, acceptées à 0009 (le test l'exige, preuve du vecteur), sont refusées |
| 0010 → 0009 | fonctions, expressions de politique (arbre stocké compris), déclencheurs et droits identiques à ceux de 0009 ; les attaques redeviennent possibles, ce qui prouve une descente fidèle |
| 0009 → 0010 | définitions identiques au premier passage ; attaques de nouveau refusées |

- `scripts/ci_migrations.py` (base vide → tête → base vide → tête, même empreinte du catalogue) : porte franchie, en local et en CI.
- Les migrations 0001 à 0009 sont inchangées par rapport à `c3563f2`.
- **Contre-épreuve.** Sur une copie du commit sans 0010, exactement 3 tests échouent : les deux attaques et l'inventaire.

## Tests

- **2147 passed**
- **0 failed**
- **0 skipped**

Suite complète avec PostgreSQL obligatoire (`MERVIO_REQUIRE_DATABASE_TESTS=1`) et `ci_test_gate --expected-tests 2147` franchie. 2147 = 2138 (baseline) + 9 nouveaux tests.

## CI

**Run :** [35352677245](https://github.com/mlab1310/mervio/actions/runs/35352677245) — `head_sha = 7ac1d9a428909c03f434a8bc0c2ed9af7b0999f2`, tentative 1, `completed / success`.

**Jobs :**
- lint PASS
- test PASS
- security PASS
- docker PASS

## Gitleaks

**PASS** — étape « Secret scan of the full history (gitleaks, verified binary) » du job `security`, run 35352677245. pip-audit sur `requirements.lock` et `requirements-build.lock` : PASS.

## Docker

**PASS** — job `docker`, run 35352677245 : construction de l'image, smoke test conteneurisé (vrai PostgreSQL, worker, admin) et absence de conteneur, volume ou réseau résiduel.

## Residual Risks

| # | Sévérité | Risque | Suite |
|---|---|---|---|
| F-1 | **LOW** | L'inventaire anti-`pg_temp` ne détecte que les lectures (`FROM`/`JOIN`) ; une cible d'écriture non qualifiée (`INSERT INTO`, `UPDATE`, `DELETE … USING`) lui échapperait. Toutes les écritures actuelles sont qualifiées. | Reporté : à élargir au plus tard avec l'introduction des fonctions privilégiées d'écriture (D-052, 004.4.5 / 004.4.6). |
| — | INFORMATIONAL | `users` sans RLS (SEC-05) et droits EXECUTE par défaut de PUBLIC sur les fonctions non `SECURITY DEFINER` : préexistants, inchangés par 004.4.1. | Hors périmètre (SEC-05 : 004.6). |
| — | INFORMATIONAL | Si on mélange certains fichiers de `tests/` et de `tests/persistence/` dans un même appel pytest ciblé, les fixtures de persistance ne sont pas trouvées. Défaut préexistant ; sans effet sur la suite complète ni sur la CI. | Non traité. |
| — | INFORMATIONAL | `mission-004.3` n'est pas encore fusionnée sur `master` (condition de procédure héritée de 004.4.0). | Hors 004.4.1. |

## Scope Exclusions

Ne font **pas** partie de 004.4.1, ni en code, ni en migration, ni en test :

- ObjectStore
- `raw_objects`
- `customer_redactions`
- `organization_identity_keys`
- identité client HMAC
- purge (client, boutique, organisation)
- S3
- Shopify
- OAuth
- API
- frontend
- LLM

Ces éléments ne sont que **décidés** (D-051 à D-059) et relèvent des sous-missions 004.4.2 et suivantes ou de missions ultérieures.

## Final Status

**RATIFIED**
