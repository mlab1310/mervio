# REPORT CONTRACT 2.0 — contrat de sortie du moteur (Mission 004.5)

Le rapport JSON produit par `mervio.analytics` est le **contrat de sortie** du moteur. Il doit
pouvoir alimenter un tableau de bord, un PDF, un e-mail ou un contexte LLM **sans transformation
métier**. Ce document est la référence de sa forme ; sa définition formelle est
[`schemas/report/2.0.json`](../schemas/report/2.0.json).

> **Pourquoi ce document et non un `ADR_*.md`.** Le dépôt n'a pas de convention ADR : les
> arbitrages sont journalisés dans [`DECISIONS.md`](DECISIONS.md) et chaque **contrat** a son
> document de référence nommé — [`LLM_CONTRACT.md`](LLM_CONTRACT.md),
> [`DATA_MODEL.md`](DATA_MODEL.md). Celui-ci suit cette convention. Les décisions qui le
> fondent sont **D-091 à D-097**, plus AN-01, AN-02, AN-05, AN-14, AN-15, AN-17, AN-18 et AN-19.

## Versions

Le contrat décrit la **forme** du rapport ; `ENGINE_VERSION` décrit le **calcul** qui le remplit.
Les deux sont distincts et bougent séparément. Matrice complète : [`VERSIONS.md`](VERSIONS.md).

| | Valeur | Propriétaire |
|---|---|---|
| `_meta.report_contract` | `2.0` | `config.REPORT_CONTRACT` (D-091) |
| `_meta.engine_version` | `0.2.0` | `config.ENGINE_VERSION` (D-097) |
| schéma | `schemas/report/2.0.json` | le chemin **porte** la version |

## Les 21 clés de premier niveau

Obligatoires et **fermées** : aucune clé supplémentaire n'est admise. Dans l'ordre de lecture.

| Clé | Contenu |
|---|---|
| `_meta` | version de contrat, moteur, horloge, devise, grain, marqueurs de livraison |
| `business_health_score` | score, interprétation, **les six** dimensions (exclues comprises) |
| `period` | période analysée et période précédente, en dates civiles locales |
| `executive_summary` | résumé ; un rapport périmé le dit en tête (AN-05) |
| `critical_issues` · `warnings` · `setup` · `opportunities` | les constats, **une liste par catégorie**, dans l'ordre ratifié (AN-18 / D-084) |
| `kpis` | les dix-sept KPI, tous publiés |
| `profitability` | profit de contribution, profit partiel, composantes de coût |
| `customers` | agrégats et rangs — **aucun identifiant** (AN-17) |
| `products` · `marketing` | performance par article et par campagne |
| `time_series` · `comparisons` | huit grandeurs suivies, séries et comparaisons périodiques |
| `anomalies` | écarts jugés anormaux, avec le critère **appliqué** (D-007) |
| `root_causes` | au plus une décomposition du CA (AN-01 / AN-02) |
| `recommendations` | une entrée par constat porteur d'une recommandation |
| `data_freshness` | fraîcheur de la donnée (AN-05 / D-079) |
| `data_quality` | sources, comptages, champs suivis, signaux |
| `limitations` | limites du rapport |

**Il n'existe ni clé `findings`, ni clé `evidence` à la racine** : les constats vivent dans les
quatre sections de catégorie, les preuves dans l'objet qu'elles soutiennent (AN-15 / D-082).

## Propriétés verrouillées

### `_meta` appartient au moteur (D-093)
`assemble_report` publie les onze clés, dans un ordre fixe. La couche application écrit des
**valeurs** (`analysis_label`, `dataset_is_synthetic`), jamais des clés. `dataset_is_synthetic`
existe aussi quand elle vaut `false` ; `analysis_label` vaut `""` quand personne n'en fournit.

### Ordre total de tout tableau publié (D-092)
`products` est trié par `(-revenue, sku)`, `marketing.campaigns` par `(-spend, campaign_id)`, les
contributeurs par leur clé totale (F-01). `sku` et `campaign_id` étant uniques, aucune égalité ne
subsiste : **les octets du rapport ne dépendent pas de l'ordre d'insertion**.

### Barèmes structurés (D-094)
`business_health_score.dimensions[].thresholds` vaut `[[seuil, score], ...]`, ou `null` pour la
seule dimension sans échelle (`data_quality`). Plus aucune valeur structurée n'est publiée comme
texte — une garde générale refuse toute chaîne qui ressemble à un `repr()` Python.

### Absence ≠ zéro
Un coût inconnu reste `null`. Un CAC sans donnée publicitaire vaut `null`, jamais `0.00`. Un
facteur **non observé** (D-073) a tous ses champs chiffrés nuls et **nomme la source qui
manque** : `contribution: 0.0` affirmerait une absence d'effet.

### Preuve structurée (AN-15 / D-082)
Chaque preuve est un fait typé — `fact_id`, `message_key`, `metric_key`, `source`,
`observability`, `period`, `comparison`, `unit`, valeurs et deltas — jamais une phrase. Un fait
**mesuré** porte sa mesure et son unité ; un fait **non mesuré** a toutes ses valeurs nulles.
Chaque constat porte un `finding_id` **stable** : `{type}` ou `{type}:{scope}`.

### Catalogue de messages (AN-19 / D-085)
Un constat ne porte plus de prose : il désigne un message du catalogue
(`analytics/messages.py`) et fournit ses paramètres. Forme **plate et additive** :
`<nom>` (texte rendu), `<nom>_key` (identité contractuelle), `<nom>_params` (valeurs mesurées).
Le texte est **dérivé** : il ne peut pas contredire sa clé. Les clés `evidence.*` restent des
identifiants purs, **sans** entrée de catalogue (D-2) : une preuve se rend depuis ses champs
typés.

### Confiance qualitative (AN-14 / D-081 option C)
`low` / `medium` / `high`, dérivés d'un **décompte entier** de quatre critères, tous exposés —
`not_applicable` compris : un critère sans objet est déclaré, pas omis. Aucun coefficient.

### Aucun identifiant client (AN-17 / D-056)
`customers.top_customers[]` ne porte que `rank`, `orders`, `revenue_share`. Le rapport publiait
`customer_id = "cust_" + sha256(customer_ref)[:12]` — déterministe, **recalculable** par qui
détient la clé d'identité, stable dans le temps, et inscrit dans une table que
`reports_forbid_update` interdit de modifier : le pseudonyme d'un client effacé y survivait
indéfiniment. Le CA absolu reste dérivable (`revenue_share × kpis.revenue.value`).

### Un seul champ non déterministe (D-058)
`_meta.generated_at` est l'unique valeur qui change d'une exécution à l'autre, et la **seule**
exclue de la comparaison octet pour octet CLI ↔ persisté. Mesuré : deux exécutions successives ne
diffèrent que par ce champ, et le rapport est identique sous `PYTHONHASHSEED` 0 / 1 / 42, dans
cinq ordres d'insertion différents.

## Le schéma

| | |
|---|---|
| Fichier | `schemas/report/2.0.json` — 53 `$defs`, 70 conditionnels |
| Dialecte | **JSON Schema Draft 2020-12** |
| `$id` | **`urn:mervio:schema:report:2.0`** |
| Statut | **artefact de test**, pas de runtime |

**Pourquoi une URN et non une URL.** Le dépôt ne possède aucun domaine où publier ce schéma, et
annoncer une adresse qui ne résout pas serait une fausse promesse de résolution. Le fichier est
la référence, versionné avec le moteur qui le produit.

**Ce que le schéma ne fait pas.** Aucune contrainte numérique non démontrée : pas de borne
`[0, 1]` sur les contributions — une décomposition dégénérée publie des parts hors de cet
intervalle (mesuré : `+1.5899` compensé par `-0.5899`), et c'est précisément ce que le critère
`share_interpretable` sert à signaler — et pas d'échelle `0-100` sur les notes, qui viennent d'un
barème **configurable**. Les patterns ne sont posés que là où la forme vient d'une fabrique
unique : labels de période, dates ISO, horodatage UTC.

**Il ne garde aucun chemin de lecture.** Aucun module de `src/` ne connaît `jsonschema` ni le
fichier de schéma, et `jsonschema` n'est pas une dépendance runtime (voir
[`ARCHITECTURE.md`](ARCHITECTURE.md)). Un rapport 1.x déjà persisté ne peut donc **pas** être
rejeté à la lecture par le contrat 2.0 — une garde statique le vérifie.

## 1.x → 2.0

**Aucun convertisseur n'existe et aucun n'est prévu.** Les rapports 1.x ne sont pas réécrits :
`reports` est immuable (`reports_forbid_update`, révision 0003). Ils restent **lisibles** par les
deux consommateurs du rapport, et c'est la seule garantie offerte. Correspondance champ par
champ : [`REPORT_CONTRACT_1X_TO_2_0.md`](REPORT_CONTRACT_1X_TO_2_0.md).

## Couverture

| Suite | Objet |
|---|---|
| `tests/test_report_schema_2_0.py` | le schéma, 147 tests et 44 sondes négatives |
| `tests/test_report_contract_2_0.py` | K-1, K-2, K-4, K-5, K-6, K-11 |
| `tests/test_contract_2_0_integration.py` | invariants non exprimables en JSON Schema |
| `tests/test_report_privacy.py` | AN-17, de bout en bout |
| `tests/test_determinism_f01.py` | déterminisme multi-graine |
| `tests/persistence/test_contract_2_0_persisted.py` | CLI ↔ persisté, les deux côtés au contrat |
