# Politique de rétention v0 (Mission 004.4, item 6)

⚖️ **Document de contrat, pas d'exécution.** Les durées ci-dessous sont **inscrites** dans les
métadonnées et appliquées par les purges déjà existantes ; l'expiration des objets bruts et le
ramassage des orphelins ne sont **pas** implémentés en 004.4.4 et relèvent de **004.9**. Les
valeurs marquées ⚖️ restent à valider juridiquement (roadmap §11, §13).

> **Mise à jour du 30/09/2026 (clôture de 004.4.6).** La section « Ce que 004.4.4 établit » est
> conservée **telle quelle** : elle décrit fidèlement l'état du dépôt au 24/09/2026, et elle est
> datée, non réécrite. Ce qui a changé depuis est consigné en fin de document, §« Ce que 004.4.5 et
> 004.4.6 ont changé ». **Ce qui n'a pas changé :** aucune **expiration** n'est exécutée — rien ne
> lit `retain_until` — et le ramassage des orphelins n'existe pas. Les deux relèvent toujours de
> **004.9**.

## Durées

| Donnée | Durée | Où elle est portée | Appliquée par |
|---|---|---|---|
| Objets bruts (`raw_objects`) | **30 jours** ⚖️ | `raw_objects.retain_until`, calculé à l'insertion depuis `RetentionPolicy.raw_objects_days` | **personne** pour l'**expiration** (rien ne lit `retain_until`) : 004.9. Les lignes sont en revanche **détruites** par l'effacement client (004.4.5) et par la purge de locataire (004.4.6), **sans égard à `retain_until`** |
| Travaux réussis | 7 jours | `RetentionPolicy.succeeded_jobs_days` | travail `purge` (004.2) |
| Travaux échoués / annulés | 30 / 7 jours | `RetentionPolicy` | travail `purge` (004.2) |
| Journal d'audit | 365 jours ⚖️ | `RetentionPolicy.audit_events_days` | travail `purge` (004.2) |
| Instantanés et rapports | conservés | — | purge de locataire (D-051) — **implémentée** en 004.4.6, révision `0016` |
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
*(État au 24/09/2026. La seconde affirmation n'est plus vraie depuis `0014` : voir la section
suivante. La première, elle, est toujours exacte — et c'est important.)*

## Ce que 004.4.5 et 004.4.6 ont changé

**Rédigé le 30/09/2026, à la clôture de 004.4.6.** Cette section **corrige** les affirmations
ci-dessus devenues fausses, et **seulement** celles-là. Elle n'implémente rien et n'anticipe pas
004.9.

### La machine à états est exécutée, elle n'est plus un simple domaine

`pending → available → purging → purged` est désormais **parcourue**, par des fonctions privilégiées
et par elles seules :

| Transition | Écrite par | Révision |
|---|---|---|
| `available → purging` (+ `purge_reason`) | `app_redact_customer`, dans la transaction de l'effacement | `0014` |
| `purging → purged` | `app_finalize_raw_object_purge`, **après** destruction réelle des octets | `0015` |
| `available → purging` (motifs `store_purge`, `organization_purge`) | `app_close_store`, `app_close_organization` | `0016` |
| `purging → ligne supprimée` | `app_purge_finalize_raw_object`, **après** destruction réelle des octets | `0016` |

**Jamais avant la destruction physique, dans aucun des deux cas.** Une ligne `purged` affirme une
destruction, et cette affirmation doit être vraie ; supprimer la ligne d'abord transformerait un
échec récupérable en octets **orphelins que plus rien ne désigne**.

### Le rôle applicatif n'a toujours ni `UPDATE` ni `DELETE` — et ce n'est pas une nuance

`0013` accorde `GRANT SELECT, INSERT` à `mervio_app` sur `raw_objects`, et **aucune** révision
ultérieure ne l'élargit : ni `0014`, ni `0015`, ni `0016`. L'affirmation de la section précédente
reste donc **exacte**. Ce qui a changé n'est pas le droit du rôle applicatif, mais l'existence de
**fonctions `SECURITY DEFINER` étroites** qui écrivent le cycle de vie à sa place, chacune liée à un
travail d'un type donné, `running`, dont le jeton d'exclusion concorde, dans l'organisation du
contexte, pour un service autorisé et un demandeur qui a **encore** le rang requis (D-052).
Conséquence pratique : `sha256`, `byte_size`, `source_kind`, `origin` et les dates restent **figés
dès l'insertion** ; seuls `state`, `purge_reason` et `purged_at` bougent, et seulement par ces
fonctions.

### Deux destructions, deux sorts pour la ligne

- **Effacement client** (D-056) : la ligne est **conservée** en `purged`, avec ses métadonnées non
  sensibles. La preuve de ce qui a existé survit à la destruction de son contenu.
- **Purge de locataire** (D-051) : la ligne est **supprimée**, parce que la boutique ou
  l'organisation elle-même disparaît. Ce qui survit est la **pierre tombale** et l'audit.

### Les durées ne gouvernent pas la destruction

Une destruction demandée — effacement client ou purge de locataire — s'exécute **sans égard à
`retain_until`** : une échéance n'est pas une protection, et un objet dont la rétention court encore
est détruit comme les autres. Inversement, une échéance **passée** ne déclenche rien.

### Exception bornée pendant une purge (D-066)

Pendant `organizations.status = 'purging'`, et pour les seuls travaux de cette organisation, le
**plancher d'une heure** avant suppression d'un travail terminal **ne s'applique plus**. Les deux
autres garanties restent entières : un travail `queued` ou `running` n'est jamais supprimable, et un
travail terminal doit avoir `finished_at` renseigné. L'exception se **referme** au passage à
`purged`. C'est la seule entorse à la politique de rétention du dépôt, et elle est ancrée sur un état
que le rôle applicatif **ne peut pas écrire**.

### Ce qui reste à 004.9 — inchangé, et à ne pas confondre avec ce qui précède

Aucun de ces points n'est traité par 004.4.5 ni 004.4.6 :

- **expiration de `retain_until`** — rien ne le lit ; un objet échu reste lisible ⚖️ ;
- **ramassage des orphelins** — octets sans ligne `raw_objects`, possibles après un dépôt ou une
  destruction interrompus (D-054, D-061) ;
- **écriture conditionnelle S3** (`IfNoneMatch`) et sémantique de **non-écrasement** de `put` (D-055) ;
- **politique IAM restreignant `s3:DeleteObject`** (D-064) — d'où `delete_capability()` qui rend
  `undetermined` sur S3, et un worker qui démarre en journalisant un avertissement ;
- **KMS réel** pour la clé maître d'identité (D-053) ;
- **validation juridique** des durées marquées ⚖️ : 30 jours pour les objets bruts, 365 jours pour
  l'audit, et les rôles de traitement (roadmap §11.5, §13).
