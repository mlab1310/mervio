# Politique de rétention v0 (Mission 004.4, item 6)

⚖️ **Document de contrat, pas d'exécution.** Les durées ci-dessous sont **inscrites** dans les
métadonnées et appliquées par les purges déjà existantes ; l'expiration des objets bruts et le
ramassage des orphelins ne sont **pas** implémentés en 004.4.4 et relèvent de **004.9**. Les
valeurs marquées ⚖️ restent à valider juridiquement (roadmap §11, §13).

## Durées

| Donnée | Durée | Où elle est portée | Appliquée par |
|---|---|---|---|
| Objets bruts (`raw_objects`) | **30 jours** ⚖️ | `raw_objects.retain_until`, calculé à l'insertion depuis `RetentionPolicy.raw_objects_days` | **personne en 004.4.4** — expiration et purge : 004.9 |
| Travaux réussis | 7 jours | `RetentionPolicy.succeeded_jobs_days` | travail `purge` (004.2) |
| Travaux échoués / annulés | 30 / 7 jours | `RetentionPolicy` | travail `purge` (004.2) |
| Journal d'audit | 365 jours ⚖️ | `RetentionPolicy.audit_events_days` | travail `purge` (004.2) |
| Instantanés et rapports | conservés | — | purge d'organisation (D-051, 004.4.6) |
| Logs | rétention de la plateforme (≤ 30 j) | hors dépôt | plateforme |

## Ce que 004.4.4 établit, et ce qu'elle n'établit pas

**Établi.** Chaque objet brut porte une échéance explicite (`retain_until NOT NULL`) dès sa
création : le principe « tout ce qui est conservé a une durée » (roadmap §11.1-4) est honoré au
niveau des métadonnées, et la valeur vient d'une constante Python testable plutôt que d'un défaut
SQL, comme les autres durées du dépôt (`workers/retention.py`).

**Non établi, volontairement.** Aucune expiration n'est exécutée : rien ne lit `retain_until`. Un
objet dont l'échéance est passée reste lisible. De même, un dépôt interrompu peut laisser des
octets **orphelins**, sans ligne `raw_objects` — donc illisibles, puisque la lisibilité vient de la
ligne sous RLS et non du magasin. D-054 accepte explicitement ce risque résiduel et reporte le
ramassage à 004.9 ; aucune compensation n'est tentée en 004.4.4.

**Destruction.** L'effacement client et la purge d'organisation (D-056, D-051) relèvent de 004.4.5
et 004.4.6 : `raw_objects` n'accorde en 004.4.4 ni `UPDATE` ni `DELETE`, et la machine à états
`pending → available → purging → purged` n'existe pour l'instant que comme domaine de contrainte.
