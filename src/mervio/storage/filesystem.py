"""Pilote systeme de fichiers CONFINE a une racine configuree (D-055, D-061).

CONFINEMENT EN QUATRE COUCHES (D-061), du plus fort au plus faible:

    1. la cle est GENEREE cote serveur; un appelant n'en fournit jamais (D-054);
    2. `validate_key` refuse toute cle hors forme canonique AVANT toute operation:
       `..`, `.`, chemin absolu, `\\`, `%`, NUL et tout separateur inattendu sont hors
       du jeu de caracteres autorise;
    3. `Path.resolve()` deroule TOUS les composants (y compris un repertoire parent
       lie), puis la cible resolue doit rester sous la racine resolue;
    4. un lien symbolique sur la cible finale est refuse explicitement.

Permissions: repertoires 0700, fichiers 0600, crees sans dependre de l'umask du processus.

TOCTOU: residuel assume et documente (D-061). Entre la resolution et l'ouverture, un attaquant
DISPOSANT DEJA du droit d'ecrire dans une racine 0700 pourrait substituer la cible. Le prerequis
suppose la compromission du compte de service; les cles sont des `uuid4` non predictibles; et le
worker monte la racine en LECTURE SEULE. `O_NOFOLLOW` n'est pas introduit (construction absente
du depot, gain marginal dans ce modele de menace).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from .base import ObjectKeyInvalid, ObjectNotFound, ObjectStoreError, PutResult, copy_and_digest, validate_key

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


class FilesystemObjectStore:
    """Objets sous une racine absolue configuree. Rien n'est jamais ecrit ni lu hors d'elle."""

    def __init__(self, root) -> None:
        root = Path(root)
        if not root.is_absolute():
            raise ObjectStoreError("racine du magasin d'objets: chemin absolu attendu")
        self.root = root

    # -- confinement -----------------------------------------------------------------------
    def _confined(self, key: str) -> Path:
        """Couches 2 et 3: forme de la cle, puis appartenance a la racine resolue."""
        validate_key(key)
        candidate = self.root.joinpath(*key.split("/"))
        root = self.root.resolve()
        resolved = candidate.resolve()  # deroule un parent lie: il sortirait de la racine
        if resolved != root and root not in resolved.parents:
            raise ObjectKeyInvalid("cle hors de la racine configuree")
        if candidate.is_symlink():  # couche 4: jamais a travers un lien
            raise ObjectKeyInvalid("cible liee refusee")
        return candidate

    def _make_parents(self, path: Path) -> None:
        """Repertoires 0700 quel que soit l'umask (mkdir applique l'umask, chmod non)."""
        missing = [parent for parent in path.parents
                   if self.root in parent.parents or parent == self.root]
        for parent in reversed(missing):
            if not parent.exists():
                parent.mkdir(mode=DIRECTORY_MODE, exist_ok=True)
                parent.chmod(DIRECTORY_MODE)

    # -- contrat ---------------------------------------------------------------------------
    def put(self, key: str, source: BinaryIO) -> PutResult:
        path = self._confined(key)
        self._make_parents(path)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            return copy_and_digest(source, handle.write)  # ne ferme pas `source`

    @contextmanager
    def open(self, key: str) -> Iterator[BinaryIO]:
        path = self._confined(key)
        try:
            handle = path.open("rb")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise ObjectNotFound() from None
        try:
            yield handle
        finally:
            handle.close()


__all__ = ["DIRECTORY_MODE", "FILE_MODE", "FilesystemObjectStore"]
