# LLM CONTRACT — couche d'interprétation (Mission 002)

Le LLM **interprète** un rapport déjà calculé. Il n'est ni le moteur ni la
source de vérité : il ne calcule aucun KPI, ne modifie ni le rapport ni le
Business Health Score, et son explication est un artefact séparé.

```
Analytics Engine ─► rapport déterministe ─► build_llm_context()  contexte 1.0 borné
                                          ─► build_request()      2 canaux séparés
                                          ─► call_provider()      délai + tentatives bornés
                                          ─► validate_response()  schéma fermé + ancrage
                                          ─► BusinessExplanation  (ou explication indisponible)
```

Point d'entrée : `mervio.llm.explain_report(report, provider, config)`. Il ne
lève jamais ; `explain_analysis(result, ...)` fait de même pour un
`AnalysisResult`. Aucun appel LLM n'est câblé dans `analyze_dataset()` : le
rapport reste `_meta.llm_used = false`.

## Versions

| Constante | Valeur | Couvre |
|---|---|---|
| `CONTRACT_VERSION` | `1.0` | forme du contexte |
| `PROMPT_VERSION` | `1.0` | instructions système + enveloppe |
| `RESPONSE_SCHEMA_VERSION` | `1.0` | réponse attendue du modèle |

Toute évolution de forme incrémente la version concernée. Les instantanés
`tests/golden/llm_context/*.json` échouent sur tout changement non versionné
(régénération : `MERVIO_UPDATE_GOLDEN=1 python -m pytest tests/test_llm_golden.py`).

## Contexte 1.0 (`build_llm_context`)

| Section | Contenu | Identifiants |
|---|---|---|
| `business_health` | score **recopié**, interprétation, dimensions, exclusions ; `computed_by = analytics_engine` | `health.score`, `health.<dimension>` |
| `profitability` | profit / marge **complets** (null si incomplet), profit partiel, composantes ; `partial_is_not_profit = true` | `profitability` |
| `facts` | KPI disponibles : valeur, unité, période, qualité, formule, sources, notes | `kpi.<metric>` |
| `findings` | constats du moteur (critique / alerte / opportunité), impact **seulement** s'il vient du moteur | `finding.<n>` |
| `evidence` | anomalies, facteurs de cause racine, contributions produit et campagne | `anomaly.<metric>`, `root_cause.<n>.factor.<f>`, `root_cause.<n>.product.<rang>`, `root_cause.<n>.campaign.<rang>` |
| `root_causes` | synthèse de décomposition ; `causality_established = false` | `root_cause.<n>` |
| `hypotheses` | énoncés non prouvés (`status = non_prouvee`) et les éléments qu'ils visent | `hypothesis.<n>` |
| `recommendations` | action, constat source, sévérité, impact du moteur | `recommendation.<n>` |
| `limitations` | limites du rapport | `limitation.<n>` |
| `unavailable_metrics` | KPI à `null` + profit et marge de contribution si incomplets, avec la raison | `unavailable.<metric>` |
| `data_quality` | sources, ratio de champs fiables, statut par champ, incidents **sans leur message** | — |
| `untrusted_content` | chemins des champs qui ressemblent à une instruction | — |
| `truncation` | `total` / `included` par collection | — |

Jamais repris : horodatage, identifiant d'analyse, chemin de fichier,
identifiant client (même pseudonymisé), email, SKU, identifiant de campagne,
message d'incident brut.

### Bornes (`ContextLimits`, valeurs par défaut)

| Borne | Défaut | Borne | Défaut |
|---|---|---|---|
| `max_facts` | 25 | `max_limitations` | 15 |
| `max_findings` | 15 | `max_unavailable_metrics` | 25 |
| `max_evidence` | 30 | `max_contributors` (par analyse) | 5 |
| `max_hypotheses` | 10 | `max_health_dimensions` | 10 |
| `max_recommendations` | 10 | `max_quality_fields` / `max_quality_issues` | 20 / 15 |
| `max_root_causes` | 3 | `max_strings_per_item` | 5 |
| `max_text_length` | 300 | `max_context_bytes` | 64 000 |

Le plafond d'octets est mesuré sur le contexte **complet**. S'il est dépassé,
les sections sont réduites de moitié dans un ordre fixe (preuves, constats,
faits, limites, hypothèses, recommandations, indisponibles, causes), puis les
références orphelines sont retirées. Si rien ne peut plus être réduit :
`LLMContextError`. Résultat identique quel que soit `PYTHONHASHSEED` (testé).

### Rapport malformé

Clé obligatoire absente (`_meta.engine_version`, `period.current`,
`business_health_score`, `kpis`), type inattendu, nombre non fini ou booléen
à la place d'un nombre : `LLMContextError`. Le message ne cite jamais la valeur.

## Données non fiables

Tout texte venant d'une source importée est une **donnée** :

1. **Séparation des canaux** (défense principale). `system_prompt` =
   `SYSTEM_CONTRACT` + format de réponse, constantes du code, jamais formatées
   avec une donnée. `user_prompt` = préambule fixe + une seule enveloppe
   `<<<MERVIO_UNTRUSTED_DATA_BEGIN>>>` … `END` contenant le JSON du contexte.
   La copie de `system_contract` présente dans le contexte n'est jamais relue.
2. **Enveloppe inviolable.** `<` et `>` sont échappés en `\u003c` et `\u003e` :
   aucune valeur ne peut produire la balise de fermeture.
3. **Hygiène des textes** (`safety.sanitize_text`) : suppression des
   caractères invisibles et bidirectionnels, contrôle → espace, masquage
   email / téléphone / carte (Luhn) / IBAN / clé API / jeton / n° de commande,
   troncature bornée avant toute expression régulière.
4. **Signalement** des textes qui ressemblent à une instruction, sans les
   supprimer : la donnée reste une donnée.
5. **Identifiants techniques** (`id`, clés, métriques) réduits à `[a-z0-9_]`.

## Fournisseur

```python
class LLMProvider(ABC):
    name: str
    def generate(self, request: LLMRequest, *, model: str, timeout_seconds: float,
                 max_output_chars: int) -> ProviderResponse: ...
```

Aucune implémentation réelle ni SDK dans cette version. `MockLLMProvider`
(déterministe, hors ligne) sert aux tests et à la démonstration.

`LLMConfig` : `model`, `timeout_seconds` (fini, ]0, 120], défaut 30),
`max_retries` ([0, 3], défaut 2), `retry_backoff_seconds` ([0, 10]),
`max_output_chars` ([1, 200 000]).

`call_provider` :
- délai appliqué par un thread démon : l'appelant reprend la main au plus
  tard à `timeout_seconds`. Le thread d'un fournisseur bloqué n'est pas tué ;
  un fournisseur réseau **doit** appliquer ce délai à sa propre connexion ;
- nouvelle tentative uniquement sur erreur transitoire (`provider_timeout`,
  `provider_unavailable`, `provider_rate_limited`), avec attente
  `backoff × tentative` ; jamais sur authentification, refus, sortie trop
  volumineuse, type invalide ou exception inattendue ;
- une exception inattendue devient `provider_error` sans reprendre son message.

## Réponse 1.0 (`validate_response`)

```json
{"schema_version": "1.0", "summary": "…",
 "facts": [{"statement": "…", "refs": ["kpi.revenue"]}],
 "explanations": [{"statement": "…", "refs": ["finding.1"]}],
 "hypotheses": [{"statement": "…", "refs": ["hypothesis.1"]}],
 "recommendations": [{"action": "…", "priority": "high|medium|low", "refs": ["recommendation.1"]}],
 "limitations": [{"statement": "…", "refs": ["unavailable.roas"]}]}
```

**Structure** : JSON strict (clé dupliquée, `NaN`, texte autour → rejet),
aucun champ inconnu, résumé ≤ 1 200 caractères, énoncé ≤ 400, 1 à 6 `refs`
(0 autorisé pour les limites), au plus 12 / 10 / 8 / 8 / 12 éléments.

**Ancrage** — chaque code rejette la réponse entière :

| Code | Règle |
|---|---|
| `reference_inconnue` | une `ref` n'existe pas dans le contexte |
| `fait_appuye_sur_une_hypothese` | un fait cite une hypothèse |
| `chiffre_non_ancre` | un nombre écrit ne correspond à aucun champ **numérique** du contexte, à la précision où il est écrit (ratio × 100 accepté pour les pourcentages). Les nombres des textes importés ne sont pas citables |
| `metrique_indisponible_chiffree_<m>` | une phrase nomme une métrique indisponible et contient un nombre ; profit partiel toléré seulement s'il est qualifié « partiel » |
| `score_de_sante_altere` | une phrase sur le score cite une note autre que celle du moteur ou d'une dimension |
| `causalite_affirmee` | causalité certaine (« a causé », « est la cause », « because of », « proven »…), sauf négation |
| `devise_incoherente` | symbole ou code devise différent de celui du rapport |
| `donnee_sensible` | email, téléphone, carte, IBAN, clé, jeton |
| `fuite_des_instructions` | reprise des instructions système ou des balises |

Les contrôles lexicaux sont une défense en profondeur : ils bloquent les
dérives détectables, pas toute formulation possible. Le score porté par
`BusinessExplanation` est recopié du contexte, jamais lu dans la réponse.

## Service

`explain_report` retourne `ExplanationResult(status, explanation, error_code,
issues, metadata)`. Codes : `context_invalid`, `context_error`, codes
fournisseur, `invalid_response` (non retentée), `validation_error`,
`analysis_not_completed`. Le rapport n'est jamais modifié.

Métadonnées et journaux : fournisseur, modèle, versions, empreinte SHA-256 de
la requête, tentatives, latence, consommation si fournie. **Jamais** le prompt,
la réponse ou une valeur métier.

## Ajouter un fournisseur réel

1. Implémenter `LLMProvider.generate` dans un nouveau module de `llm/`.
2. Lire la clé depuis l'environnement dans ce module uniquement ; ne jamais la
   journaliser ni l'inclure dans une exception.
3. Mapper les erreurs HTTP sur `ProviderTimeoutError`, `ProviderUnavailableError`,
   `ProviderRateLimitError` (transitoires), `ProviderAuthenticationError`,
   `ProviderRefusalError` (permanentes).
4. Appliquer `timeout_seconds` à la connexion et `max_output_chars` à la réponse.
5. Toute dépendance nouvelle est une décision à consigner (D-004).
