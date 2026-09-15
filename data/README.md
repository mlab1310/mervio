# data/ — nature, provenance et droit d'usage de chaque jeu

**Règle absolue :** aucune donnée de ce dossier n'est une donnée client, et aucune n'entre en production.
Les données réelles de marchands (OH5, futurs exports pilotes) vivent **hors dépôt**, sous `~/mervio-validation/`,
ou dans `data/uploads/` (ignoré par Git, D-015).

| Dossier | Nature | Versionné | Qui le produit | Usage autorisé | Interdit en production |
|---|---|---|---|---|---|
| `sample/` | **synthétique**, fixe (Mission 001) | oui | `scripts/generate_sample_data.py` | démo CLI, goldens LLM, tests historiques | oui |
| `scenarios/` | **définitions** de scénarios (JSON), aucune ligne de données | oui | `scripts/generate_data.py --export-scenarios data/scenarios` | vérité terrain des tests (`tests/test_synthetic_generator.py` vérifie la synchronisation avec le code) | sans objet |
| `synthetic/` | **synthétique**, généré à la demande | **non** (sauf README) | `scripts/generate_data.py` | tests manuels, démos marquées SYNTHÉTIQUE | oui |
| `benchmark/` | **synthétique**, gros volumes (jusqu'à 1 M de commandes, ~390 Mo) | **non** (sauf README) | `benchmarks/run_benchmark.py` | mesures de performance | oui |
| `external/` | **externe**, téléchargé | **non** (sauf README) | procédure de `research/datasets.md` | recherche et benchmark, selon la licence (`research/licensing_matrix.md`) | oui |
| `uploads/` | **fichiers importés** par un opérateur | **non** | CLI / service | analyse du client concerné uniquement | sans objet (données client) |

## Données dérivées et fixtures de validation

- **Dérivées :** tout fichier transformé depuis une source externe ou réelle doit porter un manifeste :
  - source et licence ;
  - SHA-256 de l'original et du dérivé ;
  - transformations et caractère réversible.
  Les dérivés de données réelles restent hors dépôt (OH5 : `~/mervio-validation/processed/`).
- **Fixtures de validation versionnées :** uniquement `sample/` (synthétique) et les CSV écrits par les tests dans des
  répertoires temporaires. Aucune fixture issue de données réelles.

## Jeux synthétiques Mervio (`mervio.synthetic`)

Chaque jeu généré contient :
- `shopify_orders.csv`, `shopify_products.csv`, `stripe_transactions.csv`, `google_ads.csv` : **lus par le moteur
  actuel** ;
- `customers.csv`, `sessions_daily.csv`, `inventory_daily.csv`, `shipments.csv` : tables canoniques non encore
  ingérées ;
- `manifest.json` :
  - `synthetic: true`, `not_for_production: true` ;
  - configuration et graine, version du générateur et du schéma ;
  - scénario et vérité terrain ;
  - SHA-256 et nombre de lignes par fichier.

Emails clients : `cNNNNNNN@customers.synthetic.invalid` (TLD réservé, aucune personne réelle).

```bash
python scripts/generate_data.py --scenario healthy_store --orders 10000 --days 365 --seed 42 --validate
python scripts/generate_data.py --scenario refund_spike --orders 20000 --days 126 --seed 3 --evaluate
```

## Jeux externes

Aucun jeu externe n'est versionné. Seul candidat de benchmark retenu : **UCI Online Retail II** (CC BY 4.0,
attribution obligatoire), téléchargé hors dépôt avec empreinte SHA-256 (`research/datasets.md`). Olist et
RetailRocket (non commerciaux), REES46, theLook et l'échantillon GA4 (licence non établie) ne doivent pas être
importés.
