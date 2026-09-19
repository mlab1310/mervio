"""Identite client a cle par organisation (Mission 004.4.2, D-053, D-058).

La reference client persistee n'est jamais une donnee directe: c'est un HMAC a cle.

    cle maitre        hors de PostgreSQL (MERVIO_IDENTITY_MASTER_KEY), jamais journalisee
    sel d'organisation 32 octets aleatoires en base (organization_identity_keys), destructible
    cle d'organisation HMAC-SHA256(maitre, "mervio:identity:v1:" || organization_id || ":" || sel)
                       gardee en memoire uniquement, jamais persistee, jamais serialisee
    customer_ref       "c1:" + 128 premiers bits de HMAC-SHA256(cle, "email:" || e-mail normalise)
                       "g1:" + 128 premiers bits de HMAC-SHA256(cle, "guest_order:" || identifiant de commande)

Chaque espace de noms est separe par son prefixe d'entree ("email:", "guest_order:"), ce qui
laisse la place a un identifiant de plateforme ("platform:<source>:<id>") le jour ou un
connecteur en fournit un (004.11), sans collision possible avec le repli e-mail.

Detruire le sel rend la cle d'organisation irrecuperable, meme pour qui detient la cle maitre.
Hors du chemin SaaS (CLI analytique locale), une cle EPHEMERE est tiree pour chaque execution:
les pseudonymes ne sont alors pas liables d'une execution a l'autre (D-058).

Chaque cle porte sa PROVENANCE (`organization`, `explicit`, `ephemeral`). Seule une cle
d'organisation, derivee de la cle maitre et du sel, peut alimenter un instantane persiste:
`persistence.snapshots.write_snapshot` refuse tout autre jeu de donnees (004.4.2, F-08).

Bibliotheque standard uniquement.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from pathlib import Path
from typing import Dict, Optional
from uuid import UUID

SCHEME = "hmac-sha256-v1"
#: longueur minimale de la cle maitre et longueur exacte d'une cle d'organisation
KEY_BYTES = 32
SALT_BYTES = 32
#: 128 bits de HMAC, en hexadecimal
REF_HEX = 32
EMAIL_PREFIX = "c1:"
GUEST_PREFIX = "g1:"
#: format de toute reference produite ici (impose aussi par la base, revision 0011)
REF_PATTERN = re.compile(r"^(c1|g1):[0-9a-f]{32}$")

_ORGANIZATION_DOMAIN = b"mervio:identity:v1:"
_MASTER_ID_DOMAIN = b"mervio:master-key-id:v1"
_KEY_ID_DOMAIN = b"mervio:identity-key-id:v1"
_EMAIL_DOMAIN = b"email:"
_GUEST_DOMAIN = b"guest_order:"
_HEX_KEY = re.compile(r"^[0-9a-fA-F]+$")
#: provenance d'une cle d'identite (F-08)
ORIGIN_ORGANIZATION = "organization"
ORIGIN_EXPLICIT = "explicit"
ORIGIN_EPHEMERAL = "ephemeral"


class IdentityKeyError(ValueError):
    """Cle absente, mal formee ou inutilisable. Le message ne porte jamais de valeur."""


def _hmac(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


def parse_hex_key(text: str, *, what: str = "cle") -> bytes:
    """Cle en hexadecimal, au moins 32 octets (64 caracteres). Jamais la valeur dans l'erreur."""
    value = (text or "").strip()
    if len(value) < 2 * KEY_BYTES or len(value) % 2 or not _HEX_KEY.match(value):
        raise IdentityKeyError(f"{what}: au moins {2 * KEY_BYTES} caracteres hexadecimaux attendus")
    return bytes.fromhex(value)


class _Opaque:
    """Porteur de materiel secret: jamais affiche, jamais serialise, jamais compare par repr."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}('[redacted]')"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("un materiel de cle ne se serialise pas")

    def __getstate__(self):
        raise TypeError("un materiel de cle ne se serialise pas")


class MasterKey(_Opaque):
    """Cle maitre d'identite. Vit hors de PostgreSQL; seul son identifiant non secret est publie."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) < KEY_BYTES:
            raise IdentityKeyError(f"cle maitre: au moins {KEY_BYTES} octets attendus")
        self._key = bytes(key)

    @classmethod
    def from_hex(cls, text: str) -> "MasterKey":
        return cls(parse_hex_key(text, what="cle maitre"))

    @property
    def key_id(self) -> str:
        """16 caracteres hexadecimaux, a sens unique: detecte un changement de cle maitre."""
        return _hmac(self._key, _MASTER_ID_DOMAIN).hex()[:16]

    def organization_identity(self, organization_id: UUID, salt: bytes) -> "CustomerIdentity":
        """Cle d'organisation, en memoire seulement."""
        if not isinstance(salt, (bytes, bytearray)) or len(salt) != SALT_BYTES:
            raise IdentityKeyError(f"sel d'organisation: {SALT_BYTES} octets attendus")
        organization = UUID(str(organization_id))
        message = _ORGANIZATION_DOMAIN + str(organization).encode("ascii") + b":" + bytes(salt)
        identity = CustomerIdentity(_hmac(self._key, message))
        identity._origin, identity._organization_id = ORIGIN_ORGANIZATION, organization
        return identity

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MasterKey) and hmac.compare_digest(self._key, other._key)

    def __hash__(self) -> int:
        return hash(("MasterKey", self.key_id))


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


class CustomerIdentity(_Opaque):
    """Calcule les references client d'UNE organisation (ou d'une execution CLI locale).

    Memoise par identite distincte: un calcul HMAC par client, pas par commande, sans I/O.
    """

    __slots__ = ("_key", "_memo", "_origin", "_organization_id")

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) < KEY_BYTES:
            raise IdentityKeyError(f"cle d'identite: au moins {KEY_BYTES} octets attendus")
        self._key = bytes(key)
        self._memo: Dict[bytes, str] = {}
        #: provenance de la cle (F-08): seule une cle d'ORGANISATION peut alimenter un instantane persiste
        self._origin = ORIGIN_EXPLICIT
        self._organization_id: Optional[UUID] = None

    @classmethod
    def ephemeral(cls) -> "CustomerIdentity":
        """Cle tiree pour une seule execution locale; jamais persistee (D-058)."""
        identity = cls(secrets.token_bytes(KEY_BYTES))
        identity._origin = ORIGIN_EPHEMERAL
        return identity

    @property
    def origin(self) -> str:
        """`organization` (derivee par `MasterKey.organization_identity`), `explicit` ou `ephemeral`."""
        return self._origin

    @property
    def organization_id(self) -> Optional[UUID]:
        """Organisation d'une cle d'organisation; None pour une cle locale (CLI)."""
        return self._organization_id

    @classmethod
    def from_key_file(cls, path) -> "CustomerIdentity":
        """Cle explicite (hexadecimal) lue dans un fichier: rapports CLI reproductibles."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            raise IdentityKeyError("fichier de cle d'identite illisible") from None
        return cls(parse_hex_key(text, what="fichier de cle d'identite"))

    def export_hex(self) -> str:
        """Materiel de cle en clair, pour ecrire un fichier `--identity-key-file` seulement."""
        return self._key.hex()

    @property
    def key_id(self) -> str:
        """Identifiant NON secret de la cle, 16 caracteres hexadecimaux (empreinte d'import, D-058)."""
        return _hmac(self._key, _KEY_ID_DOMAIN).hex()[:16]

    def _ref(self, prefix: str, message: bytes) -> str:
        cached = self._memo.get(message)
        if cached is None:
            cached = prefix + _hmac(self._key, message).hex()[:REF_HEX]
            self._memo[message] = cached
        return cached

    def ref_email(self, email: str) -> str:
        """Reference d'un client identifie par e-mail; l'e-mail ne sort jamais d'ici."""
        normalized = normalize_email(email)
        if normalized is None:
            raise IdentityKeyError("e-mail vide")
        return self._ref(EMAIL_PREFIX, _EMAIL_DOMAIN + normalized.encode("utf-8"))

    def ref_guest(self, order_id: str) -> str:
        """Reference d'une commande invitee: un client par commande, comme avant (D-044)."""
        normalized = (order_id or "").strip()
        if not normalized:
            raise IdentityKeyError("identifiant de commande vide")
        return self._ref(GUEST_PREFIX, _GUEST_DOMAIN + normalized.encode("utf-8"))

    def ref_customer(self, email: Optional[str], order_id: str) -> str:
        """Regle des connecteurs: e-mail s'il existe, sinon invite rattache a la commande."""
        return self.ref_email(email) if normalize_email(email) else self.ref_guest(order_id)


def normalize_email(email: Optional[str]) -> Optional[str]:
    """Contrat existant des connecteurs: espaces retires, minuscules; vide -> None."""
    value = (email or "").strip().lower()
    return value or None


__all__ = ["EMAIL_PREFIX", "GUEST_PREFIX", "KEY_BYTES", "ORIGIN_EPHEMERAL", "ORIGIN_EXPLICIT", "ORIGIN_ORGANIZATION",
           "REF_PATTERN", "SALT_BYTES", "SCHEME", "CustomerIdentity",
           "IdentityKeyError", "MasterKey", "new_salt", "normalize_email", "parse_hex_key"]
