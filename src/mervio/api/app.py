"""Application HTTP de Mervio (Mission 004.6, tranche E: squelette seul).

CE QUE CE MODULE CONTIENT, ET CE QU'IL NE CONTIENT PAS
Il porte la fabrique de l'application, les deux routes de sante, la traduction des erreurs en
`problem+json` et les en-tetes de securite. Il ne porte AUCUNE route metier: aucune ne se livre
avant que les trois gardes de `tests/api/test_api_guards.py` ne soient vertes.

L'ORGANISATION NE VIENT JAMAIS DU CLIENT
Aucune fonction de ce paquet n'accepte `organization_id` ni ses synonymes, et aucun chemin
d'URL n'en porte. La garde 3 le verifie par l'AST, donc la regle est structurelle et non
disciplinaire. L'organisation se derive de l'appartenance, verifiee EN BASE dans la transaction
RLS par `TenantSession` (ADR-004.6-001).

L'API NE CALCULE RIEN ET NE VALIDE RIEN
Elle n'importe ni le moteur analytique (garde 1) ni le validateur de schema (garde 2). Elle
projette ce que le moteur a produit et que la persistance a scelle; et elle ne peut pas rejeter
a la lecture un rapport 1.x legitimement persiste.

DEUX ERREURS DE PERSISTANCE, ET DEUX SEULEMENT, DEVIENNENT DES REFUS CLIENT (G-0)
    NotFound             -> 404 `not_found`
    PermissionDenied     -> 403 `forbidden`
Avant G-0, les deux remontaient au gestionnaire generique: MESURE sur `0dbb8a4`, les trois cas
-- `NotFound`, `TenantAccessDenied`, `PermissionDenied` -- rendaient 500 `internal_error`. Un
acces croise aurait donc ete un 500, et la propriete << tenant croise et inexistant sont
indiscernables >> aurait ete inatteignable.

L'INDISCERNABILITE EST STRUCTURELLE, PAS DISCIPLINAIRE
`TenantAccessDenied` HERITE de `NotFound` (`persistence/errors.py`), et la recherche de
gestionnaire remonte la hierarchie: les deux atteignent donc le MEME gestionnaire, qui rend le
MEME document constant. Aucun code ne les distingue, donc aucun code ne peut cesser de les
confondre par inadvertance. Teste octet pour octet, sur le MEME chemin -- `instance` porte le
chemin, donc comparer deux chemins differents ne prouverait rien.

CE QUI RESTE UN 500, ET POURQUOI
Toutes les autres erreurs de persistance. `MoneyPrecisionError`, `SchemaNotReady`,
`ImportRejected`, `JobStateError` ne decrivent pas une requete client fautive mais un defaut
interne; les traduire en 4xx ferait chercher le probleme du mauvais cote. La traduction est
donc une liste FERMEE de deux entrees, et un test negatif le verrouille.

LIMITE CONNUE: LE MIDDLEWARE N'EST PAS COUVERT PAR CES GESTIONNAIRES
Les gestionnaires vivent dans `ExceptionMiddleware`, a l'INTERIEUR de la pile de middlewares.
Une erreur de persistance levee par `_require_identity` -- donc par `resolve()` -- ne les
atteint pas et reste un 500. C'est le comportement d'avant G-0, conserve volontairement: la
resolution d'identite n'est pas une lecture de ressource, et rendre 404 sur << votre
appartenance a disparu pendant la requete >> decrirait un etat interne. Teste explicitement.
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..persistence.errors import NotFound, PermissionDenied
from .auth import TokenRejected, TokenVerifier
from .contract import API_CONTRACT, API_PREFIX
from .errors import (PROBLEM_MEDIA_TYPE, ApiError, FORBIDDEN, INTERNAL, NOT_FOUND, NOT_READY,
                     UNAUTHENTICATED)
from .health import liveness, readiness
from .identity import resolve
from .routes import me as me_routes

#: En-tetes poses sur TOUTE reponse. Une API JSON n'est pas une page, d'ou une CSP qui
#: n'autorise rien: aucun document de ce service n'est destine a etre rendu par un navigateur.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


#: Chemins servis SANS authentification. Liste FERMEE, et elle ne contient que de la sante.
#: Toute route metier passe par la dependance d'authentification; une addition ici serait un
#: endpoint non authentifie, ce que `test_only_health_is_unauthenticated` refuse.
PUBLIC_PATHS = ("/healthz", "/readyz")


def create_app(*, database_url: Optional[str] = None, verifier: Optional[TokenVerifier] = None,
               database=None) -> FastAPI:
    """Fabrique l'application.

    `database_url` ne sert qu'a `readyz`, qui lit une revision. `verifier` et `database` sont
    le seam d'authentification: sans eux, AUCUNE route metier n'est enregistree -- un squelette
    sans identite ne doit pas pouvoir servir une route protegee par inadvertance.
    """
    app = FastAPI(
        title="Mervio API",
        version=API_CONTRACT,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _problem(exc.problem, request)

    @app.exception_handler(NotFound)
    async def _absent(request: Request, exc: NotFound) -> JSONResponse:
        # `TenantAccessDenied` HERITE de `NotFound`, donc elle arrive ICI sans etre nommee:
        # l'indiscernabilite est produite par la hierarchie, pas par une discipline d'ecriture.
        # Aucun attribut de l'exception n'est lu -- ni `kind`, ni son message.
        return _problem(NOT_FOUND, request)

    @app.exception_handler(PermissionDenied)
    async def _refuse(request: Request, exc: PermissionDenied) -> JSONResponse:
        # Ni `permission` ni `role` ne sortent: le document est le meme pour tout refus de role.
        return _problem(FORBIDDEN, request)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Aucun echo, aucune trace: un defaut interne ne decrit pas la requete qui l'a revele.
        return _problem(INTERNAL, request)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse(liveness())

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        state = readiness(database_url)
        status = 200 if state.ready else NOT_READY.status
        return JSONResponse(state.to_dict(), status_code=status)

    if verifier is not None and database is not None:
        _authenticate(app, verifier=verifier, database=database)
        app.include_router(me_routes.router)

    return app


def _authenticate(app: FastAPI, *, verifier: TokenVerifier, database) -> None:
    """Pose la dependance d'authentification sur TOUT ce qui n'est pas de la sante.

    Un middleware, et non une dependance par route: une route ajoutee demain est protegee par
    DEFAUT. L'oubli possible devient << exposer de la sante >>, pas << exposer une ressource >>.
    """

    @app.middleware("http")
    async def _require_identity(request: Request, call_next):
        if request.url.path in PUBLIC_PATHS or request.url.path == f"{API_PREFIX}/openapi.json":
            return await call_next(request)
        try:
            token = _bearer(request)
            verified = verifier.verify(token)
            # La resolution touche la base: elle vient APRES la verification complete du
            # jeton, jamais avant. Un jeton non verifie ne doit atteindre aucune requete SQL.
            request.state.identity = resolve(database, verified.subject)
        except TokenRejected:
            # 401 INDIFFERENCIE. La cause est un code stable, destine aux journaux, pas au
            # client -- voir le commentaire de `UNAUTHENTICATED`.
            return _problem(UNAUTHENTICATED, request)
        return await call_next(request)


def _bearer(request: Request) -> str:
    """Extrait le porteur. Aucune autre source: ni cookie, ni parametre de requete.

    Un jeton dans une URL finit dans les journaux de tout intermediaire; un jeton dans un
    cookie demande une defense CSRF que cette API n'a pas.
    """
    header = request.headers.get("authorization")
    if not header:
        raise TokenRejected("authorization_absent")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise TokenRejected("authorization_malformed")
    return value.strip()


def _problem(problem, request: Request) -> JSONResponse:
    return JSONResponse(
        problem.to_dict(instance=request.url.path),
        status_code=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
    )
