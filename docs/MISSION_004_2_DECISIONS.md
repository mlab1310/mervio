# Mission 004.2 — Architecture Decision Records

Format : Contexte · Options · Décision · Pourquoi · Conséquences · Rejetées.
Ces ADR complètent `docs/DECISIONS.md` (D-001 à D-048), `docs/MISSION_004_0_DECISIONS.md` (ADR-004-001 à 012) et
`docs/MISSION_004_1_DECISIONS.md` (ADR-004.1-001 à 012). Aucun ne modifie un contrat analytique.

Deux ADR de 004.0 sont **exécutés** ici, pas révisés :
- ADR-004-008 (file dans PostgreSQL) : le spike qu'il demandait est ADR-004.2-001 ;
- ADR-004-009 (observabilité) : mis en œuvre par ADR-004.2-005.

---

## ADR-004.2-001 — Table maison plutôt que procrastinate

**Contexte.** ADR-004-008 a choisi PostgreSQL comme file et a explicitement reporté le choix entre une table maison
et [procrastinate](https://procrastinate.readthedocs.io/) (MIT, file PostgreSQL pour Python) « au premier jour de la
mission jobs, par spike documenté ». C'est ce spike.

**Options évaluées.**

| Option | Pour | Contre |
|---|---|---|
| **Table maison** (`jobs` + dépôt) | RLS forcée et clés composites appliquées comme à toute table de tenant (règles §4 du handoff 004.1) ; schéma sous notre contrôle Alembic ; aucune dépendance ajoutée ; le contexte tenant est posé par `TenantSession`, jamais par la bibliothèque | à écrire et à tester : prise, bail, reprises, purge |
| procrastinate | prise `FOR UPDATE SKIP LOCKED` éprouvée, `LISTEN/NOTIFY`, planification cron, worker asynchrone | son schéma est à lui : ni `organization_id`, ni RLS, ni clé composite, ni politique RESTRICTIVE ; il ouvre ses propres connexions et ses propres transactions, donc hors de `TenantSession` ; ajouter RLS par-dessus reviendrait à réécrire son schéma et ses requêtes, c'est-à-dire à maintenir un fork ; dépendance asynchrone (SQLAlchemy async, psycopg async) dans un projet synchrone |
| Celery, Dramatiq, RQ + Redis | écosystème mûr | infrastructure supplémentaire, explicitement hors périmètre (ADR-004-008) |

**Décision.** Table `jobs` maison, dépôt `mervio.persistence.jobs`, worker `mervio.workers`.

**Pourquoi.** Le coût réel de procrastinate n'est pas la file, c'est la **tenancy**. Mervio a construit en 004.1 une
isolation prouvée à trois niveaux, et sa règle est que *toute* table de tenant porte `organization_id NOT NULL`,
RLS `ENABLE` + `FORCE`, clés étrangères composites et clés uniques préfixées. Une file dont les lignes ne
respectent pas ces règles serait le seul endroit du schéma où un travail d'une organisation pourrait être lu par
une autre. Le code évité (≈ 400 lignes) est très inférieur au coût d'un fork.

**Conséquences.**
- `LISTEN/NOTIFY` et la planification cron ne sont pas fournis. Le worker interroge la file ; la planification
  récurrente (ADR-004-008 : table `schedules`) reste à faire.
- Le débit mesuré (≈ 1 460 travaux/s à 4 workers) dépasse de plusieurs ordres de grandeur le besoin de 004.2
  (un import et une analyse par boutique et par jour).

**Rejetées.** procrastinate sans RLS : reviendrait à exempter la file de la règle d'isolation, au moment précis où
le worker devient le composant qui touche le plus de tenants.

---

## ADR-004.2-002 — Le worker sert une liste explicite de tenants ; aucun contournement global de RLS

**Contexte.** Un worker doit trouver du travail. La prise se fait sous RLS, donc dans le contexte d'**une**
organisation. Comment un worker sert-il plusieurs organisations sans voir la file globale ?

**Options évaluées.**

| Option | Pour | Contre |
|---|---|---|
| Rôle `BYPASSRLS` pour le worker | une requête voit toute la file | annule l'isolation au niveau exact où elle compte le plus ; `Database` refuse déjà une connexion superutilisateur ou BYPASSRLS (004.1) ; un défaut de filtre applicatif deviendrait une fuite entre tenants |
| Fonction `SECURITY DEFINER` de répartition, possédée par un rôle dédié `mervio_dispatcher` avec une politique `FOR SELECT TO mervio_dispatcher USING (status = 'queued' AND available_at <= now())` | privilège minimal et explicite : le répartiteur ne voit que des lignes prêtes et ne rend que des métadonnées d'aiguillage ; `mervio_app` n'est pas membre du rôle, donc ne peut pas s'en servir autrement | exige un rôle **global au cluster** et le transfert de propriété de la fonction : si le rôle préexiste et que le rôle de migration n'en est pas membre, la migration échoue ; le worker n'a pas encore d'identité de service (l'OIDC arrive en 004.3) |
| **Liste explicite de `TenantSession`** | aucun privilège nouveau ; la prise passe par la vérification d'appartenance ET par RLS ; prendre le travail d'un autre tenant n'est pas « interdit par le code », c'est hors de portée de la requête | ne passe pas à l'échelle de milliers d'organisations : il faut connaître les tenants à servir |

**Décision.** `Worker(sessions=[...])` : le worker tourne sur une liste explicite de sessions, une par organisation
servie, et alterne équitablement entre elles. La fonction de répartition est **conçue et documentée ici, pas
implémentée** ; elle appartient à 004.3, avec l'identité de service et le pool de connexions.

**Pourquoi.** L'option retenue est la seule qui n'ajoute aucun privilège. Tant qu'il n'existe ni identité de
service, ni pool, ni API, un worker est lancé par un opérateur pour des organisations connues : la liste explicite
est à la fois la plus sûre et la plus honnête. Ajouter aujourd'hui un rôle global au cluster créerait une
dépendance opérationnelle (rôle partagé entre bases, propriété de fonction) sans besoin correspondant.

**Conséquences.**
- `tests/persistence/test_jobs_worker.py::test_a_worker_never_reaches_a_tenant_it_does_not_serve` fige la
  propriété.
- La montée en charge multi-tenant est un point ouvert du handoff, avec le schéma de la fonction de répartition.
- Le worker exécute avec l'autorisation d'un **membre réel** de l'organisation : le rôle est revérifié à chaque
  transaction. Un travail de purge mis en file par un `owner` échoue si le worker ne dispose que d'un `analyst`
  (testé). L'autorisation n'est donc jamais héritée du seul fait qu'un travail existe.

---

## ADR-004.2-003 — Machine à états, bail et reprises : la base fait foi

**Contexte.** Un travail passe par des états. Le code peut se tromper ; une requête SQL brute d'un futur outil
d'exploitation encore plus.

**Décision.** Trois garanties sont portées par PostgreSQL, pas seulement par Python :
1. **Machine à états** (trigger `jobs_guard_transition`) : seules les transitions
   `queued → running | cancelled | queued` et `running → succeeded | failed | queued` passent ; un état terminal
   est définitif ; les colonnes d'identité (`organization_id`, `job_type`, `payload`, `correlation_id`,
   `max_attempts`, `created_at`…) sont immuables ; `attempts` ne décroît jamais.
2. **Prise atomique** : `SELECT … FOR UPDATE SKIP LOCKED` dans une CTE et transition vers `running` dans la
   **même instruction** (`jobs._CLAIM_SQL`). Aucun intervalle entre la sélection et la prise.
3. **Bail** : `lease_expires_at` ; passé l'échéance, `recover_stale_jobs` remet la **même ligne** en file, avec
   ses tentatives. Seul le détenteur du verrou (`locked_by`) peut publier un résultat : un worker revenu d'entre
   les morts se voit refuser son résultat (`JobLeaseLost`), il ne peut pas écraser le travail de son successeur.

**Cinq états, pas plus.** `queued`, `running`, `succeeded`, `failed`, `cancelled`.
- L'échec transitoire ne crée **pas** d'état intermédiaire : le travail retourne directement en `queued` avec
  `last_error_code` renseigné. `failed` signifie donc sans ambiguïté « terminal » — c'est l'état que 004.0
  appelait `dead`.
- `cancelled` est conservé parce qu'il est le seul moyen de retirer un travail encore en file : le rôle
  applicatif n'a pas le droit de supprimer un travail non terminal, et la purge non plus.

**Reprises.** Bornées par `max_attempts` (3 par défaut). Délai exponentiel **déterministe** : 30 s, 60 s, 120 s…
plafonné à 1 h. Aucune gigue : un travail n'est pris que par un worker à la fois, la gigue n'apporterait qu'une
imprévisibilité de test.

**Erreurs transitoires et définitives.** Une erreur de validation n'est jamais rejouée : rejouer trois fois un CSV
invalide ne fait que retarder le diagnostic. Classification dans `workers/errors.py` :
définitive pour `IngestionError`, `InsufficientDataError`, `NotFound`, `PermissionDenied`, `ImportRejected`,
`FileNotFoundError` et les violations de contrainte ; transitoire pour les SQLSTATE de verrou, de sérialisation,
de ressource et de connexion ; inconnue (donc reprise, dans le budget de tentatives) pour le reste.

**Conséquences.** Le nombre de tentatives est incrémenté **à la prise**, pas à l'échec : un worker tué juste après
la prise consomme une tentative, ce qui est voulu — sinon un travail qui fait systématiquement tomber son worker
serait repris indéfiniment.

---

## ADR-004.2-004 — Purge : ce qu'elle peut supprimer, et ce qu'elle ne pourra jamais

**Contexte.** R-21 (rétention et suppression) est ouvert depuis 004.0. Les tables de service grossissent sans fin ;
les données d'un client, elles, portent une chaîne de provenance qu'aucune purge ne doit couper.

**Décision.** La purge de 004.2 ne touche que deux tables : `jobs` (travaux terminés) et `audit_events` (événements
expirés), dans cet ordre.

**Ce qui la borne, et qui n'est pas du code applicatif :**
- `reports`, `analysis_runs`, `data_snapshots`, `snapshot_sources` et les lignes canoniques sont **hors d'atteinte**
  du rôle applicatif : aucun droit `DELETE` depuis 004.1. La chaîne
  rapport → exécution → instantané → source ne peut pas être coupée par une purge, même buggée.
- Politique RESTRICTIVE `jobs_purge_terminal_only` : un travail en file ou en cours est indestructible, et un
  travail terminé ne le devient qu'après **une heure**. Vrai en SQL brut, vrai pour son propre tenant.
- Politique RESTRICTIVE `audit_events_retention_floor` : rien de plus récent que **30 jours** n'est supprimable.
- `RetentionPolicy` refuse une rétention d'audit inférieure à la plus longue rétention de travaux : l'histoire d'un
  travail survit toujours au travail lui-même.

**Défauts opérationnels** (ce ne sont **pas** des politiques métier ni de conformité ; ils bornent la croissance des
tables de service et doivent être revus avec le RGPD en 004.3) : succès 7 jours, échecs 30 jours, annulations
7 jours, audit 365 jours, 1 000 lignes par lot et par catégorie.

**Conséquences.** La purge est tracée par `purge.started` et `purge.completed` ; ses propres traces sont récentes,
donc protégées par le plancher. Elle est répétable sans dommage (testé).

**Rejetées.** Suppression en cascade depuis l'organisation : détruirait silencieusement la provenance. La
suppression d'une organisation entière reste un point ouvert (R-21), et exigera une décision écrite sur ce qui est
conservé.

---

## ADR-004.2-005 — Observabilité : logs JSON corrélés, métriques dérivées, pas d'OpenTelemetry

**Contexte.** ADR-004-009 demande des logs JSON corrélés, des métriques et, *quand une infrastructure existe*, des
traces OpenTelemetry. Aucune infrastructure n'existe.

**Décision.**
- Logs JSON, une ligne = un objet, bibliothèque standard uniquement (`mervio.observability.logging`).
- Corrélation par `ContextVar` relu par le **formateur** : les modules existants du moteur entrent dans la trace
  d'une exécution sans être modifiés. C'est ce qui permet à `job.claimed`, `import.started`,
  `instantané scellé` (log de `persisted_analysis`) et `job.succeeded` de porter le même `correlation_id`.
- Métriques : aucun backend. Les compteurs demandés (`jobs_total`, succès, échecs, reprises, `job_duration_ms`,
  `queue_wait_ms`, `attempt_count`) sont **dérivables** des logs et de l'audit, et `jobs.queue_statistics` les lit
  directement en base.
- OpenTelemetry : hors périmètre, explicitement, tant qu'il n'y a pas de collecteur.

**Pourquoi.** Un exportateur sans collecteur ajoute une dépendance, une surface de configuration et zéro
observabilité. Les logs corrélés répondent à la question opérationnelle d'aujourd'hui (« qu'est-il arrivé à ce
travail ? ») ; l'audit répond à la question de conformité (« qui a fait quoi ? »).

---

## ADR-004.2-006 — Représentation sûre des erreurs : un code, jamais un message brut

**Contexte.** Un message d'exception contient souvent une valeur de donnée : un chemin de fichier, un identifiant,
une adresse. Ces messages finissent dans la file (`last_error`), dans l'audit (`metadata`) et dans les logs.

**Décision.**
- Les logs publient le **type** d'une exception, jamais son message ni sa trace (`error_type`).
- `last_error` est normalisé : sauts de ligne supprimés, 2 000 caractères au plus, chemin absolu remplacé par
  `[redacted]`.
- Un **code** court et stable accompagne chaque échec (`validation_failed`, `payload_invalid`, `not_found`,
  `sqlstate_40P01`…). C'est ce code qui est indexable, comparable et utilisable par une alerte.
- Trois filtres, du plus large au plus étroit : les **logs** masquent toute clé évoquant un secret ou une PII,
  tout chemin absolu, et tronquent ; l'**audit** passe par le même filtre ; la **charge utile d'un travail** refuse
  une clé de secret et toute valeur dépassant 4 Ko — elle localise une source, elle ne la transporte pas.

**Pourquoi.** Perdre un champ de diagnostic coûte moins cher que publier un jeton. Les filtres sont volontairement
larges et leur liste est un point de revue explicite (`FORBIDDEN_KEY_FRAGMENTS`).

**Conséquences.** Le diagnostic complet d'un échec demande le log du worker au moment de l'incident ; la file et
l'audit n'en gardent que la forme. C'est un compromis assumé, noté dans les risques.
