# Conteneur Mervio — image, compose, smoke test (Mission 004.3.8)

Environnement conteneurisé de **développement et de CI**. Ce n'est **pas** un déploiement de
production : pas de TLS vers PostgreSQL, pas de gestion de secrets externe, pas d'orchestrateur.
L'image encapsule l'architecture existante (worker, dispatcher, identité de service, bail,
fencing, audit, santé par fichier) sans la remplacer ni la contourner.

```
postgres (17.11, digest de la CI)  ◄── réseau interne `backend` ──┐
   └─ bootstrap : rôles + base (une fois, volume vide)            │
                                                                  │
mervio worker   (up)          rôle mervio_worker_svc ─────────────┤
python -m mervio.persistence  (ops) rôle mervio_migrator ─────────┤
mervio admin    (ops)         rôle mervio_app_user ───────────────┘
        ├─ volume mervio_data    : CSV locaux ; admin écrit, worker lit (lecture seule)
        └─ volume mervio_objects : objets bruts (004.4.4) ; admin dépose, worker lit
                                   en LECTURE SEULE (il ne dépose jamais, D-054)
```

## 1. Prérequis

- Docker Engine 24+ et Docker Compose v2 (`docker compose version`), architecture amd64 ou arm64.
- Python 3.11+ sur l'hôte **uniquement** pour lancer le smoke test (bibliothèque standard).
- Aucun Python local n'est nécessaire pour construire ou lancer l'image.

## 2. Construire l'image

```bash
docker compose build              # étiquette mervio:local (MERVIO_IMAGE pour la changer)
# ou
docker build --tag mervio:local .
```

| Élément | Choix |
|---|---|
| Base | `python:3.11.16-slim-bookworm` épinglée **par digest** (même Python que la CI) |
| Étapes | `build` (construction) → `runtime` (exécution) ; seul `/opt/venv` passe de l'une à l'autre |
| Dépendances | `requirements-runtime.lock` : sous-ensemble **exact** de `requirements.lock` (extra `persistence` seul), `--no-deps --only-binary=:all:` |
| Construction du paquet | wheel sans isolation, `setuptools` épinglé par `requirements-build.lock` (jamais dans l'image finale) |
| Contenu | l'environnement `/opt/venv` (paquet `mervio` installé, non éditable) ; ni `tests/`, ni `scripts/`, ni `data/`, ni pytest, ni pip dans l'environnement |
| Utilisateur | `10001:10001` (`mervio`, sans shell), jamais root à l'exécution |
| Point d'entrée | `ENTRYPOINT ["mervio"]`, `CMD ["worker"]` (forme exec : Python reçoit SIGTERM) |
| Santé | `HEALTHCHECK CMD ["mervio", "worker", "healthcheck"]` |
| Ports | aucun (le worker n'est pas un serveur HTTP) |
| Secrets | aucun : la connexion est fournie à l'exécution |

Le contexte de construction est une **liste blanche** (`.dockerignore`) : `pyproject.toml`, les deux
locks de l'image et `src/`. `.git`, `.venv`, `.env*`, `.pgdata`, données, rapports, caches et
métadonnées d'IDE n'y entrent jamais (vérifié par `tests/test_container_artifacts.py`).

Limite : les locks portent des versions exactes mais pas d'empreintes (`--require-hashes`), comme
le reste du projet ; l'intégrité repose sur PyPI et le digest de l'image de base.

## 3. Secrets et variables

Copier `.env.example` en `.env` (ignoré par Git) et renseigner **quatre mots de passe distincts**
(16 à 128 caractères parmi `[A-Za-z0-9._~-]`, utilisables tels quels dans une URL) :

```bash
openssl rand -hex 24   # à répéter pour chaque variable
```

| Variable | Rôle PostgreSQL | Utilisé par |
|---|---|---|
| `MERVIO_POSTGRES_PASSWORD` | `mervio_admin` (superutilisateur du conteneur) | bootstrap, tests de développement — **jamais** Mervio |
| `MERVIO_MIGRATOR_DB_PASSWORD` | `mervio_migrator` (propriétaire de la base et du schéma) | `migrate` |
| `MERVIO_APP_DB_PASSWORD` | `mervio_app_user` (membre de `mervio_app`) | `admin` |
| `MERVIO_WORKER_DB_PASSWORD` | `mervio_worker_svc` (membre de `mervio_app` et `mervio_worker`) | `worker` |

Et une **clé maître d'identité** (004.4.2, D-053), 32 octets en hexadécimal, lue par le seul
`worker` (obligatoire dès qu'il traite des imports ; sinon il s'arrête avec le code 2) :

```bash
openssl rand -hex 32   # MERVIO_IDENTITY_MASTER_KEY
```

| Variable | Rôle | Utilisé par |
|---|---|---|
| `MERVIO_IDENTITY_MASTER_KEY` | clé maître d'identité client : jamais en base, jamais journalisée ; les références client en dérivent via le sel de chaque organisation | `worker` |
| `MERVIO_OBJECT_STORE_ROOT` | racine du magasin d'objets bruts (004.4.4, D-054) : `admin` y dépose, le worker y lit. Obligatoire dès que le worker traite des imports ; compose la fixe à `/var/lib/mervio/objects` | `worker`, `admin` |

La perdre bloque les nouveaux imports (refus explicite `identity_key_unavailable`) ; en changer
aussi, pour les organisations déjà servies. La conserver comme un secret de production.

**Première utilisation : la clé fournie devient la référence de l'organisation (fail-closed).**
Le premier import d'une organisation inscrit l'empreinte (`master_key_id`) de la clé maître du
worker qui le traite ; toute autre clé est ensuite refusée pour cette organisation, sans
substitution silencieuse. Un worker démarré avec une **mauvaise** clé (au format valide) rend donc
indisponibles les imports des organisations qu'il sert en premier. **Il n'existe aujourd'hui
aucune procédure de réassignation** (prévue avec la destruction en 004.4.6 et la gestion de clés
en 004.9) ; ne jamais « réparer » en modifiant `master_key_id` en base. Précautions : générer la
clé une seule fois, la sauvegarder avant le premier import, donner **la même** clé à tous les
workers, et vérifier `identity_master_configured` dans l'événement `worker.config` au démarrage.
Détail : `docs/DECISIONS.md` (D-053, statut 004.4.2).

Facultatif : `MERVIO_POSTGRES_PORT` (port publié sur 127.0.0.1, défaut 55432), `MERVIO_IMAGE`
(défaut `mervio:local`), `MERVIO_ENV`, `MERVIO_LOG_LEVEL`, `MERVIO_WORKER_NAME`.

Compose **refuse de démarrer** si l'un des quatre mots de passe ou la clé maître d'identité
manque (`${VAR:?}`). Aucun secret n'est écrit dans un fichier versionné, ni dans l'image.

Limite assumée (développement) : compose transmet les mots de passe par variables d'environnement
des conteneurs, visibles par `docker inspect` pour qui contrôle le démon Docker. Pour un
déploiement réel, utiliser `MERVIO_DATABASE_URL_FILE` (déjà supporté par le worker et l'admin)
avec un gestionnaire de secrets, et TLS (`sslmode=verify-full`) vers PostgreSQL.

## 4. PostgreSQL

```bash
docker compose up -d --wait postgres
```

- Image `postgres:17.11-bookworm` épinglée par le **même digest** que le job `test` de la CI,
  initialisée en UTF8 / locale C.
- Au premier démarrage (volume vide), `docker/postgres/10-mervio-bootstrap.sql` crée :
  - les rôles de groupe `mervio_app` et `mervio_worker` (définition identique aux révisions
    0001 et 0007, qui les créent s'ils manquent) ;
  - trois rôles de connexion **sans** SUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB ni REPLICATION ;
  - la base `mervio`, propriété de `mervio_migrator` ; `CONNECT` retiré à `PUBLIC`, accordé aux
    rôles applicatif et worker.

  Il ne crée **aucune** table, politique ni fonction : le schéma, RLS et les droits restent à
  Alembic. Il est rejouable, refuse une variable manquante, un mot de passe faible, un nom
  invalide ou un rôle existant privilégié, et ne recopie jamais une instruction (qui porte un mot
  de passe) dans le journal du serveur.
- Le healthcheck interroge le serveur en **TCP** : pendant l'initialisation, le serveur
  temporaire n'écoute que sur socket, le service n'est donc pas « healthy » avant la fin du
  bootstrap.
- Le port n'est publié que sur `127.0.0.1` (réseau `edge`) ; worker, migrate et admin sont sur le
  réseau interne `backend`, sans accès sortant.
- Le bootstrap ne s'exécute que sur un volume **vide** : changer un mot de passe dans `.env` ne
  modifie pas un rôle existant (`docker compose down -v` pour repartir de zéro).

## 5. Migrations (étape explicite)

```bash
docker compose --profile ops run --rm migrate            # upgrade head
docker compose --profile ops run --rm migrate current
docker compose --profile ops run --rm migrate heads
```

Le worker **ne migre jamais** : sur une base non migrée il s'arrête avec le code 4. Migration et
démarrage de l'application restent deux actions distinctes. Le rôle de migration n'a pas
CREATEROLE (les rôles de groupe existent déjà) ; aucune migration destructive n'est lancée
automatiquement.

## 6. Worker

```bash
docker compose up -d --wait worker
docker compose logs -f worker
```

- Rôle `mervio_worker_svc` ; son principal `service:mervio_worker_svc` est dérivé du rôle par la
  base au démarrage (révision 0007). `Database` refuse un superutilisateur ou BYPASSRLS (code 2).
- Autorisation explicite par organisation (`mervio admin service authorize`), RLS forcée,
  délégation humaine, audit, bail et fencing : inchangés.
- Durcissement : système de fichiers racine en lecture seule, `tmpfs` pour `/run/mervio` (fichier
  de santé, `uid=10001,gid=10001,mode=0700`) et `/tmp`, toutes les capacités retirées, `no-new-privileges`, aucun port, volume de
  données monté en **lecture seule**.
- Logs JSON du produit sur stderr (UTC, `correlation_id`, redaction), lisibles par `docker logs`.
- Signaux : `init: true` (tini en PID 1) transmet SIGTERM à Python, qui installe ses propres
  gestionnaires. `docker compose stop worker` → DRAINING → STOPPED, code 0. `stop_grace_period`
  (45 s) dépasse le délai de grâce du worker (30 s) : SIGKILL n'arrive qu'après l'arrêt gracieux
  ou forcé du worker (travail remis en file, code 5).

## 7. Healthcheck

`mervio worker healthcheck` lit le fichier `/run/mervio/worker-health.json` écrit par le worker :
ni base, ni réseau, ni URL, aucune écriture. Contrôle de **vie** pour Docker :

| État | Vivant | Prêt (`--ready`) |
|---|---|---|
| STARTING, DRAINING | oui | non |
| READY, BUSY | oui | oui |
| DEGRADED | oui dans la tolérance (`MERVIO_WORKER_DEGRADED_GRACE_SECONDS`), puis non | non |
| STOPPED, FORCED, fichier absent ou trop ancien | non | non |

```bash
docker inspect --format '{{.State.Health.Status}}' $(docker compose ps -q worker)
docker compose exec worker mervio worker healthcheck --ready --json
```

## 8. Démonstration (données synthétiques)

```bash
docker compose --profile ops run --rm admin demo provision --owner 'demo|me' \
    --data-dir /var/lib/mervio/data/demo --service mervio_worker_svc --json
docker compose --profile ops run --rm admin job show --as 'demo|me' --org <org> --job <import> --json
docker compose --profile ops run --rm admin job enqueue-analysis --as 'demo|me' --org <org> \
    --store <store> --from-import <import> --as-of <next.analysis_as_of> --json
docker compose --profile ops run --rm admin audit list --as 'demo|me' --org <org>
```

Les chemins passés à l'admin sont ceux **du conteneur** (`/var/lib/mervio/data/...`, volume
partagé), jamais un chemin de l'hôte. Détails des commandes : `docs/ADMIN_CLI.md`.

## 9. Smoke test

```bash
python scripts/container_smoke.py                          # construit mervio:smoke puis teste
python scripts/container_smoke.py --image mervio:local --skip-build
python scripts/container_smoke.py --keep                   # garde la pile pour diagnostic
```

Le script génère ses propres mots de passe (jamais ceux de `.env`), utilise un projet compose
unique et un port libre, et **supprime toujours** conteneurs, réseaux et volumes à la fin (sauf
`--keep`). Toute sortie est nettoyée des mots de passe avant affichage. Codes : 0 réussi,
1 échec, 2 Docker ou Compose absent (jamais un succès).

Ce qu'il vérifie, dans cet ordre, avec de vrais conteneurs :

1. image : utilisateur 10001, aucun port, point d'entrée et HEALTHCHECK attendus, aucune
   connexion ni mot de passe dans l'environnement de l'image, Python 3.11.16, ni pytest ni pip ;
2. compose valide ; PostgreSQL démarre et devient healthy ; version 17 ; rôles, appartenances,
   propriétaire, encodage et `CONNECT` de la base conformes au bootstrap ;
3. le worker refuse un schéma non migré (code 4) sans créer de table ;
4. `migrate` atteint la révision head ; toute table de tenant a sa RLS forcée ;
5. le worker refuse le superutilisateur (code 2, `worker.identity_refused`, aucun principal) ;
   l'admin refuse un mauvais mot de passe (code 3) sans le publier ;
6. le worker démarre, devient healthy puis prêt, tourne en non-root sur un système de fichiers
   en lecture seule sans port publié, annonce le principal `service:mervio_worker_svc`, logs JSON
   UTC conformes ;
7. le volume partagé appartient à l'utilisateur de l'image ; `demo provision` (rejouable), import
   et analyse réussis, rapport persisté avec la même empreinte, journal d'audit complet et
   attribué (humain pour l'admin, principal du worker au nom du propriétaire) ;
8. isolation : un autre humain ne voit pas l'organisation (code 5) et inversement ; en SQL brut,
   le rôle applicatif ne voit rien sans contexte, le worker voit l'organisation autorisée mais pas
   l'autre, et aucune donnée métier sans délégué ;
9. `docker compose stop worker` : code 0, `worker.stopping` puis `worker.stopped`, aucun travail
   laissé en cours ;
10. aucune fuite de mot de passe ni d'URL de connexion dans les sorties et les logs de tous les
    conteneurs.

Les vérifications SQL et `mervio admin` de ce script sont aussi exécutées, sans Docker, sur la
base de la suite de tests (`tests/persistence/test_container_bootstrap.py`, avec un vrai
processus `mervio worker`) ; ses règles propres (nettoyage, redaction, codes) sont couvertes par
`tests/test_container_smoke.py`.

En CI, le job `docker` construit l'image, lance ce script, puis vérifie qu'aucun conteneur,
volume ou réseau compose ne reste.

## 10. Arrêt

```bash
docker compose stop worker        # arrêt gracieux (SIGTERM)
docker compose down               # conserve la base et les données de démo
docker compose down -v            # supprime aussi les volumes (base comprise)
```

## 11. Dépannage

| Symptôme | Cause probable |
|---|---|
| `required variable MERVIO_..._PASSWORD is missing` | `.env` absent ou incomplet |
| postgres jamais healthy, log `mervio bootstrap: ...` | mot de passe hors alphabet ou trop court, rôle existant privilégié ; corriger puis `down -v` |
| `password authentication failed` après changement de `.env` | le bootstrap ne rejoue pas sur un volume existant : `down -v` |
| worker sort en **4** | migrations non appliquées : `run --rm migrate` |
| worker sort en **2** | configuration invalide ou rôle refusé (superutilisateur, BYPASSRLS, pas membre de `mervio_worker`) |
| worker sort en **3** | base injoignable au-delà de `MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS` |
| worker `unhealthy`, `reason=missing` | le worker ne peut pas écrire `/run/mervio` : un tmpfs sans `uid`/`gid` appartient à root (runc ne reprend que le **mode** du répertoire de l'image) ; garder `uid=10001,gid=10001` |
| worker `unhealthy`, `reason=stale` ou `database_unavailable` | worker bloqué ou état DEGRADED prolongé : `docker compose logs worker` |
| log postgres `function app_ensure_service_principal() does not exist` | worker lancé avant les migrations (code 4, voulu dans le smoke test) |
| log postgres `password authentication failed for user "mervio_app_user"` pendant le smoke test | étape volontaire « mauvais mot de passe refusé » (code 3) |
| travail d'import `failed` : fichier introuvable | chemin de l'hôte au lieu de `/var/lib/mervio/data/...` |
| travaux jamais pris | organisation non autorisée pour `mervio_worker_svc` (`admin service authorize`) |
| `pull access denied for mervio` | image non construite : `docker compose build` |
| smoke test : conteneurs restés après un arrêt brutal | `docker compose -p <projet affiché> down -v` |

## 12. Limitations actuelles

- Environnement de développement et de CI, **pas de production** : pas de TLS vers PostgreSQL,
  mots de passe en variables d'environnement, superutilisateur du conteneur présent pour le
  bootstrap, authentification `trust` sur la boucle locale **à l'intérieur** du conteneur postgres
  (défaut de l'image officielle ; le réseau exige scram-sha-256).
- Pas d'image publiée ni de registre, pas de signature ni de SBOM.
- Locks sans empreintes (`--require-hashes`).
- Un seul worker dans le compose (plusieurs répliques partageraient le même principal de service,
  ce que le modèle permet, mais ce n'est pas testé ici).
- Le bootstrap ne gère ni la rotation des mots de passe ni la suppression des rôles.
- Pas d'API HTTP : le conteneur n'expose aucun port et n'en a pas besoin.
