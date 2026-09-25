"""Pilote memoire (D-055): existe pour exercer EXACTEMENT le meme contrat que les autres.

Aucune dependance au systeme de fichiers. Memes erreurs, meme comportement de flux, meme
absence de garantie de non-ecrasement (D-061).
"""
from __future__ import annotations

import io
import threading
from contextlib import contextmanager
from typing import BinaryIO, Dict, Iterator

from .base import ObjectNotFound, PutResult, copy_and_digest, validate_key


class MemoryObjectStore:
    """Magasin en memoire, protege par un verrou: deux appels concurrents restent coherents."""

    def __init__(self) -> None:
        self._objects: Dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, key: str, source: BinaryIO) -> PutResult:
        validate_key(key)
        buffer = io.BytesIO()
        result = copy_and_digest(source, buffer.write)  # ne ferme pas `source`
        with self._lock:
            self._objects[key] = buffer.getvalue()
        return result

    @contextmanager
    def open(self, key: str) -> Iterator[BinaryIO]:
        validate_key(key)
        with self._lock:
            payload = self._objects.get(key)
        if payload is None:
            raise ObjectNotFound()
        handle = io.BytesIO(payload)
        try:
            yield handle
        finally:
            handle.close()

    def delete(self, key: str) -> None:
        """Retire CETTE entree. `pop(..., None)`: une cle absente n'est pas une erreur."""
        validate_key(key)
        with self._lock:
            self._objects.pop(key, None)


__all__ = ["MemoryObjectStore"]
