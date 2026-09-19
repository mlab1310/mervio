"""Dispatcher multi-tenant: quelles organisations autorisees ont du travail (Mission 004.3).

Le dispatcher ne parcourt jamais la file globale et ne choisit jamais une organisation
de lui-meme: il part de la LISTE EXPLICITE des organisations qui ont autorise le service
connecte (`service_authorizations`) et ne garde que celles qui ont un travail pret (ou un
bail expire). Il ne renvoie que des identifiants d'organisation.

La base le garantit, pas ce module: sans contexte d'organisation, le role worker ne voit
que ses propres autorisations et les travaux en file ou en cours de ces seules
organisations (revision 0007). Aucune fonction SECURITY DEFINER, aucun BYPASSRLS.

Une organisation autorisee apres le demarrage du worker apparait au prochain appel:
rien n'est mis en cache.

Equite: tour de role par identifiant d'organisation, avec un curseur. Une organisation
qui a un million de travaux en file n'est pas servie plus souvent qu'une autre.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from .database import Database
from .service import ServicePrincipal, ServiceSession, _require_connected_principal
from .stores import _limit

#: Organisations autorisees ayant au moins un travail pret, apres le curseur.
#:
#: CONTRAT. La sonde est un TEST D'EXISTENCE par organisation: le `j.id` de la sous-requete ne sert
#: qu'a `IS NOT NULL` et n'est jamais renvoye. Elle ne decide d'aucun ordre de travail:
#: - ordre des organisations: `sa.organization_id` apres le curseur (tour de role, D-050);
#: - priorite reelle: `jobs.claim_next_job` (`_CLAIM_SQL`: priority DESC, created_at, id), dans
#:   l'organisation choisie.
#: Son `ORDER BY` reprend celui de la prise pour une raison de PERFORMANCE seulement: la sous-requete
#: suit `jobs_ready_idx` et s'arrete au premier travail disponible. Sous RLS, cela suppose une
#: estimation correcte de la politique `jobs_service_dispatch` (revision 0012, D-060).
#: L'horloge est evaluee UNE fois (`(SELECT COALESCE(..., now()))`, meme instant que `now()`: celui
#: de la transaction): `now()` n'est pas leakproof et, sous RLS, ne peut pas figurer dans une
#: condition d'index; comparee a un parametre d'InitPlan, `available_at` (derniere cle de
#: `jobs_ready_idx`) est verifiee dans l'index, sans lecture de table pour les travaux differes.
_READY_SQL = (
    "SELECT sa.organization_id FROM service_authorizations sa "
    "WHERE sa.service_user_id = %s AND sa.revoked_at IS NULL "
    "  AND (%s::uuid IS NULL OR sa.organization_id > %s::uuid) "
    "  AND (SELECT j.id FROM jobs j WHERE j.organization_id = sa.organization_id AND j.status = 'queued' "
    "       AND j.available_at <= (SELECT COALESCE(%s::timestamptz, now())) AND j.attempts < j.max_attempts "
    "       ORDER BY j.priority DESC, j.created_at ASC, j.id ASC LIMIT 1) IS NOT NULL "
    "ORDER BY sa.organization_id LIMIT %s"
)

#: Organisations autorisees ayant au moins un bail expire, apres le curseur (`jobs_lease_idx`).
#: Horloge evaluee une fois, comme ci-dessus: l'echeance du bail devient une condition d'index.
_EXPIRED_SQL = (
    "SELECT sa.organization_id FROM service_authorizations sa "
    "WHERE sa.service_user_id = %s AND sa.revoked_at IS NULL "
    "  AND (%s::uuid IS NULL OR sa.organization_id > %s::uuid) "
    "  AND (SELECT j.id FROM jobs j WHERE j.organization_id = sa.organization_id AND j.status = 'running' "
    "       AND j.lease_expires_at < (SELECT COALESCE(%s::timestamptz, now())) "
    "       ORDER BY j.lease_expires_at ASC LIMIT 1) IS NOT NULL "
    "ORDER BY sa.organization_id LIMIT %s"
)


def _organizations(database: Database, principal: ServicePrincipal, statement: str, *, after: Optional[UUID],
                   limit: int, now: Optional[datetime]) -> List[UUID]:
    bound = _limit(limit)
    with database.transaction() as conn:  # aucun contexte d'organisation: vue du dispatcher
        _require_connected_principal(conn, principal)
        rows = conn.execute(statement, (principal.id, after, after, now, bound)).fetchall()
    return [r[0] for r in rows]


def ready_organizations(database: Database, principal: ServicePrincipal, *, after: Optional[UUID] = None,
                        limit: int = 50, now: Optional[datetime] = None) -> List[UUID]:
    return _organizations(database, principal, _READY_SQL, after=after, limit=limit, now=now)


def expired_lease_organizations(database: Database, principal: ServicePrincipal, *, after: Optional[UUID] = None,
                                limit: int = 100, now: Optional[datetime] = None) -> List[UUID]:
    return _organizations(database, principal, _EXPIRED_SQL, after=after, limit=limit, now=now)


class Dispatcher:
    """Tour de role entre les organisations autorisees qui ont du travail."""

    def __init__(self, database: Database, principal: ServicePrincipal, *, batch: int = 50) -> None:
        _limit(batch)
        self.database = database
        self.principal = principal
        self.batch = batch
        self._cursor: Optional[UUID] = None

    def next_organizations(self, *, now: Optional[datetime] = None) -> List[UUID]:
        """Le prochain lot, en reprenant apres la derniere organisation servie."""
        found = ready_organizations(self.database, self.principal, after=self._cursor, limit=self.batch, now=now)
        if len(found) < self.batch and self._cursor is not None:
            # fin du tour: on repart du debut, sans servir deux fois la meme organisation
            wrapped = ready_organizations(self.database, self.principal, limit=self.batch, now=now)
            found += [organization for organization in wrapped if organization not in found][: self.batch - len(found)]
        self._cursor = found[-1] if found else None
        return found

    def expired_organizations(self, *, now: Optional[datetime] = None) -> List[UUID]:
        return expired_lease_organizations(self.database, self.principal, now=now)

    def session(self, organization_id: UUID) -> ServiceSession:
        return ServiceSession(self.database, organization_id, self.principal)


__all__ = ["Dispatcher", "expired_lease_organizations", "ready_organizations"]
