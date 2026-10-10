# REPORT CONTRACT — correspondance 1.x → 2.0 (Mission 004.5)

> **Aucun convertisseur n'existe, et aucun n'est prévu.**
>
> Les rapports 1.x déjà produits **ne sont pas réécrits** : `reports` est immuable
> (`reports_forbid_update`, révision 0003), et une migration de données serait à la fois
> impossible sur une table protégée et inutile — un rapport est l'instantané d'une analyse à une
> date, pas un état courant. Ce document sert à **lire** un rapport 1.x et à comprendre où son
> information se trouve en 2.0. Il ne décrit aucun outil.

Décisions : **D-091 à D-097** ([`DECISIONS.md`](DECISIONS.md)) · contrat :
[`REPORT_CONTRACT.md`](REPORT_CONTRACT.md) · frontière ND-04.

## Ce qui disparaît

| 1.x | 2.0 | Pourquoi |
|---|---|---|
| `customers.top_customers[].customer_id` | **supprimé** | AN-17 / D-056. `"cust_" + sha256(customer_ref)[:12]` était déterministe et **recalculable** par qui détient la clé d'identité, stable dans le temps, et inscrit dans un artefact non effaçable : le pseudonyme d'un client effacé y survivait indéfiniment |
| `customers.top_customers[].revenue` | `revenue_share` | la part porte la même information analytique sans designer personne ; le CA absolu reste dérivable (`revenue_share × kpis.revenue.value`), et `null` quand le CA de période est nul — jamais `0.0` |
| `kpis.conversion_rate` | `kpis.orders_per_paid_click` | AN-02. « commandes **totales** / clics **payants** » n'est pas un taux de conversion : il baisse aussi quand le trafic organique chute. Le nom dit désormais ce qui est mesuré |
| `kpis.clicks` | `kpis.paid_clicks` | AN-02, même raison : seul Google Ads est observé |
| facteur `conversion_rate` | **supprimé** | AN-02 / D-075. Il faisait recommander « auditer le checkout » dès que la métrique baissait, y compris pour une baisse organique sans aucun checkout en cause |
| `findings[].evidence: [str]` | `evidence: [fait structuré]` | AN-15 / D-082. L'API, l'interface et le LLM reparsaient des nombres depuis du français |
| `confidence: float` | `confidence: {level, criteria}` | AN-14 / D-081 option C. Neuf nombres choisis à la main, rendus à quatre décimales comme s'ils mesuraient quelque chose |
| `thresholds: "((-0.25, 0.0), …)"` | `[[seuil, score], …]` ou `null` | D-094 / K-6. Un `repr()` Python publié comme contrat |

## Ce qui apparaît

| 2.0 | Contenu | Source |
|---|---|---|
| `_meta.report_contract` | `"2.0"` | D-091 / K-2 |
| `_meta.dataset_is_synthetic`, `_meta.analysis_label` | **toujours** présentes, y compris à `false` / `""` | D-093 / K-5 |
| `data_freshness` | dernière donnée, date d'analyse, retard, seuil, statut, détail par source | AN-05 / D-079 |
| `setup` | quatrième section de constats : une donnée à **brancher**, pas un problème détecté | AN-18 / D-084 |
| `root_causes[].factors[].layer`, `explains`, `explains_share` | décomposition à deux couches | AN-02 |
| facteurs `organic_traffic`, `site_conversion_rate` | angles morts **déclarés**, `observability: not_observed`, valeurs nulles, source manquante nommée | AN-02 / D-073 |
| `finding_id` | identité stable d'un constat : `{type}` ou `{type}:{scope}` | AN-15 / D-082 |
| `<nom>_key` et `<nom>_params` | sur `fact`, `hypothesis`, `recommendation`, `estimated_impact_basis`, `criterion`, `method`, `observed_fact`, `limitation_keys` | AN-19 / D-085 |
| `root_causes[].baseline_periods`, `baseline_revenue` | la baseline **déclarée**, celle de l'anomalie | AN-01 |

## Ce qui change de valeur sans changer de forme

| Champ | 1.x | 2.0 |
|---|---|---|
| `findings[].severity` / `category` de `profitability_unavailable` | `medium` / `warning` | `info` / `setup` — l'avertissement était permanent sur toute boutique Shopify, donc muet (AN-18) |
| `anomalies[].criterion` | phrase assemblée par concaténation | texte **rendu** depuis le catalogue, avec sa clé et ses seuils en paramètres (AN-19) |
| `root_causes[].observed_change_pct` | variation vs **période précédente** | variation vs **baseline glissante**, la même que l'anomalie (AN-01) |
| textes du moteur | français sans accents, figé | français **accentué**, rendu depuis le catalogue (AN-19) |
| `_meta.engine_version` | `0.1.0` | `0.2.0` (D-097) |

## Lire un rapport 1.x aujourd'hui

Les deux consommateurs du rapport l'acceptent, et c'est la seule garantie offerte :

- `reporting.executive.render_executive_report` rend un 1.x sans lever. Il n'a **jamais** rendu
  `top_customers` : un pseudonyme 1.x n'est donc pas affiché.
- `llm.build_llm_context` construit un contexte depuis un 1.x, version de contrat absente
  comprise. Il ne copie **jamais** le bloc `customers` : un pseudonyme 1.x **n'atteint pas le
  modèle**.
- Le schéma 2.0 **refuse** un rapport 1.x, et c'est voulu : il valide ce que le moteur
  **produit**. Il ne garde aucun chemin de lecture — aucun module de `src/` ne le connaît.

Ce qui est interdit, ce n'est pas de lire un 1.x : c'est d'en **produire** un sous l'étiquette
2.0. Couverture : `tests/test_contract_2_0_integration.py` §9,
`tests/test_report_privacy.py` (frontière 1.x / 2.0).
