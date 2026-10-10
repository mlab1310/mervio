# Mission 004.5 — Handoff (document de départ de Mission 004.6)

> **État au 09/10/2026 — à lire en premier.**
> **004.5 = IMPLÉMENTÉE ET VALIDÉE** · **3594 / 3594 tests GREEN — 0 échec, 0 ignoré** ·
> `REPORT_CONTRACT 2.0` · `ENGINE_VERSION 0.2.0` · `CONTRACT_VERSION 2.0` ·
> tête de migration **`0017`, inchangée** · **arbre de travail NON COMMITÉ.**

> **Objet.** Ce document permet de reprendre Mervio **sans aucun contexte conversationnel**. Il
> couvre la mission 004.5 — *Report Contract 2.0 & Analytics Correctness* — et énonce les
> résiduels **sans les masquer**. Les arbitrages sont journalisés dans
> [`DECISIONS.md`](DECISIONS.md) ; la feuille de route de référence est
> [`MERVIO_FINAL_ROADMAP.md`](MERVIO_FINAL_ROADMAP.md) ; le document de départ précédent est
> [`MISSION_004_4_HANDOFF.md`](MISSION_004_4_HANDOFF.md).

## 1. Baseline

| | |
|---|---|
| Dépôt | `/Users/mlab/Projects/mervio` |
| Branche | `mission-004.4` |
| Dernier commit | `f3717bda7d9cc488b502edbf0410ad87e773b1aa` — `fix(analytics): tighten AN-19 message contract` |
| Arbre de travail | **non commité** — phases K-*, D1, bump de version, schéma, tests et goldens régénérés |
| Tête de migration | **`0017_purge_recovery_audit`** — **aucune migration en 004.5** |
| Tests | **3594 passés · 0 ignoré · 0 échec** (`MERVIO_EXPECTED_TESTS = 3594`, égalité **exacte** imposée par la CI) |
| CI | à exécuter sur ce travail ; validé en local sur PostgreSQL 17 réel (port 55432) |
| Environnement | Python 3.11.16 · pytest 9.1.1 · jsonschema 4.26.0 (**dev/test uniquement**) |
| Moteur | **`ENGINE_VERSION 0.2.0`** · **`REPORT_CONTRACT 2.0`** · **`CONTRACT_VERSION 2.0`** · `PROMPT_VERSION` et `RESPONSE_SCHEMA_VERSION` inchangés en `1.0` |

**Le fait central de 004.5 :** le rapport a désormais un **contrat**. Sa forme est déclarée dans
le rapport lui-même (`_meta.report_contract`), définie formellement par un schéma versionné, et
ses onze défauts d'exactitude analytique sont corrigés. Un consommateur — l'API 004.6, l'interface
004.7, le LLM — construit sur une forme qui ne peut plus changer en silence.

## 2. Architecture du contrat

| Artefact | Rôle |
|---|---|
| [`REPORT_CONTRACT.md`](REPORT_CONTRACT.md) | la référence : 21 clés, propriétés verrouillées, schéma, frontière 1.x |
| [`schemas/report/2.0.json`](../schemas/report/2.0.json) | définition formelle, **Draft 2020-12**, 53 `$defs`, 70 conditionnels, `$id urn:mervio:schema:report:2.0` |
| [`REPORT_CONTRACT_1X_TO_2_0.md`](REPORT_CONTRACT_1X_TO_2_0.md) | correspondance champ par champ — **aucun convertisseur** |
| [`VERSIONS.md`](VERSIONS.md) | les huit versions et leur propriétaire |
| [`LLM_CONTRACT.md`](LLM_CONTRACT.md) | contexte **2.0**, bornes, rétention des preuves |
| [`ANALYTICS_ENGINE.md`](ANALYTICS_ENGINE.md) | méthodes et seuils, mis à jour pour AN-01 → AN-19 |

**Le schéma est un artefact de TEST.** Aucun module de `src/` ne connaît `jsonschema` ni le
fichier ; `jsonschema` est absent de `requirements-runtime.lock`. Conséquence voulue : un rapport
1.x déjà persisté ne peut pas être rejeté à la lecture par le contrat 2.0.

## 3. Ce qui a été corrigé

| ID | Défaut | Commit |
|---|---|---|
| F-01 | ordre des campagnes dépendant de `PYTHONHASHSEED` | `18760db` |
| AN-16 | seuils en dur hors de `config.py` | `84b236a` |
| AN-06 | périodes coupées en UTC strict | `c96cde7` |
| AN-01 | anomalie et décomposition sur deux baselines différentes | `93cf963` |
| AN-02 | « taux de conversion » confondant une baisse organique avec un problème de checkout | `d1101ca` |
| AN-05 | aucune fraîcheur exposée | `cb61c37` |
| AN-14 | confiance numérique à fausse précision | `65409f5` |
| AN-15 | preuves en français préformaté | `dd1bb9f` |
| AN-17 | pseudonyme client dans un artefact non effaçable | `6ec4f37` |
| AN-18 | `profitability_unavailable` en alerte permanente | `2cabd58` |
| AN-19 | textes du moteur hors catalogue | `e8ca47a`, `f3717bd` |
| K-1 … K-11 | contrat 2.0, schéma, ordres totaux, propriété de `_meta`, barèmes structurés | arbre de travail |
| **D1** | les contributions de campagne sortaient du contexte LLM | arbre de travail |

## 4. D1 — rétention des preuves (D-096)

Découvert pendant l'audit des goldens, **avant** toute régénération. `llm/context.py`
concaténait cinq natures de preuve puis tronquait la **tête** à `max_evidence = 30` ; AN-15 ayant
ajouté les faits de constat en tête, les contributions produit et campagne sortaient.

Mesure sur **57 rapports** (6 scénarios moteur + 17 scénarios de vérité terrain à 1 200 / 6 000 /
20 000 commandes) :

| | Avant | Après |
|---|---:|---:|
| rapports tronqués | 28 | **0** |
| rapports perdant **toutes** leurs campagnes | 10 | **0** |
| contributions produit perdues | 29 | **0** |
| `revenue_decline` : campagnes transmises | 0 / 4 | **4 / 4** |
| contexte le plus gros | 33 150 o | 38 461 o (sur 64 000) |

Correction en deux parties, **et il faut les deux** : `max_evidence = 45` (plus petite valeur
démontrée suffisante) **et** répartition du budget entre natures (`context.EVIDENCE_KINDS`).
Relever le plafond seul ne suffit pas — le nombre de preuves suit le nombre de constats, donc les
données. Avec le plafond à 45 mais la coupe de tête rétablie, quatre tests de rétention tombent.

Défaut annexe corrigé : `findings[].evidence_ids` était le seul renvoi que
`_drop_dangling_references` ne nettoyait pas.

## 5. Goldens

Les sept instantanés de `tests/golden/llm_context/` datent de `e8b4e38` (15/09/2026) et
**précèdent la totalité** du travail analytique de 004.5 — `contract_version` était seulement la
première assertion à tomber, pas l'étendue de l'écart.

Régénérés par le **mécanisme canonique** :
`MERVIO_UPDATE_GOLDEN=1 python -m pytest tests/test_llm_golden.py`. Aucune édition manuelle.
Idempotent : une seconde passe produit les mêmes octets.

Audit sémantique ancien → nouveau (rapprochement par `id` sémantique, `type`, ou texte
désaccentué — jamais un diff positionnel) : **552 différences**, réparties en AN-19 (172),
AN-15 (144), AN-02 (83), AN-14 (51), DERIVED (39), D1 (18), AN-18 (18), K-11 (7), AN-05 (7),
AN-01 (7), ENGINE_VERSION (6). **UNKNOWN = 0.**

`minimal_report` est traité à part : c'est la plus petite **entrée** acceptée par la couche LLM
(quatre clés écrites à la main), pas un rapport 2.0. Deux différences seulement, et son
`engine_version` reste `0.1.0` — la fixture le porte en dur.

`test_golden_semantics_revenue_decline` attendait `primary_factor == "conversion_rate"`. L'attente
suit désormais AN-02 **sans s'affaiblir** : facteur primaire `order_volume` nommé, `conversion_rate`
**interdit**, couches A/B vérifiées, les deux angles morts exigés avec leurs valeurs nulles, et
aucune recommandation checkout admise (D-075).

## 6. Tests

| Suite | Tests | Objet |
|---|---:|---|
| `test_report_schema_2_0.py` | 147 | le schéma, dont 44 sondes négatives |
| `test_contract_2_0_integration.py` | 69 | invariants non exprimables en JSON Schema |
| `test_message_catalog.py` | 43 | catalogue AN-19 |
| `test_report_contract_2_0.py` | 21 | K-1, K-2, K-4, K-5, K-6, K-11 |
| `test_report_privacy.py` | 20 | AN-17, de bout en bout |
| `test_synthetic_scenarios.py` | 17 | 17/17 à 20 000 commandes |
| `test_llm_evidence_retention.py` | 16 | **D1** |
| `test_determinism_f01.py` | 15 | déterminisme multi-graine |
| `test_llm_golden.py` | 14 | instantanés du contexte |
| `persistence/test_contract_2_0_persisted.py` | 7 | CLI ↔ persisté, les deux côtés au contrat |

Déterminisme vérifié : octets identiques sur `PYTHONHASHSEED` 0 / 1 / 42 et dans cinq ordres
d'insertion ; `_meta.generated_at` est le **seul** champ qui varie entre deux exécutions.

## 7. Décisions

**D-091** `_meta.report_contract` (K-2) · **D-092** ordre total des tableaux (K-4) ·
**D-093** le moteur possède `_meta` (K-5) · **D-094** barèmes structurés (K-6) ·
**D-095** contexte LLM 2.0 (K-11) · **D-096** rétention des preuves (D1) ·
**D-097** `ENGINE_VERSION 0.2.0`.

**Dette documentaire identifiée, non traitée :** les décisions **D-072 à D-090** sont citées par
le code (`src/mervio/analytics/*.py`) et par la roadmap, mais **absentes du journal**
[`DECISIONS.md`](DECISIONS.md), qui s'arrête à D-071 avant les sept entrées ci-dessus. Les
numéros `D-077`, `D-078`, `D-083`, `D-086`, `D-087`, `D-088` ne sont cités nulle part — statut
inconnu, à ne pas réutiliser sans vérification. Journaliser D-072 → D-090 demande de retrouver
l'arbitrage d'origine : ce n'est pas un travail de rédaction, et il n'a pas été fait ici.

## 8. Résiduels et observations hors périmètre

| Observation | Nature | Décision |
|---|---|---|
| `profitability.components.shipping_cost.source = "non connecte"` | libellé français dans un emplacement d'identifiant ; remonte dans `evidence[].source` du constat `setup` | **épinglé par un test** — le corriger changerait les octets de tout rapport |
| `freshness or {}` dans `assemble_report` | repli inatteignable depuis le pipeline ; `build_report(freshness=None)` produirait un rapport que le contrat refuse | documenté, non corrigé |
| sentinelle `"-"` de `timeseries.compare` | inatteignable via `compare_all` ; c'est pourquoi `comparisons[].previous_period` n'est pas contraint par un motif | documenté, non corrigé |
| A-1 échelle `data_quality`, A-2 `annotate_report`, A-3 fuseau, A-4 `rpds-py` | hors périmètre 004.5 | inchangés |
| 17 `KNOWN_GAP` sur 61 attentes de scénario | angles morts **déclarés** du moteur (trafic organique, cohortes, stock, saisonnalité) | D-076 ; restauration attendue en 004.8 et 004.11 |

## 9. Pour la mission suivante (004.6 — API v1)

1. **Le contrat est stable et formellement défini.** L'API sert le rapport **persisté** : les
   deux côtés valident le schéma, et leurs octets sont identiques à `generated_at` près
   (`persistence/test_contract_2_0_persisted.py`).
2. **Ne pas câbler le schéma dans un chemin de lecture.** Il valide ce que le moteur **produit** ;
   il refuserait un rapport 1.x légitimement persisté.
3. **`findings` et `evidence` ne sont pas des clés de rapport.** Les ressources que l'API
   exposera sous ces noms se construisent depuis les quatre sections de catégorie et depuis les
   preuves portées par chaque objet.
4. **Aucun identifiant client ne peut sortir du rapport** (AN-17) : `rank`, `orders`,
   `revenue_share`, et rien d'autre.
5. **La CI doit être exécutée** sur ce travail — `MERVIO_EXPECTED_TESTS = 3594`, égalité exacte.
