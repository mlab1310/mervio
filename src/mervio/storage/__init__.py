"""Magasin d'objets bruts (Mission 004.4.4, D-054, D-055, D-061).

Un seul contrat, trois pilotes: memoire (tests), systeme de fichiers confine (defaut local,
CI et compose) et S3 (`boto3` dans l'extra optionnel `[s3]`, importe a la demande).
"""
from __future__ import annotations

from .base import (
    CHUNK_SIZE, DELETE_CAPABILITIES, DELETE_CAPABLE, DELETE_INCAPABLE, DELETE_UNDETERMINED,
    OBJECT_KEY_PATTERN, ObjectKeyInvalid, ObjectNotFound, ObjectStore, ObjectStoreError,
    PutResult, build_object_key, validate_key,
)
from .filesystem import FilesystemObjectStore
from .memory import MemoryObjectStore


def build_object_store(settings) -> ObjectStore:
    """Construit le pilote decrit par `ObjectStoreSettings` (004.4.4).

    `boto3` n'est importe QUE si le pilote `s3` est demande (D-055): l'extra reste optionnel.
    """
    if settings is None:
        raise ObjectStoreError("magasin d'objets non configure")
    if settings.driver == "filesystem":
        return FilesystemObjectStore(settings.root)
    if settings.driver == "memory":
        return MemoryObjectStore()
    if settings.driver == "s3":
        from .s3 import S3ObjectStore  # import a la demande
        return S3ObjectStore(settings.bucket, endpoint_url=settings.endpoint_url,
                             region_name=settings.region)
    raise ObjectStoreError(f"pilote de magasin d'objets inconnu: {settings.driver}")

__all__ = [
    "CHUNK_SIZE", "DELETE_CAPABILITIES", "DELETE_CAPABLE", "DELETE_INCAPABLE", "DELETE_UNDETERMINED",
    "OBJECT_KEY_PATTERN", "FilesystemObjectStore", "MemoryObjectStore",
    "ObjectKeyInvalid", "ObjectNotFound", "ObjectStore", "ObjectStoreError", "PutResult",
    "build_object_key", "build_object_store", "validate_key",
]
