"""Pool de connexions applicatives verifiees, pour l'API (Mission 004.6, D-099).

POURQUOI UN COMPOSANT DISTINCT, ET NON UN POOL SOUS `Database`
`Database` detient AU PLUS UNE connexion, ce qui suffit a la CLI et au worker. Le registre de
decision proposait d'abord de brancher un pool SOUS elle sans changer sa signature. Verification
faite: cette formulation etait inexacte. Sa docstring dit << non partagee entre threads >>, et
c'est vrai pour les REQUETES -- mais `workers/worker.py` appelle
`main_database.cancel_current_statement()` DEPUIS le fil du gardien de bail, sur l'attribut
`_connection` du fil principal, et le worker tient par ailleurs un SECOND `Database`
(`lease_database`) pour ce fil. Le modele reel est << un `Database` par fil, plus un appel
d'annulation inter-fils >>, pas << aucun partage >>.

Sous un pool, << la connexion courante >> n'a plus de sens: `cancel_current_statement` viserait
une cible ambigue. Le pool est donc un composant SEPARE, reserve a l'API. `Database`, le worker
et son chemin d'annulation ne sont pas touches, et les tests existants non plus.

LE SEAM EST MINIMAL, ET C'EST VOULU
`TenantSession` n'utilise qu'une chose de son `Database`:

    transaction(*, organization_id, user_id) -> contextmanager[psycopg.Connection]

Ce module expose cela, et RIEN d'autre qui permettrait de requeter sans poser le contexte.
`test_the_pool_exposes_nothing_but_the_transaction_seam` le verrouille.

INVARIANT DE SECURITE GARANTI
    Aucune connexion rendue au pool ne peut porter un contexte tenant, et aucune requete sans
    contexte ne peut lire une ligne.

DEUX mecanismes independants le produisent:

1. `set_config('app.organization_id', ..., true)` -- le troisieme argument est `is_local`, donc
   le GUC meurt au `COMMIT`/`ROLLBACK`. Un `checkin` ne peut pas le transporter: la transaction
   est deja fermee quand la connexion revient.
2. `app_current_organization_id()` vaut `NULLIF(current_setting(..., true), '')::uuid`, donc
   NULL sans contexte. Les politiques comparent `colonne = NULL`, jamais TRUE: ZERO ligne. Le
   mode de defaillance est REFUS, jamais MAUVAIS TENANT.

Le seul vecteur residuel est un GUC positionne a une MAUVAISE valeur DANS une transaction: un
defaut de `TenantSession`, que sa verification d'appartenance en base couvre deja.

`DISCARD ALL` EST INTERDIT A LA REMISE
`SET TIME ZONE 'UTC'` est de portee SESSION, pas transaction. Un `DISCARD ALL` hygienique
l'effacerait, les dates civiles du rapport changeraient d'octets, et les sept goldens plus
`test_determinism_f01` tomberaient sans que la cause soit lisible dans l'echec. La remise ne
fait donc qu'une verification LOCALE, sans aller-retour: aucune transaction ne doit rester
ouverte.

SYNCHRONE, PAS ASYNCHRONE
Toute la persistance est psycopg3 synchrone -- `TenantSession`, `jobs`, `analyses`, `snapshots`,
le worker, et les fixtures de la suite. Passer async imposerait de les reecrire, pour un gain
nul sur l'objectif mesure (p95 < 300 ms en lecture de rapport persiste). FastAPI execute les
endpoints `def` synchrones dans un threadpool; une connexion par fil issue du pool conserve la
semantique actuelle sans la toucher.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg
from psycopg_pool import ConnectionPool

from .database import _verify
from .errors import UnsafeDatabaseConfiguration

#: bornes par defaut: un pool d'API, pas un pool de traitement par lots.
DEFAULT_MIN_SIZE = 1
DEFAULT_MAX_SIZE = 10
#: attente maximale d'une connexion libre, en secondes.
DEFAULT_TIMEOUT = 10.0


class DatabasePool:
    """Fabrique de transactions tenant-scopees sur un pool de connexions verifiees.

    Meme contrat que `Database.transaction`, et seulement celui-la. A ouvrir une fois par
    processus; l'objet est utilisable depuis plusieurs fils, chacun recevant sa propre
    connexion le temps d'une transaction.
    """

    def __init__(self, url: str, *, min_size: int = DEFAULT_MIN_SIZE, max_size: int = DEFAULT_MAX_SIZE,
                 timeout: float = DEFAULT_TIMEOUT, application_name: Optional[str] = None,
                 connect_timeout: Optional[int] = None) -> None:
        if not url:
            raise UnsafeDatabaseConfiguration("URL de base de donnees absente")
        self._url = url
        self._options = {key: value for key, value in (("application_name", application_name),
                                                       ("connect_timeout", connect_timeout)) if value is not None}
        self._pool = ConnectionPool(
            conninfo=url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            kwargs={"autocommit": True, **self._options},
            configure=_configure,
            reset=_reset,
            check=ConnectionPool.check_connection,
            open=False,
        )

    # -- cycle de vie ---------------------------------------------------------------------
    def open(self, *, wait: bool = True, timeout: float = DEFAULT_TIMEOUT) -> "DatabasePool":
        """Ouvre le pool, apres avoir verifie le role sur une connexion JETABLE.

        La verification anticipee n'est pas une precaution de style, elle est necessaire:
        `psycopg_pool` RETENTE une connexion qui echoue dans `configure`, puis rend
        `PoolTimeout`. Un operateur ayant configure un role superutilisateur ou `BYPASSRLS`
        lirait donc << pool initialization incomplete >> au lieu de la cause. Mesure a l'appui:
        le refus de `_verify` apparaissait bien dans les journaux, mais l'exception remontee
        etait `PoolTimeout`. On echoue donc ICI, avec l'erreur qui nomme le probleme.

        `configure` garde la meme verification, par connexion physique: la sonde dit que la
        configuration est bonne maintenant, elle ne garantit pas qu'elle le restera.
        """
        self._probe_role()
        self._pool.open(wait=wait, timeout=timeout)
        return self

    def _probe_role(self) -> None:
        with psycopg.connect(self._url, autocommit=True, **self._options) as conn:
            _verify(conn, allow_rls_bypass=False)

    def close(self) -> None:
        self._pool.close()

    def statistics(self) -> dict:
        """Compteurs du pool, pour `readyz` et la supervision. Aucune donnee de tenant."""
        return dict(self._pool.get_stats())

    def __enter__(self) -> "DatabasePool":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transactions ---------------------------------------------------------------------
    @contextmanager
    def transaction(self, *, organization_id: Any = None, user_id: Any = None) -> Iterator[psycopg.Connection]:
        """Bloc transactionnel avec contexte tenant LOCAL a la transaction.

        Meme corps que `Database.transaction`: le contexte est pose par `set_config(..., true)`,
        donc il meurt avec la transaction et ne peut pas suivre la connexion dans le pool.
        """
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT set_config('app.organization_id', %s, true), set_config('app.user_id', %s, true)",
                    (str(organization_id) if organization_id else "", str(user_id) if user_id else ""),
                )
                yield conn


def _configure(conn: psycopg.Connection) -> None:
    """Une fois par connexion PHYSIQUE: la verification de `Database`, puis le fuseau.

    `_verify` refuse un role superutilisateur ou `BYPASSRLS` -- l'isolation RLS serait
    contournee -- et exige l'encodage UTF8, sans lequel les rapports ne se conservent pas a
    l'octet pres. Le pool ne doit pas etre un chemin qui echappe a ces deux refus.
    """
    _verify(conn, allow_rls_bypass=False)
    conn.execute("SET TIME ZONE 'UTC'")


def _reset(conn: psycopg.Connection) -> None:
    """A la remise: verification LOCALE, aucun aller-retour, et JAMAIS `DISCARD ALL`.

    `DISCARD ALL` effacerait `SET TIME ZONE 'UTC'`, de portee session (voir l'en-tete). Le
    contexte tenant, lui, n'a pas besoin d'etre efface: `set_config(..., true)` l'a deja fait
    mourir avec la transaction, et son absence vaut REFUS et non acces elargi.

    Une transaction encore ouverte signale un bloc quitte anormalement. Lever ici fait
    REJETER la connexion par le pool, ce qui est le comportement souhaite: on ne rend pas au
    service une connexion dont on ignore l'etat.
    """
    status = conn.info.transaction_status
    if status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(f"connexion rendue au pool hors transaction fermee: {status!r}")
