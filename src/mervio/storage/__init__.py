"""Magasin d'objets bruts (Mission 004.4.4, D-054, D-055, D-061).

Un seul contrat, trois pilotes: memoire (tests), systeme de fichiers confine (defaut local,
CI et compose) et S3 (`boto3` dans l'extra optionnel `[s3]`, importe a la demande).
"""
from __future__ import annotations

from .base import (
    CHUNK_SIZE, OBJECT_KEY_PATTERN, ObjectKeyInvalid, ObjectNotFound, ObjectStore, ObjectStoreError,
    PutResult, build_object_key, validate_key,
)
from .filesystem import FilesystemObjectStore
from .memory import MemoryObjectStore

__all__ = [
    "CHUNK_SIZE", "OBJECT_KEY_PATTERN", "FilesystemObjectStore", "MemoryObjectStore",
    "ObjectKeyInvalid", "ObjectNotFound", "ObjectStore", "ObjectStoreError", "PutResult",
    "build_object_key", "validate_key",
]
