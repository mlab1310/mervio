"""Classification des echecs d'execution: reprise ou echec definitif.

Trois familles:

    transitoire   la meme tentative peut reussir plus tard sans rien changer
                  (verrou, connexion perdue, ressource saturee)
    definitive    aucune tentative ne reussira: la demande elle-meme est fautive
                  (fichier invalide, historique insuffisant, ressource absente)
    inconnue      bornee par max_attempts, donc reprise deux fois au plus par
                  defaut: une panne passagere non identifiee ne doit pas perdre
                  un travail, et une panne durable s'arrete vite

Une erreur de validation n'est JAMAIS reprise: rejouer trois fois un CSV
invalide ne fait que retarder le diagnostic.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import ConfigurationError, IngestionError, InsufficientDataError, MervioError
from ..persistence.errors import (
    ImportRejected, JobLeaseLost, NotFound, PayloadRejected, PermissionDenied, SnapshotIntegrityError,
    TenantAccessDenied, UnsafeDatabaseConfiguration,
)
from ..persistence.retry import database_error_code, retryable_database_error


class JobExecutionError(MervioError):
    """Echec d'un gestionnaire de travail, avec son code et sa reprenabilite."""

    retryable = False

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class RetryableJobError(JobExecutionError):
    """Echec transitoire: le travail retourne en file si des tentatives restent."""

    retryable = True


class PermanentJobError(JobExecutionError):
    """Echec definitif: aucune reprise, quel que soit le nombre de tentatives restant."""

    retryable = False


#: Erreurs dont la nature definitive est etablie: la demande est fautive, pas l'instant.
#: `FileNotFoundError` et ses voisines en font partie: une charge utile qui designe
#: une source absente designera la meme source absente au prochain essai. Une erreur
#: d'entree-sortie generique (`OSError` nu) reste inconnue, donc reprise.
_PERMANENT = (
    IngestionError, InsufficientDataError, ConfigurationError, NotFound, TenantAccessDenied,
    PermissionDenied, PayloadRejected, ImportRejected, SnapshotIntegrityError,
    UnsafeDatabaseConfiguration, FileNotFoundError, IsADirectoryError, NotADirectoryError,
)


@dataclass(frozen=True)
class Failure:
    code: str
    retryable: bool
    message: str

    @property
    def error_type(self) -> str:
        return self.code


def classify(exc: BaseException) -> Failure:
    """Code publiable et reprenabilite d'une exception."""
    if isinstance(exc, JobLeaseLost):
        return Failure("lease_lost", False, "bail perdu")
    if isinstance(exc, JobExecutionError):
        return Failure(exc.code, exc.retryable, _message(exc))
    if isinstance(exc, _PERMANENT):
        return Failure(_code(exc), False, _message(exc))
    if retryable_database_error(exc):
        return Failure(database_error_code(exc), True, _message(exc))
    if getattr(exc, "sqlstate", None) is not None:
        return Failure(database_error_code(exc), False, _message(exc))
    if isinstance(exc, MervioError):
        return Failure(_code(exc), False, _message(exc))
    # inconnue: bornee par max_attempts
    return Failure(_code(exc), True, _message(exc))


def _code(exc: BaseException) -> str:
    """Nom de classe en minuscules_soulignes, sans donnee."""
    name = type(exc).__name__
    out = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)[:100]


def _message(exc: BaseException) -> str:
    """Message court et publiable. Jamais de trace, jamais de chemin absolu."""
    text = " ".join(str(exc).split())
    if text.startswith("/"):
        return "[redacted]"
    return text[:500]
