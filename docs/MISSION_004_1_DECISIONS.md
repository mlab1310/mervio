# Mission 004.1 — Architecture Decision Records

Format : Contexte · Options · Décision · Pourquoi · Conséquences · Rejetées.
Ces ADR complètent `docs/DECISIONS.md` (D-001 à D-048) et `docs/MISSION_004_0_DECISIONS.md` (ADR-004-001 à 012).
Ils ne modifient aucun contrat analytique. Deux ADR de 004.0 sont **révisés explicitement** :
- ADR-004-002 : précision monétaire, voir ADR-004.1-002 ;
- ADR-004-005 : clé d'upsert, voir ADR-004.1-004.

---

## ADR-004.1-001 — Driver et migrations : psycopg 3 + Alembic, SQL explicite, sans ORM

**Contexte.**
- Le moteur est en stdlib (D-004). La persistance doit rester optionnelle et ne pas contaminer le moteur.
- Il faut :
  - des migrations versionnées, reproductibles, montée et descente ;
  - un contrôle fin de la transaction, indispensable à RLS (`set_config(..., true)`) ;
  - des insertions en masse ;
  - des requêtes paramétrées.

**Options évaluées.**

| Option | Pour | Contre |
|---|---|---|
| psycopg 3 seul + migrations maison (fichiers SQL numérotés) | le moins de dépendances | runner, table de version, descente et tests à réécrire ; ADR-004-002 retient Alembic |
| **psycopg 3 + Alembic** (SQLAlchemy transitif, utilisé par Alembic seulement) | driver de référence ; migrations éprouvées ; SQL relu tel quel | SQLAlchemy installé sans servir à l'exécution |
| psycopg 3 + SQLAlchemy Core + Alembic | constructeur de requêtes | couche en plus entre le code et RLS ; aucun gain pour ~40 requêtes fixes |
| SQLAlchemy ORM | modèles déclaratifs | seconde définition du modèle, en concurrence avec `domain.models` (risque signalé en 004.0) ; sessions implicites, chargements paresseux (N+1) |
| pg8000 (BSD-3) | licence permissive | pas de chargement en flux par curseur serveur aussi mûr ; écosystème réduit |
| asyncpg (Apache-2.0) | performance | asynchrone seulement ; moteur et CLI synchrones ; SQLAlchemy nécessaire pour Alembic de toute façon |

**Décision.**
- `psycopg[binary] >= 3.2, < 4` et `alembic >= 1.13, < 2`, extra optionnel `persistence` (`pyproject.toml`).
- `dependencies = []` inchangé.
- Migrations Alembic écrites **en SQL explicite**, sans autogénération.
- Dépôts en SQL paramétré psycopg.
- SQLAlchemy n'est importé que par `persistence/migrate.py` et `migrations/env.py` (test AST).

**Pourquoi.**
- La combinaison la plus petite qui satisfait migrations versionnées, descente testée et contrôle transactionnel.
- Le schéma relu en revue est exactement celui qui est appliqué.

**Conséquences.**
- psycopg est sous **LGPL-3.0**. Il est utilisé non modifié, installé séparément, côté serveur ; entrée ajoutée à
  `research/licensing_matrix.md`.
- Repli documenté : pg8000, si Mervio distribuait un logiciel installable.
- Aucune requête SQL hors de `mervio.persistence` et du cas d'usage `application/persisted_analysis.py` (test AST).

**Rejetées.** ORM, autogénération Alembic, migrations manuelles via un outil graphique.

---

## ADR-004.1-002 — Montants : `numeric(19,4)` et aller-retour float exact, ou refus

**Contexte.**
- ADR-004-002 prévoyait `numeric(14,2)`.
- Le domaine calcule en `float` (D-005). Le rapport doit être identique octet pour octet après persistance.
- Un arrondi silencieux en base changerait les sommes, donc potentiellement le rapport, sans signal.

**Options.** `numeric(14,2)` ; `numeric(19,4)` ; `numeric` sans échelle ; `double precision` ; entiers en
centimes.

**Décision.**
- Tout montant autoritaire est en `numeric(19,4)`, soit 15 chiffres entiers et 4 décimales.
- Écriture (`persistence/money.py`) : `repr(float)`, texte décimal le plus court. Il est accepté seulement s'il
  tient en 4 décimales et 15 chiffres entiers.
- Relecture : `float(texte numeric)`.
- Sont **refusés** (`MoneyPrecisionError`, jamais arrondis) :
  - plus de 4 décimales ;
  - |x| ≥ 10¹⁵ ;
  - `inf` et `nan` ;
  - `-0.0` ;
  - tout type autre que `float`.
- **ADR-004-002 révisé** : `numeric(14,2)` devient `numeric(19,4)`.

**Pourquoi.**
- Les connecteurs lisent des montants décimaux textuels : le float le plus proche redonne, par `repr`, le même texte.
  numeric(19,4) le stocke exactement ; `float()` le relit bit pour bit (test sur 6 000 valeurs aléatoires + bornes).
- 2 décimales ne suffisent pas :
  - devises à 3 décimales (KWD, BHD, TND) ;
  - coûts unitaires et coûts publicitaires à plus de 2 décimales ;
  - avec une échelle 2, ces montants seraient arrondis.
- 15 chiffres entiers couvrent les totaux en devises à petite unité (IDR, VND).
- `double precision` est interdit pour un montant (exigence 004.1).
- Les centimes entiers supposent une échelle par devise, sans bénéfice ici.

**Conséquences.**
- Le seul flottant du schéma est `ad_daily_performance.conversions`, un compte fractionnaire attribué par la régie et
  non un montant. Il est testé.
- **Devise :**
  - `orders.currency` conserve la devise lue par commande (y compris vide) ;
  - `data_snapshots.currency` conserve la devise du Dataset (`unknown` si absente, comme la CLI) ;
  - les montants sans devise au niveau domaine (paiements, remboursements, publicité) sont exprimés dans la devise
    de l'instantané ;
  - le mélange de devises reste signalé par `currency_mismatch` (qualité inchangée) ;
  - un import dont la devise contredit `stores.currency` est refusé (`currency_mismatch_with_store`).

**Rejetées.** Arrondi à l'écriture ; `numeric` sans échelle (aucune borne vérifiable) ; float en base.

---

## ADR-004.1-003 — Identifiants : UUID pour les ressources, clé naturelle pour les lignes canoniques

**Contexte.**
- Il faut distinguer trois identités :
  - identifiant interne Mervio ;
  - identifiant de la plateforme (commande Shopify) ;
  - identifiant de l'enregistrement dans sa source.
- Les identifiants séquentiels sont interdits pour les ressources visibles par un tenant.

**Décision.**
- **Ressources :** organisations, utilisateurs, appartenances, boutiques, connexions, instantanés, sources
  d'instantané, exécutions, rapports.
  - `id uuid` généré par l'application (`uuid4`).
  - L'application n'insère jamais un identifiant fourni par un appelant.
- **Lignes canoniques :** clé primaire `(organization_id, snapshot_id, position)`.
  - La ligne est immuable et ordonnée ; aucun UUID par ligne, soit 3 M d'UUID et un index de plus à 1 M de
    commandes, sans usage en 004.1.
- **Colonnes distinctes, jamais confondues :**
  - identité interne : `(organization_id, snapshot_id, position)` ;
  - identité source : `source` + `source_record_id`, clé d'unicité par instantané ;
  - référence métier du domaine : `order_ref` (ex. `#1001`), `payment_ref`, `refund_ref`, `sku` et `product_ref`,
    `campaign_ref`.
- **CSV aujourd'hui :** `source_record_id` = identifiant lu par le connecteur.
  - `Name` pour une commande, `id` Stripe, SKU, `Campaign ID` ;
  - remboursements : identifiant dérivé par le connecteur, déterministe ;
  - publicité : `campaign_ref|jour`, la clé de dédoublonnage du connecteur.
- **Connecteur API (004.4) :** `source_record_id` recevra l'identifiant de la plateforme (`gid://shopify/Order/…`),
  `order_ref` restant `#1001`.

**Conséquences.**
- Exposer une ligne canonique par API (004.3) demandera un identifiant opaque : dérivé de la clé ou colonne ajoutée
  par migration.
- UUID v7 (localité d'index) à évaluer quand PostgreSQL 18 ou un besoin mesuré le justifiera.

---

## ADR-004.1-004 — Instantané immuable, lignes copiées par import

**Contexte.**
- Il faut répondre à « quel jeu exact a produit ce rapport ? ».
- Il faut rejouer une analyse au bit près.
- ADR-004-005 prévoyait un upsert par (`store_id`, `source`, `source_record_id`).

**Options.**
1. Entités versionnées par upsert, avec table d'appartenance à l'instantané.
2. **Lignes canoniques appartenant à un instantané (copie par import).**
3. Instantané sérialisé en un seul document (JSONB ou objet).

**Décision.** Option 2.
- Cycle de vie de `data_snapshots` :
  - `ingesting` n'existe que dans la transaction d'import ;
  - `completed` (scellé) : sources, compteurs, qualité et empreinte renseignés ;
  - `failed` : motif et empreintes des fichiers, aucune ligne.
- **Immuabilité, garantie en base :**
  - trigger `BEFORE UPDATE` sur l'instantané scellé ;
  - triggers d'instruction sur les tables canoniques : ajout et suppression refusés si l'instantané n'est plus
    `ingesting` ;
  - toute modification de ligne canonique refusée ;
  - aucun droit `UPDATE` ni `DELETE` pour `mervio_app`.
- **Ordre :**
  - `position` restitue l'ordre exact des listes et dictionnaires du Dataset (les sommes flottantes en dépendent) ;
  - le document qualité est stocké en listes ordonnées, car jsonb ne conserve pas l'ordre des clés.
- **Idempotence :**
  - `inputs_sha256` = SHA-256 de (empreintes des fichiers, connecteur, versions de schéma et de normalisation,
    marqueur synthétique) ;
  - unique par boutique parmi les instantanés `completed` ;
  - réimporter les mêmes octets renvoie l'instantané existant (`reused`).
- **Remplacement :**
  - un nouveau fichier crée un nouvel instantané, jamais une réécriture ;
  - `supersedes_snapshot_id` permet de chaîner explicitement.
- **ADR-004-005 révisé :** l'upsert par clé de source est **reporté à 004.4**. Les imports CSV sont des exports
  complets ; une synchronisation incrémentale API exigera un ADR (versions d'entités ou instantané différentiel).

**Pourquoi.**
- Le déterminisme du rapport découle de l'instantané, sans versionnage d'entités.
- L'option 3 empêche requêtes, index et provenance par enregistrement.
- L'option 1 est plus complexe sans besoin actuel.

**Conséquences.**
- Stockage proportionnel au nombre d'imports ; rétention à définir (R-21).
- Vérification à la relecture : le contenu relu doit égaler les compteurs scellés, sinon `SnapshotIntegrityError`.

---

## ADR-004.1-005 — Rapport : octets exacts en `text`, projection `jsonb`, empreinte vérifiée en base

**Contexte.**
- Critère d'acceptation : rapport persisté identique octet pour octet au livrable CLI.
- `jsonb` réordonne les clés et normalise les espaces : il ne peut pas restituer les octets.

**Décision.**
- `reports.payload_json text` contient les octets UTF-8 de
  `json.dumps(report, indent=2, ensure_ascii=False)`, exactement ce qu'écrit `reporting.writers`. C'est la forme
  autoritaire.
- `reports.payload jsonb` est la projection interrogeable, non autoritaire, calculée par PostgreSQL depuis le même
  texte.
- Contraintes `CHECK` en base : `payload_sha256` = SHA-256 du texte ; `payload_bytes` = sa longueur.
- Métadonnées relationnelles **recopiées** du rapport, jamais recalculées : période, grain, versions, devise,
  `generated_at`, marqueur synthétique.
- **Refus :** version du moteur ou grain différents de l'exécution ; marqueur synthétique incohérent avec
  l'instantané, dans les deux sens.
- Une exécution (`analysis_runs`) a au plus un rapport ; rapport et exécution terminée sont immuables.

**Vérifié.**
- Octets identiques sur plusieurs chemins :
  - rapport CLI persisté puis relu ;
  - chaîne complète CSV → instantané → Dataset relu → moteur → rapport ;
  - jeu versionné `data/sample` (lignes rejetées, doublons, remboursements Stripe) ;
  - sources partielles ;
  - grain mensuel.
- `_meta.generated_at` est la seule valeur non déterministe du rapport. Elle est figée des deux côtés du test,
  comme `tests/test_pipeline_report.py` la retire déjà.
- Le critère n'a **pas** été affaibli.

**Conséquence.** L'égalité d'octets du rapport ne prouve pas seule l'exactitude des données. Un écart d'un ulp sur
un montant non utilisé ne change pas le rapport (vérifié par mutation). Elle est donc complétée par l'égalité
`Dataset relu == Dataset CSV`, champ par champ.

---

## ADR-004.1-006 — Isolation : `TenantSession` + RLS forcée + clés composites + unicité préfixée

**Contexte.**
- ADR-004-003 impose deux barrières.
- Trois brèches restaient à fermer :
  - les contrôles de clé étrangère **et d'unicité** ignorent RLS ;
  - le propriétaire d'une table échappe à RLS sans `FORCE` ;
  - un contexte tenant fabriqué par l'appelant ne doit rien accorder.

**Décision.**
1. **Barrière applicative.**
   - Toute opération passe par `TenantSession(database, TenantContext(organization_id, user_id))`.
   - Chaque transaction relit en base l'appartenance et le rôle : `TenantAccessDenied` (vu comme `NotFound`) ou
     `PermissionDenied`.
   - Chaque requête filtre explicitement `organization_id` (et `store_id`).
   - Identifiant mal formé → `NotFound`, jamais transmis au SQL.
   - Ressource d'un autre tenant et ressource inexistante → **même** erreur.
2. **Contexte base.**
   - `set_config('app.organization_id', $1, true)` et `app.user_id` : l'équivalent paramétrable de `SET LOCAL`,
     détruit à la fin de la transaction.
   - Compatible avec un pooler en mode transaction.
   - Sans contexte : aucune ligne visible, aucune écriture possible.
3. **RLS.**
   - `ENABLE` + `FORCE` sur les 15 tables de tenant.
   - Politique `USING` + `WITH CHECK` sur `organization_id`.
   - Lecture supplémentaire de ses propres appartenances par `app.user_id`.
4. **Clés étrangères composites** `(organization_id, store_id, …)` : une référence vers la ligne d'un autre tenant
   échoue même si cette ligne existe.
5. **Unicité préfixée.**
   - Toute clé primaire ou unique de donnée de tenant commence par `organization_id`.
   - Trouvé par les tests d'isolation : sans ce préfixe, insérer une ligne avec les identifiants exacts d'un autre
     tenant échouait en « doublon », confirmant son existence.
6. **Rôles.**
   - `mervio_app` : `NOLOGIN`, ni superutilisateur ni `BYPASSRLS`, ni propriétaire ; `UPDATE` limité colonne par
     colonne ; `DELETE` sur `memberships` seulement.
   - L'application **refuse de se connecter** avec un rôle superutilisateur ou `BYPASSRLS`, ou un serveur non UTF-8.
   - Les migrations passent par un rôle propriétaire distinct.
7. **Exception documentée : `users`.**
   - Identité plateforme : UUID, sujet OIDC opaque, date. Sans `organization_id` ni RLS, sans PII.
   - Nécessaire pour résoudre un utilisateur avant de connaître son organisation.

**Preuves** (`tests/persistence/test_persistence_isolation.py`, `…_tenancy.py`) :
- A ne lit pas B et B ne lit pas A, via les dépôts, avec les identifiants exacts ;
- écritures croisées refusées ;
- mauvais `store_id`, identifiants devinés et mal formés refusés ;
- contexte forgé refusé ;
- **mêmes appels avec un rôle BYPASSRLS** : la barrière applicative seule suffit ;
- **SQL brut du rôle applicatif** : la RLS seule suffit, sans contexte (0 ligne) comme avec un filtre explicite sur
  l'autre organisation ;
- insertion, mise à jour, révocation et déplacement de ligne vers un autre tenant refusés ;
- clés étrangères croisées refusées ;
- contexte local à la transaction.

**Risque résiduel accepté.**
- Les clés primaires UUID des ressources sont globales : une insertion SQL brute portant l'UUID exact d'une ressource
  d'un autre tenant échouerait en doublon.
- Inexploitable via l'application (identifiants toujours générés par le serveur) et suppose un UUID v4 déjà divulgué.

---

## ADR-004.1-007 — Politique de suppression explicite

**Décision.**

| Parent → enfant | Action | Raison |
|---|---|---|
| organisation → appartenances, boutiques | RESTRICT | purge d'organisation = job ordonné et tracé (004.2, R-21), jamais une cascade |
| utilisateur → appartenances, instantanés et exécutions créés | RESTRICT | auteur conservé pour l'audit |
| boutique → connexions, instantanés, exécutions, rapports | RESTRICT | historique analytique |
| connexion → instantanés | RESTRICT | révocation = `status = 'revoked'`, historique conservé |
| instantané → exécutions, rapports | RESTRICT | on ne supprime pas l'entrée d'un rapport existant |
| instantané → sources, lignes canoniques | **CASCADE** | contenu de l'instantané, sans existence propre ; seule purge possible d'un instantané non référencé |
| exécution → rapport | RESTRICT | purge dans l'ordre rapport → exécution → instantané |

- `mervio_app` n'a **aucun** droit `DELETE` ni `TRUNCATE` sur l'historique.
- Seule suppression applicative : une appartenance non propriétaire (`remove_member`).
- Aucun `SET NULL`, pour ne jamais produire d'orphelin silencieux.

---

## ADR-004.1-008 — Base de test éphémère, rôles réels, garde-fous

**Décision.**
- Les tests PostgreSQL n'utilisent que `MERVIO_TEST_ADMIN_DATABASE_URL`. Aucun défaut ; jamais
  `MERVIO_DATABASE_URL`.
- Hôte local exigé, sauf `MERVIO_TEST_ALLOW_REMOTE_DATABASE=1`.
- Par session :
  - base `mervio_test_<aléa>` ;
  - rôles `mervio_test_{migrator,app,bypass}_<aléa>` : le propriétaire n'est **pas** superutilisateur, l'application
    est le vrai rôle `mervio_app` ;
  - migrations Alembic depuis une base vide ;
  - destruction en fin de session de ce qui a été créé, et de rien d'autre.
- Isolation par test : `TRUNCATE` par le propriétaire.
- Données déterministes : `data/sample`, jeu synthétique (graine 7), scénario `revenue_drop` (graine 3).
- Sans la variable, ces tests sont **ignorés** (le moteur reste testable sans base). En CI,
  `MERVIO_REQUIRE_DATABASE_TESTS=1` les rend obligatoires.

---

## ADR-004.1-009 — Frontière moteur : deux extractions sans changement de comportement

**Contexte.** `run_analysis` charge des CSV puis analyse. Le chemin SaaS analyse un Dataset relu en base.

**Décision.**
- `analytics.pipeline.analyze_loaded_dataset(dataset, config, today)` : corps inchangé de `run_analysis`, qui
  devient `analyze_loaded_dataset(load_dataset(paths), config, today)`.
- `application.service.annotate_report(report, synthetic, label)` : les deux lignes qui ajoutaient
  `dataset_is_synthetic` et `analysis_label`, partagées par la CLI et le chemin persisté.
- Aucune formule, aucun seuil, aucune clé de rapport ni aucun test existant modifiés. Les 656 tests passent.

**Rejetées.**
- Dupliquer l'orchestration du pipeline dans la persistance (deux sources de vérité).
- Recalculer des KPI en SQL.

---

## ADR-004.1-010 — Environnement local : PostgreSQL 17, Docker Compose ou cluster jetable

**Décision.**
- `docker-compose.yml` : un seul service `postgres:17`.
  - Port lié à `127.0.0.1:55432`.
  - Mot de passe **obligatoire** depuis `.env` (ignoré par Git) ; aucune valeur par défaut.
- `scripts/dev_postgres.sh` : cluster jetable dans `.pgdata/` (ignoré), même port, sans Docker.
- `.env.example` : variables vides uniquement.
- Aucun autre service : pas de stockage objet, pas de file. Les fichiers bruts ne sont pas encore conservés ; seules
  leurs empreintes le sont.

---

## ADR-004.1-011 — Tables non créées en 004.1

| Table envisagée | Décision | Raison |
|---|---|---|
| `provenance_records` | non créée | la chaîne est portée par des clés étrangères composites ; une table parallèle dupliquerait sans garantie de cohérence |
| `connection_syncs` / `sync_runs` | reportée à 004.2 | cycle de vie de job ; en 004.1, `data_snapshots` est l'enregistrement d'import (début, fin, statut, motif, empreintes, compteurs) |
| `audit_events` | reportée à 004.2 | mission « Jobs & Audit » ; aucune écriture applicative exposée avant 004.3 |
| `customers` | non créée | dérivée des commandes dans le domaine (`Dataset.customers()`) ; la persister créerait une seconde vérité |
| `discounts` | non créée | la remise est un champ de commande dans le domaine (D-041) ; aucune entité remise |
| `jobs`, `schedules`, `idempotency_keys` | 004.2 et 004.3 | hors périmètre |

---

## ADR-004.1-012 — Index : préfixe tenant, et une seule clé qui commence par (organisation, instantané)

**Décision.**
- **Tables de ressources :** index composites `(organization_id, store_id, …)` pour les accès « récents d'une
  boutique » :
  - instantanés : `ingestion_started_at DESC` ;
  - exécutions : `started_at DESC` ;
  - rapports : `period_end DESC, created_at DESC`.
- Autres index de ressources :
  - `(organization_id, store_id, snapshot_id)` pour les liens exécution et rapport → instantané ;
  - `(organization_id, store_id, connection_id)` ;
  - `memberships (user_id)`.
- **Lignes canoniques :** clé primaire `(organization_id, snapshot_id, position)`. C'est l'accès unique de lecture :
  tout l'instantané, dans l'ordre.
- **Clés uniques secondaires** : identifiant métier **avant** `snapshot_id`, par exemple
  `(organization_id, source_record_id, snapshot_id, source)` et `(organization_id, order_ref, snapshot_id)`.

**Pourquoi (mesuré).**
- Première version : 5,9 s pour écrire 10 000 commandes, avec un coût croissant par lot.
- Cause, identifiée par bissection puis `EXPLAIN` du plan générique :
  - le contrôle de clé étrangère `order_lines → orders` choisissait, sur une table fraîchement remplie sans
    statistiques, l'index unique `(organization_id, snapshot_id, order_ref)` filtré sur `position` ;
  - cela revenait à parcourir tout l'instantané par ligne insérée.
- Après réordonnancement : `Index Only Scan using orders_pkey`, 0,64 s.
- Test de régression sur le catalogue : aucun index secondaire ne commence par `(organization_id, snapshot_id)`.

**Rejetées.**
- Index sur chaque colonne.
- `ANALYZE` dans la transaction d'import : exige un privilège de maintenance pour le rôle applicatif, et masquerait
  la cause.
