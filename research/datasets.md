# Jeux de données — Mission 004.0

Question : **quels jeux externes peuvent servir de tests en toute sécurité ?**
Licences et provenance détaillées dans `licensing_matrix.md`.

## 1. Réponse courte

| Usage | Source retenue | Pourquoi |
|---|---|---|
| Tests unitaires et de scénarios | **`mervio.synthetic`** (ce dépôt) | déterministe, vérité terrain connue, format des connecteurs, aucune licence tierce, aucune PII |
| Fixtures historiques de démo | `data/sample/` (synthétique, Mission 001) | inchangé ; figé par les goldens LLM |
| Benchmark de volume | `mervio.synthetic` jusqu'à 1 M de commandes | reproductible par graine ; mesuré (`performance_benchmark.md`) |
| Réalisme de volume et d'annulations sur données réelles | UCI Online Retail II (CC BY 4.0) | seul jeu réel trouvé dont la licence autorise l'usage commercial ; **non encore converti** |
| Sémantique d'export Shopify | OH5 (hors Git) + export natif d'un pilote | aucun export natif public (D-046) |
| Tout le reste (Olist, RetailRocket, REES46, theLook, GA4 sample) | aucun | non commercial ou licence non établie |

## 2. Fiches

### 2.1 UCI Online Retail II — candidat B
- **Source :** https://archive.ics.uci.edu/dataset/502/online+retail+ii ; archive
  `https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip`.
- **Licence :** CC BY 4.0. **Citation :** Chen, D. (2012). Online Retail II [Dataset]. UCI Machine Learning Repository.
  https://doi.org/10.24432/C5CG6D
- **Contenu :**
  - 1 067 371 lignes de facture ;
  - colonnes Invoice, StockCode, Description, Quantity, InvoiceDate, Price, Customer ID, Country ;
  - GBP ;
  - annulations en facture `C…`.
- **Acquisition reproductible :**
  ```bash
  mkdir -p ~/mervio-validation/external_004_0 && cd ~/mervio-validation/external_004_0
  curl -L -o online_retail_ii.zip "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
  shasum -a 256 online_retail_ii.zip   # attendu (15/09/2026): 572e3627…8e67bfb
  ```
  Taille téléchargée : 45 622 418 octets ; l'archive contient `online_retail_II.xlsx`.
- **Limites :**
  - XLSX : Mervio refuse ce format (D-039) et l'environnement n'a ni pandas ni openpyxl ;
  - schéma facture et non export Shopify (D-025, D-028) : ni remise, ni port, ni taxes, ni Total ;
  - les Customer ID sont des identifiants clients à traiter comme personnels ;
  - données de 2009 à 2011.
- **Usage prévu :** adaptateur de recherche **séparé** (hors connecteur Shopify) pour mesurer l'ingestion à volume
  réel et le traitement des annulations. Jamais versionné ; manifeste de provenance obligatoire.

### 2.2 Jeux écartés

| Jeu | Raison |
|---|---|
| Olist | CC BY-NC-SA 4.0 : non commercial |
| RetailRocket | CC BY-NC-SA 4.0 : non commercial ; valeurs hachées |
| REES46 | licence contradictoire (« © Original Authors » vs usage libre cité) ; 14,7 Go |
| theLook eCommerce | synthétique Google ; conditions formelles non trouvées ; hébergé dans BigQuery (dépendance cloud) |
| GA4 obfuscated sample | conditions non trouvées ; cohérence interne « limitée » par l'obfuscation, selon la documentation Google |
| Kaggle « Shopify » | format non supporté (D-028) |

## 3. Pourquoi un générateur propre plutôt qu'un jeu public

1. **Vérité terrain.** Un jeu public ne dit pas ce qui « aurait dû » être détecté. Les scénarios Mervio injectent
   une cause connue et déclarent ce que le moteur doit retrouver, ou ne sait pas retrouver.
2. **Format.** Le générateur écrit les exports que les connecteurs lisent déjà : le moteur est testé sans adaptateur.
3. **Licence et vie privée.** Aucune tierce partie, aucune personne réelle.
4. **Volume.** Le même scénario se génère à 100 ou à 1 000 000 de commandes.

Limite assumée : un jeu synthétique ne prouve rien sur le comportement d'un vrai marchand (D-046). Les scénarios
testent le moteur contre des causes connues ; ils ne valident pas la sémantique des exports réels.
