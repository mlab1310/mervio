"""`GET /v1/me` -- premiere surface HTTP authentifiee (Mission 004.6, tranche F).

CE QUE CETTE ROUTE PROUVE
Que le porteur d'un jeton est l'utilisateur qu'il dit etre, et quelles appartenances il
detient. Elle traverse: en-tete `Authorization` -> contrat de jeton complet (D-098a) ->
`app_ensure_user` -> `memberships` sous RLS -> `TenantSession` par organisation pour son nom.

CE QU'ELLE NE PROUVE PAS, ET IL FAUT LE DIRE
Aucune isolation entre organisations. Elle ne rend que les appartenances de l'appelant, donc
elle ne peut pas montrer ce qui arrive quand on demande la ressource d'un AUTRE tenant. Cette
preuve est une matrice par route -- succes, 401, role insuffisant, acces croise = 404 -- et
c'est la tranche G qui la porte, sur la route de rapport.

CE QU'ELLE NE PORTE PAS ENCORE
Les BOUTIQUES. La roadmap decrit `/v1/me` comme << utilisateur, organisations, roles,
boutiques >>. Les boutiques sont differees a leur propre route: une lecture de boutiques par
organisation est exactement la surface que la tranche G eprouve avec sa matrice d'acces croise,
et la livrer ici donnerait des lectures tenant-scopees sans cette matrice. Ecart assume et
ecrit, pas un oubli.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..contract import API_CONTRACT, API_PREFIX
from ..identity import Identity

router = APIRouter(prefix=API_PREFIX, tags=["identity"])


def current_identity(request: Request) -> Identity:
    """Identite de l'appelant, resolue par la dependance d'authentification de `app.py`.

    Elle ne lit AUCUN parametre de la requete: ni chemin, ni requete, ni corps. Le seul intrant
    est l'en-tete `Authorization`, et il est verifie avant d'arriver ici.
    """
    return request.state.identity


@router.get("/me", summary="Identite de l'appelant et ses appartenances")
def read_me(identity: Identity = Depends(current_identity)) -> JSONResponse:
    document = identity.to_dict()
    document["api_contract"] = API_CONTRACT
    return JSONResponse(document)
