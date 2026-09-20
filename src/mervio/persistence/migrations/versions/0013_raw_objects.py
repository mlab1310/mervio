"""Objets bruts par tenant et vocabulaire d'audit associe (Mission 004.4.4, D-054, D-055, D-061).

Revision ID: 0013_raw_objects
Revises: 0012_dispatch_probe_plan
Create Date: 2026-09-20

CE QUE FAIT CETTE REVISION
- `raw_objects`: la ligne qui rend un objet LISIBLE. C'est elle, sous RLS, et non la cle ni le
  magasin, qui porte la frontiere de tenant (D-054). RLS activee ET forcee; `SELECT` et `INSERT`
  seulement pour le role applicatif: ni `UPDATE` ni `DELETE` ne sont accordes, donc `sha256`
  est FIGE des l'insertion et aucune transition d'etat n'est possible avant 004.4.5.
- Vocabulaire d'audit: `object.uploaded` et `raw_object` s'ajoutent aux listes `CHECK` de 0005,
  par le meme mecanisme que 0008 (les listes s'allongent, rien d'autre ne change).

ETATS (D-056, D-061)
Le domaine complet `pending -> available -> purging -> purged` est fixe ICI par la contrainte,
mais 004.4.4 ne PRODUIT que `available`: la ligne nait disponible, apres que les octets sont
durables. C'est ce que decrit D-054, dont les orphelins sont "illisibles car SANS ligne
`available`" -- le mode de defaillance est l'absence de ligne, jamais une ligne `pending`.
Meme arbitrage qu'en 0011 pour `redacted:` (format accepte, implementation en 004.4.5).

AUCUN role, AUCUN privilege nouveau, AUCUNE fonction SECURITY DEFINER, AUCUN declencheur.
Les fonctions d'aide (`app_current_organization_id`, `app_service_authorized`,
`app_delegate_rank`) existent depuis 0001 et 0007 et sont reutilisees telles quelles.

DESCENTE
`raw_objects` disparait avec ses politiques et ses privileges; les deux listes d'audit sont
restaurees a l'identique de 0008, en `NOT VALID` (le journal est en ajout seul: une migration
n'en efface rien, l'ancienne liste refuse seulement toute NOUVELLE trace).
"""
from alembic import op

revision = "0013_raw_objects"
down_revision = "0012_dispatch_probe_plan"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"
WORKER_ROLE = "mervio_worker"
TABLE = "raw_objects"

#: memes expressions que 0007 et 0011 (un booleen par instruction, aucun cout par ligne)
AUTHORIZED_CONTEXT = "(SELECT app_service_authorized(app_current_organization_id()))"
DELEGATE_RANK = "(SELECT app_delegate_rank())"

HEX64 = "'^[0-9a-f]{64}$'"
#: forme canonique d'une cle, identique a `mervio.storage.base.OBJECT_KEY_PATTERN`
KEY_FORMAT = "'^org/[0-9a-f-]{36}/store/[0-9a-f-]{36}/raw/[0-9a-f-]{36}$'"
SOURCE_KINDS = ("shopify_orders", "shopify_products", "stripe", "google_ads")
ORIGINS = ("csv_upload",)
STATES = ("pending", "available", "purging", "purged")

#: listes d'audit de 0008, inchangees
BASE_ACTIONS = (
    "job.enqueued", "job.claimed", "job.succeeded", "job.failed", "job.requeued",
    "job.recovered", "job.cancelled",
    "import.started", "import.succeeded", "import.failed",
    "analysis.started", "analysis.succeeded", "analysis.failed",
    "purge.started", "purge.completed",
    "organization.created", "member.added", "store.created", "connection.created",
    "service.authorized", "service.revoked",
)
BASE_RESOURCES = ("job", "snapshot", "analysis_run", "report", "organization",
                  "membership", "store", "connection", "service_authorization")
#: ajouts de 004.4.4
OBJECT_ACTIONS = ("object.uploaded",)
OBJECT_RESOURCES = ("raw_object",)


def _in_list(values) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace_audit(column: str, values, *, validated: bool) -> None:
    """Mecanisme de 0008: seule la liste s'allonge ou se raccourcit."""
    name = f"audit_events_{column}_check"
    op.execute(f"ALTER TABLE public.audit_events DROP CONSTRAINT {name}")
    suffix = "" if validated else " NOT VALID"
    op.execute(f"ALTER TABLE public.audit_events ADD CONSTRAINT {name} "
               f"CHECK ({column} IN ({_in_list(values)})){suffix}")


def _worker_policies():
    """Worker (0007): organisation autorisee ET delegue humain de rang analyst au moins.

    Le worker LIT les objets, il n'en depose jamais: il herite pourtant des privileges de
    `mervio_app` (membre du role de groupe), d'ou un refus EXPLICITE de l'insertion.
    """
    return [
        (f"{TABLE}_service_authorized",
         f"AS RESTRICTIVE FOR ALL TO {WORKER_ROLE} USING ({AUTHORIZED_CONTEXT})"),
        (f"{TABLE}_service_delegate_read",
         f"AS RESTRICTIVE FOR SELECT TO {WORKER_ROLE} USING ({DELEGATE_RANK} >= 1)"),
        (f"{TABLE}_service_no_insert",
         f"AS RESTRICTIVE FOR INSERT TO {WORKER_ROLE} WITH CHECK (false)"),
        (f"{TABLE}_service_no_update",
         f"AS RESTRICTIVE FOR UPDATE TO {WORKER_ROLE} USING (false)"),
        (f"{TABLE}_service_no_delete",
         f"AS RESTRICTIVE FOR DELETE TO {WORKER_ROLE} USING (false)"),
    ]


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE public.{TABLE} (
            id               uuid PRIMARY KEY,
            organization_id  uuid NOT NULL,
            store_id         uuid NOT NULL,
            object_key       text NOT NULL
                CONSTRAINT {TABLE}_key_format CHECK (object_key ~ {KEY_FORMAT}),
            sha256           text NOT NULL
                CONSTRAINT {TABLE}_sha256_format CHECK (sha256 ~ {HEX64}),
            byte_size        bigint NOT NULL
                CONSTRAINT {TABLE}_byte_size_positive CHECK (byte_size >= 0),
            source_kind      text NOT NULL
                CONSTRAINT {TABLE}_source_kind_known CHECK (source_kind IN ({_in_list(SOURCE_KINDS)})),
            origin           text NOT NULL
                CONSTRAINT {TABLE}_origin_known CHECK (origin IN ({_in_list(ORIGINS)})),
            state            text NOT NULL
                CONSTRAINT {TABLE}_state_known CHECK (state IN ({_in_list(STATES)})),
            retain_until     timestamptz NOT NULL,
            purged_at        timestamptz NULL,
            created_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT {TABLE}_purge_consistent CHECK ((state = 'purged') = (purged_at IS NOT NULL)),
            CONSTRAINT {TABLE}_tenant_key UNIQUE (organization_id, store_id, id),
            CONSTRAINT {TABLE}_key_uniq UNIQUE (organization_id, object_key),
            CONSTRAINT {TABLE}_store_fkey FOREIGN KEY (organization_id, store_id)
                REFERENCES public.stores (organization_id, id) ON DELETE RESTRICT
        )
    """)
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY {TABLE}_tenant_isolation ON public.{TABLE}
            USING (organization_id = app_current_organization_id())
            WITH CHECK (organization_id = app_current_organization_id())
    """)
    for name, definition in _worker_policies():
        op.execute(f"CREATE POLICY {name} ON public.{TABLE} {definition}")
    # ni UPDATE ni DELETE: `sha256` est fige, et aucune transition d'etat avant 004.4.5
    op.execute(f"GRANT SELECT, INSERT ON public.{TABLE} TO {APP_ROLE}")

    _replace_audit("action", BASE_ACTIONS + OBJECT_ACTIONS, validated=True)
    _replace_audit("resource_type", BASE_RESOURCES + OBJECT_RESOURCES, validated=True)


def downgrade() -> None:
    _replace_audit("resource_type", BASE_RESOURCES, validated=False)
    _replace_audit("action", BASE_ACTIONS, validated=False)
    op.execute(f"DROP TABLE public.{TABLE}")
