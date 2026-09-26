# D-063 (projet) — Concurrence entre effacement/purge et import

> **Statut : CLOS — D-063 ratifiée, E5 implémentée (25/09/2026).** Ce document conserve le
> protocole expérimental et les mesures qui ont fondé la décision ; le texte ratifié fait foi et
> se trouve dans `docs/DECISIONS.md` (D-063). Le §8 ci-dessous était la *proposition* soumise à
> arbitrage — conservé tel quel pour l'histoire, il ne fait pas foi.

Portée élargie à la demande : **effacement client**, **purge de boutique**, **purge
d'organisation** — face à l'**import**.

---

## 1. Modèle de concurrence exact

### 1.1 Ce qui existe réellement

| Opération | Code | Transaction |
|---|---|---|
| Import | `write_snapshot` | **une seule** transaction : instantané → sources → *consultation du rejeu* → lignes canoniques → scellement |
| Effacement client | `app_redact_customer` (0014) + `app_finalize_raw_object_purge` (0015) | une transaction pour l'effacement en base ; **une par objet** pour la destruction des octets |
| Purge de boutique | **aucun** | — |
| Purge d'organisation | **aucun** | — |

**Fait vérifié :** `grep` ne trouve `purge_store` / `purge_organization` nulle part dans `src/`.
Le `JobType.PURGE` existant est une purge de **rétention** (travaux terminés, événements
d'audit expirés), sans rapport. Les colonnes `status` / `purged_at` de D-051 et son déclencheur
de clôture **n'existent pas** dans le schéma. Ces deux opérations sont **entièrement à
concevoir** (004.4.6).

### 1.2 Isolation

`default_transaction_isolation = read committed` ; le dépôt ne pose aucun niveau explicite.
Sous READ COMMITTED, chaque **instruction** lit un instantané frais, mais une transaction ne
relit pas ce qu'elle a déjà lu.

### 1.3 La fenêtre

```
t1  import : consultation de customer_redactions   -> vide
t2  effacement (autre worker) : COMMIT
t3  import : écriture des lignes + COMMIT          -> référence D'ORIGINE écrite
```

### 1.4 Les trois opérations ne sont PAS la même classe

C'est le résultat structurant de cette analyse.

- **Purge d'organisation et de boutique** : D-051 impose déjà l'ordre *clôture d'abord,
  destruction ensuite* (`status = 'purging'`, déclencheur interdisant toute nouvelle
  insertion, annulation des travaux en file). Un import en vol **échoue** alors à sa première
  écriture. De plus, une purge **détruit tout** : une ligne écrite juste avant est détruite
  avec le reste. La résurrection y est donc structurellement impossible **si la clôture est
  implémentée comme conçue** — ce qui reste à faire.
- **Effacement client** : il ne peut PAS clôturer l'organisation, puisqu'il ne doit bloquer ni
  les autres clients, ni les autres boutiques. Il n'a donc aucun équivalent de la clôture, et
  c'est la seule des trois opérations réellement exposée.

**Conséquence : une politique unique n'est pas nécessaire, et la chercher serait une erreur.**
Deux classes suffisent — une clôture d'état pour les purges, une exclusion ciblée pour
l'effacement client.

---

## 2. Preuves expérimentales

Rejouées contre le **schéma réel** et les **rôles réels** du dépôt (`app` pour l'import,
`migrator`/propriétaire du schéma pour l'effacement, comme `app_redact_customer`). Quinze
expériences, toutes vertes.

> **Écueil écarté.** Une première campagne, menée sous `mervio_admin`, était **invalide** :
> ce rôle est `SUPERUSER` + `BYPASSRLS`, donc la RLS ne s'appliquait pas et les mesures ne
> décrivaient pas Mervio. Les résultats ci-dessous sont ceux de la seconde campagne, sur les
> rôles non privilégiés que `Database` accepte réellement.

```
A   READ COMMITTED, effacement après la consultation      -> écrit c1:aaaa…   RESSUSCITÉ=True
B   re-vérification, effacement AVANT la re-vérification  -> ABORT (reprise)  RESSUSCITÉ=False
B   re-vérification, effacement APRÈS la re-vérification  -> COMMIT           RESSUSCITÉ=True
C1  SERIALIZABLE, même client        import=COMMIT   effacement=SERIALIZATION_FAILURE  RESSUSCITÉ=False
C2  SERIALIZABLE, client SANS RAPPORT import=COMMIT   effacement=SERIALIZATION_FAILURE  SUR-SÉRIALISÉ=True
C3  plan de l'UPDATE d'effacement    -> Index (aucun index sur customer_ref ; portée organisation)
D1  import d'abord, effacement ensuite   RESSUSCITÉ=False  (l'effacement rattrape les lignes neuves)
D2  effacement d'abord, import ensuite   RESSUSCITÉ=False  (la consultation voit l'effacement)
D3  client sans rapport verrouillable=True   même client verrouillable=False   SUR-SÉRIALISÉ=False
D4  même référence, AUTRE organisation    verrouillable=True
D5  portée organisation : deux imports coexistent=True ; purge exclusive pendant un import=False
D6  deux imports, mêmes clients, acquisition NON TRIÉE -> INTERBLOCAGE=True
D6  deux imports, mêmes clients, acquisition TRIÉE     -> INTERBLOCAGE=False
P   1500 clients : consultation 2,4 ms · 1500 verrous 95,5 ms · 1 verrou d'organisation 0,10 ms
P2  6 imports concurrents × 1500 verrous = 9000 entrées : aucun échec
```

**Lectures importantes.**

- **C ferme la fenêtre, mais sur-sérialise à l'échelle de l'organisation.** C2 le prouve :
  effacer ALICE fait échouer un import ne contenant **que** BOB. Cause (C3) : `orders` n'a
  **aucun index sur `customer_ref`** (revision 0011, explicitement) ; le prédicat de l'UPDATE
  est donc évalué sur une plage indexée préfixée par `organization_id`, et tout insert de la
  même organisation entre en conflit. C'est l'**effacement** qui est annulé, pas l'import.
- **D ferme la fenêtre dans les deux ordres.** C'est la propriété décisive : le verrou
  n'empêche pas le « mauvais » ordre, il rend l'ordre **total**, et *les deux* ordres totaux
  sont corrects (D1 : l'effacement passe après et rattrape les lignes neuves ; D2 : l'import
  passe après et voit l'effacement).
- **L'ordre total sur les clés est obligatoire** (D6) : sans tri, l'interblocage est réel et
  PostgreSQL annule l'un des deux imports.
- **Aucune saturation de la table de verrous n'a été reproduite** (P2). L'écart est de
  **latence** (95,5 ms pour 1500 verrous contre 0,10 ms pour un seul), pas un mur.

---

## 3. Matrice A / B / C / D

| | **A** actuel | **B** re-vérification | **C** SERIALIZABLE | **D** verrous ciblés |
|---|---|---|---|---|
| Résurrection impossible | **non** (A) | **non** — fenêtre résiduelle (B2) | **oui** (C1) | **oui** (D1, D2) |
| Ordre des transactions | non contraint | non contraint | détecté *a posteriori* | **total par clé** |
| Comportement PostgreSQL | — | relecture par instruction | SSI, `serialization_failure` | attente puis exécution |
| Reprise | — | l'import reprend | l'**effacement** reprend | aucune (attente) |
| Interblocage | aucun | aucun | aucun | **réel sans tri** (D6) |
| Portée du verrou | — | — | organisation (C2) | client, ou organisation |
| Sur-sérialisation | aucune | aucune | **oui** (C2) | **aucune** (D3, D4) |
| Imports multi-clients | — | 1 requête | conflit global | 1 verrou/client, ou 1 seul |
| Purge boutique | sans effet | sans effet | conflit global | verrou d'organisation (D5) |
| Purge organisation | sans effet | sans effet | conflit global | verrou d'organisation (D5) |
| Coût mesuré | 0 | +2,4 ms | échecs à reprendre | +0,10 ms ou +95,5 ms |
| Complexité opérationnelle | nulle | faible | **élevée** | faible |
| Rayon d'impact | nul | `write_snapshot` | **chemin d'import central** | `write_snapshot` + chemins privilégiés |
| RLS | inchangée | inchangée | inchangée | inchangée |
| Privilèges | aucun | aucun | aucun | **aucun** (`pg_advisory_*` est `PUBLIC`) |
| Crash / arrêt | — | rien d'écrit | rien d'écrit | verrou **libéré** au commit/rollback |

---

## 4. Recommandation

**D, en portée organisation, avec sémantique lecteur/écrivain** — et **non** en portée client.

```
import                      pg_advisory_xact_lock_shared(clé(org))      +0,10 ms
effacement client           pg_advisory_xact_lock(clé(org))
purge de boutique           pg_advisory_xact_lock(clé(org))
purge d'organisation        pg_advisory_xact_lock(clé(org))
```

Pourquoi la portée organisation plutôt que client :

- **un seul verrou par import** (0,10 ms) au lieu de 1500 (95,5 ms) ;
- **aucun tri à maintenir**, donc l'interblocage de D6 ne peut pas exister ;
- **la même primitive couvre les trois opérations**, y compris les deux qui n'existent pas
  encore — ce que la portée client ne peut pas faire, une purge devant exclure *tous* les
  clients ;
- les imports d'une organisation **coexistent** entre eux (D5), et deux organisations ne se
  bloquent jamais (D4). La sur-sérialisation de C est donc évitée là où elle coûte.

Ce que cela concède : un effacement **attend** les imports en vol de son organisation. Borné
par la durée d'un import, et acceptable pour une opération dont le délai réglementaire se
compte en jours.

**La clé.** `hashtextextended(organization_id || ':' || portée, 0)`. Non PII par construction :
la référence client est déjà un HMAC non réversible (D-053), et la clé de portée organisation
ne contient même pas de référence client. Elle ne quitte jamais la transaction — **ni table de
verrous persistante, ni colonne, ni journal**. Une collision 64 bits ne produirait qu'une
attente inutile, jamais une incorrection.

**B reste utile en complément**, pas en remplacement : il transforme une fenêtre silencieuse en
échec visible. À trancher séparément.

---

## 5. Invariants que la solution retenue doit garantir

1. Une ligne canonique commitée après un effacement commité ne porte jamais la référence
   effacée de ce client.
2. Une référence effacée reste associée au **tombstone enregistré** — jamais un second.
3. Deux clients effacés ne fusionnent jamais ; un client effacé ne se scinde jamais.
4. Aucune opération d'une organisation n'attend, ni n'échoue, à cause d'une autre organisation.
5. Deux imports d'une même organisation ne s'excluent pas mutuellement.
6. Aucun interblocage n'est atteignable par une séquence d'opérations légitimes.
7. Le rejeu n'émet aucun `customer.redacted` et ne crée aucun effacement.
8. Aucune PII, aucune référence client, aucune clé de verrou n'est journalisée ni persistée.
9. Un arrêt brutal ne laisse aucun verrou détenu ni aucune ligne à moitié effacée.
10. Les faits économiques (montants, dates, comptes) sont inchangés par le rejeu.

---

## 6. Matrice de tests exigée

**Concurrence** — effacement avant / pendant / après la consultation ; import avant / après
l'effacement ; deux imports concurrents même organisation ; deux organisations concurrentes ;
purge concurrente d'un import ; absence d'interblocage sur une séquence légitime ; libération
du verrou après crash (connexion tuée).
**Rejeu** — la matrice A–J déjà écrite (16 tests, verte).
**Purges (004.4.6)** — import en vol pendant une clôture ; import démarrant après une clôture ;
purge pendant un effacement ; effacement pendant une purge.
**Non-régression** — table d'effacements vide ⇒ comportement d'avant E5 ; rapports golden
inchangés ; banc d'import sans dégradation mesurable.

---

## 7. Impact sur 004.4.6

004.4.6 doit livrer `purge_store` et `purge_organization` **à partir de rien**. Cette analyse
lui impose trois choses :

1. **implémenter la clôture de D-051** (`status`, `purged_at`, déclencheur d'interdiction
   d'insertion) — c'est elle qui protège les purges, pas un verrou ;
2. **prendre le même verrou d'organisation en exclusif**, pour attendre proprement les imports
   en vol au lieu de les faire échouer à mi-parcours ;
3. **ne pas dupliquer** la primitive : la fonction de dérivation de clé et la prise de verrou
   doivent être écrites une fois, en 004.4.5, et réutilisées.

---

## 8. Texte de décision proposé (D-063) — À ARBITRER

> **D-063 — Effacement, purge et import concurrents : verrou consultatif d'organisation**
>
> **Contexte.** L'import s'exécute en READ COMMITTED et consulte les effacements enregistrés
> dans la transaction de `write_snapshot`. Un effacement commité entre cette consultation et
> le commit de l'import n'est pas vu : la référence d'origine est écrite. Reproduit.
>
> **Décision.** Un verrou consultatif **de transaction**, de portée **organisation**, pris en
> **partage** par l'import et en **exclusif** par l'effacement client, la purge de boutique et
> la purge d'organisation. Clé : `hashtextextended(organization_id || ':' || portée, 0)`,
> jamais persistée, jamais journalisée. Aucun privilège, aucune table, aucun changement de
> niveau d'isolation.
>
> **Justification.** Seule option qui rende l'ordre **total** et dont les **deux** ordres soient
> corrects. Ne sur-sérialise ni entre organisations, ni entre imports. Couvre les trois
> opérations d'effacement avec une primitive unique. Coût mesuré : 0,10 ms par import.
>
> **Rejetées.** *A* (résurrection démontrée) ; *B* seule (fenêtre résiduelle démontrée) ;
> *C* (ferme la fenêtre mais sur-sérialise à l'échelle de l'organisation, faute d'index sur
> `customer_ref`, et déplace l'échec sur l'effacement) ; *verrou par client* (1500 verrous et
> 95,5 ms par import, et un interblocage réel sans ordre total).
>
> **Conséquences.** Un effacement attend les imports en vol de son organisation. 004.4.6
> réutilise la primitive et implémente la clôture de D-051.
>
> **Risques résiduels.** Un import anormalement long retarde un effacement ⚖️. Collision de
> clé 64 bits : attente inutile, jamais incorrection. La fenêtre reste ouverte pour tout
> chemin d'écriture futur qui ne prendrait pas le verrou — à vérifier à chaque ajout.

---

## 9. État du code et des tests

Non commité : `erasure.py` (consultation), `snapshots.py` (câblage du rejeu),
`test_import_erasure_replay.py` (16 tests), ce document.
Suite complète : **2676 passés, 0 ignoré**. La porte de comptage exact de `ci.yml` reste
**volontairement périmée** (2661), puisque E5 n'est pas commité. Le harnais d'expérimentation
a été **supprimé** du dépôt après mesure.
