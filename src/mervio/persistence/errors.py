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
