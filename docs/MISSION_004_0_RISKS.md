# Mission 004.0 — Registre des risques

**Criticité :** élevée / moyenne / faible. **Statut :** ouvert, atténué ou accepté.
Les risques hérités de Mission 003.3 restent valables (voir `docs/PROJECT_STATE.md`, D-044 à D-048).

| # | Risque | Preuve ou origine | Criticité | Atténuation prévue | Statut |
|---|---|---|---|---|---|
| R-01 | **Fuite entre tenants** dès la première API | risque structurel du SaaS multi-tenant | élevée | ADR-004-003 : dépôts avec `TenantContext`, RLS, `404` croisé, tests par route et en SQL | ouvert (004.1, 004.3) |
| R-02 | Autorisation vérifiée à moitié (session sans rôle) | défaut observé dans ShopFlow (`execute` sans contrôle de rôle) | élevée | matrice de rôles côté serveur, test « rôle insuffisant » par route de mutation | ouvert (004.3) |
| R-03 | Jetons OAuth exposés (logs, base, frontend) | risque connecteurs | élevée | références de secret, chiffrement par enveloppe, liste « jamais journalisé » testée | ouvert (004.4) |
| R-04 | Données synthétiques présentées comme réelles | tentation démo et marketing | élevée | manifeste `not_for_production`, `SourceBadge` SYNTHÉTIQUE, emails `.invalid`, test AST moteur ↛ synthetic | atténué |
| R-05 | Scénarios synthétiques pris pour une validation marchand | la vérité terrain est celle du générateur | moyenne | `KNOWN_GAP` explicites ; D-046 inchangée ; export natif pilote toujours requis | accepté et documenté |
| R-06 | Sur-ajustement des scénarios au moteur actuel | calibrage fait sur ce moteur | moyenne | 170 exécutions sur 2 profils et 5 graines ; `MATCH` limités aux sorties robustes ; toute modification justifiée | atténué |
| R-07 | Analyse lente ou gourmande pour un gros tenant | 45 s et 3,7 Go à 1 M (mesuré) | moyenne | analyse en job (ADR-004-008) ; seuils ADR-004-012 ; budget mémoire du worker | atténué |
| R-08 | Goulot `window()` répété (séries, comparaisons) | ~38 % du temps à 1 M | faible aujourd'hui | index par période sur preuve | accepté |
| R-09 | Divergence entre rapport affiché et rapport calculé | calcul à la demande | moyenne | projections d'un rapport immuable, ETag par run | atténué par design |
| R-10 | Licences : réutilisation involontaire de code non licencié | 3 dépôts sans licence étudiés | moyenne | `RESEARCH ONLY — LICENSE UNCLEAR` ; clones hors dépôt ; revue de licence à chaque dépendance ajoutée | atténué |
| R-11 | Dépendance copyleft (AGPL, LGPL) | tap-shopify AGPL-3.0, Dramatiq LGPL-3.0 | moyenne | rejetées ; vérification de licence en CI à l'ajout de dépendance (à mettre en place) | ouvert |
| R-12 | LLM hors contrat en production (chiffres, causalité) | risque connu, testé en mock seulement | élevée | validateur existant, worker uniquement, explication `unavailable` en cas de rejet, suivi du taux de rejet | ouvert (004.6) |
| R-13 | Coût LLM non borné | aucun fournisseur réel encore | moyenne | budget par organisation, job explicite, pas d'appel automatique sans activation | ouvert |
| R-14 | Fuseau de boutique ignoré | périodes en UTC (limite 003.3) ; OCO montre l'enjeu | moyenne | fuseau de la source dans le connecteur API ; modèle de période par boutique | ouvert (004.4) |
| R-15 | Remise partielle et date de remboursement toujours non prouvées | D-045, D-046 | moyenne | connecteur GraphQL (`processedAt`, `subtotalPriceSet`) validé sur une boutique de développement ; puis export pilote | accepté (hérité) |
| R-16 | Deux piles (Python, TypeScript) à maintenir | ADR-004-010 | faible | types générés depuis OpenAPI ; CI séparées ; aucune logique métier en TypeScript | accepté |
| R-17 | Complexité excessive introduite trop tôt | tentation SaaS | moyenne | Partie X appliquée : ni Kubernetes, ni Kafka, ni microservices, ni base vectorielle ; nouvel ADR requis pour tout ajout d'infrastructure | atténué |
| R-18 | Dérive documentaire | README annonce 192 tests (656 réels) ; décalage déjà signalé en 003 | faible | corriger lors de la mise à jour du README en 004.1 ; nombre de tests tiré de pytest dans `PROJECT_STATE` | ouvert |
| R-19 | Suite de tests plus lente | +17 s pour les scénarios (26 → 44 s) | faible | scénarios à 20 000 commandes ; marqueur `slow` possible si la suite dépasse 2 min | accepté |
| R-20 | Recherche GitHub incomplète | recherche de code GitHub non authentifiée ; Sourcegraph limité | faible | recherche de dépôts par l'API Search + clones ; à compléter si un besoin précis apparaît | accepté |
| R-21 | Rétention et suppression non définies avec les clients | aucune politique contractuelle | moyenne | durée configurable, purge traçable par job ; à décider avant le premier pilote | ouvert |
| R-22 | UCI Online Retail II : obligation d'attribution et identifiants clients | CC BY 4.0 ; Customer ID | faible | hors Git ; citation obligatoire dans tout rapport de benchmark ; pseudonymisation si traité | accepté |
