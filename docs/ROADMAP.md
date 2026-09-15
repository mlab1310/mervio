# ROADMAP

## STAGE 0 — Fondations  🟡
Repo, architecture, config, logging, tests, docs ✅ — CI/CD 🔴, Git 🔴

## STAGE 1 — Ingestion  🟡
CSV Shopify / Stripe / Google Ads ✅ — OAuth et sync incrémentale 🔴

## STAGE 2 — Analytics  🟢
KPI, profitabilité, séries, produits, campagnes, clients ✅

## STAGE 3 — Intelligence  🟢
Anomalies, root cause v1, Business Health Score ✅

## STAGE 4 — AI  🔴
Interface de contexte posée ✅ — couche LLM, explications, AI Analyst 🔴

## STAGE 5 — Reports  🟡
JSON + rendu texte ✅ — PDF, email, Slack 🔴

## STAGE 6 — Action layer  🔴

## STAGE 7 — Scale  🔴
Auth, multi-tenant, billing, monitoring.

## Ordre recommandé

1. **Mission 002 — couche LLM** : `build_llm_context()` → explication en langage
   naturel, avec évaluation anti-hallucination. Transforme un JSON en livrable
   lisible par un dirigeant.
2. **Mission 003 — rapport PDF** : ce qui se vend réellement lors d'un audit.
3. **Mission 004 — FastAPI + PostgreSQL + multi-tenant**.
4. **Mission 005 — OAuth Shopify/Stripe** : supprime l'export CSV manuel.

Les missions 1 et 2 rendent le moteur vendable sans produit auto-servi. Elles
passent avant l'infrastructure.
