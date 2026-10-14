"""Fabrique de jetons pour les tests d'authentification (Mission 004.6, tranche F).

AUCUN RESEAU, AUCUN FOURNISSEUR. Les cles sont generees a l'execution et le JWKS est servi par
un objet en memoire: la matrice de securite ne depend ni de Keycloak, ni d'Internet, ni d'un
fichier de cle dans le depot. C'est la meme discipline que `llm/`, dont une garde interdit
`socket`/`http`/`httpx`.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.utils import to_base64url_uint

ISSUER = "https://idp.test.invalid/realms/mervio"
AUDIENCE = "mervio-api"


class Key:
    """Une cle de signature et sa representation JWK publique."""

    def __init__(self, key_id: str, algorithm: str) -> None:
        self.key_id = key_id
        self.algorithm = algorithm
        if algorithm == "RS256":
            self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        elif algorithm == "ES256":
            self.private = ec.generate_private_key(ec.SECP256R1())
        else:  # pragma: no cover - seules deux familles sont utilisees
            raise ValueError(algorithm)

    def jwk(self, *, declare_alg: Optional[str] = None) -> Dict[str, Any]:
        public = self.private.public_key()
        if self.algorithm == "RS256":
            numbers = public.public_numbers()
            document = {"kty": "RSA", "use": "sig", "kid": self.key_id,
                        "n": to_base64url_uint(numbers.n).decode(),
                        "e": to_base64url_uint(numbers.e).decode()}
        else:
            numbers = public.public_numbers()
            document = {"kty": "EC", "use": "sig", "crv": "P-256", "kid": self.key_id,
                        "x": to_base64url_uint(numbers.x).decode().rjust(43, "A")[:43],
                        "y": to_base64url_uint(numbers.y).decode().rjust(43, "A")[:43]}
            # `to_base64url_uint` elague les zeros de tete; P-256 exige 32 octets exacts.
            document["x"] = _fixed(numbers.x)
            document["y"] = _fixed(numbers.y)
        alg = declare_alg if declare_alg is not None else self.algorithm
        if alg:
            document["alg"] = alg
        return document


def _fixed(value: int, size: int = 32) -> str:
    import base64
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).rstrip(b"=").decode()


class MemoryJwks:
    """Source de cles en memoire qui MODELISE UN CACHE, comme `HttpJwksSource`.

    `jwks()` rend l'instantane courant; `refresh()` le reprend depuis les cles vivantes. Sans
    ce cache, une rotation serait visible sans relecture et le test de rotation ne prouverait
    rien. Les relectures sont comptees, pour verifier qu'il y en a exactement UNE.
    """

    def __init__(self, keys: List[Key]) -> None:
        self._keys = list(keys)
        self._snapshot = self._document()
        self.refreshes = 0

    def _document(self) -> Dict[str, Any]:
        return {"keys": [k.jwk() for k in self._keys]}

    def set_keys(self, keys: List[Key]) -> None:
        """Change les cles VIVANTES sans toucher a l'instantane: c'est une rotation."""
        self._keys = list(keys)

    def jwks(self) -> Mapping[str, Any]:
        return self._snapshot

    def refresh(self) -> Mapping[str, Any]:
        self.refreshes += 1
        self._snapshot = self._document()
        return self._snapshot


class RawJwks:
    """Source qui rend un document ARBITRAIRE, pour les JWKS malformes."""

    def __init__(self, document: Any) -> None:
        self._document = document
        self.refreshes = 0

    def jwks(self) -> Any:
        return self._document

    def refresh(self) -> Any:
        self.refreshes += 1
        return self._document


def token(key: Key, *, subject: str = "idp|alice", issuer: str = ISSUER,
          audience: str = AUDIENCE, algorithm: Optional[str] = None,
          now: Optional[int] = None, lifetime: int = 300, not_before: Optional[int] = None,
          issued_at: Optional[int] = None, drop: tuple = (), extra: Optional[dict] = None,
          key_id: Optional[str] = ...) -> str:
    """Forge un jeton. Chaque parametre existe pour qu'un test puisse violer UNE regle."""
    moment = int(time.time()) if now is None else now
    claims: Dict[str, Any] = {
        "iss": issuer, "aud": audience, "sub": subject,
        "iat": moment if issued_at is None else issued_at,
        "nbf": moment if not_before is None else not_before,
        "exp": moment + lifetime,
    }
    for claim in drop:
        claims.pop(claim, None)
    claims.update(extra or {})
    headers = {}
    if key_id is not ...:
        headers["kid"] = key_id
    else:
        headers["kid"] = key.key_id
    if headers.get("kid") is None:
        headers.pop("kid")
    return jwt.encode(claims, key.private, algorithm=algorithm or key.algorithm, headers=headers)


def unsigned_token(subject: str = "idp|alice") -> str:
    """Jeton `alg: none`, forge a la main: `pyjwt` refuse de le produire."""
    import base64
    import json

    def part(document: dict) -> str:
        raw = json.dumps(document, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    moment = int(time.time())
    header = part({"alg": "none", "typ": "JWT", "kid": "k1"})
    claims = part({"iss": ISSUER, "aud": AUDIENCE, "sub": subject,
                   "iat": moment, "nbf": moment, "exp": moment + 300})
    return f"{header}.{claims}."


def hmac_token(subject: str = "idp|alice", secret: str = "une-cle-symetrique-assez-longue-pour-sha256") -> str:
    """Jeton HS256: une cle symetrique partagee n'est pas une signature verifiable."""
    moment = int(time.time())
    return jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": subject,
                       "iat": moment, "nbf": moment, "exp": moment + 300},
                      secret, algorithm="HS256", headers={"kid": "k1"})
