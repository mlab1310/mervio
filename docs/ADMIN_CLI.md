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
| `member add ... --subject S --role admin\|analyst\|viewer` | owner | oui si même rôle ; autre rôle → 7 |
| `member list` | owner | lecture |
| `store create ... --name N [--currency EUR]` | admin | oui (nom) ; autre devise → 7 |
| `store list` | viewer | lecture |
| `connection create ... --store ID --label L` | admin | oui (libellé actif) |
| `service authorize\|revoke ... --service ROLE` / `service list` | owner | authorize oui ; revoke sans autorisation active → 5 |
| `object upload ... --store ID --kind K --file CHEMIN` | analyst | non (chaque dépôt crée un objet) |
| `job enqueue-import ... --store --connection --shopify-orders UUID ...` | analyst | avec `--idempotency-key` |
| `job enqueue-analysis ... --store (--snapshot ID \| --from-import JOB)` | analyst | avec `--idempotency-key` |
| `job enqueue-purge` | owner | avec `--idempotency-key` |
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
