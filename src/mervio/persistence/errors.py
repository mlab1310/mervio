"""Erreurs de la couche de persistance.

Regle d'isolation: une ressource d'un autre tenant et une ressource inexistante
levent la MEME erreur (NotFound), pour ne jamais reveler l'existence d'une
donnee d'une autre organisation (futur 404, ADR-004-003).
"""
from __future__ import annotations

from ..errors import MervioError


class PersistenceError(MervioError):
    """Erreur de base de la persistance."""


class NotFound(PersistenceError):
    """Ressource absente OU hors du tenant courant (indiscernable par construction)."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"{kind} introuvable")


class TenantAccessDenied(NotFound):
    """L'utilisateur n'appartient pas a l'organisation demandee (vu comme une organisation introuvable)."""

    def __init__(self) -> None:
        super().__init__("organization")


class PermissionDenied(PersistenceError):
    """Membre de l'organisation, mais role insuffisant pour l'operation."""

    def __init__(self, permission: str, role: str) -> None:
        self.permission = permission
        self.role = role
        super().__init__(f"role {role} insuffisant pour {permission}")


class UnsafeDatabaseConfiguration(PersistenceError):
    """Connexion refusee: role superutilisateur ou BYPASSRLS, encodage non UTF-8, URL absente."""


class MoneyPrecisionError(PersistenceError):
    """Montant non representable exactement en numeric(19,4): refuse, jamais arrondi."""


class SnapshotIntegrityError(PersistenceError):
    """Dataset incoherent avec le modele persiste, ou instantane inutilisable."""


class ImportRejected(PersistenceError):
    """Import refuse (validation, devise incompatible, connexion revoquee)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class JobStateError(PersistenceError):
    """Transition de travail impossible (etat courant incompatible)."""


class JobLeaseLost(PersistenceError):
    """Le bail du travail a expire et un autre worker l'a repris: le resultat est refuse.

    Consequence assumee de l'execution AU MOINS une fois: le travail sera refait,
    et l'idempotence de 004.1 empeche la double ecriture.
    """

    def __init__(self, job_id) -> None:
        self.job_id = job_id
        super().__init__("bail du travail perdu: resultat refuse")


class PayloadRejected(PersistenceError):
    """Charge utile de travail refusee (cle sensible, valeur trop grande, forme invalide)."""
