"""Connexion PostgreSQL et transactions tenant-scopees.

Deux barrieres d'isolation (ADR-004-003):
1. applicative: toute requete des depots filtre explicitement organization_id;
2. base: RLS FORCEE alimentee par `set_config('app.organization_id', ..., true)`,
   l'equivalent parametrable de SET LOCAL: le contexte meurt avec la transaction,
   donc aucune fuite d'un tenant a l'autre sur une connexion reutilisee.

Le code applicatif ne se connecte JAMAIS avec un role superutilisateur ou
BYPASSRLS: la connexion est refusee (verification au premier usage).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg

from .errors import UnsafeDatabaseConfiguration

APP_DATABASE_URL_ENV = "MERVIO_DATABASE_URL"


class Database:
    """Fabrique de transactions sur une connexion applicative verifiee.

    Une instance detient au plus une connexion (autocommit, chaque unite de
    travail est un bloc de transaction explicite). Non partagee entre threads;
    le pool de connexions arrive avec l'API (004.3).
    """

    def __init__(self, url: str, *, application_name: Optional[str] = None, connect_timeout: Optional[int] = None,
                 _allow_rls_bypass_for_tests: bool = False) -> None:
        if not url:
            raise UnsafeDatabaseConfiguration("URL de base de donnees absente")
        self._url = url
        #: visible dans pg_stat_activity (004.3): connexion principale et connexion du bail d'un worker
        self._options = {key: value for key, value in (("application_name", application_name),
                                                      ("connect_timeout", connect_timeout)) if value is not None}
        self._connection: Optional[psycopg.Connection] = None
        # reserve aux tests qui prouvent la barriere applicative SEULE (RLS contournee)
        self._allow_rls_bypass = _allow_rls_bypass_for_tests

    @classmethod
    def from_env(cls, variable: str = APP_DATABASE_URL_ENV) -> "Database":
        url = os.environ.get(variable)
        if not url:
            raise UnsafeDatabaseConfiguration(f"{variable} non definie: aucune base par defaut")
        return cls(url)

    # -- connexion ------------------------------------------------------------
    def connection(self) -> psycopg.Connection:
        if self._connection is None or self._connection.closed:
            connection = psycopg.connect(self._url, autocommit=True, **self._options)
            try:
                _verify(connection, allow_rls_bypass=self._allow_rls_bypass)
                connection.execute("SET TIME ZONE 'UTC'")
            except BaseException:
                connection.close()
                raise
            self._connection = connection
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def cancel_current_statement(self) -> bool:
        """Annule l'instruction en cours, depuis un AUTRE fil (004.3). Ne leve jamais.

        Sert au worker quand son bail est perdu ou son arret force: la transaction en
        cours echoue (QueryCanceled) et est annulee par la base. Vrai si une demande a
        ete envoyee.
        """
        connection = self._connection
        if connection is None or connection.closed:
            return False
        try:
            connection.cancel_safe(timeout=5.0)
        except Exception:  # noqa: BLE001 - une annulation impossible n'est pas une erreur du fil appelant
            return False
        return True

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transactions ---------------------------------------------------------
    @contextmanager
    def transaction(self, *, organization_id=None, user_id=None) -> Iterator[psycopg.Connection]:
        """Bloc transactionnel avec contexte tenant local a la transaction.

        Usage interne a la persistance: le code applicatif passe par
        `TenantSession` (tenancy.py), qui verifie l'appartenance.
        """
        connection = self.connection()
        with connection.transaction():
            connection.execute(
                "SELECT set_config('app.organization_id', %s, true), set_config('app.user_id', %s, true)",
                (str(organization_id) if organization_id else "", str(user_id) if user_id else ""),
            )
            yield connection


def _verify(connection: psycopg.Connection, *, allow_rls_bypass: bool) -> None:
    row = connection.execute(
        "SELECT r.rolsuper, r.rolbypassrls, current_setting('server_encoding') "
        "FROM pg_roles r WHERE r.rolname = current_user"
    ).fetchone()
    if row is None:  # pragma: no cover - current_user existe toujours
        raise UnsafeDatabaseConfiguration("role courant introuvable")
    superuser, bypass_rls, encoding = row
    if (superuser or bypass_rls) and not allow_rls_bypass:
        raise UnsafeDatabaseConfiguration(
            "connexion applicative refusee: le role est superutilisateur ou BYPASSRLS "
            "(l'isolation RLS serait contournee)"
        )
    if encoding != "UTF8":
        raise UnsafeDatabaseConfiguration(
            f"encodage serveur {encoding}: UTF8 requis pour conserver les rapports a l'octet pres"
        )
