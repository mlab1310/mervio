"""Purge de boutique et d'organisation: les appels privilegies, et rien d'autre (004.4.6).

COUCHE MINCE, comme `erasure.py` avant elle. Ce module ne decide rien -- ni qui a le droit, ni
ce qui est detruit, ni dans quel ordre les etats changent. Tout cela est porte par la revision
`0016`, dont la garde partagee `app_purge_guard` reverifie a CHAQUE appel les conditions de
D-052: organisation du contexte, service autorise, travail du bon type et reellement pris,
jeton d'exclusion concordant, et demandeur toujours de rang `owner` (D-065 Q6, plus strict que
l'effacement, qui se contente d'`admin`).

    close_organization()      app_close_organization        cloture, annulation, objets illisibles
    close_store()             app_close_store               idem, a l'echelle d'une boutique
    destroy_identity_key()    app_destroy_identity_key      sel detruit -- ORGANISATION seulement
    remove_object()           app_purge_finalize_raw_object ligne retiree, APRES les octets
    purge_tenant_data()       app_purge_tenant_data         rapports -> instantanes -> travaux
    tombstone_organization()  app_tombstone_organization    pierre tombale
    tombstone_store()         app_tombstone_store           pierre tombale

VERROU D-063: il est pris PAR LES FONCTIONS SQL, pas ici. Les cinq qui touchent des lignes
canoniques ou des statuts le prennent en exclusif dans leur propre transaction; les deux qui
ne changent qu'un etat d'objet ou le sel ne le prennent pas, exactement comme la finalisation
d'effacement (D-063 point 2). Le reprendre ici serait redondant, et donnerait a croire que la
protection vient du Python.

JETON D'EXCLUSION (revision 0006): declare dans la transaction de chaque appel, depuis le
travail que le worker detient. Une tentative qui a perdu son bail ne purge plus rien, meme si
son processus tourne encore.

CE QUI N'EST PAS ICI: aucune garde D-052 reecrite, aucun ordre invente, aucune destruction
d'octets. L'ordre appartient au gestionnaire (`workers/handlers.py`), et les octets au
magasin d'objets -- dont le confinement est la propriete de securite (D-055, D-061).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List
from uuid import UUID

from .jobs import JobRecord, lease_token
from .raw_objects import RawObject, _COLUMNS
from .tenancy import Permission, TenantSession


@dataclass(frozen=True)
class ClosureResult:
    """Ce que la cloture a REELLEMENT fait, tel que la fonction privilegiee le rapporte."""

    jobs_cancelled: int
    raw_objects_marked: int


@dataclass(frozen=True)
class PurgeCounts:
    """Comptes par table. Aucune identite, aucun nom, aucune cle d'objet: des NOMBRES."""

    reports: int
    analysis_runs: int
    data_snapshots: int
    connections: int
    customer_redactions: int
    jobs: int

    def as_document(self) -> dict:
        return {
            "reports": self.reports,
            "analysis_runs": self.analysis_runs,
            "data_snapshots": self.data_snapshots,
            "connections": self.connections,
            "customer_redactions": self.customer_redactions,
            "jobs": self.jobs,
        }


def _declare_holder(conn, job: JobRecord) -> None:
    """Declare le jeton d'exclusion de la tentative courante, local a la transaction."""
    conn.execute("SELECT set_config('app.job_lease_token', %s, true)",
                 (lease_token(job.attempts, job.locked_by or ""),))


def _call(session: TenantSession, job: JobRecord, sql: str, *parameters):
    with session.transaction(Permission.PURGE_TENANT) as conn:
        _declare_holder(conn, job)
        return conn.execute(sql, (job.id, *parameters)).fetchone()


def close_organization(session: TenantSession, job: JobRecord) -> ClosureResult:
    """Cloture l'organisation: `purging`, travaux en file annules, objets rendus illisibles.

    UNE transaction, sous le verrou exclusif de D-063 pris par la fonction: elle attend donc
    les imports en vol plutot que de les faire echouer a mi-parcours. C'est aussi elle qui
    OUVRE la fenetre D-066 -- a partir de `purging`, les travaux terminaux de cette
    organisation se suppriment sans attendre le plancher d'une heure.
    """
    return ClosureResult(*_call(session, job, "SELECT * FROM app_close_organization(%s)"))


def close_store(session: TenantSession, job: JobRecord) -> ClosureResult:
    """Cloture la boutique nommee par le travail. L'organisation, elle, reste vivante."""
    return ClosureResult(*_call(session, job, "SELECT * FROM app_close_store(%s)"))


def destroy_identity_key(session: TenantSession, job: JobRecord) -> bool:
    """Detruit le sel d'identite. ORGANISATION SEULEMENT (D-065 Q4).

    Rend `False` si le sel etait deja detruit: la ligne est immuable une fois videe (D-053),
    donc le rejeu est sans effet et sans erreur. Une purge de BOUTIQUE ne peut pas appeler
    ceci -- la fonction refuse le type de travail, meme si la boutique est la derniere.
    """
    return bool(_call(session, job, "SELECT app_destroy_identity_key(%s)")[0])


def purging_objects(session: TenantSession, *, limit: int = 1000) -> List[RawObject]:
    """Objets dont les octets restent a detruire, dans la portee deja close.

    La cloture a marque `purging` exactement ce qui entre dans la portee -- toute
    l'organisation, ou la seule boutique. Filtrer a nouveau ici dupliquerait cette decision:
    on lit donc ce que la base a marque, et rien d'autre. L'ordre est stable pour qu'une
    reprise reparte au meme endroit.
    """
    with session.transaction(Permission.PURGE_TENANT) as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM raw_objects WHERE state = 'purging' "
            f"ORDER BY created_at, id LIMIT %s", (int(limit),),
        ).fetchall()
    return [RawObject(*row) for row in rows]


def remove_object(session: TenantSession, job: JobRecord, raw_object_id: UUID) -> bool:
    """Retire la LIGNE d'un objet dont les octets ont deja disparu (D-065 Q3).

    Rend `False` si la ligne n'est plus la: l'appelant a pu etre interrompu apres le commit.
    C'est la difference assumee avec l'effacement client, qui CONSERVE sa ligne en `purged`
    avec ses metadonnees -- ici la boutique ou l'organisation disparait avec elle (D-051).
    """
    return bool(_call(session, job, "SELECT app_purge_finalize_raw_object(%s, %s)",
                      raw_object_id)[0])


def purge_tenant_data(session: TenantSession, job: JobRecord) -> PurgeCounts:
    """Detruit les donnees de la portee, dans l'ordre impose par les cles etrangeres reelles.

    Rapports -> executions -> instantanes (dont le CASCADE emporte sources et lignes
    canoniques) -> connexions -> preuves d'effacement (organisation seulement, D-065 Q5) ->
    travaux. Un seul `DELETE` par table, jamais par lots: la cle etrangere auto-referente
    `supersedes` interdit de supprimer un instantane superseded isolement.
    """
    return PurgeCounts(*_call(session, job, "SELECT * FROM app_purge_tenant_data(%s)"))


def tombstone_stores(session: TenantSession, job: JobRecord) -> int:
    """Tombstone TOUTES les boutiques de l'organisation, en une fois (purge d'organisation).

    Reservee a `purge_organization`: `app_tombstone_store` reste dediee a la purge de boutique,
    et la separation est portee par la garde. Un seul `UPDATE`, jamais une boucle -- une boucle
    interrompue laisserait une partie des boutiques tombstonees et l'autre vivante.

    La fonction refuse tant qu'il reste un objet brut, un travail autre que la purge, ou une
    donnee de boutique: une pierre tombale affirme que la boutique n'a plus rien. Rend le
    nombre de boutiques tombstonees, donc `0` au rejeu.
    """
    return int(_call(session, job, "SELECT app_tombstone_organization_stores(%s)")[0])


def tombstone_organization(session: TenantSession, job: JobRecord) -> bool:
    """Pose la pierre tombale de l'organisation, et REFERME la fenetre D-066.

    La fonction refuse tant qu'il reste un objet brut, un travail autre que la purge, ou une
    boutique non tombstonee: apres `purged`, la suppression des travaux redevient impossible,
    donc le nettoyage DOIT preceder.
    """
    return bool(_call(session, job, "SELECT app_tombstone_organization(%s)")[0])


def tombstone_store(session: TenantSession, job: JobRecord) -> bool:
    """Pose la pierre tombale de la boutique. Le sel de l'organisation n'est PAS touche."""
    return bool(_call(session, job, "SELECT app_tombstone_store(%s)")[0])


__all__ = ["ClosureResult", "PurgeCounts", "close_organization", "close_store",
           "destroy_identity_key", "purge_tenant_data", "purging_objects", "remove_object",
           "tombstone_organization", "tombstone_store", "tombstone_stores"]
