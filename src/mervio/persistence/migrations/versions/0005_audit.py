"""Journal d'audit en ajout seul (Mission 004.2).

Revision ID: 0005_audit
Revises: 0004_jobs
Create Date: 2026-09-15

Une ligne d'audit repond a: QUI, a fait QUOI, sur QUELLE RESSOURCE, pour QUELLE
ORGANISATION, QUAND, sous QUELLE CORRELATION, avec QUEL RESULTAT.

Ajout seul, garanti par la base:
- aucun UPDATE: droit non accorde au role applicatif ET trigger qui refuse (le
  proprietaire des tables est couvert lui aussi);
- aucun DELETE avant le plancher de retention: politique RESTRICTIVE, donc vraie
  meme en SQL brut et meme pour le tenant proprietaire de la ligne.

La retention reste possible au-dela du plancher, sinon le journal croitrait sans
fin: c'est une purge tracee (`purge.completed`), jamais une correction d'histoire.

`metadata` ne contient que des identifiants, des compteurs et des codes d'erreur.
Jamais de secret, de jeton, d'email, de chemin de fichier ni de contenu de source
(verifie par `audit.record` et par test).
"""
from alembic import op

revision = "0005_audit"
down_revision = "0004_jobs"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"

#: Plancher impose par la base: rien de plus recent que 30 jours n'est supprimable.
AUDIT_FLOOR = "30 days"

ACTIONS = (
    "job.enqueued", "job.claimed", "job.succeeded", "job.failed", "job.requeued",
    "job.recovered", "job.cancelled",
    "import.started", "import.succeeded", "import.failed",
    "analysis.started", "analysis.succeeded", "analysis.failed",
    "purge.started", "purge.completed",
)
ACTION_LIST = ", ".join(f"'{action}'" for action in ACTIONS)
ACTOR_LIST = "'user', 'system', 'worker'"
RESOURCE_LIST = "'job', 'snapshot', 'analysis_run', 'report', 'organization'"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE audit_events (
            id               uuid PRIMARY KEY,
            organization_id  uuid NOT NULL REFERENCES organizations (id) ON DELETE RESTRICT,
            store_id         uuid NULL,
            actor_type       text NOT NULL CHECK (actor_type IN ({ACTOR_LIST})),
            actor_id         uuid NULL REFERENCES users (id) ON DELETE RESTRICT,
            action           text NOT NULL CHECK (action IN ({ACTION_LIST})),
            resource_type    text NOT NULL CHECK (resource_type IN ({RESOURCE_LIST})),
            resource_id      uuid NOT NULL,
            correlation_id   uuid NOT NULL,
            outcome          text NOT NULL CHECK (outcome IN ('started', 'succeeded', 'failed')),
            metadata         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            created_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT audit_events_metadata_is_object CHECK (jsonb_typeof(metadata) = 'object'),
            CONSTRAINT audit_events_user_actor_identified CHECK (actor_type <> 'user' OR actor_id IS NOT NULL),
            CONSTRAINT audit_events_tenant_key UNIQUE (organization_id, id),
            CONSTRAINT audit_events_store_fkey FOREIGN KEY (organization_id, store_id)
                REFERENCES stores (organization_id, id) ON DELETE RESTRICT
        )
    """)
    # Journal d'une organisation, du plus recent au plus ancien.
    op.execute("CREATE INDEX audit_events_recent_idx ON audit_events (organization_id, created_at DESC, id)")
    # Histoire d'une ressource precise (un travail, un instantane).
    op.execute("""
        CREATE INDEX audit_events_resource_idx ON audit_events
            (organization_id, resource_type, resource_id, created_at)
    """)
    # Toutes les traces d'une meme execution.
    op.execute("""
        CREATE INDEX audit_events_correlation_idx ON audit_events
            (organization_id, correlation_id, created_at)
    """)

    op.execute("""
        CREATE FUNCTION audit_events_forbid_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'the audit log is append-only';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER audit_events_forbid_update BEFORE UPDATE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION audit_events_forbid_update()
    """)

    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY audit_events_tenant_isolation ON audit_events
            USING (organization_id = app_current_organization_id())
            WITH CHECK (organization_id = app_current_organization_id())
    """)
    op.execute(f"""
        CREATE POLICY audit_events_retention_floor ON audit_events AS RESTRICTIVE FOR DELETE
            USING (created_at < now() - interval '{AUDIT_FLOOR}')
    """)
    op.execute(f"GRANT SELECT, INSERT, DELETE ON audit_events TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON audit_events FROM {APP_ROLE}")
    op.execute("DROP TABLE audit_events")
    op.execute("DROP FUNCTION audit_events_forbid_update()")
