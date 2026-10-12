# API CONTRACT 1.0 — contrat HTTP de Mervio (Mission 004.6)

L'API `/v1` est le **contrat d'exposition** du produit. Elle sert ce que le moteur a produit et
que la persistance a scellé. Ce document est la référence de sa forme, suivant la convention de
[`REPORT_CONTRACT.md`](REPORT_CONTRACT.md) et [`LLM_CONTRACT.md`](LLM_CONTRACT.md).

> **État.** Ce document est ouvert par la tranche A de 004.6 et décrit les propriétés
> **ratifiées**. Les routes sont décrites au fur et à mesure qu'elles sont livrées ; une route
> absente de ce document n'existe pas. Les décisions **D-098b** (fournisseur OIDC) et **D-103**
> (identité des constats) sont **ouvertes** — voir [`DECISIONS.md`](DECISIONS.md).

## Versions

Trois choses distinctes, qu'il ne faut jamais confondre. Matrice complète :
[`VERSIONS.md`](VERSIONS.md).

| | Valeur | Propriétaire | Couvre | Bouge quand |
|---|---|---|---|---|
| `/v1` | préfixe de chemin | routage | l'**espace de noms** des URL | jamais, sauf refonte cassante → `/v2` **coexistant** |
| `API_CONTRACT` | **`1.0`** | `api/contract.py` | la **forme** des requêtes et réponses | une route gagne, perd ou retype un champ |
| document OpenAPI 3.1 | artefact | généré depuis Pydantic v2 | la description figée | régénéré dès que `API_CONTRACT` bouge ; instantané comparé en CI |

**`API_CONTRACT` ne suit pas `REPORT_CONTRACT`.** C'est la propriété porteuse de ce document.
La route de rapport sert le rapport **tel que persisté**, et `reports` est immuable
(`reports_forbid_update`, révision `0003`) : un rapport **1.x** légitimement persisté doit donc
rester **servi** sous `API_CONTRACT 1.0`. Coupler les deux versions rendrait illisible par
contrat une donnée que le dépôt s'est engagé à conserver lisible
([`REPORT_CONTRACT_1X_TO_2_0.md`](REPORT_CONTRACT_1X_TO_2_0.md) : « aucun convertisseur n'existe
et aucun n'est prévu »).

**Politique de changement.** Additif → mineure (`1.0` → `1.1`), sans nouveau préfixe. Retrait ou
retypage → majeure **et** nouveau préfixe `/v2`, l'ancien restant servi.

## Propriétés verrouillées

### L'API ne calcule rien
Aucun module de `src/mervio/api/` n'importe `mervio.analytics`. Une garde AST le vérifie. Une
route projette, filtre et pagine ; elle ne dérive aucun KPI, aucun score, aucune contribution.

### L'API ne valide pas les rapports persistés
Aucun module de `api/` n'importe `jsonschema` ni `schemas/report/2.0.json`. Le schéma valide ce
que le moteur **produit** ; il refuserait un rapport 1.x légitimement persisté. Garde AST.

### L'organisation vient de l'appartenance, jamais du client
`organization_id` n'est jamais lu depuis un chemin, une requête ou un corps. Elle est résolue
depuis `memberships`, vérifiée **en base** dans la transaction RLS
(`TenantSession.transaction`). Garde AST.

### Tenant croisé et inexistant sont indiscernables
Une ressource d'une autre organisation et une ressource absente produisent la **même** réponse
**404**. `persistence/errors.py` rend déjà `NotFound` et `TenantAccessDenied` indiscernables
(ADR-004-003). Aucune réponse, aucun en-tête, aucune durée ne doit distinguer les deux cas.

### Aucun identifiant client ne sort
Le rapport ne porte aucun identifiant client (AN-17 / D-056) : `customers.top_customers[]` n'a
que `rank`, `orders`, `revenue_share`. Aucune route, aucune projection ne peut en republier.

### Aucun chemin de fichier n'entre
Un dépôt produit un `raw_object_id` ; la charge d'un travail ne transporte jamais un chemin
(D-054, `jobs.validate_payload`).

## Erreurs — `application/problem+json` (RFC 9457)

Toute réponse d'erreur est un `problem+json` à **code stable**. Aucun écho de l'entrée, aucune
trace, aucun identifiant interne. Le `correlation_id` de la requête est le seul lien vers les
journaux.

## Authentification

Porteur JWT, `RS256` ou `ES256` uniquement. `none` et les familles HMAC sont refusés. `iss`,
`aud`, `exp`, `nbf`, `iat` et `kid` sont tous vérifiés. Détail : ADR-004.6-001.

Un jeton valide **n'accorde rien** par lui-même : il identifie un `user_id`. L'appartenance
décide, et le rôle (`owner` / `admin` / `analyst` / `viewer`) décide de la permission.

## Pagination

Curseur **opaque**, encodant une position dans un ordre **déjà total** (D-092 ; ordre des
catégories : D-084). Le curseur n'encode jamais un identifiant non ordonné. Filtres en **liste
blanche**.

## Couverture

| Suite | Objet |
|---|---|
| `tests/api/test_api_guards.py` | les trois gardes AST |
| `tests/api/test_health.py` | `healthz`, `readyz` |
| `tests/persistence/test_pool_isolation.py` | l'invariant de sécurité du pool (D-099) |
| `tests/persistence/test_users_visibility.py` | SEC-05 / TD-27 |
