# ARCHITECTURE — Mervio Core Analytics Engine

## Position dans la vision

```
DATA -> INGESTION -> NORMALIZATION -> VALIDATION -> ANALYTICS
     -> ANOMALY -> ROOT CAUSE -> BUSINESS HEALTH -> [STRUCTURED REPORT]
     -> (mission suivante) LLM -> RECOMMENDATION -> ACTION
```

La mission 001 construit la chaîne **jusqu'au rapport structuré inclus**.
La couche LLM n'existe pas encore : seule son interface d'entrée est posée
(`mervio.llm.context`).

## Arborescence

```
src/mervio/
  config.py            seuils, poids, versions — AUCUN seuil ailleurs
  errors.py            hiérarchie d'exceptions (mappable sur HTTP plus tard)
  logging_config.py
  domain/
    models.py          modèle normalisé, indépendant des sources
    quality.py         statuts, incidents et comptages de lignes
  ingestion/
    base.py            parsing tolérant (FR/US, devises, fuseaux)
    shopify.py         commandes + produits (coûts)
    stripe.py          paiements + frais + remboursements
    google_ads.py      performance campagne journalière
  analytics/
    periods.py         découpage temporel déterministe
    kpi.py             KPI Engine
    profitability.py   profit de contribution
    timeseries.py      séries, WoW/MoM/YoY, moyennes mobiles
    anomaly.py         détection v1
    root_cause.py      décomposition multiplicative
    health.py          Business Health Score
    insights.py        FACT / EVIDENCE / HYPOTHESIS / RECOMMENDATION
    report.py          assemblage du JSON de sortie
    pipeline.py        orchestration (point d'entrée réutilisable)
  application/
    imports.py         détection de source, validation de fichier
    sensitive.py       détection de secrets (sans jamais exposer la valeur)
    service.py         analyze_dataset() — point d'entrée de la future API
    workspace.py       isolation des fichiers importés
  reporting/
    executive.py       rapport lisible par un dirigeant
    writers.py         report.json / report.txt / data_quality.json
  llm/context.py       AnalyticsResult -> contexte LLM (aucun appel)
  cli/main.py          analyze / validate / demo
```

## Couches et dépendances

```
cli/  ──►  application/  ──►  analytics/  ──►  domain/
              │                  │
              └──► ingestion/ ───┘
reporting/ ──► application/ (types) + domain/
```

Une couche ne dépend jamais d'une couche au-dessus d'elle. `cli/` est
remplaçable par `api/` sans toucher à rien d'autre.

## Règles structurantes

1. **Le LLM ne calcule rien.** Tout chiffre vient de Python. `_meta.llm_used`
   est écrit dans chaque rapport.
2. **L'Analytics Engine ne connaît que `models.py`.** Ajouter WooCommerce ou
   Meta Ads = écrire un connecteur, pas toucher à un KPI.
3. **Absence != zéro.** Un coût inconnu reste `None`. Un CAC sans donnée pub
   vaut `None`, jamais `0.00`.
4. **Chaque métrique se justifie** : définition, formule, sources, période,
   statut qualité.
5. **Pas d'I/O dans la logique métier.** `pipeline.run_analysis()` retourne un
   dict ; seule la CLI écrit sur disque. C'est ce qui rend le portage FastAPI
   mécanique.

## Chemin vers le SaaS

| Aujourd'hui | Demain | Impact |
|---|---|---|
| `analyze_dataset(AnalysisRequest)` | `POST /analyses` | wrapper FastAPI |
| `AnalysisResult.summary()` | `GET /analyses/{id}` | sérialisation directe |
| `write_outputs()` | `GET /analyses/{id}/report` | lecture depuis un stockage |
| CSV | connecteurs OAuth | nouveaux modules `ingestion/` |
| `Dataset` en mémoire | PostgreSQL | mapping `models.py` -> tables |
| mono-client | multi-tenant | `organization_id` en clé de `Dataset` |

Aucune de ces étapes ne demande de réécrire le moteur analytique.

## Ce qui n'est volontairement PAS construit

Authentification, billing, OAuth, frontend, base de production, multi-agent.
Voir `ROADMAP.md`.
