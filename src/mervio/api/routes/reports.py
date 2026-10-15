"""`GET /v1/stores/{store_id}/analysis-runs/{run_id}/report` (Mission 004.6, tranche G-2).

LA TRANCHE VERTICALE. Cette route traverse en une fois: en-tete `Authorization` -> contrat de
jeton (D-098a) -> appartenances lues en base -> `TenantSession` sous RLS -> `reports` ->
projection G-1 -> octets HTTP. C'est la premiere surface qui peut prouver
<< acces croise = 404 >>, ce que `/v1/me` ne pouvait pas: elle ne rendait que les
appartenances de l'appelant, donc jamais la ressource d'un AUTRE tenant.

L'ORGANISATION NE VIENT PAS DU CLIENT, ET ELLE N'EST PAS DEVINEE NON PLUS
Le chemin porte `store_id` et `run_id`, jamais une organisation -- la garde AST 3 l'interdit
structurellement. Mais alors, SOUS QUELLE organisation lire? Celle qui possede la boutique.
Or on ne peut pas la demander a la base avant d'avoir un contexte: sans contexte,
`app_current_organization_id()` vaut NULL et les politiques ne rendent AUCUNE ligne -- et une
lecture hors contexte serait precisement l'oracle que l'isolation interdit.
La route ESSAIE donc chaque appartenance de l'appelant, l'une apres l'autre, chacune dans son
propre `TenantSession`. Chaque tentative est bornee par RLS a une organisation dont il EST
membre, donc aucune ne peut atteindre un tenant etranger. La premiere qui trouve le rapport
gagne; si aucune ne le trouve, c'est un 404. Le cout est d'au plus une requete par
appartenance, et une PME en a une ou deux.

CE QUE LA BOUCLE N'AVALE PAS
Seul `NotFound` fait passer a l'appartenance suivante. `PermissionDenied` remonte TELLE QUELLE
et devient un 403: un membre a qui son role interdit la lecture n'est pas << une organisation
ou le rapport n'est pas >>, et le confondre avec un 404 cacherait un probleme de role derriere
un probleme d'existence. Le cas est aujourd'hui INATTEIGNABLE -- `READ` exige `VIEWER`, le role
le plus bas de la matrice (D-ouverte-1) -- mais la boucle ne doit pas decider a la place de la
matrice si celle-ci change.

TROIS 404 INDISCERNABLES, ET C'EST LA PROPRIETE CENTRALE
Rapport absent, run absent, boutique absente, et ressource d'un autre tenant rendent le MEME
document. `get_report_for_run` filtre sur `(organization_id, store_id, analysis_run_id)` en une
seule condition: une combinaison croisee -- la boutique de A avec le run de B -- ne trouve rien,
exactement comme un identifiant invente. Il n'y a donc pas trois chemins a garder alignes, il
n'y en a qu'un.

LES OCTETS SERVIS SONT CEUX QUI SONT PERSISTES
`project_report` (G-1) rend le texte persiste encode en UTF-8, et un ETag FORT qui est
l'empreinte VERIFIEE de ces octets. Aucune reserialisation: `Response`, pas `JSONResponse`.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response

from ...persistence import analyses
from ...persistence.errors import NotFound
from ...persistence.tenancy import TenantContext, TenantSession
from ..contract import API_PREFIX
from ..identity import Identity
from ..projections import etag_matches, project_report

#: type de media du rapport. Le corps EST le document persiste, donc `application/json`.
REPORT_MEDIA_TYPE = "application/json"


def build_router(database) -> APIRouter:
    """Routeur de lecture de rapport, lie au pool de l'API.

    Le pool est passe EXPLICITEMENT plutot que lu sur `app.state`: ce qui touche la base est
    ainsi visible dans la signature, et un test peut fournir le sien sans etat global.
    """
    router = APIRouter(prefix=API_PREFIX, tags=["reports"])

    @router.get("/stores/{store_id}/analysis-runs/{run_id}/report",
                summary="Rapport d'une execution d'analyse, tel qu'il a ete persiste")
    def read_report(store_id: UUID, run_id: UUID, request: Request) -> Response:
        stored = _stored_report(database, _identity(request), store_id, run_id)
        projection = project_report(stored)

        validator = request.headers.get("if-none-match")
        if validator and etag_matches(projection.etag, validator):
            # 304: aucun corps. Le validateur est repete, comme la RFC 9110 le demande.
            return Response(status_code=304, headers={"ETag": projection.etag})
        return Response(content=projection.body, media_type=REPORT_MEDIA_TYPE,
                        headers={"ETag": projection.etag})

    return router


def _identity(request: Request) -> Identity:
    """Identite posee par la dependance d'authentification de `app.py`.

    Aucun parametre de la requete n'est lu ici: ni chemin, ni requete, ni corps, ni aucune
    revendication du jeton au-dela du sujet deja verifie.
    """
    return request.state.identity


def _stored_report(database, identity: Identity, store_id: UUID, run_id: UUID):
    """Rapport, cherche dans les appartenances de l'appelant et dans elles seules.

    `NotFound` fait passer a la suivante; tout le reste remonte -- voir l'en-tete du module.
    """
    for membership in identity.memberships:
        session = TenantSession(database, TenantContext(membership.organization, identity.user))
        try:
            return analyses.get_report_for_run(session, store_id, run_id)
        except NotFound:
            continue
    # Aucune appartenance ne contient ce rapport. Le document est le MEME que pour un rapport
    # inexistant, et c'est voulu: ADR-004-003 porte en HTTP.
    raise NotFound("report")
