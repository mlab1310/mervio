# Mervio — Core Analytics Engine

Moteur analytique déterministe pour l'e-commerce. Transforme des exports
Shopify / Stripe / Google Ads en **Business Health Report** structuré :
KPI, profitabilité, anomalies, cause racine, score de santé, recommandations.

**Aucun LLM n'intervient dans le calcul des chiffres.** Chaque rapport le
déclare (`_meta.llm_used = false`).

## Démarrage

```bash
pip install pytest                       # seule dépendance (dev)
python scripts/generate_sample_data.py   # fixtures SYNTHÉTIQUES
export PYTHONPATH=src

python -m mervio.analytics demo          # démonstration complète
```

### Analyser de vrais fichiers

```bash
# 1. vérifier AVANT d'analyser
python -m mervio.analytics validate \
  --shopify-orders   ~/exports/orders_export.csv \
  --shopify-products ~/exports/products_export.csv \
  --stripe           ~/exports/stripe.csv \
  --google-ads       ~/exports/ads.csv

# 2. analyser
python -m mervio.analytics analyze \
  --shopify-orders   ~/exports/orders_export.csv \
  --shopify-products ~/exports/products_export.csv \
  --stripe           ~/exports/stripe.csv \
  --google-ads       ~/exports/ads.csv \
  --out analysis/client_x --label "Client X"
```

Sources non identifiées ? Laissez Mervio détecter : `--file a.csv --file b.csv`.

Produit dans `--out` :

| Fichier | Pour qui |
|---|---|
| `report.json` | machine — contrat de sortie du moteur |
| `report.txt` | dirigeant — 15 sections lisibles |
| `data_quality.json` | opérateur — validations, couverture, limites |

Autres options : `--grain week\|month`, `--lookback N`, `--today AAAA-MM-JJ`,
`--print-report`, `--verbose`.

```bash
python -m pytest        # 1097 tests avec PostgreSQL (796 passes + 301 ignores sans base)
```

## Persistance PostgreSQL (optionnelle, Mission 004.1)

Le moteur et la CLI n'en ont pas besoin. Le chemin SaaS persiste des instantanés de
données immuables et des rapports identiques octet pour octet au `report.json` de la CLI,
avec isolation des organisations (clés composites + Row Level Security).

```bash
pip install -e '.[dev,persistence]'
scripts/dev_postgres.sh start            # ou: docker compose up -d --wait postgres (.env requis, docs/CONTAINER.md)
MERVIO_TEST_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres python -m pytest
```

Détails : `docs/MISSION_004_1_PERSISTENCE.md`.

## Tâches de fond et audit (Mission 004.2)

PostgreSQL est la file : ni Redis, ni courtier, ni ordonnanceur externe. Un worker prend
un travail par `SELECT … FOR UPDATE SKIP LOCKED`, l'exécute hors transaction, puis inscrit
son sort et sa trace d'audit dans la même transaction.

- **Trois types de travaux :** import, analyse, purge. Chacun réutilise le chemin 004.1 :
  aucun calcul n'est refait, aucun KPI ne passe par un LLM.
- **Au moins une fois :** un worker mort laisse un travail dont le bail expire et qu'un
  autre reprend. L'idempotence de 004.1 rend le rejeu inoffensif — un instantané scellé au
  plus par empreinte, un rapport au plus par exécution.
- **Audit en ajout seul :** aucune mise à jour possible (droit **et** trigger), aucune
  suppression avant 30 jours (politique RESTRICTIVE, vraie même en SQL brut).
- **Logs JSON corrélés :** une exécution complète se recolle par son `correlation_id`.
  Ni secret, ni PII, ni chemin absolu n'y entrent.
- **Mesuré :** à un million de travaux en attente, la prise coûte 0,47 ms (p50) et la
  mémoire du worker reste à 48 Mo.

Détails : `docs/MISSION_004_2_HANDOFF.md`.

## Processus worker (Mission 004.3)

```bash
pip install -e '.[persistence]'
MERVIO_DATABASE_URL=postgresql://<role>@<hote>/<base> \
MERVIO_WORKER_HEALTH_FILE=/run/mervio/health.json \
mervio worker                              # ou: python -m mervio.cli worker
mervio worker healthcheck [--ready] [--json]
```

- **Configuration :** uniquement les variables `MERVIO_*` (liste et règles : `src/mervio/settings.py`).
  Une configuration invalide est refusée avant toute connexion, sans jamais afficher de valeur.
- **Logs :** JSON sur stderr, une ligne par événement.
- **Arrêt :** premier SIGTERM/SIGINT → le travail en cours se termine (délai de grâce) ;
  délai dépassé ou second signal → le travail est remis en file et le processus sort en 5.
- **Codes de sortie :** 0 arrêt propre, 1 erreur interne, 2 configuration ou identité refusée
  (ou extra `persistence` absent), 3 base injoignable, 4 schéma non migré, 5 arrêt forcé.
- **`healthcheck` :** lit seulement le fichier de santé (ni base, ni réseau, ni URL de base) ;
  0 sain, 1 non sain, jamais d'autre code. Par défaut contrôle de vie ; `--ready` exige
  `ready` ou `busy`.

## Administration opérateur (Mission 004.3.7)

```bash
export MERVIO_DATABASE_URL=postgresql://<role applicatif>@<hote>/<base>
mervio admin demo provision --owner 'demo|me' --data-dir /srv/mervio-demo --service <role du worker>
mervio admin org show --as 'demo|me' --org <uuid>
mervio admin --help
```

Provisionnement (identité, organisation, membre, boutique, connexion CSV, autorisation d'un
worker), mise en file, inspection et journal d'audit. Chaque opération d'organisation s'exécute
au nom de l'humain `--as`, dont l'appartenance et le rôle sont relus en base (RLS forcée, rôle
applicatif ordinaire) ; chaque changement est audité. Procédure : `docs/ADMIN_CLI.md`.

## Conteneur (Mission 004.3.8)

```bash
cp .env.example .env                      # quatre mots de passe: openssl rand -hex 24
docker compose build
docker compose up -d --wait postgres      # PostgreSQL 17 + bootstrap des roles (aucun superutilisateur pour Mervio)
docker compose --profile ops run --rm migrate          # migrations: etape explicite
docker compose up -d --wait worker        # healthcheck = mervio worker healthcheck
python scripts/container_smoke.py         # smoke test complet en vrais conteneurs
```

Image `python:3.11.16-slim` épinglée par digest, dépendances depuis `requirements-runtime.lock`,
utilisateur non-root, aucun port, aucun secret. Environnement de développement et de CI, pas de
production. Guide, variables, dépannage et limites : `docs/CONTAINER.md`.

## Performance mesurée (Mission 004.3.9)

```bash
MERVIO_BENCH_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres \
    .venv/bin/python benchmarks/performance.py
```

Sur la machine de référence (Apple M5 Pro, PostgreSQL 17.11 local, données synthétiques) : analyse
de 100 000 commandes en 3,7 s et 518 Mo ; chaîne réelle import → analyse → rapport → audit en 15 s ;
à 1 M de commandes, 46 s et 3,7 à 4,9 Go pour le moteur, 159 s et 5,0 Go pour le worker. Méthode,
tableaux, goulots et limites : `docs/PERFORMANCE.md` (ce ne sont pas des engagements).

## Données et confidentialité

- Les fichiers importés vont dans `data/uploads/`, **ignoré par Git**.
- `data/sample/` est réservé aux fixtures synthétiques ; y écrire une donnée
  client lève une erreur.
- Numéros de carte, IBAN et clés API sont détectés et signalés **par colonne**,
  jamais recopiés dans un rapport ni dans les logs.
- Aucun appel réseau : le moteur n'a aucune dépendance runtime.

Procédure complète : `docs/REAL_DATA_TEST.md`.

## Ce que le moteur produit

```
BUSINESS HEALTH SCORE : 50/100
  Croissance du CA      3.9/100   CA -23.5% (2026-W37 vs W36)
  Profitabilité         N/A       EXCLU: composante de coût majeure manquante (cogs)
  Efficacité marketing  100/100   ROAS 6.40
  Santé client          23.4/100
  Santé produit         N/A       EXCLU: couverture coût 78% < 80%

FACT   : CA de 2026-W37 inférieur de 18.7% à la baseline
HYPO   : variation associée à la capacité du site à convertir le trafic
RECO   : auditer le tunnel de conversion avant de modifier les budgets.
         Dégradation la plus marquée sur Shopping - Core.
IMPACT : -5 518,55 EUR (écart vs baseline glissante)
```

## Principe de conception

Le moteur préfère dire « je ne sais pas » plutôt que produire un chiffre
flatteur :

- coût produit absent ⇒ marge **non calculable**, pas 100 %
- pas de donnée publicitaire ⇒ CAC **inconnu**, pas 0,00 €
- COGS manquant ⇒ dimension profitabilité **exclue** du score
  (sur les fixtures : 50/100 au lieu de 78/100)

Un tableau de bord qui ment une fois n'est plus jamais consulté.

## Données de démonstration

Tout ce qui se trouve dans `data/sample/` est **synthétique** et ne représente
aucune entreprise réelle. Généré par `scripts/generate_sample_data.py` avec une
graine fixe, incluant un incident de conversion et des défauts de qualité
volontaires (date invalide, doublons, remboursement négatif, coûts manquants).

## Documentation

| Fichier | Contenu |
|---|---|
| `docs/PROJECT_STATE.md` | état réel du projet |
| `docs/REAL_DATA_TEST.md` | procédure de test sur données réelles |
| `docs/ARCHITECTURE.md` | structure et chemin vers le SaaS |
| `docs/DATA_MODEL.md` | modèle normalisé et définitions |
| `docs/ANALYTICS_ENGINE.md` | méthodes, seuils, formules |
| `docs/DECISIONS.md` | arbitrages et leurs raisons |
| `docs/MISSION_004_1_PERSISTENCE.md` | persistance PostgreSQL, tenants, instantanés, rapports |
| `docs/MISSION_004_1_HANDOFF.md` | persistance : état et règles |
| `docs/MISSION_004_2_HANDOFF.md` | tâches de fond et audit ; état et suite (Mission 004.3) |
| `docs/MISSION_004_2_DECISIONS.md` | arbitrages de la file et de l'audit (ADR-004.2-001 à 006) |
| `docs/ADMIN_CLI.md` | administration opérateur (`mervio admin`) |
| `docs/CONTAINER.md` | image, compose, bootstrap PostgreSQL, smoke test conteneurisé |
| `docs/PERFORMANCE.md` | mesures reproductibles : moteur, persistance, file, worker réel, concurrence, mémoire (004.3.9) |
| `docs/ROADMAP.md` / `docs/TODO.md` | suite |
