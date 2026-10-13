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
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .contract import API_CONTRACT, API_PREFIX
from .errors import PROBLEM_MEDIA_TYPE, ApiError, INTERNAL, NOT_READY
from .health import liveness, readiness

#: En-tetes poses sur TOUTE reponse. Une API JSON n'est pas une page, d'ou une CSP qui
#: n'autorise rien: aucun document de ce service n'est destine a etre rendu par un navigateur.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


def create_app(*, database_url: Optional[str] = None) -> FastAPI:
    """Fabrique l'application. `database_url` ne sert qu'a `readyz`, qui lit une revision."""
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

    return app


def _problem(problem, request: Request) -> JSONResponse:
    return JSONResponse(
        problem.to_dict(instance=request.url.path),
        status_code=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
    )
