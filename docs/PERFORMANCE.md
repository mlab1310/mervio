# Performance de Mervio — mesures reproductibles (Mission 004.3.9)

Ce document dit **ce que Mervio fait aujourd'hui**, mesuré, sur une machine précise. Ce n'est ni une
promesse ni un objectif. Aucune optimisation n'a été faite : **mesurer d'abord** (ADR-004-012).

Conventions de lecture :

- **OBSERVÉ** : mesuré par `benchmarks/performance.py`, chiffres tirés du document JSON de référence ;
- **INFÉRÉ** : déduction raisonnable, non prouvée par une mesure dédiée ;
- **LIMITE** : ce que la mesure ne dit pas.

Référence : `benchmarks/results/performance_20260917_arm64.json` (exécution complète du 17/09/2026,
21 min 21 s). Les tableaux de la section 7 sont générés depuis ce fichier par
`benchmarks/perf_report.py`, jamais recopiés à la main.

## 1. Pourquoi ces benchmarks

Répondre factuellement à : *combien de données Mervio traite-t-il, avec combien de mémoire, en combien de
temps, avec quelle concurrence ?*

Les mesures historiques (004.0 à 004.3.5) avaient quatre défauts, corrigés ici.

| Défaut historique | Correction en 004.3.9 |
|---|---|
| Le débit worker (~1 460 travaux/s) venait d'un gestionnaire **vide** | Le cadre à vide est mesuré **et étiqueté** comme tel (catégorie `infrastructure`). Les travaux réels (import, analyse) sont mesurés à part, dans un vrai processus `mervio worker`. |
| Une seule exécution par taille | Répétitions déclarées. Médiane, p95 et p99 au rang le plus proche. `p95_is_max` signale un petit échantillon. |
| Charges mêlées (ingestion, persistance, analyse, file, worker) | Six charges séparées, chacune dans des processus neufs, chacune avec sa catégorie. |
| Environnement et jeu de données incomplètement décrits | Chaque document enregistre OS, CPU, mémoire, Python, PostgreSQL, commit, état Git, runner GitHub, graine, tailles, empreinte du CSV. |

## 2. Lancer les benchmarks

### Prérequis

- l'environnement verrouillé : `pip install --no-deps -r requirements.lock` ;
- un PostgreSQL 17 **local**, pour les charges avec base ;
- environ 8 Go de mémoire disponible pour 1 M de commandes (la suite refuse sinon, voir la section 5) ;
- environ 3 Go de disque pour les jeux synthétiques (`data/benchmark/`, ignoré par Git).

### Commandes

```bash
export MERVIO_BENCH_ADMIN_DATABASE_URL=postgresql://mervio_admin@127.0.0.1:55432/postgres
.venv/bin/python benchmarks/performance.py                       # matrice complète (~21 min ici)
.venv/bin/python benchmarks/performance.py --workloads analytics --sizes 10000,100000
.venv/bin/python benchmarks/performance.py --workloads queue --queue-depths 1000 --queue-org-scaling 100
.venv/bin/python benchmarks/perf_report.py benchmark-results/<fichier>.json   # tableaux Markdown
```

`--help` liste toutes les options : tailles, répétitions, profondeurs de file, nombre d'organisations,
fils, bail, concurrence, délai, `--force-memory`, `--output`, `--data-dir`.

Codes de sortie :

| Code | Signification |
|---|---|
| 0 | Toutes les charges demandées exécutées, ou explicitement `not_executed` avec une raison |
| 1 | Une charge en échec ou hors délai |
| 2 | Configuration invalide |

### Sécurité de la base

- `MERVIO_BENCH_ADMIN_DATABASE_URL` doit viser un serveur **local** : un hôte distant est refusé.
- Elle ne peut pas être égale à `MERVIO_DATABASE_URL`, `MERVIO_MIGRATION_DATABASE_URL` ou
  `MERVIO_TEST_ADMIN_DATABASE_URL`.
- Chaque charge crée **sa** base `mervio_perf_<charge>_<aléa>`, puis la supprime, même en cas d'échec.
  La référence finit avec `leftover_disposable_objects = {databases: 0, roles: 0}`.
- Rôles jetables, comme le bootstrap du conteneur (004.3.8) :
  - `migrator` : propriétaire, **sans** CREATEROLE ;
  - `app` : membre de `mervio_app` ;
  - `worker` : membre de `mervio_app` et `mervio_worker`.

  Aucun n'est superutilisateur ni BYPASSRLS. RLS, identité de service, délégation, audit, bail et fencing
  restent actifs.
- Deux opérations privilégiées, **identifiées**, ne sont pas du comportement applicatif :
  - `ANALYZE` par le propriétaire du schéma après le remplissage de la file ;
  - la lecture de `pg_stat_database` et `pg_stat_activity` par le compte d'administration (supervision de
    la charge de concurrence).
- Les mots de passe transitent par l'entrée standard des processus fils, jamais par `argv`. Toute sortie
  d'échec est nettoyée, et le document est refusé s'il contient un secret ou une URL avec identifiants.

## 3. Jeux de données

| Paramètre | Valeur |
|---|---|
| Générateur | `mervio.synthetic` 0.1.0 (déterministe) |
| Scénario / profil | `healthy_store` / `fashion_eu` |
| Période | 365 jours, 2025-09-14 → 2026-09-13 ; jour d'analyse 2026-09-14 |
| Graine | **42** |
| Fichiers utilisés | export commandes et produits Shopify, transactions Stripe, Google Ads |
| Tailles | 10 000, 100 000, 1 000 000 commandes cibles (voir la section 7 pour les comptes réels, clients, lignes et empreinte) |

Un jeu n'est régénéré que si sa configuration diffère ; l'empreinte SHA-256 du CSV commandes est enregistrée.

## 4. Métriques

| Métrique | Définition |
|---|---|
| `wall_s` | durée murale (`time.perf_counter`) |
| `cpu_s` | temps CPU utilisateur + système du processus (`getrusage`) |
| `peak_rss_mb` | pic de mémoire **résidente du processus entier** depuis son démarrage (`ru_maxrss`, interpréteur et bibliothèques compris), **pas** le tas Python. Pour un worker lancé comme sous-processus : `RUSAGE_CHILDREN` après sa fin. |
| `baseline_rss_mb` / `final_rss_mb` | RSS instantané (`ps` ou `/proc`) après les imports et à la fin de la mesure |
| médiane, p95, p99 | p95 et p99 au **rang le plus proche** : toujours une valeur observée ; avec N < 20, p95 = max (`p95_is_max`) |
| `handler_ms` | travail réel du gestionnaire (mesuré par le gestionnaire du produit) |
| `attempt_ms` | de la prise à la publication du sort (`started_at` → `finished_at` en base) |
| `framework_overhead_ms` | `attempt_ms − handler_ms` : délégation, gardien de bail, audit, publication |
| `queue_wait_ms` | de la disponibilité à la prise, intervalle de sondage du worker compris (0,1 s dans la mesure) |
| `observed_ms` / `chain_ms` | vu du client : de la mise en file à l'état terminal observé (sondage toutes les 50 ms) |

Répétitions par défaut :

| Charge | 10k | 100k | 1M |
|---|---|---|---|
| Moteur | 7 | 5 | 3 |
| Persistance | 5 | 3 | 1 |
| Worker réel | 5 | 3 | 1 |

Chaque répétition tourne dans un **processus neuf**. Le moteur est précédé d'un premier passage rapporté à
part (`first_process_run`). Le cache de pages de l'OS n'est pas vidé (ce qui exigerait les droits root) :
aucune mesure n'est donc « à froid » au sens disque.

## 5. Garde mémoire

Avant chaque cas, la suite lit la mémoire disponible :

- Linux : `MemAvailable` ;
- macOS : le taux libre de `memory_pressure`.

Elle refuse un cas de 500 000 commandes ou plus sous 8 Go disponibles (2 Go en dessous de cette taille).
Le cas est alors `not_executed` avec sa raison. **Il n'est jamais remplacé par une taille plus petite.**
`--force-memory` lève la garde.

## 6. Environnement de la référence

Voir le premier tableau de la section 7.

**LIMITE** :

- Mac de développement, avec d'autres applications ouvertes.
- 1,4 Go de swap et 8 Go de mémoire compressée au début de la session.
- PostgreSQL Homebrew local avec sa configuration par défaut, sur le même disque que le code.
- Aucun conteneur.
- Ces chiffres ne sont **pas comparables** à ceux d'un runner GitHub (voir la section 11).

Le document de référence a été produit sur le commit `d3f40e2` avec l'outillage 004.3.9 non encore commité
(`git_dirty: true`). **Aucun fichier de `src/` ne différait de `d3f40e2`.**

## 7. Résultats (référence)

#### Environnement

| Element | Valeur |
|---|---|
| commit | d3f40e20f9ea90f929d425db41d74e2c41ee9030 (+ modifications locales) |
| date (UTC) | 2026-09-17T16:34:19Z |
| OS | macOS-26.4-arm64-arm-64bit |
| CPU | Apple M5 Pro (18 coeurs) |
| memoire | 24.0 Go, 13.4 Go disponibles au debut |
| Python | 3.11.16 (CPython) |
| PostgreSQL | 17.11 (Homebrew) |
| conteneur | non |
| GitHub Actions | non |
| suite | mervio-performance 1.0.0, duree 1280.6 s |

#### Jeux de donnees

| Cible | Commandes | Clients | Lignes CSV | CSV commandes | Periode | sha256 (debut) |
|---|---|---|---|---|---|---|
| 10000 | 9968 | 6722 | 13340 | 2.0 Mo | 2025-09-14 → 2026-09-13 | 98cc6bdbe4af |
| 100000 | 99921 | 67673 | 133628 | 20.1 Mo | 2025-09-14 → 2026-09-13 | fb7b7292f00e |
| 1000000 | 999146 | 679174 | 1336419 | 201.2 Mo | 2025-09-14 → 2026-09-13 | 3b31f14cbdc9 |

#### Moteur analytique (sans base)

| Commandes | N | Mediane | P95 | CPU (med.) | RSS pic (med.) | RSS pic P95 | Commandes/s |
|---|---|---|---|---|---|---|---|
| 10000 | 7 | 0.31 s | 0.32 s (=max) | 0.31 s | 72 Mo | 72 Mo (=max) | 31 847 |
| 100000 | 5 | 3.70 s | 3.74 s (=max) | 3.71 s | 518 Mo | 518 Mo (=max) | 26 984 |
| 1000000 | 3 | 45.68 s | 46.17 s (=max) | 45.67 s | 4899 Mo | 4899 Mo (=max) | 21 872 |

#### Etapes du moteur

| Etape | 10000 (med. s / part) | 100000 (med. s / part) | 1000000 (med. s / part) |
|---|---|---|---|
| ingest_normalize_validate | 0.220 / 72% | 2.380 / 65% | 26.862 / 60% |
| window_slice | 0.001 / 0% | 0.009 / 0% | 0.116 / 0% |
| kpis | 0.000 / 0% | 0.003 / 0% | 0.042 / 0% |
| profitability | 0.000 / 0% | 0.001 / 0% | 0.015 / 0% |
| time_series | 0.053 / 17% | 0.811 / 22% | 11.508 / 26% |
| anomaly_detection | 0.000 / 0% | 0.000 / 0% | 0.000 / 0% |
| comparisons | 0.028 / 9% | 0.426 / 12% | 5.979 / 13% |
| root_cause | 0.002 / 1% | 0.020 / 0% | 0.270 / 1% |
| business_health | 0.000 / 0% | 0.001 / 0% | 0.017 / 0% |
| insights | 0.000 / 0% | 0.001 / 0% | 0.015 / 0% |
| report_assembly | 0.002 / 1% | 0.004 / 0% | 0.035 / 0% |
| json_serialization | 0.000 / 0% | 0.000 / 0% | 0.000 / 0% |

#### Persistance PostgreSQL

| Commandes | N | Lignes canoniques | Lecture CSV | Ecriture instantane | Relecture | Analyse | Ecriture rapport | Import complet (cas d'usage) | RSS pic |
|---|---|---|---|---|---|---|---|---|---|
| 10000 | 5 | 35508 | 0.22 s / 0.23 s (=max) | 0.76 s / 0.83 s (=max) | 0.09 s / 0.09 s (=max) | 0.08 s / 0.08 s (=max) | 0.01 s / 0.01 s (=max) | 1.32 s / 1.34 s (=max) | 111 Mo |
| 100000 | 3 | 341980 | 2.39 s / 2.41 s (=max) | 7.19 s / 7.60 s (=max) | 1.24 s / 1.25 s (=max) | 0.74 s / 0.75 s (=max) | 0.01 s / 0.01 s (=max) | 12.92 s / 13.26 s (=max) | 719 Mo |
| 1000000 | 1 | 3406013 | 27.53 s / 27.53 s (=max) | 80.98 s / 80.98 s (=max) | 16.01 s / 16.01 s (=max) | 8.02 s / 8.02 s (=max) | 0.01 s / 0.01 s (=max) | 153.49 s / 153.49 s (=max) | 4830 Mo |

#### Infrastructure: file et dispatcher (gestionnaire VIDE pour le cadre)

| Travaux en file | Organisations | Sonde dispatcher ms (med/p95) | Plan de la sonde | Prise ms (med/p95) | Fin ms (med/p95) | Mise en file API ms (med/p95) | Cadre a VIDE, travaux/s par fils |
|---|---|---|---|---|---|---|---|
| 100 | 10 | 0.80 / 0.89 | lecture + tri des travaux, 1.31 ms | 1.14 / 1.44 (N=100) | 1.19 / 1.39 | 0.68 / 1.23 | 1: 229.9 · 2: 498.1 · 4: 781.1 · 8: 638.5 |
| 1000 | 10 | 1.15 / 1.27 | lecture + tri des travaux, 1.81 ms | 1.17 / 1.48 (N=200) | 1.06 / 1.31 | 0.67 / 1.21 | 1: 214.6 · 2: 435.4 · 4: 771.7 · 8: 633.3 |
| 10000 | 10 | 4.45 / 4.60 | lecture + tri des travaux, 5.14 ms | 1.33 / 1.66 (N=200) | 1.02 / 1.33 | 0.66 / 1.24 | 1: 204.0 · 2: 407.8 · 4: 730.4 · 8: 636.6 |
| 100000 | 10 | 0.80 / 0.92 | index + LIMIT, 0.42 ms | 1.33 / 1.54 (N=200) | 1.04 / 1.23 | 0.68 / 1.22 | 1: 223.7 · 2: 511.2 · 4: 783.7 · 8: 626.0 |
| 10000 | 1 | 0.70 / 0.85 | index + LIMIT, 0.27 ms | 1.32 / 1.54 (N=200) | 1.09 / 1.33 | 0.68 / 1.17 | 1: 175.7 · 2: 397.3 · 4: 631.2 · 8: 456.9 |
| 10000 | 100 | 3.37 / 3.51 | lecture + tri des travaux, 3.52 ms | 1.31 / 1.59 (N=200) | 1.14 / 1.36 | 0.68 / 1.17 | 1: 211.1 · 2: 421.3 · 4: 746.0 · 8: 665.8 |
| 10000 | 1000 | 4.25 / 4.43 | lecture + tri des travaux, 5.01 ms | 2.73 / 3.04 (N=200) | 2.15 / 2.37 | 0.69 / 1.28 | 1: 120.9 · 2: 231.4 · 4: 451.2 · 8: 571.1 |

#### Infrastructure: gardien de bail

| Condition | Renouvellements | p50 ms | p95 ms | p99 ms | max ms | Duree du travail |
|---|---|---|---|---|---|---|
| lease/idle | 29 | 4.73 | 7.74 | 21.70 | 21.70 | 30.1 s |
| lease/import_load | 37 | 14.22 | 189.94 | 239.36 | 239.36 | 38.0 s |

#### Worker reel: import puis analyse (bout en bout)

| Commandes | N | Import (gestionnaire) | Import (cadre) | Analyse (gestionnaire) | Analyse (cadre) | Attente en file imp./ana. | Chaine med / p95 | RSS pic worker | Renouvellements |
|---|---|---|---|---|---|---|---|---|---|
| 10000 | 5 | 1.31 s | 12.0 ms | 0.19 s | 9.2 ms | 78 ms / 81 ms | 1.75 s / 1.77 s (=max) | 110 Mo | 0 |
| 100000 | 3 | 13.21 s | 14.0 ms | 1.72 s | 8.9 ms | 85 ms / 50 ms | 15.16 s / 15.28 s (=max) | 717 Mo | 0 |
| 1000000 | 1 | 140.93 s | 30.3 ms | 17.45 s | 14.6 ms | 89 ms / 77 ms | 158.69 s / 158.69 s (=max) | 4985 Mo | 1 |

#### Concurrence: processus worker reels, analyses reelles

| Workers | Analyses | Analyses/s | Duree | Analyse med/p95 | Latence med/p95 | RSS cumule (echant.) | Backends actifs max |
|---|---|---|---|---|---|---|---|
| 1 | 16 | 0.58 | 27.7 s | 1.70 s / 1.80 s (=max) | 14.55 s / 27.54 s (=max) | 242 Mo | 1 |
| 2 | 16 | 1.16 | 13.8 s | 1.71 s / 1.74 s (=max) | 7.62 s / 13.67 s (=max) | 482 Mo | 2 |
| 4 | 16 | 1.91 | 8.4 s | 1.78 s / 1.85 s (=max) | 4.96 s / 8.28 s (=max) | 968 Mo | 2 |
| 8 | 16 | 3.06 | 5.2 s | 1.97 s / 2.36 s (=max) | 3.33 s / 5.10 s (=max) | 1947 Mo | 3 |

## 8. Lecture des résultats

### 8.1 Moteur analytique

**OBSERVÉ**
- 10k : 0,31 s, 72 Mo.
- 100k : 3,70 s, 518 Mo.
- 1M : 45,7 s (p95 46,2 s, N=3), 4,9 Go de pic.
- Le CPU égale la durée murale : **le moteur est mono-cœur**.
- Le débit baisse avec la taille : 31,8 k → 27,0 k → 21,9 k commandes/s.
- La durée croît un peu plus vite que le volume (×11,9 puis ×12,3 par décade).

**OBSERVÉ — goulots**
- **Ingestion / normalisation / validation** : 72 %, 65 % puis 60 % du temps.
- **Séries temporelles** : 17 %, 22 % puis 26 %.
- **Comparaisons** : 9 %, 12 % puis 13 %.
- Le reste est négligeable : KPI, rentabilité, anomalies, cause racine, santé, rapport, JSON (< 2 % cumulés).
- Les chiffres historiques de 004.0 (« ingestion ~60 %, séries + comparaisons ~38 % ») sont **reproduits à
  1M** (60 % / 39 %). Aux petites tailles, l'ingestion pèse davantage.

**OBSERVÉ — rapport**
- Le rapport fait 35 à 36 ko **quelle que soit la taille**.

### 8.2 Mémoire (section 10 pour l'évolution avec la taille)

**OBSERVÉ**
- La lecture CSV seule (`read_csv`, export commandes) monte à 55 Mo, 317 Mo et **2,96 Go** de pic.
- À 1M, le processus garde 0,8 à 2,0 Go de RSS **après** l'analyse (`final_rss_mb`) : la mémoire n'est pas
  entièrement rendue au système.

**INFÉRÉ**
- La liste de lignes lues (dictionnaires Python, environ 2 Ko par ligne CSV à 1M) est le premier poste
  mémoire.
- Le `Dataset` normalisé s'y ajoute pendant l'ingestion.

### 8.3 Persistance

**OBSERVÉ (1M, N=1)**

| Étape | Durée |
|---|---|
| Lecture CSV | 27,5 s |
| **Écriture de l'instantané** (3,4 M de lignes canoniques) | **81,0 s**, ~42 k lignes/s |
| Relecture | 16,0 s |
| Analyse du jeu relu | 8,0 s |
| Écriture du rapport | 15 ms |
| Pic de mémoire | 4,8 Go |

- **Le cas d'usage complet `import_csv_snapshot` prend 153 s**, soit ~45 s de plus que lecture + écriture.
- L'écriture est donc le premier poste de la persistance, devant la relecture.

**OBSERVÉ dans le code** : ce cas d'usage :
1. calcule les empreintes des fichiers **avant et après** la lecture ;
2. valide les fichiers (`validate_many`, qui exécute les connecteurs) ;
3. relit les fichiers pour construire le `Dataset` ;
4. écrit.

**Chaque fichier est donc lu deux fois en entier.**

**INFÉRÉ** : la passe de validation explique l'essentiel de l'écart (le temps de lecture d'un 1M est ~27 s).

**OBSERVÉ — taille des tables** après le **premier** instantané de 1M d'une base neuve :

| Table | Taille |
|---|---|
| `orders` | 451 Mo |
| `payments` | 324 Mo |
| `order_lines` | 248 Mo |
| `refunds` | 23 Mo |
| **Total** | **~1,05 Go** (index compris) |

**LIMITE** :
- Les répétitions d'une même taille écrivent dans la même base (deux instantanés par répétition) : les
  dernières répétitions voient des tables plus grosses.
- La mesure « écriture de l'instantané » inclut le codage Python des lignes. Elle ne sépare pas le temps
  serveur du temps client.

### 8.4 Infrastructure : file, dispatcher, prise

**OBSERVÉ — latences**
- **Prise** (chemin du service, RLS du rôle worker) : 1,1 à 1,3 ms en médiane jusqu'à 100 000 travaux en
  file sur 10 organisations ; 2,7 ms avec 1 000 organisations.
- **Fin d'un travail** : ~1 ms.
- **Mise en file par l'API** (travail + audit) : 0,7 ms (p95 1,2 ms).

**OBSERVÉ — la sonde du dispatcher n'a pas un coût constant.** Le plan exécuté est enregistré à chaque cas.

| Travaux en file (organisations) | Par organisation | Plan de la sonde | Exécution serveur | Vu du client (méd.) |
|---|---|---|---|---|
| 100 (10) | 10 | lit **et trie** les travaux de chaque organisation (`jobs_tenant_key`, Bitmap Heap Scan + Sort) | 1,31 ms | 0,80 ms |
| 1 000 (10) | 100 | idem | 1,81 ms | 1,15 ms |
| 10 000 (10) | 1 000 | lit et trie 1 000 travaux par organisation (bitmap sur `jobs_ready_idx` + Sort) | 5,14 ms | 4,45 ms |
| 100 000 (10) | 10 000 | `jobs_ready_idx` + `LIMIT 1`, s'arrête au premier travail | 0,42 ms | 0,80 ms |
| 10 000 (1) | 10 000 | idem | 0,27 ms | 0,70 ms |
| 10 000 (100) | 100 | tri par organisation | 3,52 ms | 3,37 ms |
| 10 000 (1 000) | 10 | tri ; parcourt 195 organisations pour en trouver 50 (4 914 blocs lus) | 5,02 ms | 4,25 ms |

Le temps serveur vient d'`EXPLAIN (ANALYZE)`, dont l'instrumentation peut le rendre supérieur au temps
vu du client.

- **INFÉRÉ** : le planificateur préfère le tri quand une organisation a peu de travaux (jusqu'à ~1 000
  ici). Le coût croît alors avec la profondeur **par organisation**, jusqu'à ce que l'index
  redevienne plus avantageux. Le test existant `test_the_dispatch_probe_cost_does_not_grow_with_queue_depth`
  couvre 3 000 et 6 000 travaux par organisation, pas cette plage.
- **Rien n'a été modifié** : c'est une observation à traiter plus tard, pas dans cette mission de mesure.

**Chiffre historique « ~0,28 ms »**
- Introuvable dans le dépôt et l'historique Git.
- **INFÉRÉ** : il correspond au temps d'exécution **serveur** de la sonde pour une organisation (0,27 ms
  mesuré ici).
- Vu du client, la même sonde coûte 0,70 ms (aller-retour et transaction de vérification du principal).

**OBSERVÉ — cadre du worker avec un gestionnaire VIDE** (Worker dispatché complet : identité, délégation,
gardien de bail sur sa connexion, audit, publication) :

| Fils | Débit |
|---|---|
| 1 | 205 à 230 travaux/s (~4,5 ms par travail) |
| 4 | ~780 travaux/s |
| 8 | ~630 travaux/s (le débit **baisse** au-delà de 4 fils dans un même processus) |

- Avec 1 000 organisations : 121 travaux/s à 1 fil.
- **Ce n'est pas un débit analytique.** Le chiffre historique de 1 460/s mesurait le Worker 004.2 à liste
  explicite, sans dispatcher ni gardien de bail : il n'est pas comparable.

**OBSERVÉ — remplissage en lot** : 35 à 45 k travaux/s. Il sert seulement à préparer la file.

### 8.5 Gardien de bail

**OBSERVÉ** (bail de 30 s renouvelé chaque seconde : les minima acceptés par `WorkerSettings`).

| Condition | Renouvellements | p50 | p95 | p99 |
|---|---|---|---|---|
| Au repos | N=29 | 4,7 ms | 7,7 ms | 21,7 ms |
| Pendant trois imports réels de 100k sur la connexion principale du même worker | N=37 | 14,2 ms | **190 ms** | **239 ms** |

- Les renouvellements restent réguliers (0,97/s) et aucun bail n'est perdu.
- Les chiffres historiques de 004.3.5 (p50 5,5 ms, p95 13,9 ms, bail synthétique de 3 s) sont du même ordre
  au repos.

**INFÉRÉ** : la latence sous charge vient de la contention CPU et d'E/S entre l'import (Python et
PostgreSQL) et la connexion du gardien, pas d'un verrou. Le renouvellement ne touche que la ligne du
travail. Avec la configuration par défaut (bail de 300 s, renouvellement toutes les 100 s, délai
d'instruction du gardien 1 s), 240 ms laissent une large marge.

**LIMITE** : un seul travail de 38 s. Ce n'est pas une campagne de contention.

### 8.6 Worker réel : cadre contre exécution

**OBSERVÉ** (processus `mervio worker` réel ; bail de 300 s, renouvellement toutes les 100 s)

| Commandes | Import (gestionnaire) | Import (cadre) | Analyse (gestionnaire) | Analyse (cadre) |
|---|---|---|---|---|
| 10k | 1,31 s | 12 ms | 0,19 s | 9 ms |
| 100k | 13,2 s | 14 ms | 1,72 s | 9 ms |
| 1M | 140,9 s | 30 ms | 17,5 s | 15 ms |

- Le **cadre** du worker (délégation, gardien, audit, publication) coûte **9 à 30 ms par travail**.
  Le **travail réel** coûte de 0,2 s à 141 s : **à ces tailles, le coût est entièrement dans l'import et
  l'analyse, pas dans la file.**
- L'attente en file vaut 50 à 90 ms, car le worker sonde toutes les 0,1 s dans cette mesure.
  Avec la configuration par défaut (1 s, puis jusqu'à 5 s au repos), elle serait plus longue.
- À 1M, l'import dure 141 s pour un bail de 300 s : 1 renouvellement.
- Le worker démarre à 37 Mo. Son pic : 110 Mo (10k), 717 Mo (100k), **4,99 Go (1M)**.
- Après une chaîne de 1M, il garde **2,1 Go** de RSS.
- À 10k, le RSS après chaque chaîne monte de 89 à 99 Mo sur cinq chaînes. **INFÉRÉ** : fragmentation ou
  caches, non étudié.
- Aucun événement d'erreur, code de sortie 0.
- 10 traces d'audit par chaîne ; rapport persisté avec la même empreinte.

### 8.7 Concurrence

**OBSERVÉ** (16 analyses réelles d'un instantané de 100k, 1 à 8 processus `mervio worker` sur 18 cœurs)

| Workers | Débit | Gain | Analyse (médiane) |
|---|---|---|---|
| 1 | 0,58 analyse/s | ×1 | 1,70 s |
| 2 | 1,16 analyse/s | ×2 | 1,71 s |
| 4 | 1,91 analyse/s | ×3,3 | 1,78 s |
| 8 | 3,06 analyses/s | ×5,3 | 1,97 s (p95 2,36 s) |

- La mémoire s'additionne : ~242 Mo par worker, ~1,95 Go pour 8 workers.
- Côté base : aucune lecture disque (`blks_read` = 0 ou 12), aucun fichier temporaire, aucun interblocage.
  Au plus 3 connexions actives échantillonnées à la fois.

**INFÉRÉ** : la saturation vient d'abord du CPU de la machine partagée par les workers Python et
PostgreSQL. PostgreSQL n'est pas le goulot à cette échelle (tout est en cache).

**LIMITE** :
- Une seule organisation, un seul instantané, 16 analyses.
- L'échantillonnage des connexions actives (toutes les 0,2 s) sous-estime les pics.
- Les compteurs de base incluent le sondage du client.

### 8.8 Bout en bout : import → analyse → rapport → audit

**OBSERVÉ** (vu du client, mise en file de l'import jusqu'à l'analyse terminée)

| Commandes | Durée de la chaîne |
|---|---|
| 10k | **1,75 s** (p95 1,77 s, N=5) |
| 100k | **15,2 s** (p95 15,3 s, N=3) |
| 1M | **158,7 s** (N=1) |

Soit 5,7 k à 6,6 k commandes/s de bout en bout. **L'import représente 75 % de la chaîne à 10k et 87 à 89 %
à 100k et 1M.** Le LLM n'est
pas dans ce parcours (il n'existe pas en production).

## 9. Variabilité observée entre deux exécutions complètes

Une première exécution complète (même machine, même code produit, outillage avant la correction de la
charge `queue`, non versionnée) avait donné :

| Mesure | Exécution préliminaire | Référence |
|---|---|---|
| Moteur 1M, durée médiane | 46,0 s | 45,7 s |
| Moteur 1M, pic RSS médian (max) | **3,74 Go (4,05 Go)** | **4,90 Go (4,90 Go)** |
| Persistance 1M, pic RSS | 4,47 Go | 4,83 Go |
| Worker 1M, pic RSS | 4,99 Go | 4,99 Go |
| Bail sous import, p95 | 137 ms | 190 ms |
| Concurrence, 8 workers | 2,64 analyses/s | 3,06 analyses/s |

**OBSERVÉ** : les durées se reproduisent à ~1 % près ; **le pic mémoire du moteur à 1M varie de 3,7 à
4,9 Go** d'une exécution à l'autre.

**INFÉRÉ** : il dépend de l'allocateur et de la gestion mémoire de macOS (mémoire compressée) au moment de
la mesure. **Pour dimensionner, retenir le haut de la plage.**

L'outil de cette exécution préliminaire contenait un défaut, corrigé avant la référence : la file se
vidait avant les passages à 2, 4 et 8 fils, et des prises sur file vide étaient comptées dans la latence.

## 10. Évolution avec la taille

| Commandes | Moteur : durée | Moteur : pic RSS | Octets de RSS par commande (hors base de 27 Mo) |
|---|---|---|---|
| 10k | 0,31 s | 72 Mo | ~4,5 ko |
| 100k | 3,70 s | 518 Mo | ~4,9 ko |
| 1M | 45,7 s | 3,7 à 4,9 Go | ~3,7 à 4,9 ko |

- **OBSERVÉ** : la mémoire est à peu près **linéaire** en nombre de commandes, de 4 à 5 ko par commande
  (≈ 3,7 ko par ligne CSV), plus 27 Mo de base.
- **OBSERVÉ** : la durée est **légèrement super-linéaire** (×11,9 puis ×12,3 par décade).
- **INFÉRÉ**, extrapolation non mesurée : 10 M de commandes demanderaient ~40 à 50 Go et ~8 min sur cette
  machine, **hors de portée** du `Dataset` en mémoire actuel.
- **Worker**, pour dimensionner les processus :
  - pics mesurés : 110 Mo (10k), 717 Mo (100k), 5,0 Go (1M) ;
  - le chemin persistant (import puis relecture) coûte plus que le chemin CSV (4,8 à 5,0 Go contre 3,7 à
    4,9 Go à 1M).

## 11. GitHub Actions et comparabilité

- Workflow `.github/workflows/benchmarks.yml` : **manuel** (`workflow_dispatch`), jamais sur push.
  - tailles par défaut 10k et 100k ; 1M sur demande explicite (~5 Go, environ 15 min de plus) ;
  - PostgreSQL identique au job `test` de la CI ;
  - résultats en artefact pendant 30 jours.
- **Aucune exécution GitHub n'a encore eu lieu.** Il n'y a aucun chiffre de runner dans ce document.
- Un runner GitHub (processeurs et disques partagés) **n'est pas comparable** à ce Mac. On ne compare que
  des exécutions du même environnement ; chaque document enregistre `github_actions`, `ImageOS` et
  `ImageVersion`.
- **Pas de porte de performance en CI**, par choix :
  - les variations entre runners partagés dépassent les écarts qu'une porte devrait détecter ;
  - une porte serrée serait instable, une porte large n'apprendrait rien.

  La règle historique reste une règle de **revue**, non automatisée : « une régression de plus de 25 % de
  `run_analysis` à 100 000 commandes doit être expliquée » (`benchmarks/README.md`), à mesurer sur la même
  machine que la référence.
- La CI normale (`ci.yml`) n'exécute pas les benchmarks. Elle exécute leurs **tests**
  (`tests/test_benchmark_harness.py`, `tests/persistence/test_benchmark_suite.py`) à très petite échelle.

## 12. Limites

- Une seule machine de référence : Apple Silicon, macOS, PostgreSQL local non réglé.
- Données **synthétiques** (un seul scénario et un seul profil). Des exports réels peuvent avoir d'autres
  proportions (lignes par commande, remboursements, colonnes).
- Répétitions réduites à 1M (N=1 pour la persistance et le worker, N=3 pour le moteur) : p95 = max.
- Pas de mesure à froid au sens disque (cache de l'OS non vidé).
- Pas de répartition temps serveur / temps client dans la persistance ni dans la file.
- Concurrence limitée à une organisation et à 16 analyses de 100k. Pas d'import concurrent.
- La latence du gardien de bail sous charge repose sur un seul travail de 38 s.
- Le LLM, l'API HTTP, les imports planifiés et la purge à grande échelle ne sont pas mesurés
  (la purge l'était en 004.2 : `benchmarks/jobs_benchmark.py`).
- Les scripts historiques (`run_benchmark.py`, `persistence_benchmark.py`, `jobs_benchmark.py`,
  `worker_baseline.py`) restent en place pour relire leurs résultats. Ils n'ont pas été réexécutés.

## 13. Ce qui n'est PAS une conclusion

Aucun chiffre de ce document n'est un engagement de service. Les observations qui mériteraient une
mission d'optimisation sont signalées comme telles, **non traitées ici** :

- mémoire du `Dataset` à 1M ;
- double lecture des fichiers à l'import ;
- plan de la sonde du dispatcher à profondeur moyenne ;
- durée de l'import 1M par rapport au bail.
