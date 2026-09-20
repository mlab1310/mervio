"""Pilote compatible S3 (D-055): `boto3` dans un extra optionnel, importe A LA DEMANDE.

Teste avec `moto` en processus, en CI: aucun compte AWS, aucun reseau, aucun endpoint externe.

Hors perimetre (reportes a 004.9 par D-055): validation contre le fournisseur reel, ECRITURE
CONDITIONNELLE, chiffrement cote serveur, versionnement et cycle de vie du bucket. Ce pilote
n'offre donc, comme les deux autres, AUCUNE garantie de non-ecrasement (D-061).
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import BinaryIO, Iterator, Optional

from ..errors import ConfigurationError
from .base import CHUNK_SIZE, ObjectNotFound, PutResult, validate_key


def _boto3():
    """Import a la demande: choisir `s3` sans l'extra installe est une erreur de configuration."""
    try:
        import boto3  # noqa: PLC0415 - import differe voulu (D-055)
    except ImportError:  # pragma: no cover - depend de l'environnement
        raise ConfigurationError(
            "pilote de stockage 's3' choisi mais l'extra [s3] n'est pas installe (boto3 absent)"
        ) from None
    return boto3


class _DigestingReader:
    """Lecteur a UN SEUL passage avant: hache et compte pendant que le client S3 consomme.

    `seekable()` renvoie False deliberement: `s3transfer` emprunte alors un chemin strictement
    sequentiel et ne relit jamais en arriere, ce qui garantit que l'empreinte decrit exactement
    les octets televerses. La memoire reste bornee.
    """

    def __init__(self, source: BinaryIO) -> None:
        self._source = source
        self._digest = hashlib.sha256()
        self.byte_size = 0

    def read(self, size: int = -1) -> bytes:
        # `s3transfer` demande des blocs de 8 Mio: on BORNE a CHUNK_SIZE et on rend une lecture
        # courte (legitime pour un objet fichier), pour que la memoire du pilote S3 soit bornee
        # exactement comme celle des deux autres pilotes.
        wanted = CHUNK_SIZE if size is None or size < 0 else min(size, CHUNK_SIZE)
        chunk = self._source.read(wanted)
        if chunk:
            self._digest.update(chunk)
            self.byte_size += len(chunk)
        return chunk

    def seekable(self) -> bool:
        return False

    def readable(self) -> bool:
        return True

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


#: Codes S3 signifiant "aucun octet sous cette cle".
_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "404"})


class _ClosingBody:
    """Adaptateur mince: le corps renvoye par botocore n'expose pas d'etat `closed` fiable.

    Les trois pilotes doivent offrir la MEME semantique de fermeture (D-055): apres le bloc
    `with`, le handle est ferme et se declare tel.
    """

    def __init__(self, body) -> None:
        self._body = body
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("lecture sur un objet ferme")
        return self._body.read() if size is None or size < 0 else self._body.read(size)

    def readable(self) -> bool:
        return not self.closed

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._body.close()


class S3ObjectStore:
    """Objets dans un bucket compatible S3. Aucune logique de tenant ici (D-054)."""

    def __init__(self, bucket: str, *, endpoint_url: Optional[str] = None,
                 region_name: Optional[str] = None, client=None) -> None:
        if not bucket:
            raise ConfigurationError("bucket du magasin d'objets non configure")
        self.bucket = bucket
        #: identifiants: chaine native de boto3 (environnement, role d'instance).
        #: Mervio ne porte JAMAIS de secret de fournisseur dans ses reglages.
        self._client = client if client is not None else _boto3().client(
            "s3", endpoint_url=endpoint_url, region_name=region_name)

    def put(self, key: str, source: BinaryIO) -> PutResult:
        validate_key(key)
        reader = _DigestingReader(source)  # ne ferme pas `source`
        self._client.upload_fileobj(reader, self.bucket, key)
        return PutResult(reader.sha256, reader.byte_size)

    @contextmanager
    def open(self, key: str) -> Iterator[BinaryIO]:
        validate_key(key)
        try:
            body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"]
        except Exception as exc:  # noqa: BLE001 - les classes boto3 n'existent qu'avec l'extra
            # botocore fabrique une SOUS-CLASSE par code d'erreur (`NoSuchKey`, ...), pas un
            # `ClientError` nu: on lit donc la reponse, jamais le nom de classe.
            code = getattr(exc, "response", None)
            code = (code or {}).get("Error", {}).get("Code") if isinstance(code, dict) else None
            if code in _MISSING_CODES:
                raise ObjectNotFound() from None
            raise
        handle = _ClosingBody(body)
        try:
            yield handle
        finally:
            handle.close()


__all__ = ["S3ObjectStore"]
