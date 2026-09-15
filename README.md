# Mervio — Core Analytics Engine

Moteur analytique déterministe pour l'e-commerce. Transforme des exports
Shopify / Stripe / Google Ads en **Business Health Report** structuré :
KPI, profitabilité, anomalies, cause racine, score de santé, recommandations.

**Aucun LLM n'intervient dans le calcul des chiffres.** Chaque rapport le
déclare (`_meta.llm_used = false`).

## Démarrage

```bash
pip install pytest                       # seule dépendance (dev)
python scripts/generate_sample_data.py   # fixtures SYNTHÉTIQUES
export PYTHONPATH=src

python -m mervio.analytics demo          # démonstration complète
```

### Analyser de vrais fichiers

```bash
# 1. vérifier AVANT d'analyser
python -m mervio.analytics validate \
  --shopify-orders   ~/exports/orders_export.csv \
  --shopify-products ~/exports/products_export.csv \
  --stripe           ~/exports/stripe.csv \
  --google-ads       ~/exports/ads.csv

# 2. analyser
python -m mervio.analytics analyze \
  --shopify-orders   ~/exports/orders_export.csv \
  --shopify-products ~/exports/products_export.csv \
  --stripe           ~/exports/stripe.csv \
  --google-ads       ~/exports/ads.csv \
  --out analysis/client_x --label "Client X"
```

Sources non identifiées ? Laissez Mervio détecter : `--file a.csv --file b.csv`.

Produit dans `--out` :

| Fichier | Pour qui |
|---|---|
| `report.json` | machine — contrat de sortie du moteur |
| `report.txt` | dirigeant — 15 sections lisibles |
| `data_quality.json` | opérateur — validations, couverture, limites |

Autres options : `--grain week\|month`, `--lookback N`, `--today AAAA-MM-JJ`,
`--print-report`, `--verbose`.

```bash
python -m pytest        # 192 tests
```

## Données et confidentialité

- Les fichiers importés vont dans `data/uploads/`, **ignoré par Git**.
- `data/sample/` est réservé aux fixtures synthétiques ; y écrire une donnée
  client lève une erreur.
- Numéros de carte, IBAN et clés API sont détectés et signalés **par colonne**,
  jamais recopiés dans un rapport ni dans les logs.
- Aucun appel réseau : le moteur n'a aucune dépendance runtime.

Procédure complète : `docs/REAL_DATA_TEST.md`.

## Ce que le moteur produit

```
BUSINESS HEALTH SCORE : 50/100
  Croissance du CA      3.9/100   CA -23.5% (2026-W37 vs W36)
  Profitabilité         N/A       EXCLU: composante de coût majeure manquante (cogs)
  Efficacité marketing  100/100   ROAS 6.40
  Santé client          23.4/100
  Santé produit         N/A       EXCLU: couverture coût 78% < 80%

FACT   : CA de 2026-W37 inférieur de 18.7% à la baseline
HYPO   : variation associée à la capacité du site à convertir le trafic
RECO   : auditer le tunnel de conversion avant de modifier les budgets.
         Dégradation la plus marquée sur Shopping - Core.
IMPACT : -5 518,55 EUR (écart vs baseline glissante)
```

## Principe de conception

Le moteur préfère dire « je ne sais pas » plutôt que produire un chiffre
flatteur :

- coût produit absent ⇒ marge **non calculable**, pas 100 %
- pas de donnée publicitaire ⇒ CAC **inconnu**, pas 0,00 €
- COGS manquant ⇒ dimension profitabilité **exclue** du score
  (sur les fixtures : 50/100 au lieu de 78/100)

Un tableau de bord qui ment une fois n'est plus jamais consulté.

## Données de démonstration

Tout ce qui se trouve dans `data/sample/` est **synthétique** et ne représente
aucune entreprise réelle. Généré par `scripts/generate_sample_data.py` avec une
graine fixe, incluant un incident de conversion et des défauts de qualité
volontaires (date invalide, doublons, remboursement négatif, coûts manquants).

## Documentation

| Fichier | Contenu |
|---|---|
| `docs/PROJECT_STATE.md` | état réel du projet |
| `docs/REAL_DATA_TEST.md` | procédure de test sur données réelles |
| `docs/ARCHITECTURE.md` | structure et chemin vers le SaaS |
| `docs/DATA_MODEL.md` | modèle normalisé et définitions |
| `docs/ANALYTICS_ENGINE.md` | méthodes, seuils, formules |
| `docs/DECISIONS.md` | arbitrages et leurs raisons |
| `docs/ROADMAP.md` / `docs/TODO.md` | suite |
