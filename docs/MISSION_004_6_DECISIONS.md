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
