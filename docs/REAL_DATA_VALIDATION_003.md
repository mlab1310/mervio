# Mission 003 — Validation sur données réelles

**Date :** 15 septembre 2026 · **Point de départ :** `760edb0` (tag `mission-003-start`), 492 tests

> **Document historique (Mission 003).** Les conventions décrites ici sont
> celles d'avant les Missions 003.2 et 003.3 : le CA n'est plus
> `Subtotal − Discount Amount` (D-041), les fixtures synthétiques ont été
> régénérées avec un Subtotal après remise, et le libellé est « CA avant
> ajustements » (D-048). Aucune ligne de ce document ne valide une remise
> partielle sur données réelles (D-046).

> **Aucun export marchand réel n'était disponible.** Recherche sur la machine
> (Bureau, Documents, Téléchargements, iCloud, volumes montés) : seul le
> dataset Kaggle déjà étudié en 001.6 (CSV + XLSX) est un fichier externe réel.
> Il n'est pas un export Shopify natif. **La validation sur données d'un vrai
> marchand reste ouverte.** Rien dans ce document ne la remplace.

## 1. Ce qui a été testé, et sur quoi

| Données | Nature | Rôle |
|---|---|---|
| `shopify_sales_dataset_ml_eda.csv` | externe réelle, **non marchande** (Kaggle, synthétique à l'origine) | robustesse face à un format inconnu |
| `shopify_sales_dataset_ml_eda.xlsx` | même contenu, classeur Excel | robustesse face à un format non CSV |
| `data/sample/*` | **synthétique** versionné | référence où tout doit concorder |
| export « disposition native » 5 000 et 50 000 commandes | **synthétique**, généré hors dépôt, 72 colonnes de l'export commandes Shopify, lignes multi-articles, notes avec virgules et retours ligne, BOM, commandes annulées / impayées | volumétrie et particularités de format |
| 18 fichiers de cas limites | **synthétiques**, hors dépôt | comportement face aux défauts courants |

Aucun de ces fichiers n'est versionné. Aucune donnée n'a été envoyée à un
service externe ; l'étape LLM utilise uniquement le fournisseur mock local.

## 2. Outil livré : `scripts/validate_real_export.py`

À lancer sur le premier export réel, **avant** tout pilote :

```bash
python scripts/validate_real_export.py --shopify-orders orders_export.csv \
  --products products_export.csv --stripe payments.csv --google-ads ads.csv \
  --out ~/mervio-validation/client_x.json
```

- Inspection et validation par les vrais outils Mervio, puis analyse complète.
- **Référence indépendante** : parseur CSV propre au script, KPI de la période
  recalculés et comparés (CA, commandes, unités, panier moyen, remboursements,
  taux, clients actifs et nouveaux, CA article, variation vs période
  précédente, couverture COGS, frais, dépense, clics, ROAS, CAC).
- **Tolérance** : 0,01 en valeur absolue + 1e-9 relatif. Le projet ne définit
  pas de tolérance de rapprochement de KPI ; celle-ci correspond à l'arrondi du
  rapport (4 décimales pour les KPI, 2 pour les montants). Au-delà, ce n'est
  plus un arrondi.
- **Sondes sémantiques** (section 4) et contrôles du rapport : score recalculé
  depuis ses dimensions, déterminisme sur deux exécutions, refus du profit,
  recommandations reliées à un constat, absence de causalité.
- **Confidentialité** : sortie en agrégats, comptages, noms de colonnes et
  codes. Le script refuse d'émettre si un email, un numéro de commande ou un
  nom lu dans les fichiers se retrouve dans la sortie, et n'écrit dans le dépôt
  que sous `analysis/` (ignoré par Git).
- Classification : `SUPPORTED`, `PARTIALLY_SUPPORTED`, `UNSUPPORTED`, `INVALID`.

## 3. Résultats

### Fichiers externes réels

| Fichier | Classification | Preuve |
|---|---|---|
| Kaggle CSV (60 000 lignes, 17 colonnes, 2023-01-01 → 2025-06-18) | **UNSUPPORTED** | aucune signature reconnue ; connecteur Shopify forcé : colonnes obligatoires manquantes ; analyse non lancée. Identique à 001.6 (D-028) |
| Kaggle XLSX | **INVALID** | refusé comme classeur Excel. **Avant correction** : « lisible », 49 817 lignes, 1 colonne binaire, 507 doublons (défaut corrigé, section 5) |

Inspection 0,97 s, validation 0,59 s sur le CSV de 6,4 Mo.

### Fixtures synthétiques : rapprochement

16 rapprochements sur 16 à **PASS**, 5 contrôles de rapport sur 5 à **PASS**,
Subtotal « avant remise » sur 500 commandes remisées sur 500. Classification
`PARTIALLY_SUPPORTED` (profit de contribution indisponible : COGS partiel,
coût de transport jamais disponible). **Ce résultat ne prouve rien sur les
exports réels** : le générateur de fixtures applique la même convention que
le moteur (`Total = Subtotal − Remise + Port + Taxes`).

### Export synthétique en disposition native

| | 5 000 commandes (2,2 Mo) | 50 000 commandes (22 Mo, ~100 800 lignes) |
|---|---|---|
| Rapprochement indépendant | 16 / 16 PASS | 16 / 16 PASS |
| Contrôles du rapport | 5 / 5 PASS | 5 / 5 PASS |
| Commandes annulées / impayées comptées dans le CA | 2,28 % du CA | 2,58 % du CA |
| Contexte LLM | 16 Ko, 0 identifiant, 0 PII | 16 Ko, 0 identifiant, 0 PII |
| Inspection | 0,81 s | 4,15 s |
| Validation (détection + connecteur forcé) | 0,65 s | 6,52 s |
| Normalisation seule | 0,15 s | 1,65 s |
| Validation + normalisation + analyse | 0,45 s | 4,69 s |
| Rapport JSON + texte | < 0,01 s | < 0,01 s |
| Exécution complète du harnais (analyse faite deux fois) | 2,9 s, 176 Mo RSS | 24,9 s, **1,42 Go RSS** |

La mémoire mesurée est celle du processus complet (harnais + référence
indépendante + moteur), pas du moteur seul. Aucune optimisation n'a été faite.

## 4. Sondes sémantiques : ce que les fixtures ne pouvaient pas prouver

| Sujet | Constat | Niveau de preuve |
|---|---|---|
| **Convention du `Subtotal`** | Mervio calcule `CA = Subtotal − Discount Amount` (D-002). L'aide Shopify décrit le `Subtotal` de l'export comme « avant port et taxes » **sans préciser la remise** ; dans l'API Admin, `subtotalPriceSet` est défini **après remises**. Si l'export suit l'API, Mervio soustrait les remises deux fois et sous-estime le CA du montant total des remises. | documentaire, **non vérifié empiriquement** ; le harnais tranche commande par commande à partir de `Total` |
| **Commandes annulées / impayées** | Le moteur compte toutes les commandes quel que soit `Financial Status` (`pending`, `authorized`, `voided`…) ou `Cancelled at`. | code (`kpi.total_revenue`) + export synthétique |
| **Date des remboursements** | L'export commandes ne contient pas de date de remboursement : ils sont datés à la création de la commande. Une hausse de remboursements apparaît sur la période de vente, pas sur celle du remboursement. | code (`shopify.py`) + aide Shopify |
| **Colonne de date Stripe** | Le connecteur n'accepte que `Created (UTC)`, `Created`, `created`. Un export dont la colonne s'appelle autrement est entièrement rejeté (0 ligne acceptée). Le nom exact dans l'export Dashboard actuel n'a pas pu être confirmé. | code + test ; format réel **non vérifié** |
| **En-tête Google Ads** | `read_csv` prend la première ligne comme en-tête. Un rapport commençant par des lignes de titre est rejeté. La présence de ces lignes dans les téléchargements actuels n'a pas pu être confirmée. | code + test ; format réel **non vérifié** |

## 5. Défauts corrigés (bloquants pour la validation)

| Défaut | Découvert sur | Correction | Tests |
|---|---|---|---|
| Un `.xlsx` (et tout binaire) était « lu » en latin-1 : inspection fausse, refus pour un faux motif | le vrai fichier XLSX | refus explicite des signatures ZIP/XLSX, XLS, PDF, UTF-16 et octets nuls, avec « exporter en CSV UTF-8 » (`7d1e22b`) | `test_binary_formats.py` (21) |
| Devise absente ⇒ **« EUR » inventé**, enregistré comme devise observée, qualité « fiable » ; devise héritée de la commande précédente | cas limite `missing_currency` | devise `unknown`, champ `unavailable`, avertissement ; comportement inchangé quand la devise est déclarée (`a7f7390`) | `test_currency_absence.py` (5) |

## 6. Cas limites (connecteur Shopify)

| Cas | Validation | Analyse | Comportement |
|---|---|---|---|
| fichier vide / en-tête seul | invalid | rejetée | correct |
| lignes vides | valid_with_issues | ok | ligne rejetée, tracée |
| date invalide | valid_with_issues | ok | commande rejetée, tracée (D-013) |
| **montant illisible** | **invalid** | **rejetée** | tout le fichier échoue (D-013, connu) |
| valeurs négatives | valid_with_issues | ok | remboursement négatif signalé ; **sous-total et prix négatifs acceptés sans signal** |
| n° de commande répété hors bloc | valid | ok | fusionné en silence |
| ligne d'article identique | valid_with_issues | ok | dédupliquée, signalée |
| devise absente | valid_with_issues | ok | `unknown` (corrigé) |
| **plusieurs devises** | **valid** | ok | la validation de fichier ne le voit pas ; l'analyse le signale en erreur (D-020) et affiche la dernière devise lue |
| **remboursement > commande** | valid | ok | **aucun signal** |
| SKU / email absents | valid_with_issues | ok | signalés |
| séparateur `;`, BOM, champs entre guillemets, jetons nuls | valid | ok | correct |
| **statut `voided`** | valid | ok | **compté dans le CA** |
| fichier optionnel invalide (ex. Google Ads) | invalid | **rejetée** | **toute l'analyse est rejetée**, pas seulement la source optionnelle |

## 7. Matrice de capacités

Statut sur la **meilleure preuve disponible**, jamais sur un export marchand réel.

| Capacité | Statut | Preuve |
|---|---|---|
| inspection | PASS | CSV externe réel décrit correctement ; XLSX réel refusé (corrigé) |
| détection de source | PASS | fichier externe refusé au lieu d'être deviné (D-025, D-028) |
| validation | PARTIAL | fichier multi-devises déclaré `valid` ; un montant illisible invalide le fichier ; un fichier optionnel invalide rejette l'analyse |
| normalisation | PARTIAL | annulées / impayées incluses ; devise inventée corrigée |
| CA | PARTIAL | rapprochement au centime sur disposition native synthétique ; **convention du Subtotal non vérifiée sur un export réel** |
| remboursements | PARTIAL | rapprochés ; datés à la commande ; Shopify fait foi (D-006) |
| clients | PARTIAL | actifs et nouveaux rapprochés ; clé = email, invités séparés par commande |
| produits | PARTIAL | CA article et couverture COGS rapprochés ; aucun coût produit réel testé |
| profitabilité | PASS | refus systématique du profit complet quand un coût manque (D-008, D-029) |
| publicité | NOT_TESTED | formats réels Google Ads non disponibles ; PASS sur fixtures seulement |
| détection d'anomalies | NOT_TESTED | aucune série réelle ; déterministe sur synthétique |
| cause racine | PARTIAL | présentée en hypothèses ; trafic = clics Google Ads uniquement |
| score de santé | PASS | recalculé depuis ses dimensions et identique sur deux exécutions |
| rapport | PASS | JSON + texte produits, limites déclarées |
| contexte LLM | PASS | ≤ 16 Ko, 0 identifiant, 0 PII, déterministe ; mock local uniquement |

## 8. Réponse à la question de la mission

**Mervio peut-il analyser correctement un export marchand réel ? Inconnu.**
Faute d'export réel, la question ne peut pas être tranchée. Ce qui est établi :

- le moteur calcule exactement ce que ses conventions prévoient, jusqu'à
  50 000 commandes dans la disposition native de l'export ;
- il refuse correctement les formats qu'il ne connaît pas ;
- **deux conventions peuvent fausser le CA d'un vrai marchand sans aucun
  signal** : la remise possiblement déjà déduite du `Subtotal`, et les commandes
  annulées ou impayées comptées comme CA ;
- les formats réels Stripe et Google Ads ne sont pas vérifiés.

## 9. Maturité

**INTERNAL TESTING.** Un pilote contrôlé suppose au minimum :

1. une exécution du harnais sur un export réel avec convention du `Subtotal`
   tranchée et rapprochement à PASS ;
2. une décision produit sur les commandes annulées / impayées ;
3. un export Stripe et un export Google Ads réels passés au harnais.

## 10. Hors périmètre, reporté volontairement

D-013 (montant illisible), D-022, D-030, seuils codés en dur, écarts de
documentation, pseudonymes non salés, annulation incomplète du délai LLM,
absence de fournisseur LLM réel, tie-break des campagnes, nombre de tests du
README, version de Python non documentée, consommation mémoire à 50 000
commandes.
