# CLI d'administration — `mervio admin` (Mission 004.3.7)

Outil **opérateur** pour provisionner, inspecter et démontrer Mervio. Jamais exposé en HTTP.
Ce n'est pas l'interface client : le parcours produit (inscription, membres, OAuth) arrive avec l'API.

## Principes

| Garantie | Mécanisme |
|---|---|
| Aucun contournement de RLS | connexion `MERVIO_DATABASE_URL` (ou `_FILE`) avec un rôle applicatif ; `Database` refuse superutilisateur et BYPASSRLS (code 2) |
| Un `--org` n'accorde rien | chaque opération passe par `TenantSession` : appartenance **et** rôle de `--as` relus en base à chaque transaction |
| Aucune fuite d'existence | organisation étrangère = organisation inexistante (même code 5, même message) ; le rôle est vérifié **avant** de dire si une boutique, un sujet ou un service existe |
| Acteur non falsifiable | `--as` doit désigner un humain existant (jamais créé au passage, jamais `service:`) ; aucune option ne choisit l'acteur de l'audit |
| Audit cohérent | même journal `audit_events` en ajout seul, écrit dans la transaction du changement ; `actor_type=user`, `on_behalf_of` vide (l'humain agit pour lui-même) |
| Pas de secret ni de chemin publié | erreurs traduites (jamais le message du serveur), chemins de source masqués (`[redacted]`), URL de base jamais affichée |

Actions auditées (révision `0008_admin_audit`) : `organization.created`, `member.added`,
`store.created`, `connection.created`, `service.authorized`, `service.revoked` ; la mise en
file et l'annulation réutilisent `job.enqueued` / `job.cancelled`. Une identité (`user ensure`)
n'appartient à aucune organisation : elle n'est pas auditée.

## Commandes

| Commande | Rôle minimal | Idempotence |
|---|---|---|
| `user ensure --subject S` | — | oui (sujet) |
| `org create --as A --name N` | tout humain (devient propriétaire) | oui (propriétaire + nom exact) |
| `org list --as A` / `org show --as A --org O` | membre | lecture |
| `org reconcile-purges --as A [--org O] [--execute] [--limit N]` | owner | **simulation par défaut** ; voir §purge tenant |
| `member add ... --subject S --role admin\|analyst\|viewer` | owner | oui si même rôle ; autre rôle → 7 |
| `member list` | owner | lecture |
| `store create ... --name N [--currency EUR]` | admin | oui (nom) ; autre devise → 7 |
| `store list` | viewer | lecture |
| `connection create ... --store ID --label L` | admin | oui (libellé actif) |
| `service authorize\|revoke ... --service ROLE` / `service list` | owner | authorize oui ; revoke sans autorisation active → 5 |
| `object upload ... --store ID --kind K --file CHEMIN` | analyst | non (chaque dépôt crée un objet) |
| `job enqueue-import ... --store --connection --shopify-orders UUID ...` | analyst | avec `--idempotency-key` |
| `job enqueue-analysis ... --store (--snapshot ID \| --from-import JOB)` | analyst | avec `--idempotency-key` |
| `job enqueue-purge` | owner | avec `--idempotency-key` — purge de **rétention**, pas une purge de locataire |
| `job enqueue-purge-store ... --store ID` | owner | avec `--idempotency-key` ; refusée si une purge tenant est déjà en vol (7) |
| `job enqueue-purge-organization` | owner | avec `--idempotency-key` ; **aussi** le chemin de reprise manuelle |
| `job enqueue-redact ... --customer-ref c1:…` | admin | avec `--idempotency-key` |
| `job list\|show\|stats` / `job cancel --job ID` | viewer / analyst | lecture / non |
| `audit list [--action A] [--limit N]` | admin | lecture |
| `demo provision --owner S --data-dir D [--service ROLE]` | — | oui (rejouable à l'identique) |

Toutes les commandes acceptent `--json` (un objet sur stdout : `{"ok": true, "result": …}` ou
`{"ok": false, "error": {"code", "message"}}`). Sortie texte : lignes `clé: valeur`, erreurs sur stderr.

Codes de sortie : 0 succès · 1 erreur interne · 2 usage/configuration · 3 base injoignable ·
4 schéma non migré · 5 introuvable · 6 refusé · 7 conflit.

Les chemins d'import sont **absolus, normalisés, sans `..`** et lus par le worker (le fichier
doit être visible depuis son système de fichiers).

`--as` fait confiance au détenteur des identifiants de base pour nommer l'humain qui agit (même
limite que SEC-04) : l'autorisation est ensuite entièrement vérifiée en base pour cet humain.
Le jeton OIDC remplacera ce paramètre avec l'API.

## Effacement d'un client : deux commandes, deux identités (004.4.5, D-062)

`admin` **ne détient pas** la clé maître d'identité et **n'accepte aucune identité directe** :
aucune option `--email`, pas même facultative. La référence client est un HMAC à clé
d'organisation, et le seul processus autorisé à la calculer est le **worker**, qui détient déjà
cette clé et la dérive déjà à chaque import. Le parcours est donc en deux temps :

```bash
# 1. dans le conteneur worker : l'identité entre par STDIN, la référence sort sur STDOUT
#    (jamais en argument : `ps` et l'historique du shell le liraient)
printf '%s' 'client@example.com' \
    | docker compose exec -T worker mervio worker resolve-customer-ref --org <org> --as 'ops|moi'
# -> c1:0123456789abcdef0123456789abcdef

# 2. avec l'identité de l'humain habilité : la mise en file ne transporte qu'une référence
mervio admin job enqueue-redact --as 'ops|moi' --org <org> \
    --customer-ref c1:0123456789abcdef0123456789abcdef
```

La première commande n'écrit **rien** : ni ligne, ni travail, ni instantané, ni événement
d'audit, ni log, ni fichier. Elle est en lecture stricte — une organisation dont le sel
d'identité n'existe pas (donc qui n'a jamais produit de référence, donc qui n'a rien à effacer)
est **refusée**, jamais initialisée. `--as` nomme un humain membre de l'organisation : le sel
n'est lisible par une connexion worker que sous un délégué humain de rang `analyst` au moins
(révision `0011`), et c'est cette règle-là — pas une nouvelle — qui fixe le plancher.

Sortie : stdout ne porte **que** la référence. Un refus n'écrit rien sur stdout et une ligne
`resolution refusee: <code>` sur stderr, sans jamais citer l'identité fournie, la clé maître, le
sel ni la clé dérivée. Codes : 0 résolu · 2 configuration ou résolution refusée · 3 base
injoignable · 4 schéma non migré. Tous les refus de résolution partagent le code 2 : la valeur de
sortie ne doit pas devenir l'oracle que les messages évitent d'être.

Limite connue : une référence `g1:` (commande invitée, un client par commande) **n'est pas
résoluble** depuis une identité client — c'est le HMAC d'un identifiant de commande, pas d'une
personne. Elle reste effaçable si on la connaît par un autre moyen. La résolution elle-même n'est
**pas auditée par le produit** (D-062, limitation assumée) : la trace est celle du conteneur et du
système.

## Purge de locataire et reprise (004.4.6, D-051, D-065 à D-069, D-071)

⚠️ **Ne pas confondre avec `job enqueue-purge`**, qui est une purge de **rétention** : elle supprime
des travaux terminés et des événements d'audit expirés **sans toucher** aux instantanés, exécutions
ni rapports. Les deux commandes ci-dessous font exactement l'inverse — elles détruisent les données
du locataire et **conservent** l'audit. Les sémantiques sont opposées.

```bash
# purge d'UNE boutique : l'organisation reste vivante, le sel d'identité n'est PAS détruit
mervio admin job enqueue-purge-store --as 'ops|moi' --org <org> --store <store> \
    --idempotency-key <clé>

# purge de l'ORGANISATION entière : sel détruit, boutiques et organisation tombstonées
mervio admin job enqueue-purge-organization --as 'ops|moi' --org <org> --idempotency-key <clé>
```

Les deux exigent le rang **`owner`** (`Permission.PURGE_TENANT`), relu en base à la mise en file
**puis de nouveau à l'exécution** par la fonction privilégiée. La boutique visée vient de
`jobs.store_id`, contrainte par une clé étrangère : **la charge utile ne porte rien**, et aucun
chemin, aucun nom, aucune identité n'y transite.

**Au plus une purge tenant active par organisation** (D-067) : la seconde est refusée à l'insertion
avec le code **7** (conflit), avant d'avoir pu détruire quoi que ce soit. Cela vaut aussi entre une
purge de boutique et une purge d'organisation, et entre deux purges de boutiques **différentes** de
la même organisation.

**Une organisation close n'accepte plus aucune écriture.** Après la clôture, toute mise en file dans
cette organisation est refusée — à une exception près, volontairement étroite : un travail
`purge_organization`, pour permettre la reprise. `purged` est **terminal**.

### Reprise d'une purge figée

Une purge peut échouer **après** la clôture : les données sont alors intactes — la barrière de drain
de D-068 les protège — mais l'organisation reste `purging`, **le sel déjà détruit**, et l'effacement
demandé n'est pas honoré. `org show` le voit (D-071) ; `reconcile-purges` le reprend.

```bash
# 1. INSPECTER — par défaut, rien n'est écrit (simulation)
mervio admin org reconcile-purges --as 'ops|moi' --json

# 2. REPRENDRE réellement, après avoir lu le rapport
mervio admin org reconcile-purges --as 'ops|moi' --org <org> --execute --json
```

| Option | Effet |
|---|---|
| *(aucune)* | **simulation** : inspecte toutes les organisations de `--as`, n'écrit rien |
| `--org UUID` | une seule organisation ; **optionnel**, contrairement aux autres commandes `org` |
| `--execute` | met **réellement** en file les reprises dues ; sans lui, aucune écriture |
| `--limit N` | organisations inspectées au plus, sans `--org` (1 à 1000, défaut 50) |

**Il n'existe aucune option `--force`** — ni ici, ni sur les deux commandes de mise en file. Aucune
commande ne permet d'outrepasser `purged`, ni de contourner la garde ci-dessous.

**La reprise n'usurpe l'identité de personne.** La purge remise en file est autorisée par l'acteur
`--as`, dont le rang `owner` est relu en base ; le demandeur d'origine ne sert qu'à l'historique et à
la corrélation. L'audit émis est `job.enqueued`, avec **l'humain qui a repris** pour acteur. C'est
aussi pourquoi la commande vit dans `admin` et non dans le worker : `0007` (SEC-06) interdit au rôle
de service de créer le moindre travail, et cette interdiction n'est ni contournée ni amendée — pas de
`SET ROLE`, pas de `SECURITY DEFINER`, pas d'insertion directe.

**Garde anti-boucle.** La reprise automatique s'arrête si la dernière cause d'échec n'est **pas**
transitoire (seul `sqlstate_55006` l'est), si l'organisation compte déjà **3** purges échouées
(celle d'origine comprise, donc au plus **deux** reprises automatiques), ou si **moins d'une heure**
s'est écoulée depuis le dernier échec. Elle vit **hors base**, délibérément : le chemin manuel
`job enqueue-purge-organization` reste ouvert quoi qu'elle décide.

### Lire l'état sans rien lancer (D-071)

`org list` expose désormais `status` par organisation. `org show` expose un bloc `purge` :

| Champ | Sens |
|---|---|
| `status` | `active`, `purging` ou `purged` |
| `purged_at` | horodatage de la pierre tombale, ou `null` |
| `active_purge` | identifiant de la purge tenant en vol (les deux types), ou `null` |
| `recoveries` | purges d'**organisation** `failed`, celle d'origine comprise |
| `latest_purge_job` | la dernière purge d'organisation, même rendu que `job show` |
| `outcome` | ce que `reconcile-purges` **dirait** de cette organisation |
| `blocked_reason` | pourquoi elle refuserait, le cas échéant |

`outcome` et `blocked_reason` sont calculés par la **même** garde que `reconcile-purges` : une
inspection et une reprise ne peuvent pas se contredire. Valeurs d'`outcome` : `terminal` ·
`not_purging` · `already_active` · `no_requester` · `would_recover` · `deterministic_failure` ·
`guard_exhausted` · `too_soon`.

La lecture passe par `Permission.READ` — **tout membre voit** l'état de purge de son organisation,
alors que la reprise exige `owner`. Aucune donnée d'un autre locataire, aucun nom, aucun secret n'y
figure : des identifiants, des états, des nombres et des horodatages.

**Codes de sortie de `reconcile-purges`.** C'est une commande d'**inspection** : elle aboutit tout en
rapportant des obstacles, et rend alors le code **le plus sévère** rencontré — **6** pour
`owner_invalid` / `permission_denied`, **7** pour `guard_exhausted` / `deterministic_failure` /
`no_requester` / `too_soon`, **0** sinon. En `--json`, `ok` suit le code, jamais l'inverse.

**Audit des purges.** Les quatre actions `store.purge_started`, `store.purged`,
`organization.purge_started` et `organization.purged` sont écrites par le **worker**, dans la
transaction du changement, avec `actor_type = worker` et le demandeur en `on_behalf_of`. Elles ne
portent que des identifiants, `job_id` et des compteurs : jamais de nom, de devise, de clé d'objet ni
de référence client. Elles **survivent** à la purge — c'est l'objet de la pierre tombale (D-051).

⚠️ **Limite à connaître avant d'exploiter ce chemin.** Rien ne **surveille** les purges : aucune
alerte, aucune reprise périodique, aucune sonde. Une organisation peut rester `purging`, **sel
détruit**, indéfiniment, tant que personne ne lance `reconcile-purges`. Par ailleurs, une
réconciliation **refusée** n'écrit **aucun** événement d'audit (D-070, non implémentée) : le refus
n'existe que sur la sortie de la commande.

## Démonstration (données synthétiques)

```bash
# 1. migrations (rôle propriétaire du schéma)
MERVIO_MIGRATION_DATABASE_URL=... python -m mervio.persistence upgrade
# 2. le worker démarre et enregistre son principal service:<rôle>
MERVIO_DATABASE_URL=postgresql://<rôle worker>@hote/base MERVIO_IDENTITY_MASTER_KEY=<openssl rand -hex 32> mervio worker &
# 3. provisionnement complet, rejouable
export MERVIO_DATABASE_URL=postgresql://<rôle applicatif>@hote/base
mervio admin demo provision --owner 'demo|me' --data-dir /srv/mervio-demo --service <rôle worker> --json
# 4. l'import est servi sans redémarrer le worker ; puis l'analyse
mervio admin job show --as 'demo|me' --org <org> --job <import>
mervio admin job enqueue-analysis --as 'demo|me' --org <org> --store <store> \
    --from-import <import> --as-of <next.analysis_as_of>
# 5. inspection
mervio admin org show --as 'demo|me' --org <org>
mervio admin audit list --as 'demo|me' --org <org>
```

`demo provision` génère un jeu `mervio.synthetic` déterministe (1 500 commandes, 56 jours) dans un
répertoire vide ou déjà démo ; il ne réécrit jamais un fichier intact (un worker peut le lire) et
remplace un fichier altéré par renommage atomique. Il refuse `data/sample/` et tout répertoire non
vide étranger. Le scénario complet est exécuté par `tests/persistence/test_cli_admin_process.py`.

## Objets bruts et frontière d'import (004.4.4, D-054)

Le worker ne reçoit **jamais** de chemin de fichier. Un import se fait en deux temps :

```bash
# 1. déposer chaque source; le fichier est lu LOCALEMENT, la clé est générée côté serveur
mervio admin object upload --as S --org ORG --store ST --kind shopify_orders --file ./orders.csv
# -> {"status": "created", "object": {"id": "...", "sha256": "...", "state": "available", ...}}

# 2. mettre en file avec les identifiants obtenus, un par source
mervio admin job enqueue-import --as S --org ORG --store ST --connection CX \
    --shopify-orders UUID [--stripe UUID] [--google-ads UUID] [--idempotency-key CLE]
```

- le chemin local s'arrête à l'upload : ni en base, ni en charge utile, ni en audit, ni en log ;
- le nom du fichier n'est conservé **nulle part** (il peut porter une donnée personnelle) ;
- `--kind` nomme la source ; `origin` est fixé côté serveur (`csv_upload`), jamais par l'appelant ;
- rejouer une `--idempotency-key` avec **d'autres** objets est un conflit (code 7), jamais un
  travail réutilisé en silence ;
- `MERVIO_OBJECT_STORE_ROOT` (chemin absolu) désigne le magasin ; `admin` y écrit, le worker y lit.

La CLI analytique locale (`python -m mervio.analytics`) garde ses chemins : elle ne passe ni par
la file ni par le worker.
