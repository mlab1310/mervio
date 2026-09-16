"""File de travaux durable dans PostgreSQL (Mission 004.2).

Revision ID: 0004_jobs
Revises: 0003_analysis_reports
Create Date: 2026-09-15

PostgreSQL est la file (ADR-004-008): ni Redis, ni courtier, ni ordonnanceur
externe. Un worker prend un travail par `SELECT ... FOR UPDATE SKIP LOCKED` dans
une transaction courte, puis execute hors transaction.

Trois garanties sont portees par la base, pas seulement par le code:
- la machine a etats (trigger `jobs_guard_transition`): aucune transition hors du
  graphe autorise, aucun etat terminal reecrit, aucune colonne d'identite modifiee;
- la suppression (politique RESTRICTIVE `jobs_purge_terminal_only`): un travail en
  file ou en cours est indestructible, meme en SQL brut par son propre tenant;
- l'isolation (RLS FORCEE, comme toute table de tenant depuis 0001).

Le bail (`lease_expires_at`) rend un worker mort recuperable: passe l'echeance,
le travail redevient prenable. L'execution est donc AU MOINS une fois; c'est
l'idempotence de 004.1 (`inputs_sha256`, `analysis_runs_completed_uniq`) qui rend
un rejeu inoffensif.
"""
from alembic import op

revision = "0004_jobs"
down_revision = "0003_analysis_reports"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"

#: Plancher de retention impose par la base: un travail termine depuis moins
#: d'une heure n'est jamais supprimable, quelle que soit la configuration de purge.
PURGE_FLOOR = "1 hour"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE jobs (
            id               uuid PRIMARY KEY,
            organization_id  uuid NOT NULL REFERENCES organizations (id) ON DELETE RESTRICT,
            store_id         uuid NULL,
            job_type         text NOT NULL CHECK (job_type IN ('import', 'analysis', 'purge')),
            status           text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
            priority         smallint NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
            attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            max_attempts     integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
            idempotency_key  text NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
            correlation_id   uuid NOT NULL,
            enqueued_by      uuid NULL REFERENCES users (id) ON DELETE RESTRICT,
            available_at     timestamptz NOT NULL,
            locked_at        timestamptz NULL,
            locked_by        text NULL CHECK (char_length(locked_by) BETWEEN 1 AND 100),
            lease_expires_at timestamptz NULL,
            started_at       timestamptz NULL,
            finished_at      timestamptz NULL,
            last_error_code  text NULL CHECK (char_length(last_error_code) BETWEEN 1 AND 100),
            last_error       text NULL CHECK (char_length(last_error) <= 2000),
            payload          jsonb NOT NULL,
            result           jsonb NULL,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT jobs_attempts_bounded CHECK (attempts <= max_attempts),
            CONSTRAINT jobs_lock_consistent CHECK (
                (status = 'running') = (locked_at IS NOT NULL)
                AND (locked_at IS NULL) = (locked_by IS NULL)
                AND (locked_at IS NULL) = (lease_expires_at IS NULL)),
            CONSTRAINT jobs_finished_consistent CHECK (
                (status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)),
            CONSTRAINT jobs_result_only_when_succeeded CHECK (result IS NULL OR status = 'succeeded'),
            CONSTRAINT jobs_started_before_finished CHECK (
                started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at),
            CONSTRAINT jobs_payload_is_object CHECK (jsonb_typeof(payload) = 'object'),
            CONSTRAINT jobs_result_is_object CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
            CONSTRAINT jobs_tenant_key UNIQUE (organization_id, id),
            CONSTRAINT jobs_store_fkey FOREIGN KEY (organization_id, store_id)
                REFERENCES stores (organization_id, id) ON DELETE RESTRICT
        )
    """)

    # Rejeu d'une meme demande: une seule ligne. La cle est PREFIXEE par l'organisation
    # (lecon 004.1): une violation d'unicite ne peut pas servir d'oracle d'existence
    # sur le travail d'un autre tenant.
    op.execute("""
        CREATE UNIQUE INDEX jobs_idempotency_uniq ON jobs (organization_id, job_type, idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)
    # Prise du prochain travail: egalite sur l'organisation, index partiel sur la file,
    # tri porte par l'index (priorite puis anciennete). available_at est un filtre:
    # la majorite des travaux en file sont disponibles, le premier eligible arrive vite.
    op.execute("""
        CREATE INDEX jobs_ready_idx ON jobs (organization_id, priority DESC, created_at, id)
            WHERE status = 'queued'
    """)
    # Recuperation des baux expires.
    op.execute("CREATE INDEX jobs_lease_idx ON jobs (organization_id, lease_expires_at) WHERE status = 'running'")
    # Purge des travaux termines.
    op.execute("""
        CREATE INDEX jobs_terminal_idx ON jobs (organization_id, finished_at)
            WHERE status IN ('succeeded', 'failed', 'cancelled')
    """)
    # Historique d'une organisation (et d'une boutique) du plus recent au plus ancien.
    op.execute("CREATE INDEX jobs_recent_idx ON jobs (organization_id, created_at DESC, id)")

    # --- machine a etats en base -------------------------------------------------
    # queued -> running | cancelled ; running -> succeeded | failed | queued (reprise
    # ou bail expire). Les etats terminaux sont definitifs.
    op.execute("""
        CREATE FUNCTION jobs_guard_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id <> OLD.id OR NEW.organization_id <> OLD.organization_id
               OR NEW.job_type <> OLD.job_type OR NEW.payload <> OLD.payload
               OR NEW.store_id IS DISTINCT FROM OLD.store_id
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.correlation_id <> OLD.correlation_id
               OR NEW.enqueued_by IS DISTINCT FROM OLD.enqueued_by
               OR NEW.max_attempts <> OLD.max_attempts OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'job identity is immutable';
            END IF;
            IF OLD.status IN ('succeeded', 'failed', 'cancelled') THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'a finished job is immutable';
            END IF;
            IF NOT (
                (OLD.status = 'queued' AND NEW.status IN ('queued', 'running', 'cancelled'))
                OR (OLD.status = 'running' AND NEW.status IN ('succeeded', 'failed', 'queued'))
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'illegal job state transition';
            END IF;
            IF NEW.attempts < OLD.attempts THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'job attempts never decrease';
            END IF;
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER jobs_guard_transition BEFORE UPDATE ON jobs
            FOR EACH ROW EXECUTE FUNCTION jobs_guard_transition()
    """)
    op.execute("""
        CREATE FUNCTION jobs_guard_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status <> 'queued' OR NEW.attempts <> 0 OR NEW.result IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'a job is created queued, with no attempt and no result';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER jobs_guard_insert BEFORE INSERT ON jobs
            FOR EACH ROW EXECUTE FUNCTION jobs_guard_insert()
    """)

    # --- RLS et droits -----------------------------------------------------------
    op.execute("ALTER TABLE jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY jobs_tenant_isolation ON jobs
            USING (organization_id = app_current_organization_id())
            WITH CHECK (organization_id = app_current_organization_id())
    """)
    # RESTRICTIVE: s'ajoute par ET a la politique de tenant. La suppression d'un
    # travail en file ou en cours est impossible, y compris pour son propre tenant
    # et y compris en SQL brut; un travail termine ne devient supprimable qu'apres
    # le plancher. La purge applique en plus sa propre retention, plus longue.
    op.execute(f"""
        CREATE POLICY jobs_purge_terminal_only ON jobs AS RESTRICTIVE FOR DELETE
            USING (status IN ('succeeded', 'failed', 'cancelled')
                   AND finished_at IS NOT NULL
                   AND finished_at < now() - interval '{PURGE_FLOOR}')
    """)
    op.execute(f"GRANT SELECT, INSERT, DELETE ON jobs TO {APP_ROLE}")
    op.execute(f"""
        GRANT UPDATE (status, attempts, available_at, locked_at, locked_by, lease_expires_at, started_at,
                      finished_at, last_error_code, last_error, result, priority, updated_at)
        ON jobs TO {APP_ROLE}
    """)


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON jobs FROM {APP_ROLE}")
    op.execute("DROP TABLE jobs")
    op.execute("DROP FUNCTION jobs_guard_insert()")
    op.execute("DROP FUNCTION jobs_guard_transition()")
