"""Classification des erreurs de base: transitoire ou definitive.

Cette connaissance appartient a la persistance: elle depend du pilote et des
codes SQLSTATE, pas du metier. Le worker l'interroge sans jamais importer psycopg
(frontiere de `test_persistence_boundaries.py`).

Regle: en cas de doute, NON transitoire. Rejouer une erreur definitive epuise les
tentatives pour rien et retarde le diagnostic; ne pas rejouer une erreur
transitoire laisse simplement le travail en echec, visible et remis en file a la
main. Le silence coute moins cher que la boucle.
"""
from __future__ import annotations

import psycopg

from .errors import JobLeaseLost, PayloadRejected, PersistenceError, PermissionDenied, TenantAccessDenied

#: SQLSTATE transitoires: la meme requete peut reussir plus tard, sans rien changer.
RETRYABLE_SQLSTATES = frozenset({
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
    "55P03",  # lock_not_available
    "55006",  # object_in_use
    "53000", "53100", "53200", "53300",  # insufficient_resources, disk_full, out_of_memory, too_many_connections
    "57P01", "57P02", "57P03",  # admin_shutdown, crash_shutdown, cannot_connect_now
    "08000", "08003", "08006", "08001", "08004",  # connection_exception
})


def retryable_database_error(exc: BaseException) -> bool:
    """Vrai si l'erreur vient de la base ET peut disparaitre au prochain essai."""
    if isinstance(exc, (PayloadRejected, PermissionDenied, TenantAccessDenied)):
        return False
    if isinstance(exc, JobLeaseLost):
        # le travail est deja reparti chez un autre worker: ne rien reprogrammer ici
        return False
    if isinstance(exc, psycopg.OperationalError) and exc.sqlstate is None:
        # connexion coupee avant toute reponse du serveur
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is not None:
        return sqlstate in RETRYABLE_SQLSTATES
    if isinstance(exc, PersistenceError):
        return False
    return False


def database_error_code(exc: BaseException) -> str:
    """Code court et publiable d'une erreur: jamais le message du serveur."""
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate:
        return f"sqlstate_{sqlstate}"
    return type(exc).__name__
