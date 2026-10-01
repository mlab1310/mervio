"""Audit des refus d'une tentative reelle de reprise de purge (Mission 004.4.7, D-070).

Revision ID: 0017_purge_recovery_audit
Revises: 0016_tenant_purge
Create Date: 2026-09-30

CE QUE FAIT CETTE REVISION
Une seule chose: elargir `audit_events_action_check` d'UNE valeur,
`organization.purge_recovery_refused`. C'est le plus petit perimetre de base du depot.

    AUCUNE table           AUCUN index          AUCUNE fonction
    AUCUNE colonne         AUCUNE politique     AUCUN declencheur
    AUCUN role             AUCUN GRANT/REVOKE   AUCUNE autre contrainte

POURQUOI UNE MIGRATION EST OBLIGATOIRE
`action` porte un `CHECK IN (...)` fige par `0005`. Une valeur nouvelle ne peut pas etre
inseree sans amender la contrainte, et l'invariant du depot veut que tout nouvel evenement
d'audit passe par une migration de cette contrainte -- jamais par un contournement.

CE QUE CETTE REVISION NE LIVRE PAS
Ni l'ecriture de l'evenement, ni l'enum Python, ni le chemin de reconciliation. La valeur
existe ici EN BASE; `audit.Action` et `admin.operations` l'accompagnent dans le meme
changement, mais hors migration -- ce sont du code, pas du schema.

CE QUI N'EST PAS AMENDE, ET POURQUOI
  `outcome`        reste `('started','succeeded','failed')`. D-070 ecrit `failed`: c'est deja
                   le vocabulaire etabli des operations echouees (`job.failed`,
                   `import.failed`, ...). Cette contrainte n'a JAMAIS ete amendee en seize
                   revisions, et D-070 refuse de l'elargir sans necessite.
  `resource_type`  reste inchangee. D-070 ecrit sur `organization`, qui existe depuis `0005`.
  `actor_type`     reste inchangee. D-070 ecrit `user`.

DESCENTE NON DESTRUCTIVE -- MECANISME DE `0008`, REPRIS TEL QUEL
Le journal est en ajout seul: une migration n'en efface rien. La descente remet la liste de
`0016` en `NOT VALID`: elle refuse toute NOUVELLE trace de refus et CONSERVE celles deja
ecrites. Une descente ne POURRAIT pas valider la contrainte contre un evenement de refus
deja present -- d'ou `NOT VALID`, et non une validation qui echouerait. La remontee suivante
revalide la liste complete, qui contient de nouveau la valeur: les lignes existantes la
satisfont donc, et `UP -> DOWN -> UP` fonctionne avec des evenements D-070 en base.
C'est exactement ce que font `0008`, `0013`, `0014` et `0016`; rien n'est invente ici.
"""
from alembic import op

revision = "0017_purge_recovery_audit"
down_revision = "0016_tenant_purge"
branch_labels = None
depends_on = None

#: Vocabulaire d'audit du depot AVANT cette revision, releve du catalogue et non de memoire:
#: les 23 valeurs de `0005`/`0008`/`0013`/`0014`, puis les 4 de `0016`. Les 27 sont reproduites
#: LITTERALEMENT, parce que la descente doit rendre cette liste au caractere pres.
BASE_ACTIONS = (
    "job.enqueued", "job.claimed", "job.succeeded", "job.failed", "job.requeued",
    "job.recovered", "job.cancelled", "import.started", "import.succeeded", "import.failed",
    "analysis.started", "analysis.succeeded", "analysis.failed", "purge.started",
    "purge.completed", "organization.created", "member.added", "store.created",
    "connection.created", "service.authorized", "service.revoked", "object.uploaded",
    "customer.redacted",
)
#: `0016` (D-065 Q1): vocabulaire DEDIE a la purge de locataire. Inchange ici.
TENANT_PURGE_ACTIONS = ("store.purge_started", "store.purged",
                        "organization.purge_started", "organization.purged")

#: D-070. Une SEULE valeur, et une action DEDIEE -- aucune reutilisation, pour la raison meme
#: qui a conduit D-065 Q1 a refuser de reemployer `purge.started`: un refus de reprise doit
#: rester distinguable d'une purge, d'une expiration de retention et d'une mise en file.
#: Elle designe le refus d'une tentative REELLE (`--execute`) par un acteur habilite; une
#: consultation et une simulation n'ecrivent rien, et c'est le chemin applicatif qui le tient.
RECOVERY_ACTIONS = ("organization.purge_recovery_refused",)

#: etat courant de la contrainte, avant et apres.
PREVIOUS_ACTIONS = BASE_ACTIONS + TENANT_PURGE_ACTIONS
CURRENT_ACTIONS = PREVIOUS_ACTIONS + RECOVERY_ACTIONS


def _in_list(values) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace_check(table: str, column: str, values, *, validated: bool) -> None:
    """Mecanisme de `0008`, repris tel quel par `0013`, `0014` et `0016`: seule la liste change."""
    name = f"{table}_{column}_check"
    op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {name}")
    suffix = "" if validated else " NOT VALID"
    op.execute(f"ALTER TABLE public.{table} ADD CONSTRAINT {name} "
               f"CHECK ({column} IN ({_in_list(values)})){suffix}")


def upgrade() -> None:
    _replace_check("audit_events", "action", CURRENT_ACTIONS, validated=True)


def downgrade() -> None:
    _replace_check("audit_events", "action", PREVIOUS_ACTIONS, validated=False)
