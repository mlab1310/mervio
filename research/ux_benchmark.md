# UX benchmark — Mission 004.0

**Corpus :**
- **Démos consultées en navigateur (15/09/2026) :**
  - Atlas (https://analytics-dashboard-demo-khaki.vercel.app) ;
  - Open Commerce Ops (https://open-commerce-ops.vercel.app, mode démo).
- **Code et captures des README :**
  - next-cwv-monitor ;
  - ShopFlow (la démo exige une connexion : non ouverte) ;
  - enterprise-dashboard.

**Limite :** aucune étude utilisateur, aucune mesure d'accessibilité instrumentée. Ce benchmark classe des patrons ;
il ne valide pas une interface.

## 1. Constat principal

Tous les dashboards étudiés répondent à **« que s'est-il passé ? »** : KPI, delta, graphique. Aucun ne répond à
**« pourquoi ? »** avec des preuves, ni à **« que faire ? »** avec un niveau de confiance, ni à **« agir »** avec une
approbation tracée. Le seul moteur d'actions trouvé (ShopFlow) exécute des règles sans analyse amont et sans
contrôle de rôle.

Le moteur Mervio produit déjà la chaîne complète en données structurées :
- `kpis` + `comparisons` → **FACT** ;
- `anomalies` + `root_causes.factors` → **EVIDENCE** ;
- `hypothesis` → **INFERENCE** ;
- `recommendations` → **RECOMMENDATION** ;
- `data_quality`, `limitations` → confiance.

L'interface doit rendre cette chaîne visible, pas la réinventer.

## 2. Patrons observés et décision

Légende :
- **KEEP** = adopter tel quel ;
- **ADAPT** = adopter en le transformant ;
- **REJECT** = ne pas faire ;
- **BUILD** = n'existe nulle part, à concevoir.

### 2.1 Hiérarchie et navigation

| Patron | Vu dans | Décision | Raison |
|---|---|---|---|
| Barre latérale : sections métier (Orders, Products, Customers) | Atlas, OCO, ShopFlow | **ADAPT** | La navigation primaire suit les questions (Briefing → Problèmes → Actions), pas les tables |
| Page d'accueil = grille de 4 à 6 KPI + graphique | Atlas, OCO, enterprise | **ADAPT** | L'accueil Mervio commence par les 1 à 3 constats de la période, les KPI viennent en appui |
| Barre latérale droite de notifications / activité | enterprise, ShopFlow | **ADAPT** | Remplacée par une file « à décider » (recommandations en attente d'approbation) |
| Fil d'Ariane et drill-down liste → détail | next-cwv-monitor (routes → route) | **KEEP** | Chaîne Problème → Preuve → Donnée source |
| Page « Regressions » dédiée | next-cwv-monitor | **ADAPT** | Devient « Problèmes » : anomalies + cause + recommandation, pas seulement des métriques |
| 48 panneaux sur un écran | shopify-cn-dashboard (README) | **REJECT** | Charge cognitive ; Mervio priorise |

### 2.2 KPI, tendances, graphiques

| Patron | Vu dans | Décision | Raison |
|---|---|---|---|
| Carte KPI : valeur, delta %, sparkline | Atlas, OCO | **ADAPT** | Ajouter la qualité (`reliable`, `incomplete`, `unavailable`), la période exacte et un lien « pourquoi » |
| Couleur du delta selon le sens souhaitable (remboursements en hausse = rouge) | Atlas (taux de remboursement) | **KEEP** | Déjà modélisé côté moteur (`DESIRABLE_DIRECTION`, D-010) |
| Badge de source sur chaque carte (« SYNTHETIC ») | Atlas | **KEEP** | Généraliser en `SourceBadge` (Shopify, Stripe, Google Ads, synthétique) |
| Période de comparaison explicite (« Compare: Sep 14 ») | OCO | **KEEP** | Le moteur compare des périodes closes ; les deux périodes doivent être lisibles |
| Sous-échantillonnage déclaré (« 31 points → 31, LTTB ») | Atlas | **KEEP** | Transparence de rendu ; plafond de points par graphique |
| « After-ads profit » = ventes − publicité | OCO | **REJECT** | Un profit sans COGS n'est pas un profit (D-008) ; Mervio affiche « non calculable » avec les coûts manquants |
| Camembert de répartition des ventes | enterprise | **REJECT** | Illisible au-delà de 4 parts ; barres triées |
| Carte géographique décorative | enterprise | **REJECT** | Aucune décision associée |
| Percentiles p50 à p99 | next-cwv-monitor | **ADAPT** | Pour les métriques de distribution (délais de livraison futurs), pas pour le CA |

### 2.3 Filtres, périodes, tableaux

| Patron | Vu dans | Décision | Raison |
|---|---|---|---|
| Filtres synchronisés dans l'URL (lien partageable, retour arrière) | Atlas (nuqs) | **KEEP** | Un constat doit pouvoir être partagé tel quel |
| Plages glissantes 7 / 30 / 90 / 365 j + personnalisée | Atlas, OCO | **ADAPT** | Les analyses Mervio portent sur des **périodes closes** (semaine ISO, mois) ; les plages libres sont réservées à l'exploration |
| Sélecteur de boutique global | OCO (« 3 stores ») | **KEEP** | Frontière tenant visible ; jamais un filtre côté client seulement |
| Tableaux virtualisés, pagination serveur | Atlas (TanStack Virtual) | **KEEP** | Commandes et produits à volume élevé |
| Export CSV | OCO, ShopFlow | **ADAPT** | Export des agrégats et des preuves ; pas d'export de données clients brutes par défaut |
| Recherche globale commandes / produits | Atlas, OCO | **ADAPT** | Secondaire ; les identifiants clients restent pseudonymisés (D-023) |

### 2.4 États et fiabilité

| Patron | Vu dans | Décision | Raison |
|---|---|---|---|
| Skeletons et `loading.tsx` par route | Atlas, ShopFlow | **KEEP** | Perception de vitesse ; mise en page stable (CLS) |
| `error.tsx` par route, message sans trace | Atlas | **KEEP** | Erreurs isolées par zone |
| Bandeau d'état des données (« mock data ; sync = webhooks + polling ») | OCO | **ADAPT** | `DataFreshnessIndicator` : dernière synchronisation réussie, avertissements, sources absentes |
| Avertissement de synchronisation périmée | OCO (Google Ads) | **KEEP** | Un KPI calculé sur une source périmée doit le dire |
| « Demo data. Refreshing won't change values » | Atlas | **KEEP** | Toute donnée synthétique marquée comme telle |
| États vides génériques | tous | **BUILD** | L'état vide Mervio explique quelle source connecter et ce qu'elle débloquera |

### 2.5 Thème, accessibilité, clavier

| Patron | Vu dans | Décision | Raison |
|---|---|---|---|
| Thème clair / sombre / système (`next-themes`) | Atlas, enterprise, ShopFlow | **KEEP** | Standard attendu |
| Primitives accessibles (Radix, shadcn/ui) | Atlas, enterprise, ShopFlow, CWV | **KEEP** | Focus, rôles ARIA, clavier |
| Tests e2e avec axe-core | Atlas | **KEEP** | Accessibilité en CI |
| Couleur seule pour porter le sens du delta | la plupart | **REJECT** | Toujours flèche + signe + texte |

### 2.6 Insights, recommandations, actions

| Patron | Vu dans | Décision | Raison |
|---|---|---|---|
| Règles SI/ALORS avec modèles | ShopFlow | **ADAPT** | Les actions Mervio naissent d'une recommandation reliée à une preuve, pas d'une règle isolée |
| Journal d'exécution des règles (`RuleLog`) | ShopFlow | **KEEP** | Audit de chaque action |
| Exécution sans contrôle de rôle | ShopFlow | **REJECT** | Approbation par rôle obligatoire |
| Chaîne constat → pourquoi → preuves → recommandation → action | aucun | **BUILD** | Cœur produit Mervio |
| Distinction FACT / INFERENCE / RECOMMENDATION / ACTION à l'écran | aucun | **BUILD** | Déjà présente dans les données (`category`, `hypothesis`, `causality_established=false`) |
| Confiance affichée comme heuristique bornée | aucun | **BUILD** | `confidence` n'est pas une probabilité (D-012) : libellé qualitatif + formule accessible |

## 3. Le flux Mervio exprimé en interface

```
BRIEFING (période close)            « Que s'est-il passé ? »
  CA avant ajustements  -18 % vs semaine précédente     [Shopify · fiable]
  ▸ 1 problème critique · 2 avertissements · 1 opportunité
        │
        ▼
PROBLÈME                             « Pourquoi ? »
  FACT        Commandes -23 %, panier moyen +6 %
  EVIDENCE    Clics payants -19 % ; conversion stable ; 3 produits = 61 % de la baisse
  INFERENCE   Cause principale probable : baisse du trafic payé     [confiance : moyenne, non causale]
  LIMITES     Trafic organique non observé (Google Ads seul)
        │
        ▼
RECOMMANDATION                       « Que faire ? »
  Revoir l'acquisition sur le groupe de produits X    [impact estimé : non chiffrable]
        │
        ▼
ACTION                               « Laisse-moi agir »
  Brouillon d'action → approbation (rôle admin) → exécution → journal d'audit
```

Chaque niveau est un lien vers le niveau suivant et vers ses preuves. Aucun chiffre n'apparaît sans son champ
source dans le rapport du moteur.
