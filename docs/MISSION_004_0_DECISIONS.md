# Mission 004.0 — Architecture Decision Records

Format : Contexte · Options · Décision · Pourquoi · Conséquences · Alternatives rejetées.
Ces ADR complètent `docs/DECISIONS.md` (D-001 à D-048), qu'ils ne modifient pas. Toute révision passe par un
nouvel ADR.

---

## ADR-004-001 — Architecture applicative : monolithe modulaire Python + frontend Next.js

**Contexte.**
- Le moteur (Python, stdlib, 656 tests) est validé et doit rester la source des chiffres.
- Mervio doit devenir un SaaS avec API, jobs, connecteurs et interface.
- L'équipe est petite. Aucun besoin de mise à l'échelle indépendante n'est mesuré.

**Options.**
1. Monolithe modulaire Python (API + worker issus du même paquet) et frontend séparé.
2. Microservices.
3. Réécriture en TypeScript autour de Next.js.
4. Frontend appelant la CLI.

**Décision.** Option 1 :
- `mervio.api` (FastAPI) et `mervio.worker`, deux processus d'une même base de code ;
- frontend `web/` Next.js ;
- modules `persistence`, `jobs`, `api`, `connectors` et `actions` ajoutés à côté des paquets existants.

**Pourquoi.**
- Réutilisation directe du moteur, sans sérialisation réseau ni duplication.
- Une seule base, un seul déploiement backend.
- Les frontières de modules sont testables par AST (motif déjà utilisé en 003.2 et 004.0).
- Les produits proches étudiés (OCO, ShopFlow, next-cwv-monitor) tiennent en une application et une base.

**Conséquences.**
- Règles de dépendance à tester : le moteur n'importe ni API, ni base, ni jobs.
- Découpage futur possible le long des mêmes frontières si une mesure l'exige.

**Rejetées.**
- Microservices : coût réseau, versions et observabilité sans besoin prouvé.
- Réécriture TypeScript : perte d'un moteur validé.
- CLI appelée par le web : ni tenant, ni audit, ni reprise.
- Kubernetes : aucune charge ne le justifie.

---

## ADR-004-002 — Persistance : PostgreSQL + stockage objet, sans base analytique dédiée

**Contexte.**
- Il faut conserver organisations, connexions, données canoniques, rapports, jobs et audit.
- Mesure : le rapport pèse ~35 Ko quelle que soit la taille.
- L'analyse de 1 M de commandes prend 45 s et 3,7 Go en mémoire.
- Les exports bruts atteignent 200 Mo par million de commandes.

**Options.**
- PostgreSQL seul ; PostgreSQL + stockage objet ; + DuckDB ; + ClickHouse ; entrepôt cloud.

**Décision.**
- **PostgreSQL** pour le transactionnel, les entités canoniques, les rapports (JSONB immuable), les jobs et l'audit.
- **Stockage objet** pour les exports et charges brutes, immuables et adressés par SHA-256.
- Montants en `numeric(14,2)`, horodatages `timestamptz` UTC.
- Migrations **Alembic** (MIT).
- **Aucune base analytique** ajoutée.

**Pourquoi.**
- Le volume d'une PME reste loin des limites de PostgreSQL.
- Le moteur tient en mémoire jusqu'à 1 M de commandes (mesuré).
- Le stockage objet donne une provenance vérifiable des sources.

**Conséquences.**
- Instantanés canoniques identifiés.
- Rapports rejouables à l'identique.
- **Critère de réévaluation :** DuckDB en processus (MIT) sera étudié si le pic mémoire d'analyse d'un tenant réel
  dépasse le budget du worker ou si l'analyse dépasse 5 min.

**Rejetées.**
- ClickHouse : échelle d'événements, pas de commandes.
- Entrepôt cloud : coût, dépendance, confidentialité.
- Base par service : complexité.
- `float` pour les montants en base : erreurs d'arrondi (défaut observé dans ShopFlow).

---

## ADR-004-003 — Multi-tenancy : base et schéma partagés, clés explicites, RLS en défense en profondeur

**Contexte.** Hiérarchie Plateforme → Organisation → Utilisateurs → Boutiques → Connexions → Données → Analyses →
Constats → Actions. Une fuite entre tenants serait l'incident le plus grave possible.

**Options.** Base par tenant ; schéma par tenant ; schéma partagé avec clés ; schéma partagé avec clés + RLS.

**Décision.** Schéma partagé :
- `organization_id` sur **toutes** les tables métier, `store_id` sur toute donnée de boutique ;
- accès uniquement via des dépôts exigeant un `TenantContext` ;
- **Row Level Security** PostgreSQL alimentée par `SET LOCAL app.organization_id` ;
- `404` sur toute ressource d'un autre tenant.

**Pourquoi.**
- Opérations simples (une migration, un pool).
- Double barrière : code et base.
- Testable automatiquement.

**Conséquences.**
- Tests de tenant croisé obligatoires par route et en SQL.
- Index composites avec `organization_id` en tête.
- Les jobs portent le contexte tenant.

**Rejetées.**
- Base ou schéma par tenant : coût opérationnel sans exigence client.
- Filtrage côté frontend : jamais une garantie.
- Tenant passé en paramètre par le client : falsifiable.

---

## ADR-004-004 — Frontière API : REST `/v1` typé, lecture de rapports persistés, jobs asynchrones

**Contexte.**
- Le frontend doit afficher la chaîne complète sans calculer.
- Une analyse peut durer de 20 ms à 45 s.
- Les écritures (imports, syncs, actions) doivent être sûres en cas de rejeu.

**Options.** Endpoints « analytics » calculés à la demande ; GraphQL ; REST sur ressources persistées + jobs.

**Décision.**
- REST `/v1`, contrats Pydantic v2 → OpenAPI 3.1 → types TypeScript générés.
- Analyses, syncs, explications et exécutions d'action en `202 Accepted` + ressource de suivi.
- Les vues (`series`, `findings`, `anomalies`) sont des **projections d'un rapport immuable**.
- `Idempotency-Key` sur les POST créateurs, erreurs RFC 9457, pagination par curseur, filtres en liste blanche.
- ETag sur les ressources terminées.
- Détail des routes : `MISSION_004_0_ARCHITECTURE.md` §6.

**Pourquoi.**
- Latence de lecture indépendante du volume.
- Déterminisme : ce que voit l'utilisateur est exactement le rapport produit.
- Cache trivial.
- Motifs validés ailleurs : entrées validées, erreurs sans trace (Atlas), contrat d'ingestion versionné
  (next-cwv-monitor).

**Conséquences.**
- Le frontend gère des statuts de job.
- Test de contrat bloquant sur l'OpenAPI.
- Aucune route ne contourne `persistence`.

**Rejetées.**
- Calcul à la demande : 45 s dans une requête et incohérence entre deux lectures.
- GraphQL : surface de requête libre difficile à borner par tenant et par coût, sans bénéfice pour une interface
  guidée.

---

## ADR-004-005 — Abstraction de connecteur : brut → normalisé → validé → canonique

**Contexte.**
- Les connecteurs actuels lisent des CSV.
- Les exports CSV ont des limites prouvées : pas de date de remboursement, remise partielle non prouvée
  (D-045, D-046).
- Futurs connecteurs : Shopify, Stripe, Google Ads, Meta Ads.

**Options.** Connecteurs ad hoc par source ; framework tiers (dlt, Singer) ; interface Mervio minimale.

**Décision.**
- Interface Mervio : `authenticate`, `validate_connection`, `discover_schema`, `fetch`, `paginate`, `normalize`,
  `validate`, `store`, `record_provenance`.
- Sortie **exclusivement** en entités `domain.models`.
- Mêmes signaux qualité que les connecteurs CSV.
- Upsert idempotent par (`store_id`, `source`, `source_record_id`).
- `sync_runs` avec compteurs et avertissements.
- Fuseau et devise lus de la source.
- Ordre de construction : Shopify GraphQL, Stripe, Google Ads, Meta Ads.

**Pourquoi.**
- Le moteur reste agnostique des fournisseurs.
- L'API Shopify expose `Refund.processedAt` et `subtotalPriceSet`, qui ciblent directement les limites de 003.3.
- Patrons OCO éprouvés : idempotence, fuseau canonique, historique des syncs.

**Conséquences.**
- Tests de connecteur sur réponses enregistrées sans réseau.
- Versions de schéma et de normalisation dans la provenance.
- Le CSV reste un connecteur de premier rang (route `imports`).

**Rejetées.**
- `singer-io/tap-shopify` : AGPL-3.0.
- dlt maintenant : dépendance lourde avant 3 connecteurs.
- Mapping de colonnes par alias pour faire passer un format inconnu : D-025.

---

## ADR-004-006 — Frontière LLM : contrat 1.0 existant, fournisseur réel côté worker, confiance jamais auto-déclarée

**Contexte.**
- `src/mervio/llm` fournit un contexte versionné et borné, un validateur d'ancrage, un fournisseur abstrait et un
  service qui ne lève pas.
- Aucun fournisseur réel n'est branché.

**Options.** Chat libre sur les données ; agents avec outils ; explication encadrée du rapport.

**Décision.**
- Explication encadrée : `LLMInput` = contexte 1.0 ; `LLMOutput` = `BusinessExplanation` validée ; `LLMError` = codes
  `provider_*` + rejets du validateur ;
- `LLMConfidence` dérivée du moteur et de la qualité des données, **jamais** du modèle ;
- `LLMProvenance` = métadonnées existantes + `analysis_run_id` + `context_sha256` + codes du validateur ;
- appel uniquement depuis le worker ;
- explication persistée ; budget par organisation.

**Pourquoi.**
- D-032 : le LLM n'est jamais une source de chiffres.
- Le validateur rejette déjà chiffres non ancrés, causalité et profit partiel.
- Un échec LLM n'invalide pas l'analyse (D-037).

**Conséquences.**
- Le produit reste utile sans LLM.
- Toute évolution du contexte incrémente la version (D-033).
- Pas de prompt ni de réponse dans les logs.

**Rejetées.**
- Agents autonomes et exécution d'actions par le LLM.
- Base vectorielle et RAG : le contexte est déjà structuré et borné, aucun corpus à rechercher.
- Confiance déclarée par le modèle.

---

## ADR-004-007 — Provenance au grain de l'exécution

**Contexte.** Chaque résultat doit être traçable jusqu'à sa source, sans lignage colonne par colonne coûteux.

**Options.** Lignage fin par cellule ; aucune provenance ; provenance par exécution + identifiant de source par
enregistrement.

**Décision.**
- **Enregistrements canoniques :** `source`, `source_record_id`, `source_updated_at`, `connection_id`,
  `sync_run_id`, `raw_object_sha256`, `schema_version`, `normalization_version`.
- **Instantanés :** hachage du contenu.
- **Analyses :** `engine_version`, `report_contract`, `config_hash`, `snapshot_id`.
- **Explications :** `context_sha256`, `contract_version`, `prompt_version`, `model`.
- **Chaîne :** Recommandation → Constat → Métrique → Rapport → Instantané → Sync → Objet brut → Source.

**Pourquoi.**
- Suffisant pour l'audit et le rejeu.
- Aligné sur l'existant : `engine_version` dans le rapport, métadonnées LLM, manifestes synthétiques avec SHA-256.

**Conséquences.**
- Route `provenance/{kind}/{id}` auditée.
- Les données synthétiques portent `synthetic: true` jusqu'au rapport.

**Rejetées.**
- Lignage par cellule : coût de stockage et complexité.
- Event sourcing complet : non requis pour ces garanties.

---

## ADR-004-008 — Tâches de fond : file dans PostgreSQL et worker Python

**Contexte.**
- Syncs, analyses (jusqu'à 45 s), explications LLM et exécutions d'action doivent être asynchrones, reprenables et
  tracées par tenant.

**Options.** Celery ou Dramatiq + Redis ou RabbitMQ ; Kafka ; file PostgreSQL (table maison ou procrastinate, MIT).

**Décision.**
- File PostgreSQL : `SELECT … FOR UPDATE SKIP LOCKED`, `jobs` avec contexte tenant, idempotence, essais bornés sur
  erreurs transitoires, état `dead` visible.
- Table `schedules` évaluée par le worker.
- Choix entre table maison et procrastinate au premier jour de la mission jobs, par spike documenté.

**Pourquoi.**
- Aucune infrastructure supplémentaire.
- Transaction commune entre la création du job et l'écriture métier.
- Volume de jobs PME faible.

**Conséquences.**
- Âge de la file et jobs `dead` surveillés.
- Débit limité par la base, largement suffisant au démarrage.

**Rejetées.**
- Redis ou RabbitMQ : composant de plus.
- Dramatiq : LGPL-3.0 et courtier.
- Kafka : aucun flux.
- Cron externe : planification hors audit.

---

## ADR-004-009 — Observabilité : logs JSON corrélés, métriques, instrumentation OpenTelemetry sans infrastructure dédiée

**Contexte.**
- Aujourd'hui : logs texte sur stderr (`logging_config.py`).
- Demain : API, worker, connecteurs, LLM, multi-tenant.

**Options.** Logs texte ; logs structurés + métriques ; pile complète de tracing distribué.

**Décision.**
- Logs JSON avec `request_id`, `organization_id`, `store_id`, `job_id`, `sync_run_id`, `analysis_run_id`,
  `connector`, `duration_ms`, `error_code`.
- Métriques de requêtes, jobs, connecteurs, LLM et qualité.
- SDK OpenTelemetry (Apache-2.0) exporté vers l'outil de l'hébergeur.
- Liste explicite de ce qui n'est jamais journalisé, testée.

**Pourquoi.**
- Deux processus seulement : la corrélation par identifiants suffit.
- La redaction existe déjà côté moteur (D-016, D-017).

**Conséquences.**
- Format de log stable.
- Tableau de bord minimal : 5xx, file, syncs, LLM.

**Rejetées.**
- Infrastructure de tracing auto-hébergée.
- Logs de prompts et réponses.
- Logs de lignes de données.

---

## ADR-004-010 — Frontend : Next.js App Router, rendu serveur, aucun calcul métier

**Contexte.**
- L'interface doit exprimer « quoi → pourquoi → que faire → agir ».
- Aucun dashboard étudié ne le fait.
- Patrons solides observés : Atlas (filtres dans l'URL, validation, skeletons, erreurs isolées, axe-core),
  next-cwv-monitor (drill-down, cas d'usage par dossier).

**Options.** SPA React (Vite) ; Next.js App Router ; gabarit BI (Superset, Metabase embarqué).

**Décision.**
- Next.js App Router + TypeScript strict + shadcn/ui (Radix) + bibliothèque de graphiques accessible.
- Composants serveur pour Briefing et Problèmes.
- Composants client pour filtres, graphiques et approbations.
- État de filtre dans l'URL.
- Types générés depuis l'OpenAPI.
- Budgets Core Web Vitals (`research/performance_benchmark.md` §6).
- Le frontend **affiche** des champs du rapport, ne calcule rien.

**Pourquoi.**
- Données déjà agrégées côté serveur : rendu serveur rapide et sûr (jetons non exposés).
- Écosystème accessible.
- Motifs vérifiés dans le benchmark.

**Conséquences.**
- Deux piles (Python, TypeScript) dans le dépôt.
- CI séparées.
- Tests e2e Playwright + axe.

**Rejetées.**
- BI embarquée : Mervio n'est pas un outil de BI généraliste.
- Calculs dans le navigateur : double source de vérité.
- Composants sous licence propriétaire (Syncfusion, vu dans enterprise-dashboard).

---

## ADR-004-011 — Données synthétiques : générateur Mervio déterministe avec vérité terrain

**Contexte.**
- Aucun jeu public commercialement utilisable ne combine format Shopify, causes connues et volume variable
  (`research/datasets.md`).
- Les générateurs publics n'ont pas d'attente analytique testable.

**Options.** Dépendre d'un générateur externe ; jeux publics ; générateur propre.

**Décision.** `src/mervio/synthetic` (stdlib) :
- graine par espace de noms (idée d'un dépôt MIT, réécrite) ;
- simulation journalière ;
- écriture en flux aux formats des connecteurs existants, plus des tables canoniques non encore ingérées
  (clients, sessions, stock, expéditions) ;
- manifeste `synthetic` / `not_for_production` avec SHA-256 ;
- validation relationnelle relue depuis le disque ;
- 17 scénarios avec vérité terrain et attentes `MATCH` ou `KNOWN_GAP` ;
- évaluateur lisant le rapport public du moteur.

Le moteur n'importe jamais ce paquet (test AST).

**Pourquoi.**
- Tests de cause racine contre une vérité connue.
- Octets reproductibles.
- Aucune licence tierce, aucune PII.
- Volume de 100 à 1 M de commandes.

**Conséquences.**
- Les angles morts du moteur sont documentés et testés : trafic organique, attrition, remise vs panier moyen,
  saisonnalité, stock, expédition.
- Un progrès du moteur fait échouer le `KNOWN_GAP` correspondant, ce qui oblige à mettre le scénario à jour.
- Robustesse vérifiée sur 170 exécutions.

**Rejetées.**
- ablancogcr comme dépendance : schéma marketplace, dépendances lourdes, pas de vérité terrain.
- Olist, RetailRocket : non commerciaux.
- Données « réalistes » présentées comme réelles : interdit.

---

## ADR-004-012 — Performance : mesurer, analyser hors requête, optimiser sur preuve

**Contexte.**
- Benchmark : croissance quasi linéaire ; 3,6 s et 515 Mo à 100 000 commandes ; 45 s et 3,7 Go à 1 M.
- Coûts : ~60 % ingestion ; ~38 % séries et comparaisons, qui reparcourent tout le jeu par période et métrique.

**Options.** Optimiser maintenant ; réécrire en SQL ou DuckDB ; mesurer et fixer des seuils.

**Décision.**
- Aucune optimisation en 004.0.
- Analyse **toujours** en tâche de fond, rapport persisté.
- Seuils de réévaluation :
  1. si `run_analysis` dépasse 60 s ou 4 Go pour un tenant réel, indexer les commandes par période (tri + bissection)
     avant toute autre technique, avec benchmark avant/après et suite de tests inchangée ;
  2. si l'ingestion domine encore, lecture en flux ou chargement SQL ;
  3. DuckDB seulement si 1 et 2 ne suffisent pas.
- Benchmarks dans `benchmarks/`, résultats versionnés, régression > 25 % à 100k signalée en CI nocturne.

**Pourquoi.**
- Le calcul métier est < 1 % du temps.
- Les goulots sont localisés et réversibles.
- Optimiser sans charge réelle risque de déstabiliser un moteur validé.

**Conséquences.**
- Budget de mémoire du worker à fixer (≥ 8 Go pour 1 M de commandes avec marge).
- Les budgets frontend deviennent des critères mesurés en mission frontend.

**Rejetées.**
- Réécriture analytique immédiate.
- Calcul dans les requêtes.
- Mise en cache sans instantané identifié : incohérence possible.
