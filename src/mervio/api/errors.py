"""Erreurs de l'API en `application/problem+json` (RFC 9457, Mission 004.6, ADR-004.6-002).

CODES STABLES. Le `code` est l'identite contractuelle de l'erreur; `title` et `detail` en sont
une representation lisible. Un consommateur se branche sur le code, jamais sur le texte --
meme principe que le catalogue de messages du rapport (AN-19 / D-085).

AUCUN ECHO DE L'ENTREE, AUCUNE TRACE. Une reponse d'erreur ne reproduit jamais ce que
l'appelant a envoye, et ne porte aucun identifiant interne. Le `correlation_id` de la requete
est le SEUL lien vers les journaux.

TENANT CROISE ET INEXISTANT SONT INDISCERNABLES. `persistence/errors.py` rend deja `NotFound`
et `TenantAccessDenied` identiques par construction (ADR-004-003); cette couche se contente de
ne pas defaire cette propriete. Aucune reponse, aucun en-tete ne doit distinguer les deux cas.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: type de media impose par la RFC 9457.
PROBLEM_MEDIA_TYPE = "application/problem+json"

#: espace des `type` de probleme. URN, et non URL: le depot ne possede aucun domaine ou publier
#: ces documents, et annoncer une adresse qui ne resout pas serait une fausse promesse. Meme
#: raison que l'identifiant du schema de rapport, qui est une URN pour ce motif (voir
#: `docs/REPORT_CONTRACT.md`, section << Le schema >>).
PROBLEM_TYPE_PREFIX = "urn:mervio:problem:"


@dataclass(frozen=True)
class Problem:
    """Un probleme, tel qu'il sera serialise. `code` est l'identite; le reste est lisible."""

    code: str
    status: int
    title: str
    detail: Optional[str] = None

    @property
    def type(self) -> str:
        return f"{PROBLEM_TYPE_PREFIX}{self.code}"

    def to_dict(self, *, instance: Optional[str] = None) -> dict:
        document = {"type": self.type, "title": self.title, "status": self.status, "code": self.code}
        if self.detail is not None:
            document["detail"] = self.detail
        if instance is not None:
            document["instance"] = instance
        return document


class ApiError(Exception):
    """Erreur portee jusqu'a la frontiere HTTP, ou elle devient un `problem+json`."""

    def __init__(self, problem: Problem) -> None:
        self.problem = problem
        super().__init__(problem.code)


#: Catalogue. Il est FERME: une route ne forge pas un code a la volee, elle en designe un.
#: `not_found` sert AUSSI l'acces croise, et c'est la propriete centrale de l'isolation HTTP.
NOT_FOUND = Problem(
    code="not_found", status=404, title="Ressource introuvable",
    detail="La ressource n'existe pas, ou elle n'appartient pas a votre organisation. "
           "Ces deux cas sont indiscernables, deliberement.")
FORBIDDEN = Problem(
    code="forbidden", status=403, title="Role insuffisant",
    detail="Votre role dans cette organisation n'autorise pas cette operation.")
UNAUTHENTICATED = Problem(
    code="unauthenticated", status=401, title="Authentification requise")
INVALID_REQUEST = Problem(
    code="invalid_request", status=422, title="Requete invalide")
NOT_READY = Problem(
    code="not_ready", status=503, title="Service non pret",
    detail="La base n'est pas joignable, ou son schema n'est pas a la revision attendue.")
INTERNAL = Problem(
    code="internal_error", status=500, title="Erreur interne")

CATALOGUE = (NOT_FOUND, FORBIDDEN, UNAUTHENTICATED, INVALID_REQUEST, NOT_READY, INTERNAL)
