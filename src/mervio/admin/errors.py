"""Erreurs publiables de l'administration et codes de sortie de `mervio admin` (Mission 004.3.7).

Bibliotheque standard uniquement: la CLI importe ce module sans charger la persistance.

Une erreur porte un code court stable et un message SANS donnee d'un autre tenant, sans secret,
sans chemin et sans message du serveur de base. Une organisation inexistante et une organisation
dont l'acteur n'est pas membre donnent la MEME erreur (`not_found`, "organization introuvable").

    0 succes            1 erreur interne          2 usage ou configuration invalide
    3 base injoignable  4 schema non migre        5 introuvable (ou hors de portee)
    6 refuse (role)     7 conflit avec l'etat existant
"""
from __future__ import annotations

from ..errors import MervioError

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_DATABASE = 3
EXIT_SCHEMA = 4
EXIT_NOT_FOUND = 5
EXIT_DENIED = 6
EXIT_CONFLICT = 7


class AdminError(MervioError):
    """Echec publiable d'une commande d'administration."""

    code = "internal"
    exit_code = EXIT_INTERNAL

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class InvalidInput(AdminError):
    code = "invalid_input"
    exit_code = EXIT_USAGE


class ConfigurationRefused(AdminError):
    code = "config"
    exit_code = EXIT_USAGE


class DatabaseUnavailable(AdminError):
    code = "database_unavailable"
    exit_code = EXIT_DATABASE


class SchemaMissing(AdminError):
    code = "schema_not_ready"
    exit_code = EXIT_SCHEMA


class NotFoundError(AdminError):
    code = "not_found"
    exit_code = EXIT_NOT_FOUND


class Forbidden(AdminError):
    code = "forbidden"
    exit_code = EXIT_DENIED


class Conflict(AdminError):
    code = "conflict"
    exit_code = EXIT_CONFLICT


__all__ = [
    "EXIT_CONFLICT", "EXIT_DATABASE", "EXIT_DENIED", "EXIT_INTERNAL", "EXIT_NOT_FOUND", "EXIT_OK", "EXIT_SCHEMA",
    "EXIT_USAGE", "AdminError", "ConfigurationRefused", "Conflict", "DatabaseUnavailable", "Forbidden",
    "InvalidInput", "NotFoundError", "SchemaMissing",
]
