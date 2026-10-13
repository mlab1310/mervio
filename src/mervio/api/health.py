"""Sante de l'API: `GET /healthz` et `GET /readyz` (Mission 004.6).

DEUX QUESTIONS DIFFERENTES, et les confondre est le defaut habituel:

    /healthz   le processus est-il VIVANT? Aucune dependance, aucune base. Il repond 200 meme
               quand la base est tombee -- sinon un orchestrateur redemarrerait un processus
               sain parce que sa base est momentanement absente.
    /readyz    le processus peut-il SERVIR? Base joignable ET revision de migration egale a la
               tete attendue, comme la roadmap l'exige. Un schema en retard rend 503: servir
               des lectures contre un schema qui n'est pas celui du code produirait des
               reponses fausses plutot qu'une panne franche.

Aucune des deux ne porte de donnee de tenant, ni le detail d'une erreur de connexion: elles
disent `ready` ou `not_ready`, et le journal porte le reste.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..persistence import migrate
from .contract import API_CONTRACT

#: La revision est lue par `migrate.applied_revision`, qui passe par `app_schema_revision()`
#: (revision 0019) et non par `alembic_version`: le role applicatif n'a aucun privilege sur
#: cette table, et lui en donner rendrait la descente de migration non symetrique. Le pilote
#: SQL reste confine a la couche de persistance (`tests/test_persistence_boundaries.py`).


@dataclass(frozen=True)
class Readiness:
    ready: bool
    #: `None` quand la base n'est pas joignable: on ne devine pas une revision.
    revision: Optional[str]
    expected: str
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        document = {"ready": self.ready, "api_contract": API_CONTRACT,
                    "migration": {"revision": self.revision, "expected": self.expected}}
        if self.reason is not None:
            document["reason"] = self.reason
        return document


def liveness() -> dict:
    """Vivant. Volontairement sans dependance: aucune base, aucun pool, aucun reseau."""
    return {"status": "alive", "api_contract": API_CONTRACT}


def readiness(database_url: Optional[str]) -> Readiness:
    """Pret si la base repond ET si son schema est a la tete attendue.

    `reason` est un code stable, jamais le message de la base: celui-ci pourrait nommer un
    hote, un role ou un chemin.
    """
    expected = migrate.head_revision()
    if not database_url:
        return Readiness(False, None, expected, "database_not_configured")
    try:
        current = migrate.applied_revision(database_url)
    except Exception:  # noqa: BLE001 - toute panne de connexion est le MEME refus cote client
        return Readiness(False, None, expected, "database_unreachable")
    if current != expected:
        return Readiness(False, current, expected, "schema_not_at_head")
    return Readiness(True, current, expected)
