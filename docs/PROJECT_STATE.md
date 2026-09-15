# PROJECT STATE

**Mis à jour :** 15 septembre 2026 — fin de la Mission 003.3
**Version moteur :** 0.1.0 · **Contrat LLM :** 1.0 · **Tests :** 623 / 623 · **Dépendances runtime :** 0
**Maturité :** tests internes — un registre de ventes réel reconstruit (OH5) validé, sémantique des commandes et remboursements décidée ; aucun export CSV natif de marchand

## Où en est Mervio

Le moteur analytique (Mission 001) est désormais **utilisable par un humain**.
Un opérateur peut prendre quatre CSV, les valider, lancer l'analyse et repartir
avec trois livrables, sans écrire une ligne de Python.

```
IMPORT → VALIDATION → NORMALISATION → ANALYTICS → KPI → PROFITABILITÉ
      → ANOMALIES → ROOT CAUSE → BUSINESS HEALTH → INSIGHTS → LIVRABLES
```

Trois commandes : `validate`, `analyze`, `demo`.

## Build status

| Couche | Statut |
|---|---|
| Domaine (`domain/`) | 🟢 |
| Ingestion CSV (4 sources) | 🟢 |
| Détection de source, séparateur, encodage | 🟢 |
| Validation de fichier | 🟢 |
| Détection de données sensibles | 🟢 |
| Isolation des fichiers importés | 🟢 |
| Service applicatif (`analyze_dataset`) | 🟢 |
| Analytics Engine (KPI → Health Score) | 🟢 |
| Rapports : JSON, TXT, data_quality | 🟢 |
| CLI : analyze / validate / demo | 🟢 |
| Architecture prête pour une API | 🟢 |
| Contexte LLM versionné et borné | 🟢 (contrat 1.0) |
| Abstraction fournisseur, délai et tentatives bornés | 🟢 |
| Validation et ancrage de la réponse LLM | 🟢 |
| Fournisseur LLM réel (OpenAI, Claude…) | 🔴 (non branché, aucun SDK) |
| Rapport PDF | 🔴 |
| FastAPI, PostgreSQL, auth, multi-tenant, frontend, billing | 🔴 (volontaire) |
| CI/CD | 🔴 |

## Ce que la Mission 001.5 a ajouté

- **Couche applicative** : `AnalysisRequest` → `analyze_dataset()` →
  `AnalysisResult`. C'est exactement ce qu'un handler FastAPI appellera.
  Le service ne lève jamais : il rapporte un statut et un motif.
- **Validation réelle** : la validation exécute le vrai connecteur. Statut,
  lignes lues / acceptées / rejetées, période, devise, séparateur, incidents.
- **Robustesse des exports réels** : séparateurs `,` `;` tab `|` détectés,
  encodages UTF-8 / cp1252 / latin-1, ligne corrompue rejetée sans faire
  échouer le fichier.
- **Sécurité** : détection de cartes (Luhn), IBAN, clés API et colonnes
  suspectes — colonne et nombre d'occurrences uniquement, jamais la valeur.
  Messages d'erreur expurgés. `data/uploads/` isolé et ignoré par Git.
- **Incohérence de devise** traitée comme une erreur, sans conversion silencieuse.
- **Pseudonymisation** des identifiants clients dans les livrables (D-023) :
  `report.json` exposait les emails en clair, découvert en vérifiant les sorties.
- **Rapport dirigeant** en 15 sections, dont Revenue Analysis et une section
  de traçabilité formule → sources pour chaque indicateur.
- **Réorganisation** `domain/ · ingestion/ · analytics/ · application/ ·
  reporting/ · cli/`, sans modifier une seule règle de calcul.

## Ce qui n'a pas changé

Aucune formule, aucun seuil, aucune décision de la Mission 001 n'a été touché.
Les 132 tests d'origine passent toujours ; le seul modifié est le test CLI,
`--out` désignant désormais un répertoire (D-021).

## Mission 001.6 — test externe de robustesse

Dataset Kaggle `shopify_sales_dataset_ml_eda.csv` reçu et analysé :
**60 000 lignes, 17 colonnes, 2023-01-01 → 2025-06-18, 0 valeur manquante,
0 doublon, aucune devise.** Granularité : une ligne = une commande, un produit
par commande.

**Résultat : format non supporté, et c'est un succès du test.** Zéro colonne en
commun avec un export Shopify natif ; `validate` et `analyze` ont refusé sans
produire le moindre chiffre. Le connecteur Shopify et l'Analytics Engine sont
inchangés.

Vérifié empiriquement sur les 60 000 lignes : `profit = revenue − shipping_cost`
(100 %, écart max 0,0000 €). Cette colonne ignore COGS, frais de paiement,
publicité et remboursements — la mapper afficherait une marge de **98,64 %** et
un score de profitabilité de **100/100**. Elle reste interdite.

Quatre bugs corrigés dans l'**inspecteur** (aucun dans le moteur) : cardinalité,
valeurs manquantes et granularité étaient calculées sur un échantillon de 2 000
lignes puis présentées comme portant sur le fichier entier ; `customer_id`
n'était signalé comme personnel que s'il était textuel. Plus un message CLI
trompeur. Treize tests ajoutés.

Détail : `docs/REAL_DATA_COMPATIBILITY_001_6.md`.

## Mission 002 — couche d'interprétation LLM

Baseline : commit `6aa2112`, 222 tests. Le moteur, ses formules, ses seuils et
le Business Health Score sont inchangés ; tout est dans `src/mervio/llm/`.

- `build_llm_context()` produit le contrat 1.0 : faits, constats, preuves,
  hypothèses, recommandations, limites et métriques indisponibles séparés,
  identifiants stables, chaque collection bornée, plafond de 64 Ko mesuré sur
  le contexte complet, sortie identique quel que soit `PYTHONHASHSEED`.
- Textes importés traités comme non fiables : canal système constant,
  enveloppe de données inviolable, masquage PII et secrets, signalement.
- `LLMProvider` + `MockLLMProvider` ; délai fini, tentatives bornées sur
  erreurs transitoires uniquement.
- `validate_response()` : schéma fermé et ancrage (références, chiffres issus
  des seuls champs numériques, métriques indisponibles, score, causalité,
  devise, PII, fuite d'instructions).
- `explain_report()` ne lève pas : un échec LLM rend l'explication
  indisponible, le rapport reste valide et intact.
- 270 tests ajoutés, dont 7 instantanés de scénarios produits par le vrai
  pipeline et une suite adversariale.

Détail : `docs/LLM_CONTRACT.md`. Aucun appel à une API réelle n'a été fait.

## Mission 003 — validation sur données réelles

**Aucun export marchand réel n'était disponible.** Seul le dataset externe
Kaggle (CSV + XLSX) a pu être testé ; il reste non supporté (D-028).

- Harnais `scripts/validate_real_export.py` : référence indépendante,
  rapprochement des KPI, sondes sémantiques, contrôle du contexte LLM,
  sortie sans aucune valeur brute. Prêt pour le premier export réel.
- Corrigé : fichiers non CSV lus comme du texte (D-039) ; devise « EUR »
  inventée quand l'export n'en déclare aucune (D-038).
- Risques ouverts sur le CA d'un vrai marchand : `Subtotal` peut-être déjà
  net de remise (D-040), commandes annulées ou impayées comptées dans le CA.
- Formats réels Stripe et Google Ads non vérifiés.

Détail : `docs/REAL_DATA_VALIDATION_003.md`.

## Missions 003.1 et 003.2 — registre de ventes réel

Source : pièce OH5 (TTAB 92078800), registre de ventes réel déposé
publiquement, reconstruit depuis le PDF sans donnée personnelle (hors dépôt).
106 commandes, 119 lignes, USD, 2018-01 → 2020-10. Ce n'est pas l'export CSV
d'origine.

- 003.1 : `NO-GO`. Remises soustraites deux fois (−45,4 % de CA sur 12 mois,
  mois négatifs, fausses anomalies), dates tableur rejetées.
- 003.2 : remise corrigée (D-041), dates à barres sans devinette (D-042),
  CA négatif et commandes à sémantique non établie signalés (D-043). Sur le
  fichier reconstruit d'origine : 119/119 lignes acceptées, CA net, commandes,
  unités, remboursements et les 36 mois de série rapprochés du calcul
  indépendant ; anomalies 3 → 1.
- Toujours ouverts : traitement des annulations, brouillons et montants nuls ;
  base des remboursements ; remise partielle non observée ; export CSV natif,
  Stripe et Google Ads réels.

Détail : `~/mervio-validation/reports/` (hors dépôt).

## Mission 003.3 — export natif et sémantique des commandes

- **Gate 0 :** aucun export CSV natif Shopify public de provenance vérifiable
  (GitHub, Sourcegraph, Hugging Face, Zenodo, Figshare, Kaggle, CourtListener,
  TTAB, communauté Shopify). `PARTIAL-DISCOUNT EVIDENCE NOT AVAILABLE` ;
  risque résiduel accepté ; les contrôles arithmétiques sont partiels et le
  disent (D-046). Rien n'a été synthétisé.
- **Commandes (D-044) :** toute commande de l'export compte (annulées, non
  encaissées, brouillons convertis, montants nuls), sur le modèle des rapports
  Shopify sans prétendre les reproduire. Sur OH5, les annulées « payées »
  valent 0,00 et les annulées porteuses d'argent sont remboursées.
- **Remboursements (D-045) :** montant `Refunded Amount` (port et taxes
  compris), daté à la commande (cohorte), taux = remboursements / Σ `Total`.
  Nouveaux signaux `missing_order_total`, `refund_exceeds_order_total`.
- OH5 revalidé : 12/12 rapprochements PASS, plus aucun bloquant sémantique au
  harnais (seule la profitabilité reste indisponible), score 52, 1 anomalie.
- 9 tests ajoutés ; 6 instantanés golden régénérés (formule, notes, 2 valeurs
  de `refund_rate`), forme du contexte inchangée.
- **Audit de ratification, trois corrections :** profit partiel sur la part
  produit des remboursements, jamais port et taxes compris (D-047) ; libellé
  « CA avant ajustements » jusque dans le contexte LLM, sans prétendre
  reproduire les *net sales* Shopify (D-048) ; contrôle du Subtotal déclaré
  partiel, commandes non vérifiables signalées, CA `incomplete` en cas de
  contradiction (D-046). 13 tests ajoutés.

Détail : `~/mervio-validation/reports/MISSION-003.3-*.md` (hors dépôt).

## Limites assumées

- **Un seul registre réel, reconstruit depuis un PDF.** OH5 (106 commandes, un
  marchand) ne représente pas les marchands Shopify ; aucun export CSV natif,
  aucune remise partielle, aucun email client n'a encore été analysé.
  Procédure dans `docs/REAL_DATA_TEST.md`.
- **CA avant ajustements.** Retours, annulations et modifications ne le
  réduisent pas ; Mervio ne cherche pas à reproduire les *net sales* Shopify,
  qui peuvent différer.
- **Contrôle du Subtotal partiel** : certaines erreurs de convention restent
  indétectables (D-046).
- **Remboursements datés à la commande**, jamais au jour du remboursement.
- **Fuseau non configuré** : périodes découpées en UTC.
- Dépense publicitaire = Google Ads uniquement.
- Trafic approximé par les clics payants.
- Coût de transport réel indisponible depuis Shopify.
- Une seule devise par analyse ; aucune conversion.
- Taux de réachat intra-période. Pas de saisonnalité.

## Prochaine étape

1. Obtenir d'un marchand pilote un export CSV **natif** Shopify (avec remises
   partielles, et si possible Stripe et Google Ads) et le passer dans
   `scripts/validate_real_export.py` avant tout chiffre présenté (condition de
   D-046) ; écrire un test de régression pour chaque écart rencontré.
2. Brancher un premier fournisseur réel derrière `LLMProvider` et mesurer le
   taux de réponses rejetées par le validateur sur les scénarios golden.
