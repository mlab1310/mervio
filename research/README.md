# research/ — Mission 004.0 : recherche, benchmark et gate d'architecture

Ce dossier contient la **recherche** qui fonde les décisions de Mission 004. Il ne contient ni code externe ni
donnée externe : les clones et téléchargements restent hors dépôt, sous `~/mervio-validation/`.

| Document | Question traitée |
|---|---|
| `repositories.md` | Quels dépôts publics sont utiles, notés sur quels critères, pour quelle décision (A à E) ? |
| `licensing_matrix.md` | Quelles licences, quelles conditions, quelle incertitude, quelle décision de réutilisation ? |
| `datasets.md` | Quels jeux externes sont utilisables sans risque, et pourquoi Mervio génère ses propres données ? |
| `ux_benchmark.md` | Quels patrons d'interface garder, adapter, rejeter ou construire pour « quoi → pourquoi → que faire → agir » ? |
| `performance_benchmark.md` | Comment le moteur se comporte de 100 à 1 000 000 de commandes ; budgets frontend |
| `architecture_research.md` | Options d'architecture évaluées, preuves, questions ouvertes |

Documents de décision associés :
- `docs/MISSION_004_0_ARCHITECTURE.md` : architecture cible, contrats d'API, tenancy, sécurité, tests ;
- `docs/MISSION_004_0_DECISIONS.md` : ADR-004-001 à 012 ;
- `docs/MISSION_004_0_RISKS.md` ;
- `docs/MISSION_004_0_HANDOFF.md`.

Outils livrés :
- `src/mervio/synthetic/` : générateur déterministe, scénarios, validation, évaluation ;
- `scripts/generate_data.py` ;
- `benchmarks/run_benchmark.py` ;
- `benchmarks/scenario_robustness.py`.

## Règles de ce dossier

1. Aucune affirmation sans source : URL, commit inspecté, fichier ou mesure.
2. Une licence non établie signifie `RESEARCH ONLY — LICENSE UNCLEAR`.
3. Aucune donnée synthétique n'est présentée comme réelle ; aucune donnée externe n'entre en production.
4. Les recommandations d'architecture sont des décisions révisables par ADR, pas des certitudes.
