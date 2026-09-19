# Mission 004.4.2 — Audit post-implémentation et durcissement avant ratification

Date : 18/09/2026. Branche `mission-004.4`, baseline ratifiée `3150b91`, migration candidate
`0011_identity_pii_schema`. Ce document trace les constats (F-01 à F-10) de l'audit
post-implémentation indépendant et leur traitement **dans le périmètre 004.4.2**. Aucun constat
n'est masqué : ceux qui ne sont pas corrigés ici sont des risques résiduels explicites.

## Synthèse

| ID | Sévérité | Origine | Statut 004.4.2 |
|---|---|---|---|
| F-01 | MEDIUM | préexistant (`analytics/root_cause.py`) | **Non corrigé** (hors périmètre) — risque résiduel, tâche ci-dessous |
| F-02 | LOW | 004.4.2 | **Corrigé** — `import.started` seulement après une clé d'identité utilisable |
| F-03 | MEDIUM | 004.4.2 (conception D-053) | **Documenté** — risque résiduel, fail-closed conservé |
| F-04 | LOW | 004.4.2 | **Corrigé** — SQL de `identity_keys.py` qualifié `public.` |
| F-05 | LOW | 004.4.2 | **Corrigé** — tests renforcés (voir ci-dessous) |
| F-06 | LOW | 004.4.2 / préexistant | **Corrigé** — documentation alignée sur le comportement réel |
| F-07 | INFO | préexistant | Non traité : normalisation e-mail = espaces retirés + minuscules (pas de NFC, pas d'alias) |
| F-08 | LOW | 004.4.2 | **Corrigé** — la persistance refuse tout jeu non calculé avec la clé de l'organisation |
| F-09 | INFO | accepté (D-057) | Anciens instantanés synthétiques : référence = e-mail synthétique |
| F-10 | — | — | Smoke conteneur et CI non vérifiés hors CI : exiger un run CI vert avant ratification |

## F-01 — Ordre non déterministe de `campaign_contributors` (MEDIUM, préexistant, hors 004.4.2)

**Constat.** `analytics/root_cause.py`, `_campaign_contributors` : itération sur
`set(current) | set(previous)` puis tri **stable** sur `conversions_delta` seul. En cas d'égalité,
l'ordre final dépend de l'ordre d'itération de l'ensemble, donc de `PYTHONHASHSEED` (aléatoire par
défaut à chaque processus). `root_causes[].campaign_contributors` fait partie du `report.json`
contractuel et alimente le contexte LLM.

**Preuve (audit).** Jeu synthétique `healthy_store` : 6 processus CLI (`--identity-key-file`,
graines 1 à 6) et 3 processus persistants (même clé, graines 11 à 13) ; deux ordres distincts des
éléments 1 et 2, donc des octets différents entre CLI et persisté selon les graines. `data/sample`
(aucune égalité) : identique. Aucun impact de sécurité ni de PII.

**Conséquence contractuelle.** D-058 (« mêmes octets de rapport persisté, égaux à ceux de la
CLI ») n'est **pas** garanti entre processus dans ce cas. Les tests d'égalité d'octets de 004.1 et
004.4.2 tournent dans un seul processus ; le test multi-processus ajouté en 004.4.2
(`test_cli_and_persisted_processes_with_different_hash_seeds_agree_on_the_versioned_sample`) ne
porte volontairement que sur `data/sample` et **ne couvre pas** F-01.

**Tâche de correction (mission dédiée, hors 004.4.2 ; ne pas modifier le code analytique ici).**
1. Tri déterministe de `campaign_contributors` par `conversions_delta`, avec départage par
   `campaign_id` (clé totale, indépendante de l'ordre d'itération).
2. Test multi-`PYTHONHASHSEED` (au moins trois graines, sous-processus) sur un jeu **avec**
   égalités de `conversions_delta`.
3. Test CLI en sous-processus contre worker/chemin persisté en sous-processus, graines différentes,
   octets identiques hors `_meta.generated_at`.
4. Vérifier `insights.py` (`max(..., key=conversions_delta)`) et `llm/context.py` pour le même
   motif d'égalité.
5. Évaluer l'effet sur les golden LLM (l'ordre peut changer une fois, de façon déterministe).

## F-02 — `import.started` écrit avant le refus d'identité (corrigé)

`import_csv_snapshot` accepte un rappel `on_started`, appelé **après** l'autorisation et
`ensure_identity_key`, **avant** tout accès fichier ; le worker y écrit l'audit et le log
`import.started`. Un refus d'identité (clé maître absente, discordante, sel détruit) laisse donc :
aucun `import.started`, aucun fichier ouvert, aucun instantané, aucune référence client ; travail
`failed` permanent `identity_key_unavailable` ; audit `job.enqueued → job.claimed → job.failed`.

## F-03 — Première clé maître définitive (MEDIUM, documenté)

Voir `docs/DECISIONS.md` (D-053, statut 004.4.2) et `docs/CONTAINER.md`. Mécanisme cryptographique
inchangé ; comportement fail-closed conservé ; aucune procédure de réassignation n'est inventée.

## F-04 — Ombrage `pg_temp` (corrigé)

L'audit avait démontré, dans la même session, qu'une table temporaire homonyme permettait à
`ensure_identity_key` : d'utiliser un sel forgé, d'y écrire la clé au lieu de la table canonique,
et de **faire revivre une clé détruite**. Les deux instructions visent désormais
`public.organization_identity_keys`. Tests : table temporaire homonyme, `search_path = pg_temp,
public`, appel du vrai chemin (sel forgé ignoré, aucune écriture temporaire, clé détruite toujours
refusée), plus une garde statique sur le SQL du module. Observation hors périmètre : les requêtes
applicatives préexistantes (`TenantSession`, dépôts) restent non qualifiées, convention du dépôt ;
l'ombrage exige l'exécution de SQL arbitraire dans la session applicative elle-même.

## F-05 — Qualité des tests (corrigé)

- Refus avant lecture : chemin inexistant (toute ouverture échouerait) + crochet d'audit Python
  `open` ; témoin : avec une clé utilisable, le même chemin est ouvert et échoue.
- Worker : audit réellement inspecté (absence de `import.started` pour trois causes de refus ;
  présence avec une clé utilisable).
- `pg_temp` : exerce le vrai chemin non qualifié qui était vulnérable (échouait avant correction).
- Déterminisme : test multi-processus honnête, limité à `data/sample` (ne couvre pas F-01).

## F-08 — Repli éphémère (corrigé)

Règle : CLI éphémère (défaut) → autorisé ; CLI `--identity-key-file` → déterministe ; chemin
persistant → clé d'organisation obligatoire. Chaque `CustomerIdentity` porte sa provenance
(`organization`, `explicit`, `ephemeral`) ; `load_dataset` inscrit l'identifiant de la clé utilisée
dans le `Dataset` ; `write_snapshot` exige `identity` et refuse, avant toute écriture, une clé non
issue de l'organisation, une clé d'une autre organisation, ou un jeu normalisé avec une autre clé
(dont le repli éphémère des aides locales). Le repli historique de `load_dataset` /
`ingest_shopify_orders` reste disponible pour les outils locaux (CLI, scripts, évaluation
synthétique, validation) et ne peut plus atteindre la base.
