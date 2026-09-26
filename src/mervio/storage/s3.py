"""Pilote compatible S3 (D-055): `boto3` dans un extra optionnel, importe A LA DEMANDE.

Teste avec `moto` en processus, en CI: aucun compte AWS, aucun reseau, aucun endpoint externe.

Hors perimetre (reportes a 004.9 par D-055): validation contre le fournisseur reel, ECRITURE
CONDITIONNELLE, chiffrement cote serveur, versionnement et cycle de vie du bucket. Ce pilote
n'offre donc, comme les deux autres, AUCUNE garantie de non-ecrasement (D-061).

`delete` (004.4.5) n'introduit AUCUNE garantie propre au fournisseur: pas de suppression
conditionnelle, pas de gestion de version. Sur un bucket VERSIONNE, `delete_object` pose un
marqueur et les versions anterieures survivent -- la destruction reelle des octets releve alors
du cycle de vie du bucket, reporte a 004.9. Le pilote de reference de 004.4.5 est le systeme de
fichiers confine, ou la destruction est immediate.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import BinaryIO, Iterator, Optional

from ..errors import ConfigurationError
from .base import (
    CHUNK_SIZE, DELETE_UNDETERMINED, ObjectNotFound, ObjectStoreError, PutResult, validate_key,
)


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


def _provider_code(exc: BaseException) -> Optional[str]:
    """Code d'erreur de la REPONSE du fournisseur, jamais le nom de classe ni le message.

    botocore fabrique une SOUS-CLASSE par code d'erreur (`NoSuchKey`, ...) plutot qu'un
    `ClientError` nu: on lit donc la reponse. Les classes de boto3 n'existent qu'avec l'extra,
    d'ou l'absence de `except ClientError` dans ce module.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        return str(code) if code else None
    return None


def _failure(exc: BaseException, *, what: str) -> ObjectStoreError:
    """Echec fournisseur traduit SANS valeur (D-064).

    Seul le CODE de reponse est publie -- une classe d'erreur, pas une donnee. Le message natif de
    botocore est ecarte deliberement: il cite couramment le bucket, et parfois l'endpoint ou l'ARN.
    Ni bucket, ni endpoint, ni cle, ni identifiant ne franchissent donc cette frontiere.
    """
    code = _provider_code(exc) or "unknown"
    return ObjectStoreError(f"magasin d'objets: {what} refuse par le fournisseur (s3:{code})")


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
        try:
            self._client.upload_fileobj(reader, self.bucket, key)
        except ObjectStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - les classes boto3 n'existent qu'avec l'extra
            raise _failure(exc, what="ecriture") from None
        return PutResult(reader.sha256, reader.byte_size)

    @contextmanager
    def open(self, key: str) -> Iterator[BinaryIO]:
        validate_key(key)
        try:
            body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"]
        except Exception as exc:  # noqa: BLE001 - les classes boto3 n'existent qu'avec l'extra
            if _provider_code(exc) in _MISSING_CODES:
                raise ObjectNotFound() from None
            # contrat total (D-064): aucune exception native ne franchit l'abstraction
            raise _failure(exc, what="lecture") from None
        handle = _ClosingBody(body)
        try:
            yield handle
        finally:
            handle.close()

    def delete(self, key: str) -> None:
        """`delete_object` sur UNE cle. S3 est deja idempotent: une cle absente rend un succes.

        Ni `delete_objects` (lot), ni suppression par prefixe, ni versionnement: la seule unite
        de destruction est la cle. Un `NoSuchBucket` reste une VRAIE erreur et remonte -- c'est une
        erreur de configuration, pas une destruction deja faite -- mais il remonte desormais en
        `ObjectStoreError`, jamais en exception botocore nue (D-064).
        """
        validate_key(key)
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except ObjectStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - les classes boto3 n'existent qu'avec l'extra
            raise _failure(exc, what="destruction") from None

    def delete_capability(self) -> str:
        """`undetermined`, et c'est la seule reponse honnete (D-064 point 5).

        Prouver `s3:DeleteObject` exigerait de l'APPELER. Aucun controle non destructif n'existe:
        un `delete_object` sur une cle inexistante reussit sans rien detruire sur un bucket non
        versionne, mais pose un MARQUEUR DE SUPPRESSION sur un bucket versionne -- donc un effet de
        bord -- et `head_bucket` ou `list_objects` ne prouvent rien de la permission de suppression.
        Rendre `capable` serait un drapeau declaratif susceptible de deriver de la politique IAM
        reelle; c'est precisement ce que D-064 interdit.

        Le preflight du worker ACCEPTE cette valeur et la journalise. La verification IAM etroite
        (`s3:DeleteObject` accorde separement de `s3:PutObject`, restreint par prefixe) est
        l'exigence de production enregistree par D-064 point 5 et reportee a 004.9.
        """
        return DELETE_UNDETERMINED


__all__ = ["S3ObjectStore"]
