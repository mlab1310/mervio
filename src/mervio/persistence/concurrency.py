"""Ordonnancement des ecritures destructrices et des imports (D-063).

D-063 impose UN invariant, et rien de plus:

    un import et une operation destructrice (effacement client, et plus tard purge de
    boutique ou d'organisation) portant sur la MEME organisation sont totalement ordonnes
    a la frontiere de transaction.

Aucun ordre n'est privilegie: les DEUX ordres totaux sont corrects, et c'est ce qui rend le
mecanisme sur. Si l'import passe d'abord, l'effacement le suit et rattrape les lignes neuves;
s'il passe apres, sa consultation du rejeu voit l'effacement deja enregistre.

MECANISME. Un verrou consultatif de TRANSACTION, de portee ORGANISATION:

    import                          partage    (plusieurs imports d'un tenant coexistent)
    effacement client               exclusif
    purge de boutique (004.4.6)     exclusif   -- a livrer, meme primitive
    purge d'organisation (004.4.6)  exclusif   -- a livrer, meme primitive

Ni table de verrous, ni colonne, ni privilege: `EXECUTE` sur `pg_advisory_xact_lock*` est
deja acquis a PUBLIC. Le verrou est libere AUTOMATIQUEMENT au commit, au rollback et a la mort
de la connexion -- un arret brutal ne laisse donc rien derriere lui.

PIEGE RATIFIE PAR D-063. `Database` ouvre ses connexions en `autocommit=True`: une instruction
executee HORS d'un bloc transactionnel est sa propre transaction, et un verrou de transaction y
serait relache IMMEDIATEMENT. Pris au mauvais endroit, ce module serait donc un no-op
silencieux -- le pire des modes de defaillance, puisque tout continuerait de passer. D'ou le
refus explicite de `_require_transaction`: mieux vaut lever que proteger pour de faux.
"""
from __future__ import annotations

from uuid import UUID

from psycopg.pq import TransactionStatus

from .errors import PersistenceError

#: Portees de verrou. Une seule aujourd'hui: l'organisation entiere (D-063).
#: 004.4.6 reutilise CETTE portee pour les purges, il n'en ajoute pas une autre.
ORGANIZATION_SCOPE = "org"


class LockOutsideTransaction(PersistenceError):
    """Verrou demande hors transaction: il serait relache aussitot (autocommit)."""

    def __init__(self) -> None:
        super().__init__(
            "un verrou de transaction exige une transaction ouverte "
            "(Database.transaction() / TenantSession.transaction())")


def _require_transaction(conn) -> None:
    if TransactionStatus(conn.info.transaction_status) == TransactionStatus.IDLE:
        raise LockOutsideTransaction()


def organization_scope_key(conn, organization_id: UUID, *, scope: str = ORGANIZATION_SCOPE) -> int:
    """Cle deterministe et NON PII du verrou, dans la construction RATIFIEE par D-063.

        hashtextextended(organization_id || ':' || portee, 0)

    Elle ne contient aucune reference client -- et n'en contiendrait pas davantage si elle en
    portait une: une reference est deja un HMAC non reversible (D-053). La cle ne quitte jamais
    la transaction: ni stockee, ni journalisee, ni indexee.

    Une collision sur 64 bits ne peut causer qu'une ATTENTE inutile, jamais un ordonnancement
    incorrect: le verrou n'accorde aucun acces aux donnees, il ne fait qu'ordonner.
    """
    return conn.execute("SELECT hashtextextended(%s, 0)",
                        (f"{organization_id}:{scope}",)).fetchone()[0]


def lock_organization_shared(conn, organization_id: UUID) -> None:
    """Verrou PARTAGE: un import. Deux imports d'une meme organisation coexistent.

    A prendre AVANT la consultation du rejeu et avant toute ecriture de ligne canonique, et
    a conserver jusqu'au commit -- c'est-a-dire dans la transaction qui porte ces ecritures.
    """
    _require_transaction(conn)
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s)",
                 (organization_scope_key(conn, organization_id),))


def lock_organization_exclusive(conn, organization_id: UUID) -> None:
    """Verrou EXCLUSIF: une operation destructrice. Elle attend les imports en vol.

    Utilise aujourd'hui par l'effacement client, pour sa transaction de tombstone. Les purges
    de boutique et d'organisation (004.4.6) doivent prendre CE MEME verrou -- D-063 leur
    interdit d'inventer un autre mecanisme de concurrence.
    """
    _require_transaction(conn)
    conn.execute("SELECT pg_advisory_xact_lock(%s)",
                 (organization_scope_key(conn, organization_id),))


__all__ = ["ORGANIZATION_SCOPE", "LockOutsideTransaction", "lock_organization_exclusive",
           "lock_organization_shared", "organization_scope_key"]
