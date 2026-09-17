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


#: SQLSTATE d'un schema incomplet: table, colonne, fonction ou schema absent (migration manquante).
SCHEMA_SQLSTATES = frozenset({"42P01", "42703", "42883", "3F000"})
#: Contraintes du vocabulaire d'audit (0005, etendues par 0008).
AUDIT_VOCABULARY_CONSTRAINTS = frozenset({"audit_events_action_check", "audit_events_resource_type_check"})


def database_failure_kind(exc: BaseException):
    """Famille publiable d'une erreur du pilote, pour un code de sortie (CLI d'administration, 004.3.7).

    `unavailable` base injoignable ou refusant la connexion; `schema` migration manquante;
    `conflict` contrainte ou garde de la base; `privilege` droit refuse par la base; `other`.
    None si l'exception ne vient pas du pilote (les erreurs de la persistance ont leur propre type).
    Jamais le message du serveur: il peut citer une valeur.
    """
    if not isinstance(exc, psycopg.Error):
        return None
    sqlstate = exc.sqlstate
    if sqlstate is None:
        return "unavailable" if isinstance(exc, psycopg.OperationalError) else "other"
    if sqlstate[:2] in ("08", "28") or sqlstate in ("57P01", "57P02", "57P03", "53300"):
        return "unavailable"
    if sqlstate in SCHEMA_SQLSTATES:
        return "schema"
    if sqlstate == "23514" and getattr(exc.diag, "constraint_name", None) in AUDIT_VOCABULARY_CONSTRAINTS:
        # une action d'audit que la base ne connait pas encore: revision manquante, pas un conflit
        return "schema"
    if sqlstate.startswith("23"):
        return "conflict"
    if sqlstate == "42501":
        return "privilege"
    return "other"
