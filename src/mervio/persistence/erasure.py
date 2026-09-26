"""Effacement client: les deux appels privilegies, et rien d'autre (004.4.5, D-052, D-056).

Ce module est une COUCHE MINCE au-dessus de deux fonctions `SECURITY DEFINER`. Il ne decide
rien: ni qui a le droit, ni ce qui est efface, ni dans quel ordre les etats changent. Tout
cela est porte par PostgreSQL (revisions 0014 et 0015), qui reverifie chaque condition de
D-052 a CHAQUE appel -- organisation du contexte, service autorise, travail du bon type et
reellement pris, jeton d'exclusion concordant, demandeur toujours de rang `admin`.

    redact_customer()            0014   tombstone, preuve, objets -> `purging`, audit
    finalize_object_purge()      0015   `purging` -> `purged`, APRES destruction des octets

JETON D'EXCLUSION. Les deux fonctions l'exigent (revision 0006): une tentative qui a perdu
son bail ne peut plus rien effacer, meme si son processus tourne encore. Il est declare ici,
dans la transaction de l'appel, depuis le travail que le worker detient.

AUCUNE IDENTITE. Le `customer_ref` circule tel qu'il a ete mis en file: un HMAC. Rien ici ne
le derive, ne le recalcule, ni ne lit d'e-mail -- la resolution identite -> reference est
worker-local, hors file, et appartient a E6 (D-062).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List
from uuid import UUID

from .concurrency import lock_organization_exclusive
from .jobs import JobRecord, lease_token
from .raw_objects import RawObject, _COLUMNS
from .tenancy import Permission, TenantSession

#: Objets bruts porteurs d'identite client (D-056). Les autres ne sont jamais detruits.
IDENTITY_BEARING = ("shopify_orders", "stripe")


@dataclass(frozen=True)
class RedactionResult:
    """Ce que l'operation privilegiee a REELLEMENT fait, tel qu'elle le rapporte."""

    redaction_id: UUID
    #: jeton de tombstone (`redacted:<uuid4>`), aleatoire et distinct par client
    redacted_ref: str
    orders_tombstoned: int
    raw_objects_marked: int


def _declare_holder(conn, job: JobRecord) -> None:
    """Declare le jeton d'exclusion de la tentative courante, local a la transaction."""
    conn.execute("SELECT set_config('app.job_lease_token', %s, true)",
                 (lease_token(job.attempts, job.locked_by or ""),))


def redact_customer(session: TenantSession, job: JobRecord) -> RedactionResult:
    """Effacement en base (0014): tombstone, preuve, objets illisibles, audit -- en UNE fois.

    L'objet de l'operation est lu par la fonction dans `jobs.payload`, jamais passe ici: le
    seul parametre est l'identifiant du travail (D-052). Rejouer le meme travail retrouve la
    meme preuve et le meme tombstone, sans en creer un second.
    """
    with session.transaction(Permission.REDACT_CUSTOMER) as conn:
        # D-063: verrou EXCLUSIF de l'organisation. C'est CETTE transaction, et elle seule, qui
        # touche des lignes canoniques: elle attend donc les imports en vol de l'organisation.
        # Les transactions de finalisation des objets ne sont PAS couvertes, et n'ont pas a
        # l'etre -- elles ne modifient que `raw_objects.state`.
        lock_organization_exclusive(conn, session.organization_id)
        _declare_holder(conn, job)
        row = conn.execute(
            "SELECT redaction_id, redacted_ref, orders_tombstoned, raw_objects_marked "
            "FROM app_redact_customer(%s)", (job.id,),
        ).fetchone()
    return RedactionResult(*row)


def purging_objects(session: TenantSession, *, limit: int = 1000) -> List[RawObject]:
    """Objets de l'organisation dont les octets restent a detruire (`purging`).

    Toute l'organisation, toutes boutiques confondues: un client appartient a l'organisation,
    et D-056 porte l'effacement sur ses objets porteurs d'identite, pas sur une boutique.
    L'ordre est stable (`created_at, id`) pour qu'une reprise reparte au meme endroit.
    """
    with session.transaction(Permission.REDACT_CUSTOMER) as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM raw_objects WHERE state = 'purging' "
            f"ORDER BY created_at, id LIMIT %s", (int(limit),),
        ).fetchall()
    return [RawObject(*row) for row in rows]


def recorded_redactions(conn, references) -> Dict[str, str]:
    """Effacements DEJA enregistres parmi `references`: {reference d'origine -> tombstone}.

    Lecture SEULE, dans la transaction DEJA ouverte par l'appelant -- c'est ce qui la rend
    utile a l'import: la consultation et l'ecriture des lignes canoniques partagent la meme
    transaction, donc le rejeu s'annule avec l'import s'il echoue.

    Confinee au locataire par la RLS (`customer_redactions_tenant_isolation`), pas par une
    condition ici: l'effacement d'une organisation est INVISIBLE pour une autre, donc ne peut
    pas lui etre applique -- et aucun oracle d'existence ne fuit.

    UNE seule requete, bornee par les references reellement presentes dans le jeu: jamais un
    aller-retour par commande. `UNIQUE (organization_id, customer_ref)` garantit au plus une
    ligne par client, donc le dictionnaire ne peut pas etre ambigu.
    """
    wanted = sorted({str(reference) for reference in references if reference})
    if not wanted:
        return {}
    rows = conn.execute(
        "SELECT customer_ref, redacted_ref FROM customer_redactions WHERE customer_ref = ANY(%s)",
        (wanted,),
    ).fetchall()
    return {original: tombstone for original, tombstone in rows}


def finalize_object_purge(session: TenantSession, job: JobRecord, raw_object_id: UUID) -> bool:
    """`purging -> purged` (0015). A N'APPELER QU'APRES un `ObjectStore.delete` revenu sain.

    Rend `True` si la ligne vient de passer `purged`, `False` si elle l'etait deja -- ce
    second cas est le rejeu normal d'une tentative interrompue entre la destruction des
    octets et le commit, pas une anomalie.

    La fonction ne peut pas verifier que les octets ont disparu: c'est l'appelant qui en
    repond, en respectant l'ordre. Elle garantit le reste: aucune ligne ne devient `purged`
    sans etre passee par `purging`, et seul un travail autorise le demande.
    """
    with session.transaction(Permission.REDACT_CUSTOMER) as conn:
        _declare_holder(conn, job)
        return conn.execute("SELECT app_finalize_raw_object_purge(%s, %s)",
                            (job.id, raw_object_id)).fetchone()[0]


__all__ = ["IDENTITY_BEARING", "RedactionResult", "finalize_object_purge", "purging_objects",
           "recorded_redactions", "redact_customer"]
