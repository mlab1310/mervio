"""Verification des jetons OIDC (Mission 004.6, tranche F, D-098a).

CE QUE CE MODULE GARANTIT
Un jeton accepte ici a passe la TOTALITE du contrat de D-098a, pas seulement sa signature.
Une signature valide ne prouve que l'origine des octets; elle ne dit rien de l'emetteur
attendu, du destinataire, ni de la fenetre temporelle. Les trois sont verifies separement, et
chacun a son propre refus nomme -- de sorte qu'un echec DIT lequel des huit controles a cede.

INDEPENDANT DU FOURNISSEUR
Rien ici ne nomme Keycloak. L'emetteur et l'audience sont des reglages (`ApiSettings`), la
decouverte suit `/.well-known/openid-configuration`, et la verification repose sur `pyjwt`,
une implementation JWS GENERIQUE -- pas un SDK de fournisseur. Changer de fournisseur doit
couter deux variables d'environnement.

AUCUN RESEAU DANS LES TESTS
`JwksSource` est un seam: `HttpJwksSource` va chercher les cles, et un test en injecte une
autre implementation. C'est la meme discipline que `llm/`, dont une garde interdit
`socket`/`http`/`httpx`.

TROIS DUREES DISTINCTES, ET ELLES NE MESURENT PAS LA MEME CHOSE (F.2)
    cache_seconds        FRAICHEUR. Comptee depuis le dernier chargement REUSSI. Tant qu'elle
                         dure, les cles servent sans aucun appel sortant.
    max_stale_seconds    TOLERANCE, comptee EN PLUS de la fraicheur. Apres l'expiration et
                         pendant cette periode, un cache deja valide continue de servir des
                         cles CONNUES si l'emetteur est injoignable. Au-dela: refus.
    min_refresh_seconds  CADENCE des tentatives reseau. Independante par ce qu'elle MESURE:
                         elle ne decide jamais si une cle est acceptee, seulement quand on
                         ressaie. Independante par ses EFFETS, non -- voir plus bas.
La fenetre totale d'utilisabilite est donc `cache_seconds + max_stale_seconds` depuis le
dernier chargement REUSSI. Une tentative ECHOUEE ne deplace JAMAIS cet instant: sinon une panne
prolongee repousserait indefiniment l'expiration des cles, ce qui est exactement ce qu'une
revocation doit empecher.

CE QUE LA CADENCE IMPOSE A LA TOLERANCE, ET LA MESURE QUI LE MONTRE
La tolerance n'absorbe une panne SANS refus visible que si une tentative autorisee ET reussie
peut avoir lieu DANS la fenetre de tolerance. La premiere tentative a lieu a l'expiration de la
fraicheur; son echec marque `_attempted_at`, donc la suivante attend `min_refresh_seconds`. La
relation RECOMMANDEE est par consequent `max_stale_seconds >= min_refresh_seconds`.
Mesure, `cache_seconds=300`, trafic d'une requete par seconde, emetteur revenu PENDANT la
tolerance:
    60/30  retour a t=315, tentative a t=330 < 360  ->  ZERO refus, panne absorbee
    10/30  retour a t=305, tentative a t=330 > 310  ->  20 s de refus, t=310 a t=329
Dans les deux cas la fenetre vaut bien `cache_seconds + max_stale_seconds` (dernier accepte a
t=359 et t=309, premier refus a t=360 et t=310, `jwks_too_stale`) et le verdict reste MONOTONE
pendant la panne: zero transition refus -> acceptation. `max_stale_seconds < min_refresh_seconds`
reste donc LEGAL -- ce n'est pas une validation oubliee -- mais la tolerance s'y reduit a un
REPORT du refus, sans possibilite de retablissement pendant ce delai.

TROIS INSTANTS A NE PAS CONFONDRE DANS UN RETABLISSEMENT
    tentative AUTORISEE  au plus tard `min_refresh_seconds` apres la derniere tentative,
                         reussie ou non. C'est le SEUL des trois qui soit borne.
    tentative EFFECTIVE  a la premiere requete arrivee apres cet instant: il n'y a AUCUN
                         rafraichisseur en arriere-plan, la recuperation est tiree par le
                         trafic. Mesure, 10/30, aucune requete entre t=311 et t=400: aucun
                         appel sortant avant t=400.
    RETABLISSEMENT       a la premiere tentative effective qui REUSSIT. Si l'emetteur reste
                         indisponible, aucune borne ne s'applique: les refus durent autant
                         que la panne.

LE PAPILLONNEMENT DE F.1, ET POURQUOI IL EXISTAIT
F.1 servait le document rassis UNIQUEMENT dans la fenetre de refroidissement, et levait dans la
branche de tentative. Le verdict dependait donc de l'instant d'arrivee de la requete: celle qui
declenchait la tentative etait refusee, les suivantes acceptees. Mesure, `cache_seconds=60`,
`min_refresh_seconds=30`, emetteur en panne, 16 instants de t=60 a t=210: 10 acceptations et
6 refus, en ALTERNANCE de periode `min_refresh_seconds`. La documentation de F.1 affirmait par
ailleurs que le rassis etait servi << de facon coherente et borne par `cache_seconds` >>:
faux sur les deux points.
F.2 corrige la cause: les DEUX branches -- refroidissement et echec de tentative -- prennent
la MEME decision, `_tolerated()`. Le verdict ne depend plus que de l'AGE du dernier chargement
reussi, donc il est monotone: accepte tant que l'age tient dans la fenetre, refuse ensuite, et
jamais l'inverse.

CE QUE LA TOLERANCE NE FAIT PAS
Elle ne contourne AUCUNE verification. Une signature invalide, un algorithme interdit, un
mauvais `iss` ou `aud`, un claim manquant: tous refuses, cache rassis ou non. La tolerance ne
porte que sur la DISPONIBILITE de cles DEJA validees. Et un `kid` absent du cache n'est jamais
accepte: le cache ne contient pas sa cle, donc il n'y a rien a tolerer.

AMPLIFICATION SUR `kid` INCONNU: CORRIGEE EN F.1, ET LA MESURE QUI L'A IMPOSEE
La premiere version relisait le JWKS des qu'un `kid` manquait, SANS intervalle minimal, et
chaque relecture refaisait la decouverte OIDC. Mesure: 20 requetes a `kid` inconnu produisaient
**42 appels HTTP** vers l'emetteur, soit 2,1 par requete refusee -- devant un controle qui
precede toute authentification, donc sans aucune limitation de debit. Le commentaire d'origine
affirmait qu'UNE relecture par requete suffisait a borner le flot: c'etait faux, puisque le
flot est fait de requetes.
Trois corrections, chacune testee: le `jwks_uri` VALIDE est mis en cache, donc une relecture
coute un appel au lieu de deux; un INTERVALLE MINIMAL (`min_refresh_seconds`) separe deux
tentatives, donc un flot de `kid` inventes dans la fenetre ne sort pas du tout; et une
tentative ECHOUEE marque l'horodatage comme une reussie, ce qui empeche une boucle de
relectures sur un emetteur en panne.
L'asymetrie reste voulue -- on se depeche d'accepter une cle nouvelle, on ne se depeche pas
d'oublier une ancienne -- mais << se depecher >> est desormais borne: une rotation legitime est
vue au plus tard apres `min_refresh_seconds`.

TROUPEAU AU DEMARRAGE A FROID: CORRIGE EN F.1
Mesure: 8 requetes simultanees sur un cache vide declenchaient 8 recuperations, le verrou ne
protegeant que le cache et pas le fetch. Un verrou de RECUPERATION s'y ajoute, avec une
double verification: le gagnant va chercher le document, les perdants le trouvent frais en
sortant d'attente et le reutilisent. Une seule recuperation par emetteur.
Le chemin chaud ne prend AUCUN verrou de recuperation: les verifications JWT ne se serialisent
donc pas derriere lui une fois le cache rempli.

CE QUI N'EST PAS CONFIGURABLE, ET C'EST VOULU
Les algorithmes acceptes. `ALLOWED_ALGORITHMS` est une constante: une installation ne doit pas
pouvoir s'ouvrir a `none` ou a une famille HMAC par une variable d'environnement. Une cle
symetrique partagee avec l'emetteur n'est pas une signature verifiable, et `alg: none` n'est
pas une signature.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol
from urllib.parse import urlsplit

import jwt
from jwt import PyJWKSet

#: Les SEULS algorithmes acceptes. Constante, jamais un reglage -- voir l'en-tete.
ALLOWED_ALGORITHMS = ("RS256", "ES256")

#: Revendications exigees dans TOUS les cas. `iat` et `nbf` sont dans la liste: une fenetre
#: temporelle ouverte d'un cote n'est pas une fenetre.
REQUIRED_CLAIMS = ("iss", "aud", "exp", "iat", "nbf", "sub")

#: Prefixe reserve aux principaux de SERVICE (revision 0007). Un jeton humain ne peut pas le
#: porter: ce serait une escalade vers l'identite du worker par la frontiere HTTP.
SERVICE_SUBJECT_PREFIX = "service:"

#: Bornes du document de decouverte et du JWKS. Un emetteur compromis ou mal configure ne doit
#: pas pouvoir faire grandir notre memoire sans limite.
MAX_DOCUMENT_BYTES = 256 * 1024
#: Nombre maximal de cles retenues. Un JWKS honnete en porte quelques unes.
MAX_KEYS = 32
DEFAULT_HTTP_TIMEOUT = 5.0
#: Intervalle minimal entre deux TENTATIVES de recuperation, reussies ou non. Il borne a la
#: fois le flot de `kid` inventes et les boucles sur un emetteur en panne. 30 s par defaut:
#: assez court pour qu'une rotation legitime soit vue vite, assez long pour qu'un flot ne
#: sorte pas. Reglable par `MERVIO_OIDC_MIN_REFRESH_SECONDS`, borne a [1, 3600].
DEFAULT_MIN_REFRESH_SECONDS = 30.0
#: Tolerance SUPPLEMENTAIRE apres l'expiration de la fraicheur. Pendant cette periode, un
#: cache deja VALIDE continue de servir des cles CONNUES quand l'emetteur est injoignable --
#: et au-dela, l'authentification est refusee. 60 s par defaut, bornes [0, 3600]: `0` signifie
#: aucune tolerance, c'est-a-dire refus des l'expiration. Reglable par
#: `MERVIO_OIDC_MAX_STALE_SECONDS`.
DEFAULT_MAX_STALE_SECONDS = 60.0

#: Echecs de recuperation que la tolerance peut couvrir: ils disent que l'emetteur est
#: momentanement INJOIGNABLE ou illisible, pas qu'il est devenu suspect.
TOLERABLE_FAILURES = ("issuer_unreachable", "document_too_large", "document_not_json")
#: Tout le reste -- `insecure_discovery_url`, `jwks_uri_outside_issuer`,
#: `discovery_without_jwks_uri` -- est un echec d'INTEGRITE ou de CONFIGURATION, et il remonte
#: TEL QUEL, sans tolerance. Un document de decouverte altere ou un emetteur joint sans TLS
#: n'est pas un hoquet: continuer a servir en silence masquerait un probleme de confiance, et
#: l'operateur verrait `issuer_unreachable` la ou le defaut est ailleurs.
#: Compromis assume: un attaquant capable d'alterer la decouverte obtient un refus de service
#: plutot qu'une degradation silencieuse. C'est le sens choisi, parce qu'un `jwks_uri` qui
#: change d'hote peut aussi etre une migration LEGITIME -- et elle demande un humain.


class TokenRejected(Exception):
    """Jeton refuse. `reason` est un code STABLE, destine a l'observabilite.

    Le message n'est PAS destine au client: la frontiere HTTP rend un `problem+json`
    indifferencie (`unauthenticated`), parce que distinguer << mauvaise audience >> de
    << signature invalide >> aide un attaquant a calibrer sa prochaine tentative.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class VerifiedToken:
    """Ce qu'un jeton accepte apporte, et RIEN de plus.

    Le jeton complet n'est pas conserve: aucune couche au-dessus n'a besoin de le relire, et
    un objet qui le porterait finirait par apparaitre dans une trace.
    """

    subject: str
    issuer: str
    key_id: str
    expires_at: int


class JwksSource(Protocol):
    """Source de cles. Le seam qui rend les tests hermetiques."""

    def jwks(self) -> Mapping[str, Any]:
        """Document JWKS courant."""

    def refresh(self) -> Mapping[str, Any]:
        """Document JWKS re-obtenu, pour une rotation de cles."""


class HttpJwksSource:
    """Decouverte OIDC puis JWKS, en HTTP, avec cache borne et recuperation unique.

    `urllib.request` de la stdlib par defaut: aucun client HTTP n'entre dans l'image de
    production pour cette seule fonction. `clock` et `fetch` sont injectables, de sorte que le
    comportement temporel et le nombre d'appels sortants soient MESURABLES en test, sans
    reseau et sans desactiver la cryptographie.

    DEUX VERROUS, ET ILS NE SERVENT PAS A LA MEME CHOSE
        `_state_lock`  protege le cache et les horodatages. Tenu brievement, jamais pendant un
                       appel reseau.
        `_fetch_lock`  n'autorise qu'UNE recuperation a la fois (single-flight). Les perdants
                       d'une course re-verifient en sortant d'attente et reutilisent le
                       document du gagnant.
    Le chemin chaud ne prend NI l'un NI l'autre plus longtemps qu'une lecture: une fois le
    cache rempli, les verifications JWT ne se serialisent pas.
    """

    def __init__(self, issuer: str, *, cache_seconds: float = 300.0,
                 min_refresh_seconds: float = DEFAULT_MIN_REFRESH_SECONDS,
                 max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS,
                 timeout: float = DEFAULT_HTTP_TIMEOUT,
                 clock=time.monotonic, fetch=None) -> None:
        self._issuer = issuer.rstrip("/")
        self._cache_seconds = cache_seconds
        self._min_refresh_seconds = min_refresh_seconds
        self._max_stale_seconds = max_stale_seconds
        self._timeout = timeout
        self._clock = clock
        self._fetch = fetch if fetch is not None else _fetch_json
        self._state_lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._document: Optional[Mapping[str, Any]] = None
        self._fetched_at: Optional[float] = None
        #: horodatage de la derniere TENTATIVE, reussie OU NON. C'est lui qui borne les
        #: boucles sur un emetteur en panne: un echec coute une fenetre, comme une reussite.
        self._attempted_at: Optional[float] = None
        #: `jwks_uri` deja VALIDE (schema, meme hote). Mis en cache pour qu'une relecture
        #: coute un appel au lieu de deux. La validation n'est pas sautee, elle est faite une
        #: fois et son resultat est conserve.
        self._jwks_uri: Optional[str] = None
        #: raison du dernier echec de recuperation. Conservee pour qu'une panne ne soit pas
        #: MASQUEE par la tolerance: elle est servie, mais elle reste nommee et lisible.
        self._last_failure: Optional[str] = None

    # -- contrat `JwksSource` --------------------------------------------------------------
    def jwks(self) -> Mapping[str, Any]:
        """Document courant: le cache s'il est frais, sinon une recuperation."""
        return self._load(ignore_freshness=False)

    def refresh(self) -> Mapping[str, Any]:
        """Document re-obtenu -- appele quand un `kid` manque.

        Soumis a l'intervalle minimal: dans la fenetre, rend le document COURANT sans aucun
        appel sortant. Le `kid` reste alors inconnu et le jeton est refuse, ce qui est le
        comportement voulu: un flot de `kid` inventes ne doit pas atteindre l'emetteur.
        """
        return self._load(ignore_freshness=True)

    # -- mecanique -------------------------------------------------------------------------
    def _age(self) -> Optional[float]:
        """Age du dernier chargement REUSSI. `None` s'il n'y en a jamais eu."""
        if self._document is None or self._fetched_at is None:
            return None
        return self._clock() - self._fetched_at

    def _fresh(self) -> bool:
        age = self._age()
        return age is not None and age < self._cache_seconds

    def _cooling_down(self) -> bool:
        return (self._attempted_at is not None
                and (self._clock() - self._attempted_at) < self._min_refresh_seconds)

    def _tolerated(self) -> Mapping[str, Any]:
        """Document utilisable quand une recuperation n'est pas possible MAINTENANT.

        C'est la SEULE decision de tolerance du module, et les deux branches qui en ont besoin
        -- refroidissement et echec de tentative -- l'appellent toutes les deux. C'est ce qui
        supprime le papillonnement de F.1: le verdict ne depend plus de l'instant d'arrivee de
        la requete, mais uniquement de l'AGE du dernier chargement reussi.

        A appeler sous `_state_lock`.
        """
        age = self._age()
        if age is None:
            # Cache initialement vide: on n'accepte JAMAIS un jeton sans jeu de cles valide.
            raise TokenRejected("issuer_unreachable", self._last_failure or "aucun jeu de cles")
        if age < self._cache_seconds + self._max_stale_seconds:
            return self._document  # type: ignore[return-value]
        raise TokenRejected("jwks_too_stale", f"age {age:.0f}s, {self._last_failure or 'expire'}")

    def _load(self, *, ignore_freshness: bool) -> Mapping[str, Any]:
        # CHEMIN CHAUD: aucun verrou de recuperation, donc aucune serialisation.
        if not ignore_freshness:
            with self._state_lock:
                if self._fresh():
                    return self._document  # type: ignore[return-value]

        with self._fetch_lock:
            with self._state_lock:
                # Double verification: un concurrent a pu remplir le cache pendant l'attente.
                if not ignore_freshness and self._fresh():
                    return self._document  # type: ignore[return-value]
                if self._cooling_down():
                    # Trop tot pour ressayer: meme decision que si on avait essaye et echoue.
                    return self._tolerated()
                # Marque AVANT l'appel: un echec coute une fenetre, comme une reussite. Ne
                # touche PAS `_fetched_at`, qui ne bouge qu'a un chargement reussi.
                self._attempted_at = self._clock()

            try:
                document = self._retrieve()
            except TokenRejected as failure:
                with self._state_lock:
                    self._last_failure = failure.reason
                if failure.reason not in TOLERABLE_FAILURES:
                    # Echec d'INTEGRITE ou de CONFIGURATION: il remonte tel quel. Le masquer
                    # derriere la tolerance ferait chercher une panne reseau devant un
                    # probleme de confiance.
                    raise
                with self._state_lock:
                    # MEME decision que la branche de refroidissement: c'est l'invariant qui
                    # rend le verdict monotone.
                    return self._tolerated()

            with self._state_lock:
                self._document = document
                self._fetched_at = self._clock()
                self._last_failure = None
            return document

    def state(self) -> dict:
        """Etat du cache, sans secret: pour la supervision et un futur `readyz`.

        Une panne servie par tolerance reste ainsi NOMMEE, donc non masquee.
        """
        with self._state_lock:
            age = self._age()
            return {"has_keys": self._document is not None,
                    "age_seconds": None if age is None else round(age, 3),
                    "fresh": self._fresh(),
                    "cache_seconds": self._cache_seconds,
                    "max_stale_seconds": self._max_stale_seconds,
                    "min_refresh_seconds": self._min_refresh_seconds,
                    "last_failure": self._last_failure}

    def _retrieve(self) -> Mapping[str, Any]:
        """Un seul appel quand le `jwks_uri` est connu, deux la premiere fois."""
        uri = self._jwks_uri
        if uri is None:
            uri = self._discover()
            self._jwks_uri = uri
        return self._fetch(uri, timeout=self._timeout)

    def _discover(self) -> str:
        metadata = self._fetch(f"{self._issuer}/.well-known/openid-configuration",
                               timeout=self._timeout)
        uri = metadata.get("jwks_uri") if isinstance(metadata, Mapping) else None
        if not isinstance(uri, str) or not uri:
            raise TokenRejected("discovery_without_jwks_uri")
        # Le JWKS doit venir du MEME hote que l'emetteur: un document de decouverte altere ne
        # doit pas pouvoir nous faire chercher des cles ailleurs. Verifie ICI, une fois, et
        # c'est le resultat VALIDE qui est mis en cache -- jamais la validation qui est sautee.
        if urlsplit(uri).netloc != urlsplit(self._issuer).netloc:
            raise TokenRejected("jwks_uri_outside_issuer")
        _require_safe_url(uri)
        return uri


#: Hotes pour lesquels `http` en clair est tolere. Hors de cette liste, un emetteur joint
#: sans TLS pourrait etre remplace, et le JWKS avec lui.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _require_safe_url(url: str) -> None:
    """Schema sur: `https`, ou `http` sur une boucle locale. Leve sinon."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return
    raise TokenRejected("insecure_discovery_url")


def _fetch_json(url: str, *, timeout: float) -> Mapping[str, Any]:
    _require_safe_url(url)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - schema verifie
            payload = response.read(MAX_DOCUMENT_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise TokenRejected("issuer_unreachable") from exc
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise TokenRejected("document_too_large")
    try:
        document = json.loads(payload)
    except ValueError as exc:
        raise TokenRejected("document_not_json") from exc
    if not isinstance(document, dict):
        raise TokenRejected("document_not_json")
    return document


class TokenVerifier:
    """Verifie un jeton contre le contrat COMPLET de D-098a.

    L'ordre des controles est deliberé: la forme avant la cryptographie, la cryptographie avant
    les revendications. Un jeton malforme ne doit pas atteindre la verification de signature, et
    un jeton non signe par l'emetteur ne doit pas voir ses revendications interpretees.
    """

    def __init__(self, *, issuer: str, audience: str, source: JwksSource,
                 leeway_seconds: float = 0.0) -> None:
        if not issuer or not audience:
            raise ValueError("emetteur et audience sont obligatoires")
        self._issuer = issuer
        self._audience = audience
        self._source = source
        self._leeway = leeway_seconds

    def verify(self, token: str) -> VerifiedToken:
        header = self._header(token)
        key = self._key(header["kid"], algorithm=header["alg"])
        claims = self._claims(token, key, algorithm=header["alg"])
        return VerifiedToken(subject=claims["sub"], issuer=claims["iss"],
                             key_id=header["kid"], expires_at=int(claims["exp"]))

    # -- 1. la forme -----------------------------------------------------------------------
    def _header(self, token: str) -> Dict[str, str]:
        if not isinstance(token, str) or not token.strip():
            raise TokenRejected("token_absent")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenRejected("token_malformed") from exc
        algorithm = header.get("alg")
        if algorithm is None:
            raise TokenRejected("algorithm_absent")
        if algorithm not in ALLOWED_ALGORITHMS:
            # `none` et les familles HMAC tombent ici, et c'est le point: le refus precede
            # toute tentative de verification.
            raise TokenRejected("algorithm_forbidden", str(algorithm))
        key_id = header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            # Sans `kid`, choisir une cle serait deviner -- et un attaquant choisirait pour nous.
            raise TokenRejected("key_id_absent")
        return {"alg": algorithm, "kid": key_id}

    # -- 2. la cle, avec rotation ----------------------------------------------------------
    def _key(self, key_id: str, *, algorithm: str):
        key = self._lookup(self._source.jwks(), key_id, algorithm=algorithm)
        if key is not None:
            return key
        # `kid` inconnu: une rotation a pu avoir lieu depuis la mise en cache. UNE relecture,
        # puis refus -- sinon un flot de `kid` inventes devient une amplification vers l'emetteur.
        key = self._lookup(self._source.refresh(), key_id, algorithm=algorithm)
        if key is None:
            raise TokenRejected("key_id_unknown")
        return key

    @staticmethod
    def _lookup(document: Mapping[str, Any], key_id: str, *, algorithm: str):
        """Trouve la cle, ou rend `None` si le `kid` manque dans un JWKS VALIDE.

        TROIS SITUATIONS DISTINGUEES, et c'est le correctif F.1 de l'objectif 3. La version
        precedente les confondait: un JWKS vide rendait `jwks_malformed`, ce qui melangeait un
        document mal FORME avec un document bien forme mais sans contenu utilisable.

            jwks_malformed            la FORME est fausse: `keys` absent ou pas une liste.
            jwks_without_usable_key   la forme est bonne, le CONTENU est inutilisable: liste
                                      vide, ou aucune cle que `pyjwt` sache lire. C'est une
                                      erreur de l'EMETTEUR, et elle ne doit pas etre deguisee
                                      en `kid` inconnu -- sinon un emetteur casse ressemble a
                                      un jeton douteux, et on cherche le probleme du mauvais
                                      cote.
            None (donc key_id_unknown) le JWKS est valide et porte des cles utilisables, mais
                                      pas celle-la. C'est le seul cas qui justifie une
                                      relecture.
        """
        keys = document.get("keys") if isinstance(document, Mapping) else None
        if not isinstance(keys, list):
            raise TokenRejected("jwks_malformed")
        if len(keys) > MAX_KEYS:
            raise TokenRejected("jwks_too_many_keys")
        if not keys:
            raise TokenRejected("jwks_without_usable_key", "liste vide")
        try:
            key_set = PyJWKSet.from_dict({"keys": keys})
        except Exception as exc:  # noqa: BLE001 - un JWKS illisible est un refus, pas une panne
            # `pyjwt` ignore les cles qu'il ne sait pas lire et ne leve que s'il n'en reste
            # AUCUNE: a ce stade la forme est bonne, donc le defaut est le contenu.
            raise TokenRejected("jwks_without_usable_key", "aucune cle lisible") from exc
        if not key_set.keys:  # pragma: no cover - `pyjwt` leve avant d'en arriver la
            raise TokenRejected("jwks_without_usable_key", "jeu vide apres lecture")
        for candidate in key_set.keys:
            if candidate.key_id != key_id:
                continue
            # L'algorithme ANNONCE par le jeton doit correspondre a celui de la cle: sans cela,
            # une cle RSA pourrait servir a valider un jeton pretendant ES256.
            declared = candidate._jwk_data.get("alg")  # noqa: SLF001 - pyjwt n'expose pas `alg`
            if declared is not None and declared != algorithm:
                raise TokenRejected("algorithm_key_mismatch")
            return candidate.key
        return None

    # -- 3. les revendications -------------------------------------------------------------
    def _claims(self, token: str, key, *, algorithm: str) -> Mapping[str, Any]:
        try:
            claims = jwt.decode(
                token, key=key, algorithms=[algorithm],
                issuer=self._issuer, audience=self._audience,
                leeway=self._leeway,
                options={"require": list(REQUIRED_CLAIMS), "verify_signature": True,
                         "verify_exp": True, "verify_nbf": True, "verify_iat": True,
                         "verify_aud": True, "verify_iss": True},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenRejected("token_expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise TokenRejected("token_not_yet_valid") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenRejected("audience_invalid") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenRejected("issuer_invalid") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise TokenRejected("claim_absent", exc.claim) from exc
        except jwt.InvalidSignatureError as exc:
            raise TokenRejected("signature_invalid") from exc
        except jwt.PyJWTError as exc:
            raise TokenRejected("token_invalid") from exc

        # RE-AFFIRMATION. `pyjwt` verifie deja ce qui precede; ces controles sont ici pour que
        # le contrat de D-098a vive dans NOTRE code et ait ses propres tests. Une bibliotheque
        # qui changerait un defaut ne doit pas pouvoir desserrer le contrat en silence.
        for claim in REQUIRED_CLAIMS:
            if claims.get(claim) in (None, ""):
                raise TokenRejected("claim_absent", claim)
        if claims["iss"] != self._issuer:
            raise TokenRejected("issuer_invalid")
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject.strip():
            raise TokenRejected("subject_invalid")
        if subject.startswith(SERVICE_SUBJECT_PREFIX):
            # Un principal de service ne s'authentifie JAMAIS par HTTP: ce serait une escalade
            # vers l'identite du worker depuis l'exterieur.
            raise TokenRejected("subject_reserved")
        if len(subject) > 255:
            # Meme borne que la colonne `users.idp_subject`: un sujet plus long serait refuse
            # par la base, et le refus doit arriver avant d'y toucher.
            raise TokenRejected("subject_too_long")
        return claims
