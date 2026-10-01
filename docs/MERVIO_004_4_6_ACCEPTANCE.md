# MERVIO 004.4.6 — ACCEPTANCE

**Date :** 2026-09-30 · **Mission :** 004.4.6 — Purge de boutique et purge d'organisation :
clôture, destruction, pierre tombale, reprise (D-051, D-065 à D-069, D-071 ; D-070 **non traitée**)

## Status

**ACCEPTED / CLOSED / RATIFIED — avec résiduels**

Revue formelle sur le SHA figé. Les résiduels listés en §I sont explicitement acceptés par les
décisions ; aucun n'est une exigence non tenue, à une réserve près, nommée et non euphémisée : la
**détection** d'une purge figée est entièrement manuelle (§I.1). D-051 est honorée de bout en bout
— une organisation peut être détruite par un travail audité — et D-053 l'est avec elle : le sel
d'identité est détruit, et il ne l'est que dans une organisation close.

**Ce document ne revendique pas** de preuve de runtime Docker pour les purges tenant : voir §J.

## Baseline

`b101547` — `docs(004.4.5): ratify customer erasure acceptance`, dernier commit antérieur à
004.4.6 et dernière CI entièrement verte avant la mission (run #29, `36268645711`, 2804 tests).

## Final commit

`e1a68efa8d591f7058eba1235297a7d934e9567f` — `feat(persistence): finalize tenant purge recovery`.
Branche `mission-004.4`, identique à `origin/mission-004.4` ; arbre de travail propre.

Périmètre complet de la mission (`b101547..e1a68ef`) : **24 fichiers, +4469 −42**.

| Catégorie | Fichiers | Lignes |
|---|---|---|
| `tests/` | 13 | +2613 −19 |
| `src/` (code produit) | 9 | +1794 −22 |
| `docs/` | 1 | +61 −0 |
| Infrastructure (`ci.yml`) | 1 | +1 −1 |

Commits de la mission, dans l'ordre :

| SHA | Objet |
|---|---|
| `f38e78f` | `docs(004.4.6)` — ratification de D-065 (huit arbitrages de purge) |
| `75ebf81` | `docs(004.4.6)` — ratification de D-066 (exception de rétention pendant la purge) |
| `60505e1` | `feat(persistence)` — révision `0016` : fondation en base, et clôture du résiduel F-1 |
| `790c8db` | `feat(persistence)` — chemins worker : gestionnaires, façade, commandes de mise en file |
| `e1a68ef` | `feat(persistence)` — reprise : D-067, D-068, D-069, D-071, garde anti-boucle, observabilité |

**Fait à ne pas embellir :** `60505e1` et `790c8db` **n'ont pas de run CI propre** — vérifié, la
liste des runs de la branche n'en contient aucun pour ces deux SHA. Les trois commits
d'implémentation ont été poussés ensemble et sont validés par **une seule** exécution, celle de la
tête (`36793763636`). Les valeurs intermédiaires de `MERVIO_EXPECTED_TESTS` (2850, puis 2873)
n'ont donc **jamais** été exercées par la CI. Conséquence : la granularité de bissection promise
par l'historique n'existe pas sur cette mission, et §I.7 la porte comme résiduel.

---

## A. Scope

Livrer la purge de locataire — boutique et organisation — telle que D-051 l'exige et telle que
D-065 et D-066 l'ont arbitrée, puis fermer les courses que l'implémentation a révélées.

1. **D-051** — purge d'organisation : pierre tombale, audit conservé. Achevée.
2. **D-053** — destruction du sel d'identité d'organisation. Achevée, et resserrée par D-069.
3. **D-065** — les huit arbitrages d'implémentation (Q1–Q8). Tous livrés.
4. **D-066** — exception bornée au seul plancher temporel de rétention. Livrée dans la politique.
5. **D-067, D-068, D-069** — décisions **nées de la revue de concurrence**, ratifiées et livrées
   dans la même révision.
6. **D-071** — observabilité minimale du cycle de vie.
7. **F-1** — résiduel 004.4.1 : élargissement de l'inventaire anti-`pg_temp` aux cibles
   d'**écriture**, dont l'échéance était « au plus tard 004.4.5 / 004.4.6 ». **Fermé** (§C.5).

**D-070** — audit des refus de réconciliation — est **explicitement hors périmètre et non
implémentée** : `0016` l'écrit en tête (« D-070 (audit des refus) n'est pas traitée ici »), et deux
tests **constatent** son absence plutôt que de la masquer. Voir §H.4 et §I.3.

Hors périmètre, explicitement : le ramassage des orphelins, l'expiration de `retain_until`,
l'écriture conditionnelle S3 et la politique IAM `s3:DeleteObject` (**004.9**) ; l'enchaînement
import → analyse (**004.6**, D-059).

## B. Architecture / implémentation

### B.1 Révision `0016_tenant_purge` (revises `0015_raw_object_purge`)

| Étage | Contenu livré |
|---|---|
| État de locataire | `status` (`active`/`purging`/`purged`) et `purged_at` sur `organizations` **et** `stores`, avec `<table>_status_known` et `<table>_purge_consistent` (`(status = 'purged') = (purged_at IS NOT NULL)`) |
| Types de travaux | `purge_store`, `purge_organization` ajoutés au domaine `jobs.job_type` par le mécanisme `_replace` de `0008`/`0013`/`0014` |
| Vocabulaire d'audit | quatre actions nouvelles (D-065 Q1), sur des types de ressource **existants** : `store.purge_started`, `store.purged`, `organization.purge_started`, `organization.purged` |
| Motifs de purge | `store_purge`, `organization_purge` ajoutés au domaine `raw_objects.purge_reason` |
| Déclencheur de clôture | `tenant_closure_guard()` — une organisation ou une boutique close n'accepte plus aucune écriture, avec l'exception de reprise de D-068 |
| Politique de rétention | `jobs_purge_terminal_only` remplacée : I1 et I2 intactes, **seul** le plancher d'une heure amendé pendant `purging` (D-066) |
| Unicité | `jobs_tenant_purge_active_uniq` — index unique partiel (D-067) |
| Opérations privilégiées | **huit** fonctions `SECURITY DEFINER` + **un** helper `SECURITY INVOKER` |

### B.2 Les huit opérations privilégiées (D-052, D-065 Q2)

`app_close_organization`, `app_close_store`, `app_destroy_identity_key`,
`app_purge_finalize_raw_object`, `app_purge_tenant_data`, `app_tombstone_organization_stores`,
`app_tombstone_organization`, `app_tombstone_store`.

Toutes : possédées par le **propriétaire du schéma**, **aucun rôle nouveau**,
`REVOKE ALL ... FROM PUBLIC` puis `EXECUTE` au seul `mervio_worker`,
`SET search_path = pg_catalog, public, pg_temp`, relations qualifiées `public.` (D-049).

La batterie de gardes D-052 est portée par **`app_purge_guard(job_id, job_type)`**, et c'est un
arbitrage mesuré, non une commodité : à huit corps, la propriété « les batteries sont
équivalentes » n'était garantie par rien de structurel, et D-065 consignait déjà la dérive comme
risque résiduel à **deux** corps. Le helper **n'ajoute aucune surface privilégiée** :

- il est **`SECURITY INVOKER`** — appelé depuis un définisseur il s'exécute avec les privilèges du
  définisseur (mesuré : `current_user` vaut le propriétaire dans le helper aussi) et `session_user`
  y est **préservé**, de sorte que la garde d'identité de service, la plus subtile de D-052,
  fonctionne à l'identique ;
- il porte son **propre** `SET search_path` et qualifie toutes ses relations, bien que le `SET` de
  l'appelant soit déjà hérité (mesuré) : une relecture ne doit pas avoir à raisonner sur l'héritage ;
- `REVOKE ALL ... FROM PUBLIC`, **aucun `GRANT`** : `mervio_app` et `mervio_worker` obtiennent
  `permission denied` à l'appel direct (mesuré), tandis que les définisseurs continuent de
  l'appeler, le propriétaire conservant son `EXECUTE` implicite.

`app_redact_customer` (`0014`) et `app_finalize_raw_object_purge` (`0015`) **ne sont pas touchées**
et conservent leur batterie inlinée : D-065 Q2 l'exige, chaque fonction devant rester auditable
seule. **Trois** corps restent donc à maintenir équivalents — porté en §I.5.

### B.3 Séquences livrées

**`purge_store`** — clôture (boutique → `purging`, travaux en file annulés, objets rendus
illisibles, audit `store.purge_started`) → destruction des octets (`ObjectStore.delete`,
idempotente, objet par objet) → retrait des lignes `raw_objects` → données (rapports → exécutions →
instantanés par CASCADE → connexions) → pierre tombale (boutique → `purged`, audit `store.purged`).

Ce que `purge_store` **ne touche pas**, et c'est ratifié : le sel d'identité de l'organisation
(D-065 Q4), **même si cette boutique est la dernière** ; et les preuves `customer_redactions`
(Q5), qui sont à l'échelle de l'organisation et attestent d'un effacement demandé par une personne.
L'organisation reste vivante.

**`purge_organization`** — clôture → **le sel** (D-065 Q4, D-053 ; **ici seulement**) → octets puis
lignes `raw_objects` → données (… → preuves d'effacement → travaux, dans la fenêtre D-066) →
boutiques tombstonées une par une → pierre tombale de l'organisation (audit `organization.purged`).

Deux propriétés d'ordre, délibérées : **le sel avant les données**, pour que les références client
encore présentes deviennent définitivement non réversibles **avant** d'être supprimées ; **le
nettoyage des travaux avant la pierre tombale**, parce qu'après `purged` la fenêtre D-066 se
**referme** et l'étape 10 de D-051 redevient impossible.

### B.4 Ce que le schéma réel impose, et qui a été mesuré

- **C1** — un `DELETE` **direct** sur une table canonique ou sur `snapshot_sources` d'un instantané
  **scellé** est refusé par `canonical_rows_guard_sealed` ; le `DELETE` sur `data_snapshots`
  réussit et le CASCADE emporte sources et lignes canoniques. La purge passe donc par
  `data_snapshots` et **ne supprime jamais une canonique directement**.
- **C2** — `data_snapshots` porte une FK auto-référente `supersedes` en RESTRICT : supprimer un
  instantané superseded **seul** échoue ; les supprimer **tous en un seul `DELETE`** réussit. D'où
  un `DELETE` unique couvrant toute la portée, **jamais** un traitement par lots.

### B.5 Dépendance non enregistrée par le moteur

Mesure : PostgreSQL n'enregistre **aucune** entrée `pg_depend` entre une fonction plpgsql et les
fonctions qu'elle appelle — un corps plpgsql est une chaîne opaque. Supprimer `app_purge_guard`
avant ses appelants **réussit** sans erreur et laisse huit fonctions privilégiées pointant dans le
vide, détectables seulement au premier appel. La descente retire donc les **huit définisseurs avant
le helper**, et un test verrouille cet ordre : la garantie vient de la migration et du test, jamais
du moteur.

## C. Security

### C.1 Aucun privilège élargi, aucun rôle nouveau

Aucun rôle PostgreSQL créé. Aucun `GRANT` nouveau à `mervio_app` au-delà de ce que `0001`
accordait ; `SELECT` et l'`INSERT` des autres colonnes restent inchangés, `UPDATE (name)` et
`UPDATE (name, currency)` ne sont pas touchés. `EXECUTE` sur les neuf fonctions nouvelles :
`REVOKE ALL ... FROM PUBLIC`, puis `mervio_worker` seul pour les huit définisseurs, **et personne**
pour le helper.

### C.2 Les colonnes de cycle de vie sont inaccessibles au rôle applicatif

`status` et `purged_at` ne reçoivent **aucun `GRANT UPDATE`** : `mervio_app` ne peut pas écrire
`status = 'purging'`, donc **ne peut pas ouvrir l'exception D-066 lui-même**. C'est ce qui la borne.

Mesure consignée dans la migration : `REVOKE INSERT (status) ... FROM role` est **sans effet**
quand `INSERT` a été accordé au niveau **table**. `0016` révoque donc l'`INSERT` de table et le
réaccorde **colonne par colonne**, en excluant `status` et `purged_at` — sans quoi `mervio_app`
aurait pu créer une organisation déjà `purging`. La descente restaure l'`INSERT` de table.

### C.3 Aucun GUC n'est une autorisation

L'ancre de D-066 et des préconditions est l'état **durable** `organizations.status`. **Aucun** GUC
`app.*` n'intervient : ils sont positionnables par n'importe quel rôle (`set_config`) et ne portent
qu'un **contexte**, jamais une autorisation. Un GUC forgé ne produit aucun effet, la condition
n'en lisant aucun — vérifié par le test 7 de D-066.

### C.4 Immunité au shadowing `pg_temp`

Deux mécanismes distincts, et il importe de ne pas les confondre :

- **Expression de politique** — résolue au `CREATE POLICY`, elle **stocke des OID** et n'est pas
  ré-résolue à l'exécution. Vérifié avec témoin sur une base appartenant à un rôle
  **non-superutilisateur**, table en `FORCE ROW LEVEL SECURITY` : une table `pg_temp` homonyme
  disant « vrai », placée en tête de `search_path`, ne dupe pas la politique ; en basculant la
  **vraie** table, le `DELETE` est accepté — ce qui prouve que la politique lit bien la relation
  réelle. L'immunité vient du **stockage**, non de l'écriture qualifiée. La qualification
  `public.organizations` est néanmoins **exigée** par D-066, par cohérence avec D-049.
- **Corps plpgsql** — ré-résout ses noms à l'exécution. D'où `SET search_path` **et** la
  qualification `public.` sur les neuf fonctions, sans exception.

### C.5 F-1 — résiduel 004.4.1 : FERMÉ, et démontrable

F-1 exigeait d'élargir l'inventaire anti-`pg_temp` « au plus tard avec l'introduction des fonctions
privilégiées d'écriture (D-052, 004.4.5 / 004.4.6) », l'inventaire ne détectant jusque-là que les
**lectures** (`FROM`/`JOIN`). 004.4.5 a introduit deux fonctions sans l'élargir — sans défaut réel,
toutes leurs cibles étant qualifiées, mais avec une garde plus étroite que le risque.

**004.4.6 le ferme**, dans `60505e1` (`git log -S` sur les deux symboles le confirme) :
`tests/persistence/test_pg_temp_residual.py` porte désormais

```
_DESIGNATIONS = r"(?:FROM|JOIN|USING|UPDATE|INSERT\s+INTO)"
```

couvrant la cible d'une insertion, la cible d'une mise à jour et la source d'un `DELETE ... USING`
(`DELETE FROM` et `UPDATE ... FROM` étaient déjà couverts par `FROM`). Deux tests l'exploitent :
`test_no_public_function_names_a_relation_by_an_unqualified_name` applique l'inventaire à **toutes**
les fonctions de `public` et exige un résultat vide ; `test_the_inventory_would_catch_an_unqualified_write_target`
**vérifie la garde elle-même** sur des corps fabriqués, forme par forme, et vérifie qu'une cible
qualifiée n'est pas signalée — sans quoi un motif cassé passerait pour un succès.

Le commentaire du fichier cite F-1 nommément et date sa clôture. **F-1 n'est plus ouvert.**

### C.6 Inventaire du privilège

`test_the_hygiene_changes_no_privilege_and_adds_no_definer` énumère les fonctions
`SECURITY DEFINER` de `public` : toute fonction **autre** que celles attendues fait échouer le test.
`app_purge_guard` en est **absente**, et ce n'est pas un oubli — elle est `SECURITY INVOKER`.

### C.7 Aucun `SET ROLE` ajouté — vérifié

`grep -rn 'SET ROLE'` sur `src/` rend **zéro occurrence**. Les seules mentions du dépôt sont :
D-052, qui range `SET ROLE` vers un rôle de purge parmi les options **rejetées** (« le worker
pourrait basculer à tout moment ; rôle global ») ; et
`test_tenant_purge_recovery.py::test_the_recovery_path_uses_no_privileged_shortcut`, qui asserte
`"SET ROLE" not in body` sur le code source réel du chemin de reprise. Il n'existe **aucun** `SET
ROLE` dans Mervio, ni ajouté par cette mission, ni hérité d'une révision antérieure.

### C.8 Le worker reste strictement plus contraint

`jobs_service_purge_by_owner` (RESTRICTIVE, `TO mervio_worker`, rang délégué ≥ 3) s'ajoute **par
ET** à la politique D-066 amendée : le worker reste strictement plus contraint, jamais moins.
`SECURITY DEFINER` **ne contourne pas la RLS** — toutes les tables touchées sont en RLS forcée,
donc le propriétaire du schéma y est soumis et chaque fonction reste confinée à une organisation
**par PostgreSQL**, pas par une condition Python. `Database` continue de refuser toute connexion
superutilisateur ou `BYPASSRLS`.

## D. Concurrency

### D.1 D-063 : intacte

**Aucune portée de verrou ajoutée, aucune seconde primitive de concurrence.** `0016` l'écrit en
tête. Le verrou consultatif d'organisation garde exactement son rôle : partagé pour l'import,
exclusif pour la destruction, pris **dans la transaction**. Les purges tenant le prennent par
`pg_advisory_xact_lock(hashtextextended(organization_id || ':org', 0))`, la même clé que
l'effacement client, de sorte qu'un import en vol et une purge sont totalement ordonnés.

### D.2 Le dead-end mesuré, et les trois réponses

La revue de concurrence a mesuré que deux purges tenant concurrentes d'une même organisation
**détruisaient toutes deux** les données puis **échouaient toutes deux** à la pierre tombale —
chacune voyant survivre le travail de l'autre, protégé par D-066 I1 — laissant l'organisation
`purging`, le sel détruit et **plus aucune mise en file possible**. 20 échecs sur 20. Elle a ensuite
mesuré que le même blocage naissait de **n'importe quel** travail en vol, des **cinq** types, et non
des seules purges.

| Décision | Mécanisme | Étage |
|---|---|---|
| **D-067** | `jobs_tenant_purge_active_uniq`, index unique partiel sur `(organization_id)` où `job_type IN ('purge_store','purge_organization')` et `status IN ('queued','running')` | moteur de stockage, **à l'insertion** |
| **D-068** | barrière de drain → `object_in_use` (**`55006`**), transitoire donc **repris** ; travail terminal résiduel → `restrict_violation` (**`23001`**), définitif ; + exception de reprise du déclencheur | fonction privilégiée + déclencheur |
| **D-069** | `app_destroy_identity_key` exige `organizations.status = 'purging'` | fonction privilégiée |

**D-067** tranche la course **au seul point où rien n'est encore détruit** : le perdant reçoit
`23505` à l'`INSERT`, qu'`admin` rend comme `already_active`. La clé étant l'`organization_id`
seul, `purge_store` et `purge_organization` sont mutuellement exclusives **sans qu'aucune n'ait à
connaître l'autre**. Les travaux ordinaires et les autres organisations ne sont pas contraints ; un
travail terminal sort du prédicat, ce qui rend une reprise possible.

**D-068** porte la barrière dans **quatre** fonctions — `app_destroy_identity_key` (première
destruction irréversible : la barrière est là, pas plus loin), `app_purge_tenant_data` (**portée
organisation seulement** : une purge de boutique ne porte pas la barrière des pierres tombales,
donc ne produit pas le blocage), `app_tombstone_organization_stores`, `app_tombstone_organization`
— et **pas** dans `app_close_*`, qui doivent pouvoir commiter pour **fermer l'entrée**. Le choix de
`55006` est une **lecture du dépôt**, non une convention nouvelle : il figure déjà dans
`RETRYABLE_SQLSTATES` de `persistence/retry.py`, donc `classify` le rend reprenable **sans qu'aucune
ligne de Python ne change**. L'ordre des deux contrôles compte : le cas **transitoire se teste
d'abord**, sans quoi un travail terminal masquerait un travail en vol et rendrait définitif ce qui
était reprenable.

**Le drain est borné, et ce n'est pas une fenêtre TOCTOU** : la clôture ayant fermé l'entrée,
l'ensemble des travaux actifs ne peut plus que **décroître**. Ce que la barrière observe vide reste
vide.

**D-069** ferme une asymétrie mesurée : `app_destroy_identity_key` détruisait le sel d'une
organisation restée `active`, là où `app_purge_tenant_data` refusait déjà. Au-delà du défaut propre,
cette absence privait la barrière de D-068 de sa **prémisse** — l'entrée fermée — donc de sa sûreté.
`active` → refus · `purging` → autorisé · `purged` → refus.

### D.3 Scénarios de course exécutés

`tests/persistence/test_tenant_purge_concurrency.py` — **60 tests**, sur **deux connexions réelles**
synchronisées par barrière, jamais sur un ordre supposé :

- **C1** — deux `purge_organization` concurrentes. Reproduisait le dead-end **20 fois sur 20** avant
  D-067 ; une seule détruit désormais, l'autre échoue à l'insertion.
- **C2** — `purge_store` + `purge_organization`. Même dead-end, et il passait **inaperçu** : la purge
  de boutique réussissait pendant que l'organisation restait figée.
- **C3** — même boutique, deux fois. Ne produisait pas de dead-end (`app_tombstone_store` ne porte
  pas la barrière), et reste refusé : une purge redondante n'a aucun sens.

Le fichier documente aussi la propriété qui rend l'ensemble cohérent : **le verrou de D-063 est un
verrou de TRANSACTION**, et chaque opération privilégiée est **sa propre** transaction ; ce qui
sérialise les étapes entre elles est le **STATUT** (`purging`), pas le verrou — exactement ce que
D-063 point 2 écrit.

### D.4 Effacement client et purge

D-065 Q7 est inchangée et **renforcée** : un `redact_customer` `queued` est annulable par la
clôture ; un `running` ne l'est pas et reste **sérialisé** par D-063 ; et I1, non amendée, garantit
qu'il ne peut pas non plus être **supprimé** pendant la fenêtre D-066.

## E. Recovery

### E.1 Pourquoi la reprise vit dans `admin`, et pas dans le worker

`0007` (SEC-06) impose `jobs_service_no_insert`, RESTRICTIVE `WITH CHECK (false)` sur
`mervio_worker` : **un worker n'a jamais le droit de créer un travail**. La boucle de
réconciliation du worker ne pouvait donc pas remettre une purge en file, et l'y autoriser aurait
exigé d'**amender une frontière de sécurité ratifiée**. La reprise vit donc dans `mervio admin`,
sous l'identité d'un humain `owner`, **par la mise en file nominale** — sans `SET ROLE`, sans
`SECURITY DEFINER`, sans insertion directe. Un test de **forme** le verrouille sur le code source
réel (`test_the_recovery_path_uses_no_privileged_shortcut`) : ni `SET ROLE`, ni `SECURITY DEFINER`,
ni `INSERT INTO`, ni `on_behalf_of` — « l'audit ne sert jamais d'entrée d'autorisation ».

### E.2 Aucune identité usurpée

La purge remise en file est autorisée par l'acteur `--as`, dont le rang `owner` est relu **en base
ici, puis de nouveau par `app_purge_guard` à l'exécution**. Le demandeur d'origine ne sert qu'à
l'historique et à la corrélation. L'audit émis est `job.enqueued`, `actor_type = user`, acteur =
**l'humain qui a repris**.

### E.3 Garde anti-boucle D + B + A, délibérément hors base

Aucune contrainte PostgreSQL ne la porte, **et c'est la propriété qui compte** : une garde en base
créerait un nouveau dead-end, alors qu'ici `job enqueue-purge-organization` reste **ouvert quoi
qu'elle décide**. Elle ne demande aucune colonne : son état entier se relit depuis `jobs`, donc elle
survit à un arrêt brutal du processus comme à un changement de machine.

| Lettre | Règle | Valeur | Provenance |
|---|---|---|---|
| **D** | fail-closed : `55006` est le **seul** refus transitoire ; tout autre code — y compris un code inconnu aujourd'hui — arrête la reprise automatique | `{sqlstate_55006}` | D-068 |
| **B** | plafond d'échecs, la purge d'origine **comprise** → au plus **deux** reprises automatiques | 3 | — |
| **A** | délai minimal entre deux reprises, ancré sur `finished_at` du dernier échec | 3600 s | `jobs.BACKOFF_CAP_SECONDS`, **reprise du dépôt**, jamais inventée |

**L'ordre d'évaluation n'est pas indifférent** : une cause déterministe arrête **avant** que le
plafond ne soit consommé, sans quoi la garde B brûlerait deux reprises inutiles et deux traces.

### E.4 Budget de tentatives

`PURGE_RECOVERY_MAX_ATTEMPTS = 7` pour une purge d'organisation, au-delà du défaut de 3. Motif
chiffré, repris du commentaire de `admin/operations.py` : un intrus dont le worker est mort n'est
repris qu'après son bail, soit au pire 3 × (`MERVIO_JOB_LEASE_SECONDS` 300 s +
`MERVIO_WORKER_RECOVERY_INTERVAL_SECONDS` 30 s) = 990 s **aux valeurs par défaut**, que seul un
budget de 7 domine (1890 s). **Ce n'est pas une garantie générale** — voir §I.4.

### E.5 Chemin manuel, et absence de `--force`

`mervio admin job enqueue-purge-organization` est **aussi** le chemin de reprise **manuelle** :
aucune garde anti-boucle ne s'y applique, elle appartient à `org reconcile-purges`. Le déclencheur
de clôture l'autorise par l'exception de D-068, dont la portée est `jobs` seule,
`purge_organization` seul, `purging` seul.

**Il n'existe aucune option `--force`**, nulle part : ni sur `reconcile-purges`, ni sur les deux
commandes de mise en file. `purged` reste **terminal** — rien n'y entre, pas même une purge — et
aucune commande ne propose de l'outrepasser.

### E.6 Vocabulaire d'issue

`recovered` · `would_recover` · `already_active` · `not_purging` · `terminal` · `owner_invalid` ·
`permission_denied` · `guard_exhausted` · `deterministic_failure` · `no_requester` · `too_soon`.

`reconcile-purges` est une commande d'**inspection** : elle aboutit tout en rapportant des
obstacles, et rend alors le code **le plus sévère** rencontré — `6` pour `owner_invalid` /
`permission_denied`, `7` pour `guard_exhausted` / `deterministic_failure` / `no_requester` /
`too_soon`, `0` sinon. En `--json`, `ok` suit le code, jamais l'inverse.

## F. Tests

| Métrique | Valeur |
|---|---|
| Total | **2982 passés** |
| Ignorés | **0** |
| Échecs / erreurs | **0** |
| Baseline avant la mission | 2804 |
| Ajoutés par 004.4.6 | **+178** |

`MERVIO_EXPECTED_TESTS` est un **nombre exact** contrôlé par la CI (étape « Test gate (exact count,
zero skipped, required modules executed) ») : passé de `2804` à `2982`. Toute dérive, dans un sens
ou dans l'autre, casse la CI. Vérifié localement sur le SHA figé : `pytest --collect-only` rend
**2982 tests collected**, soit exactement la valeur de `ci.yml`.

Suites dédiées à la purge tenant, comptées sur le SHA figé :

| Fichier | Tests | Ce qu'il défend |
|---|---|---|
| `tests/persistence/test_tenant_purge.py` | **68** | invariants D-051, D-065, D-066 ; bornage de l'exception (tests 1–4, 13, 14) ; §9 D-067 ; §10 D-068 ; §11 D-069 |
| `tests/persistence/test_tenant_purge_concurrency.py` | **60** | courses réelles C1/C2/C3, deux connexions, barrière ; dead-end d'avant D-067 reproduit |
| `tests/persistence/test_tenant_purge_recovery.py` | **26** | chemin `reconcile-purges`, D-068, D-071, et les **deux constats D-070** |
| `tests/persistence/test_migration_0016.py` | **7** | aller-retour, restauration de la politique **au caractère près** |
| `tests/persistence/test_tenant_purge_handler.py` | **6** | chemin **réel** du worker |
| **Total dédié** | **167** | |

Le solde entre +178 et 167 vient des suites **étendues**, non créées :
`test_pg_temp_residual.py` (**10**, dont la clôture F-1), `test_jobs_query_plans.py`,
`test_persistence_migrations.py`, `test_migration_0014.py`, `test_dispatch_probe_plan.py`,
`test_admin_operations.py`, `tests/test_cli_admin.py`, `tests/test_worker_delete_preflight.py`.

**Les quatorze tests d'acceptance obligatoires de D-066 sont écrits**, sur PostgreSQL réel, rôles
non-superutilisateur, sans `BYPASSRLS`. Les deux derniers — `purging` + travail `queued` **refusé**,
`purging` + travail `running` **refusé** — ne sont pas optionnels : sans eux l'amendement ne serait
pas borné.

## G. Migration / rollback

**Aller-retour mesuré** sur une empreinte riche : fonctions avec **md5 du corps**, ACL et
`proconfig` ; politiques avec expression complète ; contraintes ; déclencheurs avec leurs
arguments ; colonnes ; privilèges de **colonne** ; RLS.

- **`UP` est DÉTERMINISTE** : UP1 == UP2.
- **`jobs_purge_terminal_only` revient IDENTIQUE au caractère près**, comme D-066 l'**exige** —
  mêmes nom, type RESTRICTIVE, commande `FOR DELETE`, absence de clause `TO`, condition
  textuellement identique à celle de `0004`. **`0004` n'est pas modifiée.**
- Ordre de descente : les **huit définisseurs**, puis le **helper** (§B.5), puis le déclencheur,
  puis la politique D-066 remplacée par la clause de `0004` reproduite **littéralement**, puis les
  domaines rendus à leur liste d'origine, puis les colonnes. L'index D-067 est retiré **avant** le
  domaine `job_type`, son prédicat nommant des valeurs que la contrainte doit accepter.
- La CI exerce cela à chaque exécution : étape « Migrations on a fresh database (up, down, up, same
  schema) », `scripts/ci_migrations.py --postgres-major 17`.

**Une seule différence subsiste entre l'état d'origine et l'état descendu, et elle est VOULUE :**
les trois domaines rendus à leur liste d'origine reviennent marqués **`NOT VALID`**. C'est le
mécanisme de `0008`, repris tel quel par `0013` et `0014`, dont la descente fait exactement cela —
une descente ne peut pas **valider** une contrainte contre des lignes qui peuvent la violer (un
travail `purge_store` subsistant, par exemple). La contrainte est donc appliquée aux nouvelles
lignes sans relire les anciennes. **Le test d'aller-retour l'affirme explicitement plutôt que de le
masquer.**

## H. Observability

### H.1 D-071 — le cycle de vie, dès la liste

`org list` expose `status` par ligne. Une organisation `purging` dont plus rien ne s'occupe était
jusqu'ici **invisible** : l'effacement demandé restait inachevé en silence.

### H.2 `org show` — de quoi décider sans lancer aucune commande

Bloc `purge` : `status`, `purged_at`, `active_purge` (les **deux** types, D-067 n'en tolérant
qu'un), `recoveries` (purges d'organisation `failed`, celle d'origine comprise — le compteur même
de la garde B), `latest_purge_job`, `outcome`, `blocked_reason`.

**`outcome` et `blocked_reason` sont calculés par la MÊME garde que `reconcile-purges`**, pour
qu'une inspection et une reprise ne puissent **jamais** se contredire. Une inspection qui
annoncerait une reprise possible là où la reprise refuserait serait pire que l'absence
d'observabilité, parce qu'elle serait crue.

### H.3 Isolation locative de l'observabilité

Lecture **dans le contexte du locataire**, par `TenantSession` : aucune requête inter-locataire,
**aucune fonction privilégiée**, aucun droit nouveau. Tout vient de `organizations` et de `jobs`,
que la RLS borne déjà. Une organisation étrangère reste **indiscernable d'une organisation
inexistante** (code 5). Le rapport ne porte **aucun nom, aucun secret, aucune donnée d'un autre
locataire** : des identifiants, des états, des nombres, des horodatages. La lecture passe par
`Permission.READ` — tout membre voit ; la reprise exige `owner`. **Voir n'est pas pouvoir.**

### H.4 Audit : le vocabulaire existant, et lui seul

Quatre actions nouvelles en base (`store.purge_started`, `store.purged`,
`organization.purge_started`, `organization.purged`), écrites par les fonctions privilégiées
**dans la même transaction** que le changement, avec `actor_type = 'worker'`, acteur = principal de
`session_user`, `on_behalf_of` = demandeur. Métadonnées : `job_id` et des **compteurs** non
sensibles. **Interdits sans exception** : toute PII, tout nom d'organisation ou de boutique, toute
devise, toute clé d'objet, tout chemin, tout secret, toute référence client.

Une reprise **réussie** n'émet que le vocabulaire **existant** (`job.enqueued`). Une réconciliation
**bloquée** n'écrit **aucun** événement d'audit : constat **assumé**, relevant de **D-070**, non
implémentée — voir §I.3.

## I. Résiduels acceptés

Aucun n'est une exigence non tenue ; **une CI verte n'en referme aucun.**

1. **La détection d'une purge figée est entièrement manuelle** ⚖️ — et c'est la limite principale
   de cette mission. `org reconcile-purges` est une commande d'**opérateur**, en **simulation par
   défaut** : rien n'est périodique, rien n'alerte, rien ne relance. Une organisation peut rester
   `purging`, **sel détruit**, indéfiniment, sans que quiconque en soit averti, tant que personne
   ne lance la commande. D-071 rend l'état **lisible** ; elle ne le surveille pas. La reprise
   automatique par un ordonnanceur, ou par une boucle du worker, n'est **pas** livrée — et la
   seconde exigerait d'amender SEC-06 (§E.1).
2. **Un historique supprimé rend la reprise impossible → `no_requester`.** `latest_purge` est lu
   dans `jobs` ; si la purge d'origine a été supprimée (la fenêtre D-066 le permet précisément
   pendant `purging`), l'organisation reste `purging` **sans demandeur identifiable**, et
   `reconcile-purges` refuse. Le chemin **manuel** reste ouvert, mais il exige un humain `owner`
   qui sache qu'il doit agir. État atteignable, non théorique.
3. **D-070 n'est pas implémentée par cette mission.** Aucune trace durable d'une tentative de reprise **refusée**.
   Une revue de conformité portant sur « qu'a-t-on tenté pour honorer cet effacement » ne trouverait
   que les purges **mises en file**, jamais les refus. Deux tests figent ce comportement pour qu'il
   ne change pas sans décision.
   *(Mise à jour du 01/10/2026 : **D-070 est ratifiée, implémentée et validée** en **004.4.7**
   (`41f5f34`, `aec8ddd`, révision `0017`). Ce résiduel reste néanmoins **entier pour 004.4.6** :
   la présente acceptance ne le referme pas, elle constate qu'une mission ultérieure l'a fait. **La couverture
   prévue est PARTIELLE, et ne doit jamais être présentée comme totale** : `owner_invalid` et
   `permission_denied` resteront définitivement non journalisés — le premier par le modèle
   d'autorisation, le second par impossibilité technique sous RLS — et les quatre classes de garde
   ne seront auditées que lorsque l'acteur est `owner`. Voir D-070 dans
   [`DECISIONS.md`](DECISIONS.md).)*
4. **`max_attempts = 7` n'est dominant qu'aux paramètres par défaut.** Le calcul de §E.4 vaut pour
   un bail de 300 s et un renouvellement de 30 s. **Aucune valeur admise par le schéma**
   (`max_attempts BETWEEN 1 AND 20`) ne couvre un bail configuré au maximum
   (`MERVIO_JOB_LEASE_SECONDS` monte à 86400). Ce n'est pas une garantie générale, et ce n'est
   **pas** la réponse à une purge devenue `failed`, qui relève de la reprise.
5. **Trois batteries de gardes D-052 à maintenir équivalentes.** `app_purge_guard` couvre les huit
   fonctions nouvelles ; `app_redact_customer` (`0014`) et `app_finalize_raw_object_purge` (`0015`)
   conservent délibérément la leur, inlinée (D-065 Q2). Le risque de **dérive** entre corps qui
   doivent rester équivalents est réduit, **non annulé**.
6. **La dépendance d'ordre de D-066 n'est protégée que par des tests.** Une réorganisation future
   des étapes de D-051 refermerait l'exception **trop tôt** et ferait échouer l'étape 10 —
   **silencieusement** à nouveau, un `DELETE` sans effet ne levant rien. Le test 4 de D-066 est le
   seul garde-fou.
7. **Les deux commits intermédiaires n'ont pas de CI propre.** `60505e1` et `790c8db` n'ont aucun
   run ; les valeurs `MERVIO_EXPECTED_TESTS` 2850 et 2873 n'ont jamais été exercées. La mission est
   validée **en bloc**, à sa tête.
8. **Pendant `purging`, tout rôle agissant dans cette organisation — `mervio_app` compris — peut
   supprimer ses travaux terminaux sans attendre le plancher** ⚖️. Élargissement **réel**, non une
   nuance rhétorique, borné par un état que ce rôle ne peut pas écrire, et accepté parce que
   l'organisation est en cours de destruction (D-066).
9. **Une clôture interrompue laisse la fenêtre D-066 ouverte** tant que l'organisation reste
   `purging` : **aucun délai d'expiration n'est prévu**. Lié à §I.1.
10. **Une annulation commitée est irréversible** (`jobs_guard_transition`) : si la purge échoue
    après l'étape 4, les travaux restent `cancelled` — assumé, la purge détruisant un sur-ensemble
    de ce qu'ils auraient produit.
11. **Une purge de boutique légitime est refusée** pendant qu'une autre purge de la même
    organisation est en vol (D-067) ⚖️ : refus **propre** (`23505` → `already_active`), jamais une
    destruction partielle, mais une organisation à nombreuses boutiques ne peut pas les purger en
    parallèle.
12. **Une organisation purgée conserve des `memberships` et des `service_authorizations`
    révoquées**, donc des identifiants, sans donnée personnelle — assumé par D-051. La pierre
    tombale subsiste tant que l'audit est retenu (365 jours ⚖️).
13. **Le coût d'évaluation de la politique de suppression n'est pas mesuré** : chaque `DELETE` de
    travail lit désormais `organizations`. **Aucune campagne de charge n'est revendiquée.**
14. **Les quatre actions d'audit de purge tenant sont écrites mais NON FILTRABLES par la CLI.**
    Défaut **mesuré** sur le SHA figé : la contrainte `audit_events_action_check` porte 27 valeurs,
    l'enum Python `Action` en porte 23, et `store.purge_started`, `store.purged`,
    `organization.purge_started`, `organization.purged` en sont **absentes**. Comme
    `list_audit_events` valide `--action` contre cet enum, `mervio admin audit list --action
    organization.purged` est **refusé** (`InvalidInput`, code 2) alors que de tels événements
    existent en base. `0013` et `0014` avaient chacune ajouté leurs actions à l'enum **et**
    verrouillé l'inclusion par un test ; `0016` n'a fait ni l'un ni l'autre, et c'est l'absence de
    ce test qui a laissé passer le défaut. Les événements eux-mêmes sont corrects et complets — seul
    le filtre de lecture est en cause. **Corrigé en 004.4.7** (`aec8ddd`), dans le même changement
    que D-070 : l'enum porte désormais 28 valeurs, exactement égales à la contrainte, et le test
    de cohérence que `0013` et `0014` avaient — et dont l'absence ici a laissé passer le défaut —
    est restauré. Le défaut reste imputable à **004.4.6**, et cette ligne le consigne comme tel.
15. **Résiduels hérités, inchangés par cette mission** : ramassage des orphelins et expiration de
    `retain_until` (**004.9**) ; absence de garantie de non-écrasement de `put` (D-055, 004.9) ;
    politique IAM `s3:DeleteObject` non implémentée, donc `delete_capability()` de S3 rend
    `undetermined` (D-064, 004.9) ; TOCTOU du pilote filesystem réduit par `O_NOFOLLOW`, non annulé ;
    résolution d'identité non auditée par le produit (D-062).

## J. Hors périmètre — à ne pas confondre avec une preuve

Ces points ne sont **pas** validés par cette acceptance et ne doivent pas être présentés comme tels :

- **Exécution des purges tenant dans un runtime Docker.** Contrairement à 004.4.5, dont D-064
  faisait de la preuve Docker une **condition de clôture**, aucune décision de 004.4.6 ne l'exige,
  et `scripts/container_smoke.py` n'a **pas** été étendu aux purges tenant (le fichier n'est pas
  dans le périmètre `b101547..e1a68ef`). La destruction est prouvée sur PostgreSQL réel et sur le
  magasin d'objets des tests, **pas** en conteneurs. Le job `docker` de la CI reste vert : il
  n'exerce pas ce chemin.
- **Déploiement en production.** Aucune preuve n'est apportée ni revendiquée.
- **S3 hébergé.** Le pilote S3 n'est exercé que par `moto`, en processus : aucun bucket réel,
  aucun compte, aucun réseau, **aucune politique IAM vérifiée** (D-055, D-064 → 004.9).
- **Performance et charge.** Aucune campagne. Aucun chiffre de débit, de durée de purge ou de coût
  de politique n'est produit.
- **Couverture de code.** Aucune mesure produite ni revendiquée ; les comptes cités sont des
  nombres de **tests**.
- **Reprise automatique.** Non livrée (§I.1). La présence de `reconcile-purges` n'en est pas une.
- **D-070.** Ratifiée, puis **implémentée et validée en 004.4.7** (`41f5f34`, `aec8ddd`, révision `0017`). **Rien de ce qu'elle prévoit n'est validé par la présente acceptance** : 004.4.6 n'a créé ni `0017`, ni l'action d'audit, ni le chemin d'écriture. Les preuves de D-070 sont dans [`MERVIO_004_4_7_ACCEPTANCE.md`](MERVIO_004_4_7_ACCEPTANCE.md).
- **Validation juridique des durées** ⚖️ (rétention d'audit à 365 jours, objets bruts à 30 jours,
  rôles de traitement) : **004.9**.

## K. Verdict

**004.4.6 — ACCEPTED / CLOSED / RATIFIED**, avec les résiduels de §I et les limites de §J.

| Condition | État |
|---|---|
| D-065 ratifiée, ses huit arbitrages implémentés | ✅ `f38e78f` → `e1a68ef`, §A, §B |
| D-066 ratifiée, la politique livrée dans la forme prescrite | ✅ `75ebf81` → `60505e1`, §G |
| D-067, D-068, D-069 ratifiées **et** implémentées | ✅ `e1a68ef`, §D |
| D-071 implémentée | ✅ `e1a68ef`, §H |
| D-070 — **non implémentée** par cette mission ; ratifiée, implémentée et validée depuis, en 004.4.7 | ✅ déclarée telle, §I.3, §J |
| D-063 intacte — aucune portée de verrou, aucune primitive ajoutée | ✅ §D.1 |
| Aucun `SET ROLE` ajouté — vérifié, zéro occurrence dans `src/` | ✅ §C.7 |
| Aucun `INSERT` direct dans `jobs` par le worker ; SEC-06 non amendée | ✅ §E.1, test de forme |
| Worker toujours soumis à la RLS, strictement plus contraint | ✅ §C.8 |
| Aucun rôle nouveau, aucun privilège élargi | ✅ §C.1, §C.2 |
| F-1 (résiduel 004.4.1) fermé et démontrable | ✅ §C.5, `60505e1` |
| Aller-retour de migration déterministe, politique restaurée au caractère près | ✅ §G |
| 60 tests de concurrence sur connexions réelles | ✅ §D.3, §F |
| 26 tests de reprise | ✅ §E, §F |
| Suite complète : 2982 passés / 0 ignoré / 0 échec, porte de comptage exact | ✅ §F |
| CI verte sur `test`, `security`, `lint`, `docker` | ✅ run #32 `36793763636` |

**D-051 : achevée.** **D-053 : achevée, et resserrée par D-069.** **D-065, D-066, D-067, D-068,
D-069, D-071 : implémentées.** **D-070 : hors périmètre de cette mission — ratifiée, implémentée
et validée depuis, en 004.4.7.** **004.4.6 : close et ratifiée, avec résiduels.**

**Prochaine mission prévue par la roadmap : 004.5 — Report Contract 2.0 & Analytics Correctness.**
Ses dépendances (`stores.timezone`, identité client) sont livrées depuis 004.4.2 / `0011`. Voir
`docs/MISSION_004_4_HANDOFF.md`.
