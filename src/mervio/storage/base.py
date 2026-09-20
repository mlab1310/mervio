"""Contrat du magasin d'objets bruts (Mission 004.4.4, D-054, D-055, D-061).

Octets en entree, octets en sortie. Un magasin ne connait NI l'organisation, NI la boutique,
NI la table `raw_objects`: il recoit une cle opaque, toujours generee cote serveur.

    Le magasin n'est PAS une frontiere de securite.
    La frontiere est la ligne `raw_objects` sous RLS (D-054).

NON-ECRASEMENT (D-061): `put` n'offre AUCUNE garantie de non-ecrasement, et il n'existe pas
d'erreur `ObjectAlreadyExists`. Sur S3, la seule garantie reelle serait une ECRITURE
CONDITIONNELLE (`IfNoneMatch`), que D-055 reporte explicitement a 004.9; un `head_object` suivi
d'un `put_object` serait sujet a une course et ne garantirait rien. La propriete est donc retiree
des TROIS pilotes: l'interface reste identique pour tous (exigence centrale de D-055). L'unicite
est portee par les cles `uuid4` et par `UNIQUE (organization_id, object_key)` en base.

Hors perimetre 004.4.4: `stat()` et `delete()`. La destruction d'octets releve de D-056 (004.4.5)
et le ramassage des orphelins de 004.9.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO, Iterator, Protocol, runtime_checkable
from uuid import UUID

from ..errors import MervioError

#: Taille de bloc de toute copie et de tout hachage: la memoire reste bornee quelle que soit
#: la taille de l'objet (meme idiome que `persistence.codec.file_sha256`).
CHUNK_SIZE = 1 << 20

#: Forme canonique d'une cle (roadmap 004.4 item 5). Aucun nom de fichier, aucune extension,
#: aucun type MIME, aucune donnee d'appelant: seulement des identifiants generes.
#: La MEME expression est imposee par la contrainte `raw_objects_key_format` de la revision 0013:
#: un pilote et la base acceptent donc exactement les memes cles.
OBJECT_KEY_PATTERN = r"^org/[0-9a-f-]{36}/store/[0-9a-f-]{36}/raw/[0-9a-f-]{36}$"
_OBJECT_KEY_RE = re.compile(OBJECT_KEY_PATTERN)


class ObjectStoreError(MervioError):
    """Echec du magasin d'objets."""


class ObjectKeyInvalid(ObjectStoreError):
    """Cle hors de la forme canonique: aucune operation de fichier n'est tentee."""

    def __init__(self, reason: str = "forme de cle refusee") -> None:
        super().__init__(reason)


class ObjectNotFound(ObjectStoreError):
    """Aucun octet sous cette cle."""

    def __init__(self) -> None:
        super().__init__("objet introuvable")


@dataclass(frozen=True)
class PutResult:
    """Empreinte et taille des octets REELLEMENT ecrits, calculees en un seul passage."""

    sha256: str
    byte_size: int


def build_object_key(organization_id: UUID, store_id: UUID) -> str:
    """Cle generee COTE SERVEUR. L'appelant n'en controle aucun segment (D-054).

    `organization_id` et `store_id` viennent de la `TenantSession`, jamais d'une entree;
    le dernier segment est un `uuid4` tire ici.
    """
    return f"org/{organization_id}/store/{store_id}/raw/{uuid.uuid4()}"


def validate_key(key: object) -> str:
    """Refuse toute cle hors forme canonique AVANT toute operation.

    Elimine par construction `..`, `.`, les chemins absolus, `\\`, `%`, l'octet NUL et
    tout separateur inattendu: aucun de ces caracteres n'appartient au jeu autorise.
    """
    if not isinstance(key, str) or not _OBJECT_KEY_RE.match(key):
        raise ObjectKeyInvalid()
    return key


def copy_and_digest(source: BinaryIO, sink) -> PutResult:
    """Copie `source` vers `sink` par blocs bornes, en calculant empreinte et taille au vol.

    Ne ferme PAS `source`: il appartient a l'appelant qui l'a ouvert.
    """
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        sink(chunk)
    return PutResult(digest.hexdigest(), size)


@runtime_checkable
class ObjectStore(Protocol):
    """Interface commune aux trois pilotes (D-055). Une seule suite de contrat les couvre."""

    def put(self, key: str, source: BinaryIO) -> PutResult:
        """Ecrit `source` sous `key`, en un passage, et renvoie empreinte et taille.

        Ne ferme pas `source`. Leve `ObjectKeyInvalid` si la cle est hors forme.
        """
        ...  # pragma: no cover - protocole

    @contextmanager
    def open(self, key: str) -> Iterator[BinaryIO]:
        """Ouvre l'objet en lecture binaire; l'appelant referme (gestionnaire de contexte).

        Leve `ObjectKeyInvalid` ou `ObjectNotFound`.
        """
        ...  # pragma: no cover - protocole


__all__ = [
    "CHUNK_SIZE", "OBJECT_KEY_PATTERN", "ObjectKeyInvalid", "ObjectNotFound", "ObjectStore",
    "ObjectStoreError", "PutResult", "build_object_key", "copy_and_digest", "validate_key",
]
