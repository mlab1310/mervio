"""Vocabulaire d'audit de l'administration (Mission 004.3.7).

Revision ID: 0008_admin_audit
Revises: 0007_service_identity
Create Date: 2026-09-17

Pourquoi une migration: la contrainte CHECK de 0005 fixe la liste des actions et des types
de ressource. Les operations d'administration qui changent la securite ou le perimetre d'une
organisation (creation, appartenance, boutique, connexion, autorisation d'un service) ne
pouvaient donc pas etre tracees dans le journal existant. Aucune table, aucune colonne,
aucun droit ni aucune politique ne change: seules les deux listes s'allongent.

Descente NON destructive: le journal est en ajout seul, une migration n'en efface rien.
L'ancienne liste est remise en `NOT VALID`: elle refuse toute NOUVELLE trace d'administration
et conserve celles deja ecrites. La remontee suivante revalide la liste complete.
"""
from alembic import op

revision = "0008_admin_audit"
down_revision = "0007_service_identity"
branch_labels = None
depends_on = None

#: Listes de 0005, inchangees.
BASE_ACTIONS = (
    "job.enqueued", "job.claimed", "job.succeeded", "job.failed", "job.requeued",
    "job.recovered", "job.cancelled",
    "import.started", "import.succeeded", "import.failed",
    "analysis.started", "analysis.succeeded", "analysis.failed",
    "purge.started", "purge.completed",
)
BASE_RESOURCES = ("job", "snapshot", "analysis_run", "report", "organization")

#: Ajouts de 004.3.7.
ADMIN_ACTIONS = (
    "organization.created", "member.added", "store.created", "connection.created",
    "service.authorized", "service.revoked",
)
ADMIN_RESOURCES = ("membership", "store", "connection", "service_authorization")


def _in_list(values) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace(column: str, values, *, validated: bool) -> None:
    name = f"audit_events_{column}_check"
    op.execute(f"ALTER TABLE audit_events DROP CONSTRAINT {name}")
    suffix = "" if validated else " NOT VALID"
    op.execute(f"ALTER TABLE audit_events ADD CONSTRAINT {name} CHECK ({column} IN ({_in_list(values)})){suffix}")


def upgrade() -> None:
    _replace("action", BASE_ACTIONS + ADMIN_ACTIONS, validated=True)
    _replace("resource_type", BASE_RESOURCES + ADMIN_RESOURCES, validated=True)


def downgrade() -> None:
    _replace("resource_type", BASE_RESOURCES, validated=False)
    _replace("action", BASE_ACTIONS, validated=False)
