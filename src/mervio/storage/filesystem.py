"""Pilote systeme de fichiers CONFINE a une racine configuree (D-055, D-061, D-064).

CONFINEMENT EN QUATRE COUCHES (D-061), du plus fort au plus faible:

    1. la cle est GENEREE cote serveur; un appelant n'en fournit jamais (D-054);
    2. `validate_key` refuse toute cle hors forme canonique AVANT toute operation:
       `..`, `.`, chemin absolu, `\\`, `%`, NUL et tout separateur inattendu sont hors
       du jeu de caracteres autorise;
    3. `Path.resolve()` deroule TOUS les composants (y compris un repertoire parent
       lie), puis la cible resolue doit rester sous la racine resolue;
    4. un lien symbolique sur la cible finale est refuse explicitement -- et, en LECTURE,
       l'ouverture elle-meme est faite avec `O_NOFOLLOW` (voir ci-dessous).

Permissions: repertoires 0700, fichiers 0600, crees sans dependre de l'umask du processus.

TOCTOU (D-064 amende D-061 sur ce point). D-061 avait omis `O_NOFOLLOW` en s'appuyant sur trois
motifs, dont "la racine est montee en LECTURE SEULE pour le worker". Ce motif n'existe plus:
D-056 exige que le worker DETRUISE les octets, donc D-064 ouvre son montage en ecriture. Un worker
compromis peut desormais planter un lien symbolique sous la racine, c'est-a-dire remplir la
precondition meme de la fenetre TOCTOU que D-061 avait acceptee. La couche 4 cesse donc d'etre un
controle AVANT l'ouverture, sujet a la course, pour devenir une propriete DE L'OUVERTURE: le
chemin de LECTURE ouvre avec `O_NOFOLLOW`, et un lien substitue entre la resolution et l'ouverture
fait echouer l'ouverture (`ELOOP`) au lieu d'etre suivi.

Perimetre assume: `O_NOFOLLOW` porte sur la LECTURE seule, comme D-064 le ratifie. `put` conserve
son controle prealable de la couche 4 sans `O_NOFOLLOW`; le durcissement du chemin d'ecriture
touche a la semantique de non-ecrasement de D-055 (reportee a 004.9) et reste hors de ce perimetre.
`O_NOFOLLOW` ne concerne que le DERNIER composant: un repertoire parent lie restant sous la racine
demeure tolere, exactement comme D-061 l'avait arbitre.

CONTRAT D'ERREUR TOTAL (D-064): aucun `OSError` ne franchit ce pilote. Chaque echec systeme est
traduit en `ObjectStoreError` (ou en la sous-classe semantique deja prevue), en conservant le nom
`errno` -- qui est une CLASSE d'erreur, pas une valeur -- et en n'interpolant JAMAIS le chemin.
La redaction en aval (`jobs._safe_error`) reste en place; elle n'est plus la seule barriere.
"""
from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from .base import (
    DELETE_CAPABLE, DELETE_INCAPABLE, ObjectKeyInvalid, ObjectNotFound, ObjectStoreError, PutResult,
    copy_and_digest, validate_key,
)

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
#: Absent de Windows; le depot ne cible que Linux et macOS, mais l'absence ne doit pas casser
#: l'import du module -- elle degrade seulement la couche 4 a son controle prealable.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
#: `errno` d'un lien symbolique refuse par `O_NOFOLLOW`. Linux et macOS rendent tous deux `ELOOP`
#: (verifie). `EMLINK`, que FreeBSD emploie, n'est PAS inclus: il signifie "trop de liens" et
#: l'ajouter masquerait un echec sans rapport sur les plateformes ciblees.
_SYMLINK_ERRNOS = {errno.ELOOP}


def _failure(exc: OSError, *, what: str) -> ObjectStoreError:
    """Echec systeme traduit SANS valeur: le nom `errno` seul, jamais le chemin (D-064)."""
    name = errno.errorcode.get(exc.errno, "EIO") if exc.errno is not None else "EIO"
    return ObjectStoreError(f"magasin d'objets: {what} refuse par le systeme ({name})")


def _read_opener(path, flags: int) -> int:
    """Ouverture de LECTURE avec `O_NOFOLLOW`: un lien sur la cible finale echoue au lieu d'etre suivi.

    Passe a `open(..., opener=...)` -- la primitive INTEGREE, car `Path.open` n'expose pas
    `opener` -- plutot qu'utilise via `os.open` directement: `io.open` conserve alors ses propres
    refus, dont `IsADirectoryError`, qu'un `os.open` nu ne produirait PAS (ouvrir un repertoire en
    lecture reussit au niveau de l'appel systeme, et l'echec n'arriverait qu'a la premiere lecture).
    """
    return os.open(path, flags | _O_NOFOLLOW)


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
        try:
            root = self.root.resolve()
            resolved = candidate.resolve()  # deroule un parent lie: il sortirait de la racine
            linked = candidate.is_symlink()  # couche 4: jamais a travers un lien
        except OSError as exc:
            raise _failure(exc, what="resolution de cle") from None
        if resolved != root and root not in resolved.parents:
            raise ObjectKeyInvalid("cle hors de la racine configuree")
        if linked:
            raise ObjectKeyInvalid("cible liee refusee")
        return candidate

    def _make_parents(self, path: Path) -> None:
        """Repertoires 0700 quel que soit l'umask (mkdir applique l'umask, chmod non)."""
        missing = [parent for parent in path.parents
                   if self.root in parent.parents or parent == self.root]
        try:
            for parent in reversed(missing):
                if not parent.exists():
                    parent.mkdir(mode=DIRECTORY_MODE, exist_ok=True)
                    parent.chmod(DIRECTORY_MODE)
        except OSError as exc:
            raise _failure(exc, what="creation de repertoire") from None

    # -- contrat ---------------------------------------------------------------------------
    def put(self, key: str, source: BinaryIO) -> PutResult:
        path = self._confined(key)
        self._make_parents(path)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
            with os.fdopen(descriptor, "wb") as handle:
                return copy_and_digest(source, handle.write)  # ne ferme pas `source`
        except OSError as exc:
            raise _failure(exc, what="ecriture") from None

    @contextmanager
    def open(self, key: str) -> Iterator[BinaryIO]:
        path = self._confined(key)
        try:
            handle = open(path, "rb", opener=_read_opener)  # noqa: SIM115 - referme en `finally`
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise ObjectNotFound() from None
        except OSError as exc:
            # `O_NOFOLLOW` a refuse un lien substitue APRES la resolution: meme verdict que la
            # couche 4, pour que la course et le cas nominal soient indiscernables de l'appelant.
            if exc.errno in _SYMLINK_ERRNOS:
                raise ObjectKeyInvalid("cible liee refusee") from None
            raise _failure(exc, what="lecture") from None
        try:
            yield handle  # PAS dans le try de traduction: une erreur de l'appelant reste la sienne
        finally:
            handle.close()

    def delete(self, key: str) -> None:
        """Retire le fichier de CETTE cle, sous les memes quatre couches de confinement.

        `missing_ok=True` porte l'idempotence: une cle deja detruite n'est pas une erreur.
        Les repertoires parents sont LAISSES en place -- les retirer serait une suppression
        recursive, hors du contrat, et une course avec un depot concurrent sous la meme boutique.

        Un echec systeme (racine en lecture seule, permission retiree) devient `ObjectStoreError`
        et non un `OSError` nu: c'est ce qui permet au gestionnaire d'effacement de le classer
        `object_delete_failed` REPRENABLE et de laisser la ligne en `purging` (D-064).
        """
        path = self._confined(key)  # refuse une cle hors forme ET une cible liee
        try:
            if path.is_dir():
                # Une cle canonique ne designe jamais un repertoire. Rendre un succes ici
                # affirmerait une destruction qui n'a pas eu lieu; `unlink` leve d'ailleurs des
                # erreurs differentes selon la plateforme (EISDIR sur Linux, EPERM sur macOS).
                raise ObjectKeyInvalid("cle designant un repertoire")
            path.unlink(missing_ok=True)  # idempotence: une cle deja detruite n'est pas une erreur
        except OSError as exc:
            raise _failure(exc, what="destruction") from None

    def delete_capability(self) -> str:
        """Controle VIVANT et SANS EFFET DE BORD de la capacite de destruction (D-064).

        `unlink` exige le droit d'ECRITURE sur le repertoire parent; un montage en lecture seule
        ou une racine non inscriptible l'interdit. `os.access(..., W_OK | X_OK)` repond a cette
        question exacte sans rien creer ni detruire, et reflete l'etat REEL du montage -- ce n'est
        pas un drapeau declaratif. Un pseudo-controle par suppression d'une cle inexistante serait
        inutile: il rend `ENOENT` que le repertoire soit inscriptible ou non.

        La racine peut ne pas exister encore (`put` la cree): on interroge alors le premier ancetre
        existant, qui est celui ou la creation aurait lieu.
        """
        probe = self.root
        try:
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            writable = os.access(probe, os.W_OK | os.X_OK)
        except OSError:
            return DELETE_INCAPABLE
        return DELETE_CAPABLE if writable else DELETE_INCAPABLE


__all__ = ["DIRECTORY_MODE", "FILE_MODE", "FilesystemObjectStore"]
