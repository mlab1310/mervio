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
`aud`, `exp`, `nbf`, `iat`, `sub` et `kid` sont tous vérifiés. Détail : ADR-004.6-001.

Un jeton valide **n'accorde rien** par lui-même : il identifie un `user_id`. L'appartenance
décide, et le rôle (`owner` / `admin` / `analyst` / `viewer`) décide de la permission.

### Le jeton ne vient que de l'en-tête `Authorization`
Ni cookie, ni paramètre de requête. Un jeton dans une URL finit dans les journaux de tout
intermédiaire ; un jeton en cookie demanderait une défense CSRF que cette API n'a pas.

### Tout refus est un **401 indifférencié**
Jeton absent, malformé, signature invalide, algorithme interdit, mauvais émetteur, mauvaise
audience, expiré, pas encore valide, `kid` absent ou inconnu, sujet absent, vide, réservé ou
trop long : **le même document, octet pour octet**. Distinguer « mauvaise audience » de
« signature invalide » aiderait un attaquant à calibrer sa tentative suivante. Le code stable
de la cause vit dans les journaux, jamais dans la réponse.

### Un principal de service ne s'authentifie jamais par HTTP
Un `sub` préfixé `service:` est refusé : ce serait une escalade vers l'identité du worker
depuis l'extérieur.

### Les algorithmes ne sont pas configurables
`api.auth.ALLOWED_ALGORITHMS` est une constante. Une installation ne doit pas pouvoir s'ouvrir
à `none` ou à une famille HMAC par une variable d'environnement.

### Cache JWKS : appels sortants bornés (F.1)
La première version relisait le JWKS dès qu'un `kid` manquait, **sans intervalle minimal**, et
chaque relecture refaisait la découverte OIDC. **Mesuré : 20 requêtes à `kid` inconnu →
42 appels HTTP** vers l'émetteur, devant un contrôle qui précède toute authentification, donc
sans limitation de débit. Trois corrections, chacune mesurée :

| Mécanisme | Effet mesuré |
|---|---|
| `jwks_uri` **validé** mis en cache | une relecture coûte **1** appel au lieu de 2 ; la découverte reste **unique** |
| **Intervalle minimal** `MERVIO_OIDC_MIN_REFRESH_SECONDS` (défaut 30 s, bornes [1, 3600], jamais > `JWKS_CACHE_SECONDS`) | 20 requêtes à `kid` inconnu → **2** appels au total |
| Une tentative **échouée** marque l'horodatage comme une réussie | 10 appels sur émetteur en panne → **1** tentative ; aucune boucle |
| **Recuperation unique** (verrou de récupération + double vérification) | 8 requêtes simultanées à froid → **1** récupération ; cache expiré + 8 concurrentes → **1** relecture |

**Ce que l'intervalle coûte, et c'est assumé** : une rotation légitime est reconnue **au plus
tard après `MERVIO_OIDC_MIN_REFRESH_SECONDS`**, pas instantanément.

### Trois durées distinctes, et elles ne mesurent pas la même chose (F.2)

> **Correction de la documentation de F.1.** Elle affirmait que le cache périmé était servi
> « de façon cohérente et borné par `cache_seconds` ». **Faux sur les deux points**, et mesuré :
> le document périmé n'était servi que dans la fenêtre de refroidissement, donc le verdict
> **alternait** — 16 instants donnaient 10 acceptations et 6 refus en alternance — et un
> document périmé pouvait être servi bien au-delà de `cache_seconds`.

| Réglage | Mesure | Défaut | Bornes |
|---|---|---|---|
| `MERVIO_OIDC_JWKS_CACHE_SECONDS` | **fraîcheur**, depuis le dernier chargement **réussi** | 300 s | [10, 86400] |
| `MERVIO_OIDC_MAX_STALE_SECONDS` | **tolérance en plus**, pendant laquelle un cache déjà validé sert encore des clés **connues** si l'émetteur est injoignable | 60 s | [0, 3600] — `0` = refus dès l'expiration |
| `MERVIO_OIDC_MIN_REFRESH_SECONDS` | **cadence** des tentatives réseau ; n'accepte jamais une clé à elle seule | 30 s | [1, 3600], et ≤ `JWKS_CACHE_SECONDS` |

La fenêtre totale d'utilisabilité vaut **`cache_seconds + max_stale_seconds`** depuis le dernier
chargement **réussi**. Une tentative **échouée** ne déplace jamais cet instant : sinon une panne
prolongée repousserait indéfiniment l'expiration des clés, ce qu'une révocation doit empêcher.

**Verdict monotone.** Les deux branches qui peuvent manquer de clés fraîches — refroidissement
et échec de tentative — prennent la **même** décision. Le verdict ne dépend donc que de l'**âge**
du dernier chargement réussi : accepté tant que l'âge tient dans la fenêtre, refusé ensuite, et
jamais l'inverse. Mesuré sur le scénario exact qui papillonnait : **6 acceptations puis
10 refus, sans aucune transition refus → acceptation**.

**Conséquences en disponibilité et en révocation.** Une tolérance longue protège d'une panne de
l'émetteur et **retarde d'autant** la prise d'effet d'une révocation de clé : au pire
`cache_seconds + max_stale_seconds`. Une tolérance nulle fait l'inverse — révocation immédiate
à l'expiration, et indisponibilité dès que l'émetteur hoquette. Le défaut (300 + 60 = 360 s)
privilégie la disponibilité ; `MERVIO_OIDC_MAX_STALE_SECONDS=0` privilégie la révocation.

**La tolérance ne contourne aucune vérification.** Une signature invalide, un algorithme
interdit, un mauvais `iss` ou `aud`, un claim manquant : tous refusés, cache périmé ou non. Et
un `kid` **absent** du cache n'est jamais accepté — il n'y a rien à tolérer. Un refus pour cache
trop vieux porte son propre code, `jwks_too_stale`, distinct de `issuer_unreachable`.

**La panne n'est pas masquée.** `HttpJwksSource.state()` expose `fresh`, `age_seconds`,
`last_failure` et les trois durées — sans aucun secret. Une panne servie par tolérance reste
donc **nommée** et lisible.

Le **chemin chaud ne prend aucun verrou de récupération** : une fois le cache rempli, les
vérifications JWT ne se sérialisent pas. Prouvé de façon déterministe — le verrou est tenu par
un fil et une vérification chaude répond quand même.

### Ce que la cadence impose à la tolérance (F.2, complément mesuré)

La tolérance n'absorbe une panne **sans refus visible** que si une tentative de rafraîchissement
**autorisée et réussie** peut avoir lieu **à l'intérieur** de la fenêtre de tolérance. Or la
première tentative a lieu à l'expiration de la fraîcheur, et son échec marque l'horodatage de
tentative : la suivante n'est permise qu'après `MERVIO_OIDC_MIN_REFRESH_SECONDS`. La condition
recommandée est donc **`max_stale_seconds >= min_refresh_seconds`**.

Mesuré sur le harnais déterministe, `cache_seconds = 300`, émetteur en panne à partir de
`t = 0⁺`, une requête par seconde :

| Observation | A — `max_stale=60`, `min_refresh=30` | B — `max_stale=10`, `min_refresh=30` |
|---|---|---|
| dernière acceptation | `t = 359` | `t = 309` |
| premier refus | `t = 360`, `jwks_too_stale` | `t = 310`, `jwks_too_stale` |
| instants des tentatives réseau | 300, 330, 360, 390, 420 | 300, 330, 360, 390, 420 |
| transitions refus → acceptation pendant la panne | **0** | **0** |
| émetteur revenu **dans** la tolérance | retour `t = 315` → **aucun refus**, rechargement à `t = 330` | retour `t = 305` → **20 s de refus** (`t = 310` à `t = 329`), rechargement à `t = 330` |

Dans les deux cas la fenêtre d'utilisabilité vaut bien `cache_seconds + max_stale_seconds` et le
verdict reste **monotone pendant la panne**. La différence est ailleurs : en A une tentative tombe
**dans** la fenêtre (`330 < 360`), donc une panne plus courte que la tolérance est absorbée sans
aucun refus ; en B aucune tentative n'y tombe (`330 > 310`), donc la tolérance ne fait que
**retarder le refus** de 10 s — mesuré : 20 s de refus alors que l'émetteur était revenu depuis
`t = 305`.

**`max_stale_seconds < min_refresh_seconds` reste légal, et ce n'est pas une validation oubliée.**
`max_stale_seconds = 0` est le cas limite déjà ratifié — refus dès l'expiration, révocation
privilégiée — et une tolérance courte est un choix d'exploitant légitime. Le contrat ne l'interdit
donc pas ; il en **nomme la conséquence**. Les défauts (`60` et `30`) satisfont la relation
recommandée.

**La cadence est indépendante par ce qu'elle MESURE, pas par ses effets.** Elle ne décide jamais
à elle seule qu'une clé est acceptée — cela reste vrai. Mais elle détermine s'il existe une
occasion de renouveler la tolérance avant son expiration, donc elle conditionne ce que la
tolérance apporte réellement.

**Délai de rétablissement : trois instants qu'il ne faut pas confondre.**

| Instant | Ce qui le détermine |
|---|---|
| tentative **autorisée** | au plus tard `min_refresh_seconds` après la dernière tentative, réussie **ou non** |
| tentative **effective** | la **première requête** arrivée après cet instant : il n'existe **aucun rafraîchisseur en arrière-plan** |
| **rétablissement** | la première tentative effective qui **réussit** |

Seul le premier est borné par `min_refresh_seconds`. Mesuré en B avec un trafic interrompu entre
`t = 311` et `t = 400` : la tentative est autorisée dès `t = 330`, **aucun appel sortant** n'a lieu
avant `t = 400`, et le rétablissement est à `t = 400`. Sans trafic, la tolérance n'est donc jamais
renouvelée d'elle-même. Et si l'émetteur reste indisponible, **aucune borne de rétablissement ne
s'applique** : les refus durent autant que la panne.

### Trois classifications distinctes pour un JWKS qui ne sert pas de clé (F.1)
Ne pas confondre une erreur de l'**émetteur** avec un jeton douteux : sinon on cherche le
problème du mauvais côté.

| Code interne | Situation |
|---|---|
| `jwks_malformed` | la **forme** est fausse : `keys` absent, ou pas une liste |
| `jwks_without_usable_key` | la forme est bonne, le **contenu** est inutilisable : liste vide, ou aucune clé que la bibliothèque sache lire |
| `key_id_unknown` | le JWKS est **valide** et porte des clés utilisables, mais pas celle-là. **Seul cas qui justifie une relecture** |

Ces trois codes vivent dans les **journaux**. La réponse HTTP reste le **401 indifférencié** :
aucun détail de l'état de l'émetteur ne sort.

### Provisionnement à la volée
D-098a écrit `sub` → `users.idp_subject` via `tenancy.ensure_user`, un insert-or-get. Tout
jeton valide de l'émetteur configuré **crée** donc l'utilisateur s'il n'existe pas. La barrière
est `iss` + `aud`, vérifiés avant d'atteindre la base ; une limitation de débit la complétera.
Conséquence voulue : la réponse a la **même forme** pour un sujet connu et un sujet inconnu, de
sorte qu'elle ne révèle pas qui est inscrit.

## Des erreurs de persistance aux refus HTTP (G-0)

Deux erreurs de la couche de persistance, et **deux seulement**, deviennent des refus client :

| Erreur de persistance | Réponse |
|---|---|
| `NotFound` | 404 `not_found` |
| `PermissionDenied` | 403 `forbidden` |

**Mesuré avant G-0, sur `0dbb8a4`** : les trois cas — `NotFound`, `TenantAccessDenied`,
`PermissionDenied` — rendaient `500 internal_error`. Un accès croisé aurait donc été un 500, et
la propriété « tenant croisé et inexistant sont indiscernables », promise plus haut, était
**inatteignable**.

**L'indiscernabilité est structurelle.** `TenantAccessDenied` hérite de `NotFound`, et la
recherche de gestionnaire remonte la hiérarchie : les deux atteignent le même gestionnaire, qui
rend le même document constant. Aucun code de `api/` ne nomme `TenantAccessDenied` — vérifié par
l'AST — donc rien ne peut cesser de les confondre par inadvertance. Comparé **octet pour octet**,
corps et en-têtes, sur le **même chemin** : `instance` porte le chemin, donc comparer deux
chemins différents ne prouverait rien.

**Tout le reste demeure un 500.** `MoneyPrecisionError`, `SchemaNotReady`, `ImportRejected`,
`JobStateError` décrivent un défaut interne, pas une requête client fautive ; les traduire en 4xx
ferait chercher le problème du mauvais côté. La liste est fermée et un test négatif la verrouille.

**Limite connue.** Les gestionnaires vivent à l'intérieur de la pile de middlewares : une erreur
de persistance levée par le middleware d'authentification — donc par la résolution d'identité —
ne les atteint pas et reste un 500. Comportement d'avant G-0, conservé volontairement et testé.

## Routes

| Route | Rôle min. | État |
|---|---|---|
| `GET /healthz`, `GET /readyz` | aucun | livrées (tranche E) |
| `GET /v1/me` | authentifié | **livrée (tranche F)** |

`GET /v1/me` rend `user_id`, puis une entrée par appartenance — `organization_id`, `role`,
`name`. Elle ne porte **pas** le sujet IdP : le porteur le connaît déjà, et un document qui le
transporte finit par apparaître dans une trace.

**Elle ne porte pas encore les boutiques**, que la roadmap mentionne pour cette route. Écart
assumé : une lecture de boutiques par organisation est exactement la surface que la tranche G
éprouve avec sa matrice « accès croisé = 404 », et la livrer ici donnerait des lectures
tenant-scopées sans cette matrice.

**Ce que `/v1/me` ne prouve pas.** Aucune isolation entre organisations. Elle ne rend que les
appartenances de l'appelant, donc elle ne peut pas montrer ce qui arrive quand on demande la
ressource d'un **autre** tenant. Cette preuve est une matrice par route — succès, 401, rôle
insuffisant, accès croisé = 404 — et c'est la tranche G qui la porte.

## Pagination

Curseur **opaque**, encodant une position dans un ordre **déjà total** (D-092 ; ordre des
catégories : D-084). Le curseur n'encode jamais un identifiant non ordonné. Filtres en **liste
blanche**.

## Couverture

| Suite | Objet |
|---|---|
| `tests/api/test_api_guards.py` | les trois gardes AST |
| `tests/api/test_error_translation.py` | la traduction des erreurs de persistance (G-0) |
| `tests/api/test_health_and_errors.py` | `healthz`, `problem+json`, en-têtes, versionnage |
| `tests/api/test_auth_contract.py` | le contrat de jeton de D-098a, contrôle par contrôle |
| `tests/test_settings_api.py` | `ApiSettings`, et aucun nom de fournisseur dans la logique |
| `tests/persistence/test_api_readiness.py` | `readyz` contre une vraie base |
| `tests/persistence/test_api_me.py` | `/v1/me`, la matrice de refus, l'absence de fuite |
| `tests/persistence/test_pool_isolation.py` | l'invariant de sécurité du pool (D-099) |
| `tests/persistence/test_users_visibility.py` | SEC-05 / TD-27 |
