# MERVIO 004.4.7 — ACCEPTANCE

**Date :** 2026-10-01 (implémentation livrée le 2026-09-30) · **Mission :** 004.4.7 — Audit des refus d'une tentative réelle de reprise
de purge (**D-070**), et clôture du défaut d'enum de `0016`

## Status

**ACCEPTED / CLOSED / RATIFIED — avec une couverture explicitement PARTIELLE**

Mission de petite taille et de périmètre volontairement étroit : **une** contrainte de base,
**cinq** valeurs d'enum, **un** point d'écriture d'audit. Elle clôt un résiduel directement issu
de 004.4.6 (§I.3 de son acceptance) et un défaut mesuré de `0016` (§I.14).

**Ce document ne revendique pas** une couverture totale des tentatives de reprise : voir **§I.2**,
qui est la limite centrale de cette mission et non une note de bas de page.

## Baseline

`41f5f34e7d5bcb0bed83502287a42e1b5b2d2386` — `docs(004.4.7): ratify D-070 purge recovery refusal
audit`, dernier commit antérieur à l'implémentation. Lui-même suit `941b050`
(`docs(004.4.6): close mission and journal concurrency decisions`), poussé et vert.

## Final commit

`aec8ddd49f2da37c247ce2bcf0920781fc13627a` — `feat(004.4.7): audit purge recovery refusals
(D-070)`. Branche `mission-004.4` ; arbre de travail propre.

Périmètre (`41f5f34..aec8ddd`) : **10 fichiers, +979 −48**.

| Catégorie | Fichiers | Lignes |
|---|---|---|
| `tests/` | 6 | +757 −33 |
| `src/` (code produit) | 3 | +221 −14 |
| Infrastructure (`ci.yml`) | 1 | +1 −1 |

Commits de la mission, dans l'ordre :

| SHA | Objet |
|---|---|
| `41f5f34` | `docs(004.4.7)` — ratification de D-070, **avant** toute ligne de code |
| `aec8ddd` | `feat(004.4.7)` — implémentation : `0017`, enum `Action`, chemin d'audit, tests |

Le protocole du dépôt est respecté : **ratification documentaire d'abord, implémentation
ensuite** — comme D-063, D-065 et D-066, et contrairement à D-067→D-071, dont la journalisation
a dû être rattrapée par la clôture de 004.4.6.

---

## A. Scope

1. **D-070** — journaliser le refus d'une **tentative réelle** de reprise de purge, par une
   action d'audit **dédiée** : `organization.purge_recovery_refused`.
2. **Défaut de `0016`** — les quatre actions d'audit de purge tenant étaient présentes dans la
   contrainte `CHECK` mais **absentes de l'enum Python `Action`** ; `list_audit_events` validant
   `--action` contre cet enum, ces traces étaient **écrites et non filtrables**.

Hors périmètre, explicitement : toute reprise automatique ou périodique des purges figées
(résiduel 1 de l'acceptance 004.4.6, **inchangé**) ; toute extension de la portée de l'audit
au-delà des cinq classes ratifiées ; toute modification du modèle d'autorisation livré en
004.4.6.

## B. Décision ratifiée — ce que D-070 engage

Reproduit ici pour mémoire ; le texte normatif est D-070 dans [`DECISIONS.md`](DECISIONS.md).

| Élément | Ratifié |
|---|---|
| Portée | une **tentative réelle** (`--execute`) refusée ; jamais une consultation, jamais une simulation |
| `action` | `organization.purge_recovery_refused` — dédiée, aucune réutilisation (D-065 Q1) |
| `resource_type` / `resource_id` | `organization` / `organization_id` |
| `outcome` | **`failed`** — aucune valeur `refused` créée ; la contrainte de `0005` n'est pas amendée |
| Acteur | `actor_type = user`, `actor_id` = l'humain `--as`, **`on_behalf_of = NULL`** |
| `correlation_id` | celui de `latest_purge` s'il existe, UUID neuf sinon |
| Métadonnées | `{outcome, reason, recoveries, purge_job_id?}`, via `scrub()` |
| Autorisation | **`Permission.PURGE_TENANT`** — la permission de l'action décrite, jamais inférieure |
| Classes auditées (5) | `no_requester`, `too_soon`, `guard_exhausted`, `deterministic_failure`, `already_active` (course `23505`) |
| Classes non auditées (6) | `terminal`, `not_purging`, `already_active` (pré-contrôle), `would_recover`, **`owner_invalid`**, **`permission_denied`** |
| Déduplication | **aucune** — ni empreinte, ni `SELECT` préalable, ni clé unique, ni cooldown |
| Migration | `0017`, `audit_events_action_check` **seul** objet modifié |

## C. Implémentation — ce qui est effectivement présent

### C.1 Révision `0017_purge_recovery_audit` (`down_revision = "0016_tenant_purge"`)

**Deux instructions SQL exécutées, en tout** : `DROP CONSTRAINT` puis `ADD CONSTRAINT` sur
`audit_events_action_check`. 27 → 28 valeurs ; delta exactement une valeur.

| Objet | Touché ? |
|---|---|
| `audit_events_action_check` | ✅ **seul objet modifié** |
| `outcome`, `resource_type`, `actor_type`, toute autre contrainte | ❌ |
| Table, colonne, index | ❌ |
| Politique RLS, rôle, `GRANT`, `REVOKE` | ❌ |
| Fonction, déclencheur | ❌ |

Vérification : l'unique occurrence de `GRANT` dans le fichier est dans la **prose** de son
en-tête (« AUCUN GRANT/REVOKE ») ; `grep op.execute` ne rend que les deux `ALTER TABLE`.

**La descente rend la liste de `0016` au caractère près**, en `NOT VALID` — mécanisme de `0008`,
repris par `0013`, `0014` et `0016`. Vérifié par script hors base : la liste de descente de
`0017` est **identique, ordre compris**, à la liste de montée de `0016`, et le SQL produit par
les deux est le même caractère pour caractère.

### C.2 Enum `Action` — 23 → 28, et désormais **exactement égal** à la contrainte

| Valeur ajoutée | Provenance |
|---|---|
| `store.purge_started`, `store.purged`, `organization.purge_started`, `organization.purged` | **dette de `0016`**, fermée ici |
| `organization.purge_recovery_refused` | D-070 |

**Effet mesurable** : `audit list --action organization.purged` rendait `InvalidInput` (code 2)
alors que de telles traces existaient en base ; il est désormais accepté. Aucune sémantique des
quatre événements de `0016` n'est changée — ils restent écrits par les fonctions
`SECURITY DEFINER` en SQL brut, et rien ne les écrit depuis Python.

### C.3 Chemin d'audit dans `reconcile_purges`

- `_reconcile_one` rend `(ligne, auditable)` et **ne trace rien** : il décide, l'appelant écrit.
- `_trace_refusal` écrit par **`audit.record_event`** — helper **existant** du dépôt, prévu pour
  « sa propre transaction (hors d'une unité de travail existante) », jusqu'ici jamais employé en
  production. **Aucun mécanisme d'audit nouveau n'est introduit.**
- L'écriture a lieu **après** le retour de `_reconcile_one`, donc **après** l'annulation de la
  transaction par la `23505`. C'est la propriété non évidente de D-070 : écrite dedans, la trace
  disparaîtrait avec le rollback et la classe la plus intéressante serait silencieusement vide.
- **Deux conditions, et elles ne disent pas la même chose** : le drapeau dit « une tentative
  réelle a eu lieu », `RECOVERY_AUDITED` dit « cette classe est ratifiée ». Les deux doivent
  tenir, de sorte qu'une classe ajoutée au drapeau sans ratification n'écrit rien.

### C.4 Q3 — échec d'écriture de l'audit

**Fail-closed sur l'intégrité, best-effort sur la boucle**, exactement comme arbitré : la ligne
porte `audit_error` (code publiable, jamais le message du serveur) ; la boucle **continue** sur
les organisations suivantes ; l'échec n'est **jamais** masqué ; `_recovery_exit` durcit le code
de sortie à **`7`**, de sorte qu'un refus non tracé ne peut pas se lire comme une exécution
propre. Un refus d'**autorisation** sur l'écriture n'est pas un échec : c'est la couverture
partielle assumée, et il ne produit **aucune** annotation.

## D. Security

| Invariant | Résultat mesuré après implémentation |
|---|---|
| `SET ROLE` dans `src/` | **0** occurrence |
| `SECURITY DEFINER` ajoutée par `0017` | **0** |
| Rôle nouveau · `GRANT` nouveau | **aucun** · **aucun** |
| Bypass RLS | **aucun** — `audit_events_tenant_isolation` s'applique en `WITH CHECK` à l'insertion, inchangée |
| `INSERT` contournant le système d'audit | **aucun** — l'écriture passe par `record_event` → `record`, donc par `scrub()`, les contraintes et les deux déclencheurs |
| SEC-06 (`jobs_service_no_insert`) | **inchangée** — D-070 vit dans `admin` ; le rôle de service n'est pas concerné |
| D-063 | **inchangée** — aucun verrou, aucune primitive de concurrence ajoutée |
| D-067, D-068, D-069, D-071 | **aucun contact** |
| Autorisation livrée en 004.4.6 | **non modifiée** — la porte `_require(PURGE_TENANT)` reste à sa place, les codes de sortie des issues existantes sont inchangés |
| `on_behalf_of` | **`NULL`** ; le test de forme existant l'asserte, désormais étendu à `_trace_refusal` |
| PII dans les métadonnées | **aucune** — quatre clés, toutes traversant `scrub()` ; un test l'exige |

**Pourquoi `PURGE_TENANT` et pas moins.** La base n'impose **aucun** rang pour écrire l'audit
(`0005` accorde `SELECT, INSERT, DELETE` à `mervio_app`) : la porte est entièrement en Python, et
la choisir est un acte de conception. `READ_AUDIT` exige `ADMIN` : autoriser un `viewer` à écrire
créerait un canal **en écriture seule** vers un journal **en ajout seul** que
`audit_events_retention_floor` rend **indestructible 30 jours**. La garde referme ce chemin
**sans déplacer aucune porte** — et elle rend `owner_invalid` non auditable, ce qui est accepté.

## E. Concurrency

**La course `23505` est validée 20 fois sur 20**, sur **deux connexions réelles** synchronisées
par barrière (`test_c4_two_administrators_resume_the_same_stalled_purge`, paramétré sur
`REPEATS = 20`) : deux administrateurs reprennent la même purge figée, les deux franchissent le
pré-contrôle, D-067 tranche à l'insertion, et le résultat est **un** `job.enqueued` **et un**
refus tracé. Le motif enregistré nomme la course.

Ce que cela prouve, et qu'aucun test déterministe ne prouverait : la trace du perdant **existe**
alors que sa transaction a été annulée.

Deux administrateurs ne produisent **jamais** deux traces du même refus : D-067 garantit un seul
gagnant, et chaque administrateur décrit **sa** tentative. Ce ne sont pas des doublons.

## F. Tests

| Métrique | Valeur |
|---|---|
| Total | **3036 passés** |
| Ignorés | **0** |
| Échecs / erreurs | **0** |
| Baseline avant la mission | 2982 |
| Ajoutés par 004.4.7 | **+54** |

`MERVIO_EXPECTED_TESTS` : `2982` → `3036`, dans le commit qui ajoute les tests. Porte
`scripts/ci_test_gate.py` **franchie** à `--expected-tests 3036` (0 échec, 0 ignoré, modules
obligatoires exécutés).

Répartition des +54 :

| Fichier | Avant | Après | Δ |
|---|---|---|---|
| `tests/persistence/test_migration_0017.py` | — | **12** | +12 (nouveau) |
| `tests/persistence/test_tenant_purge_concurrency.py` | 60 | **81** | +21 |
| `tests/persistence/test_tenant_purge_recovery.py` | 26 | **43** | +17 |
| `tests/test_cli_admin.py` | 97 | **101** | +4 |

Couverture, par exigence :

- **les cinq classes auditées**, chacune avec un acteur `owner`, métadonnées vérifiées ;
- **forme de l'événement** : action, ressource, `failed`, acteur, `on_behalf_of IS NULL`,
  `store_id IS NULL`, corrélation dans ses **deux** cas ;
- **absence d'écriture** : `terminal`, `not_purging`, `already_active` pré-contrôle, simulation,
  `owner_invalid`, `permission_denied`, et pour un **non-owner** atteignant un refus de garde ;
- **isolation locative** : l'événement n'existe que dans son organisation, la voisine n'en voit
  aucun ;
- **Q3** : l'échec d'écriture est rapporté, durcit le code de sortie, et **n'arrête pas la
  boucle** — deux tests distincts ;
- **migration** : montée, descente, remontée, **survie d'un événement existant à la descente**,
  aller-retour déterministe, et égalité **exacte** enum ↔ contrainte ;
- **garde de la liste ratifiée** : les cinq classes, figées ; les six autres, exclues.

### Tests existants modifiés, et leur justification

**Toute modification d'un test est justifiée par écrit** (invariant 5.0.2 de la roadmap) ; ici,
la justification est D-070 elle-même, ratifiée **avant** le code.

- `test_a_blocked_reconciliation_writes_no_audit_event` — **remplacé**. Son comportement, un
  refus n'écrit rien, est précisément ce que D-070 change ; son ancienne docstring renvoyait
  déjà à « D-070, décision séparée ».
- `test_a_successful_recovery_records_the_existing_enqueue_event` — **conservé et renforcé** :
  un succès n'émet toujours que `job.enqueued`, **et** aucun événement de refus.
- `test_migration_0016.py` — `0016` n'est plus la tête. Ses tests la visent désormais
  **explicitement** (`migrate.upgrade(staged, REVISION)`), sans quoi `upgrade()` filait jusqu'à
  `0017` et l'aller-retour portait sur une autre révision ; son test de rang ne vérifie plus que
  la position de `0016` dans la chaîne.
- `test_persistence_migrations.py` — chaîne et tête étendues à `0017`, dont la ligne note
  « aucune table » comme pour `0015` et `0016`.
- `test_the_recovery_path_uses_no_privileged_shortcut` — **étendu** à `_trace_refusal` : la
  nouvelle fonction du chemin de reprise est soumise au même contrôle de forme (ni `SET ROLE`,
  ni `SECURITY DEFINER`, ni `INSERT INTO`, ni `on_behalf_of`).

## G. Migration / rollback

- `ci_migrations.py --postgres-major 17` : **vert** — montée jusqu'à la tête, descente jusqu'à
  `base`, remontée, schéma identique. C'est le contrôle que la CI exécute à chaque exécution.
- **Aller-retour dédié** (`test_migration_0017.py`) : `UP1 == UP2` ; **une seule** ligne
  d'empreinte diffère entre l'état de départ et l'état descendu, et c'est la contrainte, avec
  ses deux écarts voulus — le suffixe ` NOT VALID` de `pg_get_constraintdef` et le drapeau
  `convalidated`. La **condition** est identique.
- **Un événement D-070 déjà écrit survit à la descente**, et la remontée le revalide : la
  descente ne **pourrait pas** valider la contrainte contre cette ligne, d'où `NOT VALID`.
- `test_the_upgrade_touches_nothing_but_that_constraint` : sur une empreinte de plus de 500
  lignes (fonctions avec md5 du corps, politiques, contraintes, déclencheurs, colonnes,
  privilèges de colonne, index, RLS), **exactement une** ligne change.

## H. Observability

D-070 **complète** D-071, elle ne la remplace pas, et les deux responsabilités restent
distinctes : `org show` dit **l'état courant** (ce que la réconciliation *dirait*), le journal
dit **l'historique des tentatives** (ce qui *a été* tenté, et refusé). Aucun des deux ne
surveille : rien n'alerte, rien ne relance, rien n'est périodique — voir §I.1.

## I. Résiduels acceptés

Aucun n'est une exigence non tenue ; **une CI verte n'en referme aucun.**

1. **La détection d'une purge figée reste entièrement manuelle** ⚖️ — résiduel 1 de l'acceptance
   004.4.6, **inchangé par cette mission**. D-070 enregistre ce qu'un opérateur a tenté ; elle ne
   lui dit pas qu'il devrait tenter. Une organisation peut rester `purging`, **sel détruit**,
   indéfiniment, tant que personne ne lance `reconcile-purges`.
2. **Couverture PARTIELLE, par conception.** `owner_invalid` n'est pas journalisé — l'acteur n'a
   pas la permission de l'action, donc pas celle de tracer son refus, et aucun privilège
   inférieur n'a été inventé. `permission_denied` ne l'est pas davantage : l'appartenance a été
   retirée, `TenantSession` lève, et `audit_events_tenant_isolation` refuserait l'insertion —
   **impossibilité technique, pas un arbitrage**. De plus les quatre classes de garde ne sont
   auditées que pour un acteur `owner` : un `viewer` ou un `analyst` lançant `--execute` atteint
   un refus de garde (elles précèdent la porte d'autorisation) et **n'écrit rien**.
   **D-070 ne journalise pas 100 % des tentatives de reprise.**
3. **Volume non borné en théorie.** Aucune déduplication n'est créée, par arbitrage. La règle
   « seul `--execute` écrit » borne le volume aux tentatives réelles, mais un `--execute`
   automatisé produirait un refus par passage, et le plancher de rétention de 30 jours rend un
   excès **irrécupérable** pendant un mois.
4. **Mêler des refus à un journal qui n'enregistrait que des faits accomplis change sa
   nature** ⚖️, et aucun lecteur existant n'est préparé à cette distinction. `outcome = failed`
   la rend lisible, mais ne la supprime pas.
5. **Flake préexistant, sensible au temps, hors périmètre de D-070.**
   `tests/persistence/test_worker_runtime.py::test_a_handler_that_never_yields_triggers_the_hard_exit`
   — **known pre-existing timing-sensitive test flake**. Observé : **reproduit une fois** sur une
   exécution complète, **passé 3/3 en isolation**, **passé sur les deux exécutions complètes
   suivantes** (3036/3036). Il est **hors des fichiers touchés par D-070** : `src/mervio/workers/`
   et `src/mervio/persistence/jobs.py` ne sont pas modifiés par cette mission. Sa nature est
   structurelle — handler CPU de 3 s, `forced_exit_delay = 0.5`, fenêtre d'attente de 5 s — et il
   peut faire échouer la CI de façon intermittente. **Ce n'est pas un échec de D-070 et ne doit
   pas être compté comme tel** ; son traitement relève d'un travail distinct.
6. **Résiduels hérités, inchangés** : tous ceux de l'acceptance 004.4.6 (§I) et de 004.4.5
   demeurent — expiration de `retain_until`, ramassage des orphelins, S3 réel, IAM, KMS (004.9),
   trois batteries de gardes D-052, dépendance d'ordre de D-066, purges tenant non exercées en
   conteneurs.

## J. Hors périmètre — à ne pas confondre avec une preuve

- **Reprise automatique des purges figées.** Non livrée, et D-070 ne s'en approche pas.
- **Exécution en conteneurs.** `scripts/container_smoke.py` n'est **pas** modifié : le chemin
  d'audit de D-070 n'est pas exercé en runtime Docker. Le job `docker` de la CI reste vert ; il
  n'exerce pas ce chemin.
- **Déploiement en production.** Aucune preuve apportée ni revendiquée.
- **Performance et charge.** Aucune campagne. Le coût d'un `INSERT` d'audit supplémentaire par
  refus n'est pas mesuré.
- **Couverture de code.** Aucune mesure produite ; les comptes cités sont des nombres de **tests**.
- **Validation juridique** des durées ⚖️ : **004.9**.

## K. Verdict

**004.4.7 — ACCEPTED / CLOSED / RATIFIED**, avec les résiduels de §I et les limites de §J.

| Condition | État |
|---|---|
| D-070 ratifiée **avant** le code | ✅ `41f5f34` |
| `0017` créée, périmètre d'une seule contrainte | ✅ §C.1 |
| Descente restaurant la liste de `0016` en `NOT VALID` | ✅ §C.1, §G |
| Événement D-070 existant survivant à la descente | ✅ §G |
| Enum `Action` aligné **exactement** sur la contrainte | ✅ §C.2 |
| Défaut `audit list --action` de `0016` fermé | ✅ §C.2 |
| `reconcile_purges` implémentant l'audit D-070 | ✅ §C.3 |
| Trace survivant à l'annulation de la `23505` | ✅ §C.3, §E — 20/20 |
| Q3 implémenté (fail-closed intégrité, best-effort boucle) | ✅ §C.4 |
| Couverture partielle **déclarée**, jamais arrondie | ✅ §I.2 |
| Suite complète : 3036 / 0 / 0, PostgreSQL 17 réel | ✅ §F |
| Porte de comptage exact franchie à 3036 | ✅ §F |
| Aller-retour de migration validé | ✅ §G |
| `ruff`, `compileall`, `git diff --check` | ✅ |
| `SET ROLE` = 0 · `SECURITY DEFINER` dans `0017` = 0 | ✅ §D |
| SEC-06, D-063, D-067→D-071 intactes | ✅ §D |

**D-070 : implémentée et validée.** **004.4.7 : close.** **004.4.6 : close.**

**Prochaine mission prévue par la roadmap : 004.5 — Report Contract 2.0 & Analytics
Correctness.** Elle n'est pas commencée. Voir
[`MISSION_004_4_HANDOFF.md`](MISSION_004_4_HANDOFF.md).
