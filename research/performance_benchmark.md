# Performance — benchmark du moteur et budgets frontend (Mission 004.0)

Principe : **mesurer d'abord.** Aucun algorithme du moteur n'a été modifié pendant Mission 004.0.

## 1. Protocole

- **Script :** `benchmarks/run_benchmark.py`. Procédure dans `benchmarks/README.md`.
- **Données :**
  - `mervio.synthetic`, scénario `healthy_store`, profil `fashion_eu` ;
  - 365 jours, graine 42 ;
  - 4 fichiers moteur (commandes, produits, Stripe, Google Ads) ;
  - ~1,34 ligne de commande par commande.
- **Isolation :** trois processus séparés par taille :
  1. `parse` : `read_csv` seul sur l'export commandes ;
  2. `stages` : chaque étape du pipeline chronométrée ;
  3. `end_to_end` : `run_analysis` + JSON, comme la CLI.
- **Mémoire :** `ru_maxrss`, pic du processus entier.
- **Machine :** Apple M5 Pro (arm64), 18 cœurs, 24 Go, macOS 26.4, Python 3.11.16. Mesure unique par taille ;
  pas d'intervalle de confiance.
- **Résultats bruts :** `benchmarks/results/engine_20260915_arm64.json`.

## 2. Résultats

Temps en secondes, mémoire en Mo.
- **Ingestion** = ingestion + normalisation + validation, entremêlées dans les connecteurs.
- **Santé+insights+rapport** = somme des trois étapes finales.

| Commandes | Lignes CSV | CSV commandes (Mo) | Génération | Parse CSV | Ingestion | KPI | Séries | Anomalies | Comparaisons | Cause racine | Santé+insights+rapport | **`run_analysis`** | **Pic mémoire** | Pic parse seul | Rapport JSON (octets) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 97 | 134 | 0,02 | 0,02 | 0,001 | 0,012 | 0,000 | 0,004 | 0,000 | 0,002 | 0,000 | 0,001 | **0,02** | **27** | 21 | 34 209 |
| 1 002 | 1 364 | 0,20 | 0,04 | 0,006 | 0,030 | 0,000 | 0,008 | 0,000 | 0,004 | 0,000 | 0,001 | **0,04** | **30** | 23 | 38 124 |
| 9 968 | 13 340 | 2,0 | 0,20 | 0,051 | 0,220 | 0,000 | 0,053 | 0,000 | 0,028 | 0,002 | 0,002 | **0,31** | **70** | 49 | 36 453 |
| 99 921 | 133 628 | 20,1 | 1,71 | 0,510 | 2,383 | 0,003 | 0,845 | 0,000 | 0,443 | 0,019 | 0,007 | **3,64** | **515** | 311 | 35 324 |
| 999 146 | 1 336 419 | 201,2 | 16,6 | 5,520 | 27,309 | 0,042 | 11,391 | 0,000 | 5,948 | 0,272 | 0,066 | **45,38** | **3 741** | 2 015 | 35 643 |

## 3. Lecture

1. **Croissance quasi linéaire** du temps et de la mémoire, de 10⁴ à 10⁶ commandes (×100 commandes → ×147 temps,
   ×53 mémoire). Aucun effondrement.
2. **Goulot n°1 : ingestion (~60 %).**
   - `read_csv` charge le fichier entier en texte, puis en liste de dictionnaires : le parse seul atteint 2 Go à 1 M ;
   - les connecteurs créent ensuite des objets `Order` et `OrderItem` Python.
3. **Goulot n°2 : séries temporelles + comparaisons (~38 %).**
   - `build_series` et `compare_all` appellent `Dataset.window()` pour chaque période et chaque métrique ;
   - chaque appel reparcourt toutes les commandes : coût O(commandes × périodes × métriques) ;
   - c'est le seul coût algorithmique évitable identifié.
4. **Le calcul métier est négligeable.** KPI, anomalies, cause racine, santé et insights prennent < 1 % à toutes
   les tailles.
5. **Le rapport a une taille constante (~35 Ko)** quel que soit le volume : c'est une bonne frontière pour une API
   et un cache.
6. **Ordres de grandeur produit :**
   - une PME à 1 000 commandes/mois accumule ~36 000 commandes en 3 ans d'historique → < 1,5 s, < 250 Mo ;
   - 1 M de commandes (gros marchand) → 45 s, 3,7 Go : acceptable en tâche de fond, pas dans une requête HTTP.

## 4. Décisions (ADR-004-012)

| Constat | Décision | Quand |
|---|---|---|
| Analyse ≤ 4 s jusqu'à 100 000 commandes | aucune optimisation maintenant | — |
| Analyse de 45 s et 3,7 Go à 1 M | l'analyse ne s'exécute **jamais** dans une requête HTTP ; toujours en tâche de fond, rapport persisté | ADR-004-008 |
| `window()` répété | index des commandes par période construit une fois (tri + bissection), **si** un benchmark le justifie pour un tenant réel | mission de montée en charge, avec benchmark avant/après et suite de tests inchangée |
| Parse en mémoire | lecture en flux ou chargement en base puis agrégats SQL | quand l'ingestion passera par PostgreSQL (004.1+) |
| Au-delà de ~10⁶ commandes par tenant | évaluer DuckDB en processus (MIT) pour les agrégats, sans changer les contrats du rapport | sur preuve uniquement |

## 5. Robustesse des scénarios (coût de la vérité terrain)

- **Script :** `benchmarks/scenario_robustness.py`.
- **Couverture :** 17 scénarios × 2 profils × 5 graines = **170 exécutions, 170 conformes** en 162 s
  (`benchmarks/results/scenario_robustness_20260915.json`).
- **Tests unitaires :** une exécution par scénario (graine 3), ~1 s chacune, soit ~17 s ajoutées à la suite.

## 6. Budgets frontend (cibles, non mesurées : aucun frontend n'existe)

| Domaine | Budget | Source ou raison |
|---|---|---|
| LCP | ≤ 2,5 s au 75ᵉ percentile | seuil « bon » des Core Web Vitals (Google) |
| INP | ≤ 200 ms au p75 | idem |
| CLS | ≤ 0,1 au p75 | idem ; skeletons de taille fixe |
| Réponse API « rapport de période » | ≤ 100 Ko compressé, p95 ≤ 300 ms | rapport mesuré à ~35 Ko ; lu depuis un instantané persisté, jamais recalculé |
| Listes (commandes, produits) | pagination serveur (curseur), 50 lignes par défaut, virtualisation au-delà de 200 lignes affichées | Atlas (TanStack Virtual) |
| Points par graphique | ≤ 500 affichés ; sous-échantillonnage déclaré à l'écran | Atlas (LTTB) |
| JavaScript initial | ≤ 200 Ko compressé sur la page Briefing | à confirmer au premier build |
| Rendu | composants serveur pour le Briefing et les pages Problème (données déjà agrégées) ; composants client limités aux filtres, graphiques et formulaires d'approbation | ADR-004-010 |
| Mesure | RUM sur les pages authentifiées, sans cookie tiers ni PII ; next-cwv-monitor (MIT) est une option auto-hébergée, à comparer à la télémétrie de la plateforme d'hébergement | mission frontend |

Aucun de ces budgets n'est une mesure. Ils deviendront des critères d'acceptation mesurés en mission frontend.
