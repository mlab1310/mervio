# benchmarks/

Mesures reproductibles du moteur Mervio. **Mesurer d'abord ; aucune optimisation sans preuve** (ADR-004-012).

## 1. Performance du moteur — `run_benchmark.py`

```bash
.venv/bin/python benchmarks/run_benchmark.py --sizes 100,1000,10000,100000
.venv/bin/python benchmarks/run_benchmark.py --sizes 1000000 --timeout 3600 --out benchmarks/results/engine_1m.json
```

- **Données :** `healthy_store`, profil `fashion_eu`, 365 jours, graine 42, générées dans `data/benchmark/` (ignoré
  par Git). Un jeu déjà présent avec la même configuration est réutilisé.
- **Mesures par taille, en trois processus isolés :**
  - `parse` : `read_csv` de l'export commandes ;
  - `stages` : ingestion+normalisation+validation, découpage de période, KPI, profitabilité, séries, anomalies,
    comparaisons, cause racine, santé, insights, assemblage, JSON ;
  - `end_to_end` : `run_analysis` + JSON ;
  - `peak_rss_mb` : pic de mémoire du processus (`ru_maxrss`).
- **Sortie :** `benchmarks/results/engine_<date>_<arch>.json`.
- **Durée :** ~15 s jusqu'à 100 000 commandes ; ~4 min pour 1 M (génération incluse), ~8 Go de RAM libre
  recommandés.
- **Limites :**
  - une mesure par taille ;
  - machine unique ;
  - ingestion, normalisation et validation mesurées ensemble (une seule passe dans les connecteurs) ;
  - aucune modification du moteur pour mesurer.

Résultats et lecture : `research/performance_benchmark.md`.

## 2. Robustesse des scénarios — `scenario_robustness.py`

```bash
.venv/bin/python benchmarks/scenario_robustness.py --seeds 1,2,3,4,5 --orders 20000 --days 126
```

Exécute les 17 scénarios × 2 profils × graines sur le vrai moteur et confronte la vérité terrain. Code de sortie
non nul si une attente n'est pas satisfaite.
- **Sortie :** `benchmarks/results/scenario_robustness_<date>.json`.
- **Référence (15/09/2026) :** 170/170 en 162 s.

## 3. Persistance PostgreSQL — `persistence_benchmark.py` (Mission 004.1)

```bash
MERVIO_BENCH_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres \
    .venv/bin/python benchmarks/persistence_benchmark.py --sizes 10000,100000,1000000
```

Base et rôles jetables (hôte local uniquement), un processus par taille. Mesure : lecture CSV, écriture de
l'instantané, relecture du Dataset, analyse, écriture et lecture du rapport. Vérifie à chaque taille que le Dataset
relu est égal au Dataset CSV et que le rapport relu est identique à l'octet ; relève les plans de relecture.
- **Sortie :** `benchmarks/results/persistence_<date>_<arch>.json`.
- **Référence (15/09/2026, PostgreSQL 17.11) :** 1 M de commandes = écriture 78 s, relecture 17 s, analyse 7,8 s.

## 4. File de travaux — `jobs_benchmark.py` (Mission 004.2)

```bash
MERVIO_BENCH_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres \
    .venv/bin/python benchmarks/jobs_benchmark.py --sizes 10000,100000,1000000
```

Base et rôles jetables, un processus par profondeur de file. Pour 10 k, 100 k puis 1 M de travaux **en attente** :
coût unitaire d'une mise en file par l'API réelle, latence de prise (p50 / p95 / max) à froid puis en régime établi,
débit de prise et d'exécution avec 1, 2, 4 et 8 workers concurrents (une connexion PostgreSQL chacun, vrai
`Worker.run_until_empty`), débit de purge, tailles des tables et des index, mémoire de pointe, et plan d'exécution
de **l'instruction réelle de prise** (`jobs.explain_claim`).

Seul le remplissage de la file est fait en lot (`INSERT … SELECT FROM unnest`, comme les lignes canoniques de
004.1) : mêmes colonnes, mêmes contraintes, mêmes triggers, moins d'allers-retours. Le coût d'une mise en file
unitaire est mesuré à part, avec `workers.enqueue`, audit compris.
- **Sortie :** `benchmarks/results/jobs_<date>_<arch>.json`.
- **Référence (16/09/2026, PostgreSQL 17.11, Apple Silicon) :** prise à 1 M de travaux en attente = 0,47 ms (p50),
  0,68 ms (p95) ; débit maximal ~1 460 travaux/s à 4 workers ; table `jobs` 486 Mo ; mémoire du worker 48 Mo,
  constante de 10 k à 1 M.
- **Durée :** ~1 min pour 10 k et 100 k, ~7 min pour 1 M.

## 5. Résultats versionnés

`results/` contient des JSON de quelques kilo-octets (mesures et compteurs, aucune ligne de données). Ils servent
de référence : une régression de plus de 25 % de `run_analysis` à 100 000 commandes doit être expliquée.
