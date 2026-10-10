# VERSIONS — matrice et propriétaires

Huit versions circulent dans Mervio. Chacune a **un** propriétaire, et une seule raison de
bouger. Les confondre produirait exactement ce que chaque version sert à éviter : un changement
de forme invisible pour celui qui lit l'artefact.

| Version | Valeur | Propriétaire | Couvre | Bouge quand |
|---|---|---|---|---|
| `ENGINE_VERSION` | **`0.2.0`** | [`config.py`](../src/mervio/config.py) | le **calcul** du rapport | une formule, un seuil par défaut ou une clé de rapport change (D-097) |
| `REPORT_CONTRACT` | **`2.0`** | [`config.py`](../src/mervio/config.py) | la **forme** du rapport | une clé de premier niveau, un type, une nullabilité ou un vocabulaire fermé change (D-091) |
| `CONTRACT_VERSION` | **`2.0`** | [`llm/contract.py`](../src/mervio/llm/contract.py) | la forme du **contexte LLM** | le contexte gagne, perd ou retype une section (D-095) |
| `PROMPT_VERSION` | `1.0` | [`llm/contract.py`](../src/mervio/llm/contract.py) | instructions système + enveloppe de données | `SYSTEM_CONTRACT` ou la structure du prompt change |
| `RESPONSE_SCHEMA_VERSION` | `1.0` | [`llm/contract.py`](../src/mervio/llm/contract.py) | la réponse attendue du modèle | le schéma de réponse ou une règle d'ancrage change |
| `NORMALIZATION_VERSION` | `mervio-normalization/2` | [`persistence/snapshots.py`](../src/mervio/persistence/snapshots.py) | la normalisation CSV → `Dataset` | la normalisation change — un instantané ancien n'est alors plus comparable |
| `CSV_SCHEMA_VERSION` | `shopify_orders_csv/1,shopify_products_csv/1,stripe_csv/1,google_ads_csv/1` | [`persistence/snapshots.py`](../src/mervio/persistence/snapshots.py) | le schéma attendu de chaque export | un connecteur lit une colonne de plus ou autrement |
| `QUALITY_DOCUMENT_VERSION` | `1` | [`persistence/codec.py`](../src/mervio/persistence/codec.py) | le document `data_quality.json` livré | ce document change de forme |

**Non concernées.** `GENERATOR_VERSION` et `SCHEMA_VERSION` de
[`synthetic/generator.py`](../src/mervio/synthetic/generator.py) versionnent le **générateur de
données de test** et son manifeste, pas le moteur : `GENERATOR_VERSION` vaut encore `0.1.0`, même
littéral qu'`ENGINE_VERSION` avant 004.5, autre contrat.

## Règles

1. **`ENGINE_VERSION` suit la version de distribution.** `mervio.__version__` **est**
   `ENGINE_VERSION`, et `pyproject.toml` porte la même valeur. Les désynchroniser ferait livrer
   une roue annonçant une autre version que son moteur. Un test le refuse.
2. **`REPORT_CONTRACT` gouverne trois endroits** : la constante, le **chemin** du fichier de
   schéma (`schemas/report/2.0.json`) et la valeur que ce schéma impose (`const`). Un test
   vérifie les trois ensemble.
3. **`CONTRACT_VERSION` bouge avec `REPORT_CONTRACT`** quand le contexte suit le rapport
   (D-095) — mais `PROMPT_VERSION` et `RESPONSE_SCHEMA_VERSION` ne sont **pas** entraînés.
   `llm/prompt.build_prompt` refuse un contexte dont la version ne correspond pas à la constante
   du module : une forme nouvelle sous une version ancienne rendrait intraçable une explication
   déjà produite.
4. **Une divergence accidentelle échoue en un seul endroit.**
   `tests/test_contract_2_0_integration.py` §8 épingle les huit valeurs, une par test paramétré,
   pour que l'échec **nomme** la version qui a bougé.

## Historique des ruptures

| Mission | Rupture |
|---|---|
| 001 | `ENGINE_VERSION 0.1.0` — contrat de rapport implicite, non versionné |
| 002 | contexte LLM `1.0`, prompt `1.0`, réponse `1.0` |
| 004.1 | `NORMALIZATION_VERSION`, `CSV_SCHEMA_VERSION`, `QUALITY_DOCUMENT_VERSION` |
| 004.5 | `REPORT_CONTRACT 2.0` (nouveau), `CONTRACT_VERSION 2.0`, `ENGINE_VERSION 0.2.0` |
