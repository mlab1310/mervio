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

## 3. Résultats versionnés

`results/` contient des JSON de quelques kilo-octets (mesures et compteurs, aucune ligne de données). Ils servent
de référence : une régression de plus de 25 % de `run_analysis` à 100 000 commandes doit être expliquée.
