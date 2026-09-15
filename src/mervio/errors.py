"""Exceptions du coeur Mervio.

Toutes les erreurs metier heritent de MervioError afin qu'une future couche
API (FastAPI) puisse les mapper proprement sur des codes HTTP.
"""
from __future__ import annotations

from typing import Iterable, Optional


class MervioError(Exception):
    """Erreur de base."""


class ConfigurationError(MervioError):
    """Configuration invalide."""


class IngestionError(MervioError):
    """Erreur pendant le chargement d'une source."""

    def __init__(self, message: str, *, source: str, row: Optional[int] = None) -> None:
        self.source = source
        self.row = row
        location = f" (source={source}" + (f", ligne={row}" if row is not None else "") + ")"
        super().__init__(message + location)


class MissingColumnsError(IngestionError):
    """Colonnes obligatoires absentes du CSV."""

    def __init__(self, missing: Iterable[str], *, source: str) -> None:
        self.missing = sorted(missing)
        super().__init__(
            "Colonnes obligatoires manquantes: " + ", ".join(self.missing),
            source=source,
        )


class InvalidDataError(IngestionError):
    """Valeur non exploitable dans une colonne obligatoire."""


class InsufficientDataError(MervioError):
    """Pas assez d'historique pour produire l'analyse demandee."""
