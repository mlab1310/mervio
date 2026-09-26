# MERVIO 004.4.5 — ACCEPTANCE

**Date :** 2026-09-26 · **Mission :** 004.4.5 — Effacement client : désignation, concurrence,
destruction réelle des octets (D-062, D-063, D-064, achèvement de D-056)

## Status

**ACCEPTED / CLOSED / RATIFIED — avec résiduels**

Revue formelle sur le SHA figé, après quatre échecs de CI diagnostiqués et corrigés. Les résiduels
listés en §T sont explicitement acceptés par les décisions ; aucun n'est une exigence non tenue.
La condition de clôture de D-064 — *exécution réelle de l'effacement dans un runtime Docker, contre
le montage réel, destruction physique vérifiée et non inférée* — est **satisfaite**.

## Baseline

`0b6091c` — `docs(004.4.4): ratify acceptance`, dernier commit antérieur à 004.4.5 et dernière CI
entièrement verte avant la mission (run #16, `36054689691`).

## Final commit

`720404d2e03856615e92f5a61b78b6c2792e1440` — `fix(004.4.5): assert ratified customer erasure audit
event`. Branche `mission-004.4`, identique à `origin/mission-004.4` ; arbre de travail propre.

Périmètre complet de la mission (`0b6091c..720404d`) : **52 fichiers, +7375 −106**.

| Catégorie | Fichiers | Lignes |
|---|---|---|
| `tests/` | 23 | +4681 −27 |
| `src/` (code produit) | 21 | +1738 −63 |
| `docs/` | 5 | +568 −6 |
| `scripts/` (smoke conteneur) | 1 | +380 −6 |
| Infrastructure (`docker-compose.yml`, `ci.yml`) | 2 | +8 −4 |

Commits de la mission, dans l'ordre :

| SHA | Objet |
|---|---|
| `701a5d1` | `docs(004.4.5)` — ratification de D-062 (désignation du client) |
| `f27e618` | `feat(004.4.5)` — primitives d'effacement (E1–E3, révision `0014`) |
| `e2e34bf` | `feat(004.4.5)` — exécution de l'effacement (E4, révision `0015`) |
| `ae7ebdc` | `docs(004.4.5)` — ratification de D-063 (concurrence effacement/import) |
| `810cb5f` | `feat(004.4.5)` — rejeu des effacements à l'import (E5) |
| `93f0f23` | `feat(004.4.5)` — résolution worker-locale du client (E6) |
| `dbe806d` | `docs(004.4.5)` — ratification de D-064 (architecture de destruction) |
| `9cd36ae` | `feat(004.4.5)` — implémentation D-064 : magasin et runtime d'effacement |
| `3157359` | `fix(004.4.5)` — isolation des workers négatifs du smoke vis-à-vis du préflight |
| `8ddc24b` | `fix(004.4.5)` — assertion d'effacement depuis l'état faisant autorité |
| `720404d` | `fix(004.4.5)` — assertion sur l'événement d'audit ratifié |

---

## A. Scope

Achever l'effacement client de bout en bout, dans le runtime réellement livré :

1. **D-062** — d'où vient le `customer_ref` : résolution locale au worker, hors file.
2. **D-063** — effacement, purge et import concurrents : verrou consultatif d'organisation.
3. **D-064** — destruction des octets bruts par le worker : montage en écriture, `O_NOFOLLOW`,
   contrat d'erreur total, contrôle de capacité.
4. **D-056** — clôture de la décision d'origine : tombstone en base **et** destruction physique.

Hors périmètre, explicitement : la purge d'organisation et la destruction du sel d'identité
(**004.4.6**, non commencée), le ramassage des orphelins et l'écriture conditionnelle S3
(**004.9**), la politique IAM S3 (**004.9**).

## B. D-062 — dépendance : désignation du client

Le worker détient la clé maître ; `admin` ne la reçoit à aucun moment. La résolution est
`operator identity → worker local → c1:<32 hex> → stdout uniquement`, sans persistance.

Vérifié : `mervio worker resolve-customer-ref --org <org> --as <sujet>` lit l'identité **uniquement
sur stdin** et n'écrit ni ligne, ni travail, ni instantané, ni événement d'audit, ni log, ni
fichier. `mervio admin job enqueue-redact --customer-ref` n'accepte **aucune** identité directe ;
`validate_customer_ref` n'admet que la forme canonique. `AdminSettings` ne porte pas la clé maître.
Le smoke Docker prouve les deux jambes : le worker résout, `admin` ne peut pas.

Couverture : `tests/test_resolve_customer_ref.py` (+312), `tests/persistence/test_customer_resolution.py`
(+504).

## C. D-063 — dépendance : concurrence effacement/import

Verrou consultatif PostgreSQL d'organisation, **pris dans la transaction** :
import → `pg_advisory_xact_lock_shared(org_scope_key)` ; effacement/purge →
`pg_advisory_xact_lock(org_scope_key)`.

`persistence/concurrency.py` refuse explicitement (`LockOutsideTransaction`) un verrou demandé hors
transaction — sans ce refus, `autocommit=True` en ferait un no-op silencieux. Surcoût mesuré lors de
la campagne E5 : verrou partagé 0,119 ms ; consultation du rejeu 2,14 ms pour 1500 références, soit
2,26 ms sur un import réel de 208 ms (2141 commandes) = **1,09 %**. La course reproduite est devenue
un test de non-régression prouvant sa fermeture, dans les deux ordres.

Couverture : `tests/persistence/test_erasure_import_concurrency.py` (+309).

## D. D-064 — architecture

Architecture « A-durcie » : le montage du worker est ouvert en écriture, et la protection incidente
que cette ouverture retire est **remplacée explicitement**, à la couche où elle appartient.

Ce qui change et ce qui ne change pas, sans confusion des plans :

- **Aucun privilège PostgreSQL n'est élargi.** Rôles, politiques RLS, fonctions `SECURITY DEFINER`
  et droits de table sont intégralement inchangés.
- **Seule la capacité filesystem du worker est élargie**, de lecture seule vers destruction. C'est
  une perte de défense en profondeur **acceptée et documentée**, nécessaire pour satisfaire D-056.
- **La frontière de sécurité et d'isolation locative ne bouge pas** : elle est, et reste, la ligne
  `raw_objects` sous RLS forcée, avec l'autorisation de service et le délégué humain.
- Le worker **ne dépose aucun objet** : propriété désormais **comportementale**, imposée par le
  code et vérifiable par test, non plus garantie par le noyau — et donc jamais invocable comme un
  contrôle de sécurité.

## E. E1–E6 — implémentation

| Tranche | Livré | Preuve |
|---|---|---|
| E1 | `ObjectStore.delete(key)`, idempotente sur les trois pilotes, sans joker ni récursion | `tests/test_object_store_contract.py` |
| E2 | Révision `0014` : `customer_redactions`, `raw_objects.purge_reason`, type `redact_customer` | `tests/persistence/test_migration_0014.py` |
| E3 | `app_redact_customer(job_id)`, première fonction privilégiée (D-052) | `tests/persistence/test_customer_erasure.py`, `..._adversarial.py` |
| E4 | Révision `0015` : `app_finalize_raw_object_purge`, gestionnaire `redact_customer` | `tests/persistence/test_migration_0015.py`, `test_customer_erasure_handler.py` |
| E5 | Rejeu des effacements à l'import + verrou D-063 | `tests/persistence/test_import_erasure_replay.py` |
| E6 | Résolution worker-locale + `enqueue-redact` | `tests/test_resolve_customer_ref.py` |

Les sept points d'implémentation de D-064 sont livrés : montage `rw`, `delete_capability()` à trois
valeurs, contrat d'erreur total, `O_NOFOLLOW`, préflight fail-closed, et la documentation
(`docs/DECISIONS.md`, `docs/CONTAINER.md`, exigence S3 consignée pour 004.9).

## F. Contrat de destruction `ObjectStore` — totalité des erreurs

**Aucune exception native de pilote ne franchit l'abstraction.** Chaque méthode des trois pilotes ne
lève que `ObjectStoreError` ou une sous-classe. La traduction se fait à la frontière du pilote
(`_failure` dans `filesystem.py` et `s3.py`), conserve la **classe** d'erreur exploitable (`errno`
pour POSIX, code de réponse pour S3) et **n'interpole aucun chemin ni aucune clé** dans le message.
`s3.py` ne contient volontairement aucun `except ClientError` nu.

Avant D-064, une `PermissionError` ou un `OSError` `EROFS` contournait la branche
`except ObjectStoreError` de l'appelant privilégié, et le classement de repli publiait un code dérivé
du **nom de classe Python** (`permission_error`, `o_s_error`) au lieu du code de domaine
`object_delete_failed`. La reprenabilité coïncidait par accident : comportement fortuitement correct,
contractuellement faux. C'est cette **cohérence de classification** qui est corrigée — il n'existait
aucune fuite de chemin en cours (voir §M).

## G. Pilote filesystem — `O_NOFOLLOW`

Le chemin de **lecture** ouvre avec `O_NOFOLLOW` : un lien symbolique substitué **après** la
résolution échoue (`ELOOP`) au lieu d'être suivi. La couche 4 du confinement cesse d'être un contrôle
*avant* l'ouverture, sujet à la course, pour devenir une propriété **de l'ouverture elle-même**.

Périmètre assumé et consigné dans le module : `O_NOFOLLOW` porte sur la **lecture** seule ; `put`
conserve son contrôle préalable sans `O_NOFOLLOW`, et le drapeau ne concerne que le **dernier**
composant du chemin. Le TOCTOU est donc **réduit, non annulé** (§T).

Couverture : `tests/test_object_store_traversal.py` (+182).

## H. Préflight de capacité de destruction

Un worker configuré pour servir `redact_customer` vérifie au démarrage que son magasin est réellement
capable de détruire, et **refuse de démarrer** sinon : code de sortie de configuration **2**, message
ne nommant que des variables et des règles. La mauvaise configuration est ainsi détectée **avant**
qu'un travail ne fasse passer des lignes en `purging`, et non au milieu d'un effacement.

| Pilote | Verdict | Méthode |
|---|---|---|
| filesystem | `capable` / `incapable` | `os.access(racine, W_OK \| X_OK)`, sans effet de bord ; reflète l'état **réel** du montage, pas un drapeau déclaratif. Si la racine n'existe pas encore, le premier ancêtre existant est interrogé |
| mémoire | `capable` | destruction toujours possible |
| S3 | `undetermined` | **aucune sonde destructive** : prouver `s3:DeleteObject` exigerait de l'appeler, et un `delete_object` pose un marqueur de suppression sur un bucket versionné. Le worker démarre en journalisant un avertissement |

Couverture : `tests/test_worker_delete_preflight.py` (+227).

## I. Tombstone client

Tombstone `redacted:<uuid4>`, **aléatoire et distinct par client**, jamais dérivé du HMAC ni de
l'identité. `orders.customer_ref` seule est modifiée, vers un tombstone seul, par le seul
propriétaire de la table, toutes les autres colonnes comparées en `jsonb` pour qu'aucune ne bouge.

Prouvé dans Docker : la référence effacée ne subsiste dans **aucune** ligne canonique ; exactement
**un** tombstone conforme à la forme attendue ; le nombre de commandes portant le tombstone égale le
nombre de commandes du client ; `customer_redactions` porte **exactement une** preuve.

## J. Destruction physique des objets bruts

**Ordre des opérations, propriété de sécurité :** `available → purging` en base (`0014`), puis
`ObjectStore.delete`, puis `purging → purged` (`0015`). Marquer `purged` avant de supprimer
affirmerait une destruction qui n'a pas eu lieu.

**Aucune transaction commune PostgreSQL + magasin** n'existe ni n'est simulée. Un `delete` en échec
laisse la ligne `purging` — illisible, non détruite, **reprenable** — et le travail est reprenable
(`RetryableJobError`), jamais définitivement échoué.

Prouvé dans Docker, **constaté et non inféré** : les octets des objets porteurs d'identité
(`shopify_orders`, `stripe`) sont constatés **présents** sous leurs clés avant l'effacement, puis
**absents du volume** après. Les objets sans identité (`shopify_products`, `google_ads`) sont
**intacts**. Les lignes `raw_objects` **survivent** en `purged`, avec `purged_at` renseigné et leurs
métadonnées non sensibles (`sha256`, `byte_size`) conservées ; **aucune ligne n'a disparu**.

## K. Rejeu / idempotence

Rejouer le **même** effacement ne ressuscite rien et ne double aucune preuve. `0014` retrouve la
redaction existante au lieu d'en créer une seconde (donc le même tombstone) ; `0015` rend `false`
sans écrire si la ligne est déjà `purged` ; `ObjectStore.delete` est idempotente sur les trois
pilotes — une clé absente n'est **pas** une erreur, précisément parce que l'effacement est rejouable.

Une interruption après la suppression laisse la ligne `purging` sur un objet déjà vide, qu'un rejeu
redétruit sans erreur puis finalise. Le rejeu à l'import (E5) est entièrement piloté par
`customer_redactions` : un nouvel export du marchand **n'annule pas** un effacement.

Exécuté dans le run Docker de référence (étape j de `check_erasure`).

## L. Isolation locative

L'organisation étrangère montée par le contrôle d'isolation du smoke n'est **pas** touchée : le
nombre d'objets `purged` après l'effacement égale exactement le nombre d'objets porteurs d'identité
de l'organisation visée. RLS forcée, autorisation de service et délégué humain sont inchangés ; un
worker compromis ne gagnerait aucune visibilité ni aucun droit sur une ligne `raw_objects` d'une
organisation pour laquelle son service n'est pas autorisé.

## M. Protection des données personnelles

Aucune PII en charge utile, en base, en log, en audit ni en erreur. La charge utile du travail ne
transporte qu'un HMAC. `jobs.mark_failed` applique `_safe_error` → `redact_text` : un `unlink` refusé
est persisté sous la forme `[Errno 13] Permission denied: '[redacted-path]'`. Les logs ne publient que
`error_type`, les métadonnées d'audit que `error_code` — aucun message.

Le smoke exécute un **balayage d'absence d'identité** : l'identité du client ne doit apparaître
**nulle part**, ni en base (y compris `audit_events.metadata`), ni dans les sorties et journaux de
**tous** les conteneurs du projet. Étape exécutée et passée dans le run de référence.

## N. Vocabulaire d'audit

L'effacement est tracé, dans la **même transaction** (D-052), par le vocabulaire ratifié de `0014` :

| Champ | Valeur |
|---|---|
| `action` | `customer.redacted` |
| `resource_type` | `customer_redaction` |
| `resource_id` | l'identifiant de la ligne de **preuve** |

La trace désigne la ligne de preuve, qui porte elle-même la référence sous RLS — elle ne contient
aucune identité ni aucune valeur effacée. `0015` **n'écrit aucune trace supplémentaire** pour la purge
des octets.

**Défaut trouvé et corrigé dans le smoke (BUG #3, §Historique CI).** Le contrôle filtrait
`resource_type IN ('job', 'raw_object')`, ce qui excluait précisément le seul événement d'effacement
existant. Le contrôle exige désormais le **couple ratifié**, et non un motif approchant. Bug du
contrôle, pas du produit : aucune migration, aucun gestionnaire et aucune implémentation d'audit n'a
été modifié — et en particulier `scrub()` n'a **pas** été assoupli (§T).

## O. Preuve de runtime Docker

**Exigence 6 de l'acceptance D-064 — tenue contre le montage réel, pas un substitut.**

Dans le job `docker` du run de référence, sur Linux, le **vrai worker conteneurisé** a : résolu le
client, pris le travail, exécuté `redact_customer`, et terminé en `succeeded`. Étapes du smoke
exécutées, dans l'ordre, toutes passées :

| Étape | Objet |
|---|---|
| compose / bootstrap | fichier validé, PostgreSQL démarré |
| refus de schéma non migré | aucune migration implicite |
| `migrate` | `upgrade head`, étape explicite |
| refus de superutilisateur / mauvais mot de passe | worker et admin |
| démarrage worker | healthcheck, identité de service |
| démo | provisionnement, import, analyse, rapport, audit |
| isolation | autre organisation, RLS applicative et worker |
| résolution | le worker résout, `admin` ne peut pas (D-062) |
| **effacement** | **le vrai worker détruit les octets porteurs d'identité (D-064)** |
| SIGTERM | arrêt propre |
| **préflight** | **un magasin en lecture seule refuse de servir l'effacement** |
| fuites | sorties et journaux de **tous** les conteneurs |
| nettoyage | conteneurs, réseaux et volumes du projet |

Durée du job : **2 min 47 s**. Le job a franchi les étapes qu'aucun run antérieur n'avait atteintes.

## P. Preuve Linux read-only / EROFS

Deux preuves distinctes, de portées différentes, à ne pas confondre :

1. **Linux, dans la CI.** L'étape « un magasin d'objets en lecture seule refuse de servir
   l'effacement » est exécutée dans le job `docker` du run de référence, sur Linux : un worker servant
   `redact_customer` avec un magasin incapable de détruire **refuse de démarrer**. C'est le préflight
   fail-closed de D-064 point 4, prouvé dans le runtime réel.
2. **Darwin/HFS+, localement.** Un vrai système de fichiers HFS+ monté en lecture seule a été utilisé.
   Répertoire en mode `0755` — donc **les permissions seules indiquaient « inscriptible »** — et
   pourtant `os.access(W_OK | X_OK)` faux, `unlink` en `errno 30 / EROFS`, et
   `delete_capability() == incapable`. Cette preuve ferme le risque du côté Darwin ; elle ne
   remplace pas la preuve Linux ci-dessus, et n'est pas reproduite par la CI.

La machine de développement locale ne possède **aucun runtime de conteneur** (ni docker, ni podman,
ni colima, ni lima, ni nerdctl, ni finch) : **toutes** les gates Docker sont donc validées par la CI
GitHub, et par elle seule.

## Q. Nettoyage et absence de résidus

Deux contrôles indépendants, tous deux passés dans le run de référence :

1. l'étape de nettoyage du smoke lui-même (conteneurs, réseaux et volumes du projet) ;
2. une étape CI **distincte du script**, « No container, volume or network left behind », qui
   interroge directement le démon sur le label `com.docker.compose.project` et exige un résultat vide
   — zéro conteneur, zéro volume, zéro réseau résiduel.

## R. Suite de tests complète

| Métrique | Valeur |
|---|---|
| Total | **2804 passés** |
| Ignorés | **0** |
| Échecs / erreurs | **0** |
| Baseline avant la mission | 2468 |
| Ajoutés par 004.4.5 | **+336** |

`MERVIO_EXPECTED_TESTS` est un **nombre exact** contrôlé par la CI (étape « Test gate (exact count,
zero skipped, required modules executed) ») : passé de `2468` à `2804`. Toute dérive, dans un sens ou
dans l'autre, casse la CI.

Reproduction locale sur la base réelle, avant le commit final :

```bash
MERVIO_TEST_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres \
MERVIO_REQUIRE_DATABASE_TESTS=1 .venv/bin/python -m pytest -o addopts="" -q
```

Résultat : `2804 passed in 257.46s`.

## S. Référence CI

| Champ | Valeur |
|---|---|
| Workflow | `ci` (`.github/workflows/ci.yml`) |
| Run | **#28** |
| Run ID | **`36263666353`** |
| SHA de tête | `720404d2e03856615e92f5a61b78b6c2792e1440` |
| Conclusion | **`success`** — 4 jobs sur 4 |
| `test` | ✅ 2804 passés / 0 ignoré ; `pip check` ; licences du verrou ; migrations sur base neuve (montée, descente, remontée, schéma identique) ; porte de comptage exact |
| `security` | ✅ gitleaks sur l'**historique complet** (binaire vérifié) ; `pip-audit` sur le verrou et sur l'outil de build d'image |
| `lint` | ✅ `ruff E9,F63,F7,F82` sur `src tests scripts benchmarks` + compilation de chaque fichier Python |
| `docker` | ✅ build (image de base épinglée, dépendances verrouillées), smoke conteneurisé, absence de résidus — 2 min 47 s |

**Historique CI de la mission** (branche `mission-004.4`) :

| Run | ID | SHA | Conclusion |
|---|---|---|---|
| #17 | `36060372891` | `701a5d1` | success |
| #18 | `36087030179` | `f27e618` | success |
| #19 | `36157740326` | `e2e34bf` | success |
| #20 | `36203216454` | `810cb5f` | success |
| #21 | `36220631183` | `6cd74f8` | failure |
| #22 | `36249359210` | `93f0f23` | success |
| #23 | `36253593517` | `dbe806d` | success |
| #24 | `36258539901` | `9cd36ae` | **failure** — BUG #1 |
| #25 | `36260057101` | `3157359` | **failure** — BUG #2 |
| #27 | `36261694617` | `8ddc24b` | **failure** — BUG #3 |
| **#28** | **`36263666353`** | **`720404d`** | **success** |

**BUG #1 — le préflight D-064 refusait les conteneurs négatifs du smoke.** Le nouveau préflight
s'exécute **avant** la base et refusait un conteneur ad-hoc sans volume d'objets, faisant échouer un
contrôle d'identité *négatif*. Correction : les conteneurs négatifs conservent les types de travaux
qui leur sont nécessaires — `import` **conservé** (il rend la clé maître obligatoire, ce dont le
contrôle dépend), `redact_customer` **retiré**. Bug du smoke.

**BUG #2 — le smoke lisait des compteurs volontairement masqués.** Le vrai worker avait bien exécuté
l'effacement (`orders_tombstoned = 1`), mais le smoke lisait `raw_objects_purged` /
`raw_objects_already_purged` via `admin job show --json`, où `scrub()` masque toute clé contenant
« raw » pour qu'aucune donnée brute ne ressorte par la CLI. Les valeurs apparaissaient `[redacted]`.
Correction : les assertions sur ces deux compteurs sont retirées et la preuve est prise **à la
source** — état des lignes et contenu du volume. `scrub()` n'a **pas** été modifié et les compteurs
n'ont **pas** été exposés ; un test de garde verrouille le masquage comme volontaire (`tests/test_container_smoke.py::test_the_smoke_never_asserts_on_the_deliberately_masked_raw_counters`). Bug du smoke.

**BUG #3 — le contrôle d'audit filtrait le mauvais vocabulaire.** Voir §N. Bug du smoke.

**Aucune des trois corrections n'a modifié le produit pour faire passer un contrôle.** Le correctif a
chaque fois porté sur le contrôle lui-même.

## T. Résiduels acceptés

Aucun n'est une exigence non tenue ; **une CI verte n'en referme aucun.**

1. **Compromission du worker → destruction ou écrasement des octets bruts de tous les locataires du
   volume partagé.** ⚖️ Accepté par D-064 : D-056 **exige** cette capacité, et l'isolation locative
   n'en dépend pas. Le worker et `admin` s'exécutent sous le **même uid** (`10001`) : `rw` confère au
   worker l'autorité de fichier qu'`admin` possédait déjà, ni plus, ni moins.
2. **TOCTOU du pilote filesystem : réduit, non annulé.** `O_NOFOLLOW` couvre la **lecture** et le
   **dernier** composant du chemin ; `put` et `delete` résolvent toujours avant d'agir.
3. **Politique IAM S3 restreignant `s3:DeleteObject` non implémentée.** ⚖️ Consignée comme exigence
   de **004.9**. En conséquence, `delete_capability()` de S3 rend `undetermined` et le worker démarre
   en journalisant un avertissement : le fail-closed **ne protège pas** un déploiement S3 mal
   autorisé, par choix d'honnêteté plutôt que par un drapeau déclaratif.
4. **Absence de garantie de non-écrasement de `put`** (D-055, reportée à 004.9) : le worker en est
   techniquement capable, aucun de ses chemins de code ne le fait.
5. **Fichiers de longueur nulle ou orphelins** possibles après interruption — ramassage différé à
   **004.9** par D-054 et D-061.
6. **Compteurs `raw_objects_*` non observables par la CLI.** Masquage **volontaire** de `scrub()`,
   conservé tel quel ; la preuve de destruction est prise à la source (§J). Un test de garde le verrouille.
7. **La résolution d'identité n'est pas auditée par le produit** (D-062, assumé) : qui peut exécuter
   un processus dans le conteneur `worker` peut ré-identifier. La trace est celle du conteneur et du
   système. Restreindre `docker exec` sur ce service en conséquence.
8. **Les transactions de finalisation d'objets sont hors couverture du verrou D-063**, délibérément
   (D-063 §2). Seule la transaction de tombstone porte le verrou exclusif.

## U. Hors périmètre — à ne pas confondre avec une preuve

Ces points ne sont **pas** validés par cette acceptance et ne doivent pas être présentés comme tels :

- **Déploiement en production.** La preuve est apportée par le job `docker` de la CI. Une preuve de
  runtime Docker **n'est pas** une preuve de déploiement en production.
- **S3 hébergé.** Le pilote S3 n'est exercé que par **`moto`**, en processus : aucun bucket réel,
  aucun compte AWS, aucun réseau, **aucune politique IAM vérifiée**. Les écarts entre `moto` et le
  fournisseur réel restent un résiduel connu (D-055, validation en 004.9).
- **Performance et charge.** Aucune campagne de charge n'a été menée sur l'effacement. Les seuls
  chiffres cités (§C) proviennent de la campagne D-063 et portent sur le verrou et le rejeu à
  l'import.
- **Couverture de code.** Aucune mesure de couverture n'est produite ni revendiquée ; les comptes
  cités sont des nombres de **tests**.
- **004.4.6** — purge d'organisation, destruction du sel d'identité : **non commencée**.

## Verdict

**004.4.5 — ACCEPTED / CLOSED / RATIFIED**, avec les résiduels de §T et les limites de §U.

La condition de clôture de D-064 est satisfaite, point par point :

| Condition de clôture | État |
|---|---|
| D-064 ratifiée | ✅ `dbe806d` |
| Les sept points d'implémentation complets | ✅ §E |
| CI verte sur `test`, `security`, `lint`, `docker` | ✅ run #28 `36263666353` |
| Exécution réelle de l'effacement dans Docker (acceptance 6) | ✅ §O |
| Destruction physique des octets vérifiée, **non inférée** | ✅ §J — présence constatée avant, absence constatée après |
| Aucune régression sur 004.4.4, E5 et E6 | ✅ §R — 2804 / 0 ignoré, porte de comptage exact |

**D-064 : implémentée.** **004.4.5 : close et ratifiée.** **004.4.6 : non commencée.**
