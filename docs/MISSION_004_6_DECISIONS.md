# Mission 004.6 — Architecture Decision Records

Format : Contexte · Options · Décision · Pourquoi · Conséquences · Rejetées.
Ces ADR complètent [`DECISIONS.md`](DECISIONS.md) (D-001 à D-105),
[`MISSION_004_0_DECISIONS.md`](MISSION_004_0_DECISIONS.md) (ADR-004-001 à 012) et
[`MISSION_004_1_DECISIONS.md`](MISSION_004_1_DECISIONS.md) (ADR-004.1-001 à 006).

Le contrat d'API a son document de référence nommé : [`API_CONTRACT.md`](API_CONTRACT.md),
suivant la convention de [`REPORT_CONTRACT.md`](REPORT_CONTRACT.md) et
[`LLM_CONTRACT.md`](LLM_CONTRACT.md).

> **Nomenclature.** Mission 004.6 est **API v1 — Vertical Slice**
> ([`MERVIO_FINAL_ROADMAP.md`](MERVIO_FINAL_ROADMAP.md), §004.6). Deux documents anciens
> portent l'ancienne numérotation et ne sont **pas** la source de vérité :
> `MISSION_004_0_HANDOFF.md` §004.6 (« LLM de production ») et `MISSION_004_2_HANDOFF.md`
> (« frontend 004.5, LLM 004.6 »). La roadmap a scindé cette mission ancienne : LLM vers
> 004.10, approbation d'actions vers 006.0. Réciproquement, l'ancienne « 004.3 API v1 » est
> devenue 004.6. `MERVIO_004_4_6_ACCEPTANCE.md` concerne **004.4.6** (purge tenant), sans
> rapport.

> **Décisions encore OUVERTES à la rédaction de ces ADR.** Elles ne sont pas ratifiées et ne
> doivent pas être présentées comme telles :
> - **D-098b** — choix du fournisseur OIDC déployé. Bloque la tranche F, pas A à E.
> - **D-103** — identité des constats. Trois propriétés sur quatre démontrées ; voir l'entrée.

---

## ADR-004.6-001 — Identité : contrat de vérification de jeton, indépendant du fournisseur

**Contexte.**
- La roadmap exige « validation de jetons OIDC (fournisseur choisi par ADR ; critères :
  hébergement UE, MFA, coût, SDK Next.js) ».
- Trois de ces quatre critères sont des propriétés de **déploiement**, pas de code.
- `users.idp_subject` existe depuis la révision `0001` ; `tenancy.ensure_user` sait déjà
  résoudre un sujet IdP en `user_id`.
- La culture de test du dépôt est hermétique : `test_llm_package_imports_no_network_or_ai_sdk`
  bannit `socket`, `http`, `httpx` sous `llm/`. Un fournisseur non reproductible hors ligne
  casserait cette propriété.

**Options.**
1. Choisir un fournisseur et écrire la vérification contre son SDK.
2. Ratifier le **contrat de vérification** — que nous possédons et testons — et traiter le
   fournisseur comme une configuration de déploiement.

**Décision — option 2.** Le contrat de vérification est :

| Règle | Valeur |
|---|---|
| Algorithmes acceptés | **`RS256` ou `ES256` uniquement** |
| Algorithmes refusés | `none`, et toute famille HMAC (`HS*`) — une clé symétrique partagée avec l'IdP n'est pas une signature vérifiable |
| Découverte des clés | métadonnées OIDC (`/.well-known/openid-configuration`) → `jwks_uri` |
| Cache JWKS | borné en **durée** et en **taille**, rotation par `kid` |
| Revendications vérifiées | `iss`, `aud`, `exp`, `nbf`, `iat`, `kid` — **toutes obligatoires** |
| Dérive d'horloge | bornée et **déclarée**, jamais implicite |
| Identité | `sub` → `users.idp_subject` via `tenancy.ensure_user` |
| Tests | **aucun réseau** : le JWKS est injecté par un seam, comme `llm/` le fait pour son fournisseur |

**Pourquoi.** Marier le code à un vendeur ferait payer un changement de fournisseur en refonte,
alors qu'il doit coûter une variable d'environnement. Le seul critère de la roadmap qui touche
le code — « jetons OIDC standard » — est satisfait par tout fournisseur certifié.

**Conséquences.**
- L'organisation n'est **jamais** dérivée d'une revendication de jeton ni d'un paramètre client :
  elle vient de `memberships`, vérifiée en base dans la transaction RLS
  (`TenantSession.transaction`). Une garde AST le rend structurel (ADR-004.6-002, garde 3).
- Un jeton valide n'accorde rien par lui-même : il identifie un `user_id`, et l'appartenance
  décide.
- Le fournisseur déployé reste **à décider** — voir D-098b, **ouvert**.

**Rejetées.**
- *Sessions par cookie côté serveur* : l'interface 004.7 est un client distinct ; un porteur
  standard est la frontière la plus simple à tester et à auditer.
- *Vérification par introspection distante* (RFC 7662) : un aller-retour réseau par requête,
  incompatible avec l'objectif p95 et avec des tests hermétiques.

---

## ADR-004.6-002 — Architecture d'API : pool, erreurs, pagination, versionnage, gardes

**Contexte.**
- `src/mervio/api/` n'existe pas. Premier point d'exposition réseau du produit.
- `Database` détient **au plus une** connexion (`database.py`), suffisant pour la CLI et le
  worker, insuffisant pour un serveur HTTP.
- La lecture du rapport persisté existe et est prouvée : `persistence/analyses.py`
  (`get_report_for_run`, `latest_report`), `tests/persistence/test_contract_2_0_persisted.py`.

**Décision.**

### 1. Pool de connexions — `psycopg_pool.ConnectionPool`, synchrone, **à côté** de `Database`

Un nouveau composant de persistance expose le **seul** contrat dont `TenantSession` a besoin :

```
transaction(*, organization_id, user_id) -> contextmanager[psycopg.Connection]
```

`Database` n'est **pas** modifiée. Voir D-099 pour le cycle de vie réel et la raison de ce
découplage (le worker appelle `Database.cancel_current_statement()` depuis un **autre fil**, sur
l'attribut `_connection` ; un pool rendrait cette cible ambiguë).

**Invariant de sécurité garanti** — *aucune connexion rendue au pool ne peut porter un contexte
tenant, et aucune requête sans contexte ne peut lire une ligne.* Garanti par **deux mécanismes
indépendants** :

1. `set_config('app.organization_id', …, true)` — le troisième argument est `is_local` : le GUC
   meurt au `COMMIT`/`ROLLBACK`. Un `checkin` de pool ne peut pas le transporter.
2. `app_current_organization_id()` vaut `NULLIF(current_setting('app.organization_id', true), '')::uuid`
   → **NULL** si absent. Les politiques comparent `colonne = NULL`, qui n'est jamais TRUE → **zéro
   ligne**. Le mode de défaillance est *refus*, jamais *mauvais tenant*.

`DISCARD ALL` est **interdit** dans le hook de remise : `SET TIME ZONE 'UTC'` est de portée
session, et l'effacer changerait les dates civiles du rapport — donc ses octets.

### 2. Erreurs — `application/problem+json` (RFC 9457)

Codes stables, aucun écho d'entrée, aucune trace. Une ressource d'un autre tenant et une
ressource inexistante produisent la **même** réponse 404 : `persistence/errors.py` rend déjà
`NotFound` et `TenantAccessDenied` indiscernables (ADR-004-003).

### 3. Pagination — curseur **opaque**

Le curseur encode une position dans un ordre **déjà total** (D-092, et l'ordre des catégories de
D-084). Il n'encode jamais un identifiant non ordonné. Filtres en **liste blanche**.

### 4. Versionnage — voir D-100 et [`API_CONTRACT.md`](API_CONTRACT.md)

`/v1` est un espace de noms ; `API_CONTRACT` est la forme. Ils ne bougent pas ensemble, et
**aucun des deux** ne suit `REPORT_CONTRACT`.

### 5. Trois gardes AST, posées **avant la première route métier**

| Garde | Propriété |
|---|---|
| 1 | aucun module de `api/` n'importe `mervio.analytics` → **aucune route ne calcule de KPI** |
| 2 | aucun module de `api/` n'importe `jsonschema` ni le fichier de schéma → un rapport **1.x** persisté reste servi |
| 3 | `organization_id` n'est jamais lu depuis un chemin, une requête ou un corps → l'organisation vient de l'appartenance |

**Pourquoi des gardes AST et pourquoi d'abord.** Les trois propriétés sont structurelles : les
reposer après coup signifierait supprimer du code qui fonctionne. La garde 2 interdit la pire
erreur disponible — rejeter à la lecture un rapport légitimement persisté. Le motif existe déjà
dans le dépôt (`test_llm_provider.py`, `test_llm_package_imports_no_network_or_ai_sdk`).

**Conséquences.**
- L'API est un **lecteur** : elle projette ce que le moteur a produit et que la persistance a
  scellé. Elle ne recalcule rien.
- `jsonschema` reste un artefact de test et n'entre dans aucun chemin de lecture
  (handoff 004.5, §9.2).
- Aucun convertisseur `1.x → 2.0` n'est écrit, ni ici ni ailleurs.

**Rejetées.**
- *Pool async* : la persistance entière est psycopg3 synchrone — `TenantSession`, `jobs`,
  `analyses`, `snapshots`, le worker, et les fixtures de 3594 tests. Le passage async est un
  chantier hors périmètre, pour un gain nul sur l'objectif mesuré (p95 < 300 ms en lecture).
- *PgBouncer en transaction pooling* : compatible avec un GUC transactionnel, mais ajoute un
  composant à exploiter et casse `pg_stat_activity.application_name`, utilisé depuis 004.3.
- *Pool SQLAlchemy* : SQLAlchemy n'est présent que comme dépendance d'Alembic ; aucun module de
  `src/` ne l'importe. L'introduire dans le chemin de requête ajouterait une abstraction que ce
  dépôt a refusée (SQL explicite partout).
- *Pas de pool, une connexion par requête* : paie la poignée de main et `_verify()` à chaque
  appel, ce qui compromet directement le p95.

---

## ADR-004.6-003 — SEC-05 : le privilège de colonne, et pourquoi pas RLS

**Contexte.** `users` était la seule table de tenant sans restriction de lecture : `SELECT` était
accordé à `mervio_app` sur **toutes** ses colonnes depuis `0001`. TD-27 proposait « restrict via
view or policy on own row ».

**Options.**
1. Politique RLS « sa propre ligne ».
2. Politique RLS « sa propre ligne OU co-membre », plus une fonction de provisionnement.
3. Privilège de **colonne**, plus des fonctions `SECURITY DEFINER` à clé exacte.

**Décision — option 3.** Voir [D-106](DECISIONS.md) pour le détail et la mesure.

**Pourquoi.** L'option 1 est **impossible**, mesuré : `ensure_user` provisionne **sans contexte
tenant**, donc toute politique adossée à `app_current_user_id()` refuse. L'option 2 réduit
l'énumération sans l'éliminer — un membre pourrait toujours lister les sujets de son
organisation. L'option 3 ferme la menace exacte : RLS ne distingue pas une lecture à clé d'un
balayage, le privilège de colonne si.

**Conséquences.**
- Cinq définisseurs de plus, nommés dans trois inventaires de test.
- `app_current_service_id()` passe INVOKER → DEFINER, avec `session_user` au lieu de
  `current_user` : voir D-106, c'est une obligation, pas un choix.
- L'`id` d'une autre ligne reste lisible, et c'est dit comme tel dans le test et dans D-106.

**Rejetées.** *Vue au lieu de fonctions* : une vue ne peut pas porter le provisionnement
(`INSERT ... ON CONFLICT`), qui est précisément le cas qu'aucune politique ne pouvait servir.

---

## Graphe de dépendances réel des tranches (révisé le 13/10/2026)

Le plan de tranches du registre de décision supposait que `recommendation_states`,
`idempotency_keys` et `rate_limit_buckets` arrivaient tous dans une migration unique, avant la
première route. Les tranches A à E ont montré que c'était faux sur trois points. Ce qui suit
remplace ce plan.

```
A  documentation et décisions          LIVREE   bb7b438
B  SEC-05, migration 0018              LIVREE   8e3ba1f
C  extra api, verrous                  LIVREE   f53126f
D  pool, invariant prouvé              LIVREE   0fc0cfc
E  squelette, API_CONTRACT, gardes     LIVREE   6fbcb02
        │
        ├─ E'  projections PURES, sans route        PRETE A DEMARRER
        │      rapport persisté -> corps de réponse, ETag, dérivation de `findings`
        │      depuis les quatre sections. Aucune route enregistrée, donc aucune
        │      surface non authentifiée. Les trois gardes AST la couvrent déjà.
        │
        └─ F  authentification OIDC, /v1/me         BLOQUEE PAR D-098b
                 │
                 └─ G  tranche verticale: route de rapport      BLOQUEE PAR F
                          │    son parcours HTTP protégé exige une identité vérifiée
                          │
                          └─ H  routes de lecture restantes
                                   │
                                   └─ I  routes d'écriture
                                            + migration `idempotency_keys`,
                                              `rate_limit_buckets` DANS CETTE TRANCHE
                                            │
                                            └─ recommandations: statut
                                                  + `recommendation_states`
                                                  BLOQUEE PAR D-103
```

**Ce que « préparer sans exposer » veut dire, précisément.** La tranche **E′** écrit des
fonctions pures et leurs tests : un rapport persisté en entrée, un corps de réponse en sortie,
plus le calcul d'ETag et la dérivation de `findings` depuis `critical_issues` / `warnings` /
`setup` / `opportunities`. Rien n'est enregistré auprès de l'application, donc aucune route
non authentifiée n'existe à aucun moment. Les trois gardes AST s'appliquent dès l'écriture.

**Pourquoi G reste bloquée malgré E′.** G est la tranche verticale : son intérêt est de
traverser jeton → appartenance → `TenantSession` → persistance → HTTP en une fois, et de
prouver du même coup « accès croisé = 404 ». Sans F, elle ne prouverait que la moitié basse,
que E′ couvre déjà. La livrer à moitié ferait passer pour validée une isolation HTTP qui ne
l'est pas.

**Ordre documenté des deux tables de la tranche d'écriture.** `idempotency_keys` et
`rate_limit_buckets` arrivent **dans la même migration que les routes qui les consomment**,
jamais avant. Précédent du dépôt : `0013` a livré `raw_objects` avec son chemin d'import. Une
table sans consommateur a une forme que rien ne valide, et son premier usage la trouve presque
toujours fausse.

**`recommendation_states` est bloquée par D-103, pour une raison mesurée.** Voir le statut du
13/10/2026 de D-103 : `finding_id` est stable, et c'est le problème — un identifiant non scopé
désigne une catégorie, pas une occurrence. La clé `(organisation, boutique, finding_id)` est
sûre pour les constats scopés et fausse pour les autres. Il manque une notion d'épisode, et
c'est un arbitrage produit.

---

## Note de décision produit — sémantique du statut d'une recommandation (D-103)

**Ceci n'est pas une décision. C'est le choix à faire, avec ses conséquences mesurées.**
`recommendation_states` n'est pas créée tant qu'il n'est pas tranché.

**Le fait qui force le choix.** `finding_id` vaut `{type}` ou `{type}:{scope}`. Mesuré sur 17
scénarios × 2 profils, graine 3 contre graine 11 : **92 identifiants non scopés conservés**,
7 disparus, 24 apparus, **0 cas de « même type, scope différent »**. Donc un identifiant
**non scopé** (`refunds_anomaly`, `revenue_decline`, `roas_anomaly`, `aov_anomaly`) **est** son
type : il désigne une **catégorie**, pas une occurrence — et ces constats **vont et viennent**
(`refunds_anomaly` disparaît dans trois configurations, réapparaît dans une autre). Un
identifiant **scopé** (`campaign_opportunity:90010004`, `low_margin_product:<sku>`) désigne un
objet, et y attacher un statut durable est le comportement attendu.

| Option | Clé | Ce qu'un « fait » signifie | Conséquence à accepter |
|---|---|---|---|
| **A — par type de problème** | `(org, store, finding_id)` | « ne me reparle plus de cette catégorie » | Marquer `refunds_anomaly` fait le **supprime définitivement**, y compris une poussée genuinement nouvelle plus tard. C'est un **filtre d'affichage**, pas un suivi d'action. Simple, et dangereux si présenté comme un suivi. |
| **B — par occurrence** | `(org, store, run_id, finding_id)` | « j'ai traité ce constat, dans ce rapport » | Aucune continuité : le même problème au rapport suivant est un constat **neuf**, non traité. Le marchand re-marque chaque semaine. Fidèle, et fatigant. |
| **C — par épisode** | `(org, store, finding_id, episode)`, l'épisode se fermant après N périodes **sans** le constat | « j'ai traité cette poussée ; si elle revient, redis-le-moi » | Demande une règle de réouverture explicite — N, et ce que « sans le constat » veut dire quand une période manque de données. C'est le comportement qu'un marchand attend, au prix d'un état à entretenir. |
| **D — par période** | `(org, store, finding_id, period_label)` | « traité pour la semaine du … » | Lisible et borné, mais un problème qui dure trois semaines demande trois marquages. Confond la granularité du rapport avec celle de l'action. |

**Ce que la mesure dit, et ne dit pas.** Elle dit que A est faux pour les constats non scopés
et correct pour les scopés — donc qu'une clé **unique** pour les deux familles est
nécessairement fausse pour l'une d'elles. Elle ne dit **pas** laquelle des options B, C ou D
sert le mieux le produit : c'est un arbitrage de produit, et il n'est pas pris ici.

**Si C est retenue, ce qu'il faudra ratifier en plus :** la valeur de N, le traitement d'une
période sans donnée, et si un changement de **sévérité** du constat rouvre l'épisode.

**Travail préparatoire autorisé et fait :** la mesure de stabilité ci-dessus, et la
démonstration du mécanisme de collision de `_slug` (quatre formes distinctes de SKU rendent le
même identifiant ; `'é'` survit à `_slug` et viole le motif que le dépôt s'impose). Aucune
persistance de statut n'est implémentée.

---

## ADR-004.6-004 — Fournisseur OIDC : Keycloak, et ce que ce choix n'engage pas

**Contexte.** D-098a a ratifié le contrat de vérification de jeton, indépendant du fournisseur.
Restait le fournisseur déployé, laissé ouvert faute de faits vérifiables.

**Décision.** **Keycloak**, ratifié le 13/10/2026. Voir [D-098b](DECISIONS.md) pour les motifs,
la vérification de certification et les réserves de coût.

**Pourquoi ce choix ne change rien au code.** L'engagement reste D-098a. Keycloak entre par des
**variables d'environnement** — URL d'émetteur, audience — et rien de `src/` ne le nomme. Aucun
SDK propriétaire : la découverte OIDC et la validation JWT se font sur les standards, avec un
JWKS injectable pour les tests hermétiques. Changer de fournisseur doit coûter une variable
d'environnement, pas une refonte.

**Conséquences.**
- Le JWKS est **injecté** en test : aucun réseau, conformément à la culture du dépôt.
- Keycloak s'adosse à PostgreSQL 14–18 : le `docker-compose.yml` n'acquiert aucun substrat
  nouveau à exploiter.
- À vérifier **avant production**, et consigné comme tel : coût d'exploitation, coût du support
  commercial, et conformité de la **version déployée** à un profil de certification.

**Rejetées.** *Zitadel* : techniquement apte et auto-hébergeable, mais son auto-hébergement
communautaire est sous **AGPL**, ce qui demande une détermination juridique que cette mission
n'a pas les moyens de trancher, et ses propres pages se contredisent sur les paliers, la devise
et les régions. *Auth0* : sources contradictoires sur un traitement aux États-Unis sous Okta.

---

## `app_find_user` — portée actuelle de l'oracle d'existence

**État mesuré.** Les seuls appelants de `app_find_user` et `app_ensure_user` sont la **CLI
admin** (`admin/operations.py`, `cli/admin.py`) et le **worker**
(`workers/resolution.py`, `persistence/service.py`). **Aucune surface HTTP ne les atteint** :
`src/mervio/api/` ne contient que `contract`, `errors`, `health` et `app`. L'oracle exige donc
aujourd'hui un **identifiant de base de données applicatif**, pas une requête réseau.

**Ce qui est exigé avant toute exposition HTTP**, et c'est la tranche F qui l'introduira en
résolvant le sujet d'un jeton :
1. **identité vérifiée** — la résolution ne s'atteint qu'après validation complète du jeton
   (D-098a) ; jamais depuis une route anonyme ;
2. **limitation de débit** par utilisateur **et** par organisation, plus stricte sur ce chemin
   que sur une lecture ordinaire, puisqu'un oracle se sonde en boucle ;
3. **aucun écho** : une réponse ne doit pas distinguer « sujet inconnu » de « sujet connu mais
   hors de votre organisation » — même règle que l'accès croisé = 404.
