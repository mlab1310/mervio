"""Du sujet d'un jeton verifie a l'identite Mervio (Mission 004.6, tranche F).

LA SEULE SOURCE DE L'ORGANISATION, C'EST L'APPARTENANCE
Aucune fonction de ce module n'accepte un identifiant d'organisation. Elles le LISENT depuis
`memberships`, sous RLS, et le rendent dans un objet `Membership` dont la provenance est donc
structurelle. La garde AST 3 de `tests/api/test_api_guards.py` interdit par ailleurs tout
parametre nomme ainsi dans `api/`: la regle n'est pas disciplinaire, elle est verifiee.

LA RESOLUTION PASSE PAR LES FONCTIONS AUTORISEES
`idp_subject` est hors de portee du role applicatif depuis la revision `0018` (SEC-05/D-106).
Le provisionnement et la lecture passent donc par `tenancy.ensure_user`, qui appelle
`app_ensure_user` -- une fonction a cle EXACTE, sans balayage possible.

PROVISIONNEMENT A LA VOLEE: RATIFIE, ET SA CONSEQUENCE EST DITE
D-098a ecrit << `sub` -> `users.idp_subject` via `tenancy.ensure_user` >>, et `ensure_user` est
un insert-or-get. Donc TOUT jeton valide de l'emetteur configure CREE une ligne `users` s'il
n'en existe pas. La barriere n'est pas ici: elle est `iss` + `aud`, verifies avant d'arriver
jusqu'a ce module. Une limitation de debit la completera (tranche d'ecriture). Dit maintenant
plutot que decouvert plus tard.

CE QUI N'EST PAS PROUVE PAR CE MODULE
Une identite resolue ne prouve AUCUNE isolation entre organisations. Elle prouve seulement que
le porteur du jeton est l'utilisateur qu'il dit etre, et quelles appartenances il detient. La
preuve d'isolation est une matrice par route -- succes, 401, role insuffisant, acces croise =
404 -- et c'est la tranche G qui la porte.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from uuid import UUID

from ..persistence import tenancy
from ..persistence.tenancy import Permission, Role, TenantContext, TenantSession


@dataclass(frozen=True)
class Membership:
    """Une appartenance LUE en base. L'organisation ne vient jamais d'ailleurs."""

    organization: UUID
    role: Role
    name: Optional[str] = None


@dataclass(frozen=True)
class Identity:
    """Qui appelle, et ce qu'il detient. Le sujet IdP n'y figure PAS.

    Le porteur connait deja son propre sujet -- il est dans son jeton -- donc le renvoyer
    n'apprend rien; mais un objet qui le transporte finit par apparaitre dans une trace.
    """

    user: UUID
    memberships: List[Membership]

    def to_dict(self) -> dict:
        return {
            "user_id": str(self.user),
            "organizations": [
                {"organization_id": str(m.organization), "role": m.role.value, "name": m.name}
                for m in self.memberships
            ],
        }


def resolve(database, subject: str) -> Identity:
    """Identite d'un sujet VERIFIE: provisionne si besoin, puis lit ses appartenances.

    `database` est le pool de l'API (`persistence.pool.DatabasePool`) ou un `Database`: seul
    le contrat `transaction(organization_id=, user_id=)` est utilise.
    """
    user = tenancy.ensure_user(database, subject)
    return Identity(user=user, memberships=_memberships(database, user))


def _memberships(database, user: UUID) -> List[Membership]:
    """Appartenances de l'utilisateur, puis le nom de chaque organisation.

    Deux temps, et c'est impose par les politiques. `memberships_own_rows_readable` laisse lire
    SES propres lignes sans contexte d'organisation; `organizations` exige en revanche le
    contexte de l'organisation lue. Le nom passe donc par un `TenantSession` par organisation,
    qui verifie l'appartenance EN BASE a chaque fois -- jamais par une jointure qui
    contournerait cette verification.
    """
    with database.transaction(user_id=user) as conn:
        rows = conn.execute(
            "SELECT organization_id, role FROM memberships WHERE user_id = %s "
            "ORDER BY created_at, organization_id",
            (user,),
        ).fetchall()

    memberships: List[Membership] = []
    for organization, role in rows:
        session = TenantSession(database, TenantContext(organization, user))
        with session.transaction(Permission.READ) as conn:
            named = conn.execute("SELECT name FROM organizations WHERE id = %s",
                                 (organization,)).fetchone()
        memberships.append(Membership(organization=organization, role=Role(role),
                                      name=named[0] if named else None))
    return memberships
