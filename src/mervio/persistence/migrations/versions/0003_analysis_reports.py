"""Executions d'analyse et rapports persistes.

Revision ID: 0003_analysis_reports
Revises: 0002_snapshots
Create Date: 2026-09-15

Le rapport est conserve sous deux formes:
- payload_json (text): les octets exacts du livrable CLI (json.dumps indent=2,
  ensure_ascii=False, UTF-8). C'est la forme AUTORITAIRE, verifiee par
  payload_sha256 et payload_bytes directement en base;
- payload (jsonb): projection interrogeable, non autoritaire. jsonb reordonne
  les cles et normalise les espaces: il ne peut pas restituer les octets.
"""
from alembic import op

revision = "0003_analysis_reports"
down_revision = "0002_snapshots"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"
HEX64 = "'^[0-9a-f]{64}$'"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE analysis_runs (
            id               uuid PRIMARY KEY,
            organization_id  uuid NOT NULL,
            store_id         uuid NOT NULL,
            snapshot_id      uuid NOT NULL,
            created_by       uuid NULL REFERENCES users (id) ON DELETE RESTRICT,
            status           text NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            engine_version   text NOT NULL,
            grain            text NOT NULL CHECK (grain IN ('week', 'month')),
            as_of_date       date NULL,
            config           jsonb NOT NULL,
            config_sha256    text NOT NULL CHECK (config_sha256 ~ {HEX64}),
            label            text NOT NULL DEFAULT '',
            started_at       timestamptz NOT NULL,
            finished_at      timestamptz NULL,
            failure_code     text NULL,
            CONSTRAINT analysis_runs_finished_consistent CHECK ((status = 'running') = (finished_at IS NULL)),
            CONSTRAINT analysis_runs_failed_has_code CHECK (status <> 'failed' OR failure_code IS NOT NULL),
            CONSTRAINT analysis_runs_tenant_key UNIQUE (organization_id, store_id, id),
            CONSTRAINT analysis_runs_snapshot_key UNIQUE (organization_id, store_id, id, snapshot_id),
            CONSTRAINT analysis_runs_snapshot_fkey FOREIGN KEY (organization_id, store_id, snapshot_id)
                REFERENCES data_snapshots (organization_id, store_id, id) ON DELETE RESTRICT
        )
    """)
    op.execute("CREATE INDEX analysis_runs_store_recent_idx ON analysis_runs (organization_id, store_id, started_at DESC)")
    op.execute("CREATE INDEX analysis_runs_snapshot_idx ON analysis_runs (organization_id, store_id, snapshot_id)")
    # rejeu: une meme analyse (instantane, configuration, date de reference, moteur, libelle)
    # ne produit qu'une execution terminee
    op.execute("""
        CREATE UNIQUE INDEX analysis_runs_completed_uniq ON analysis_runs
            (organization_id, store_id, snapshot_id, config_sha256, engine_version, label,
             COALESCE(as_of_date, DATE '0001-01-01'))
            WHERE status = 'completed'
    """)

    op.execute("""
        CREATE FUNCTION analysis_runs_require_sealed_snapshot() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM data_snapshots s
                WHERE s.id = NEW.snapshot_id AND s.organization_id = NEW.organization_id AND s.status = 'completed'
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'an analysis run requires a completed data snapshot';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER analysis_runs_require_sealed_snapshot BEFORE INSERT ON analysis_runs
            FOR EACH ROW EXECUTE FUNCTION analysis_runs_require_sealed_snapshot()
    """)
    op.execute("""
        CREATE FUNCTION analysis_runs_guard_finished() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> 'running' THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'a finished analysis run is immutable';
            END IF;
            IF NEW.id <> OLD.id OR NEW.organization_id <> OLD.organization_id OR NEW.store_id <> OLD.store_id
               OR NEW.snapshot_id <> OLD.snapshot_id OR NEW.config_sha256 <> OLD.config_sha256
               OR NEW.engine_version <> OLD.engine_version THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'analysis run identity is immutable';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER analysis_runs_guard_finished BEFORE UPDATE ON analysis_runs
            FOR EACH ROW EXECUTE FUNCTION analysis_runs_guard_finished()
    """)

    op.execute(f"""
        CREATE TABLE reports (
            id                uuid PRIMARY KEY,
            organization_id   uuid NOT NULL,
            store_id          uuid NOT NULL,
            analysis_run_id   uuid NOT NULL,
            snapshot_id       uuid NOT NULL,
            engine_version    text NOT NULL,
            generated_at      timestamptz NOT NULL,
            grain             text NOT NULL CHECK (grain IN ('week', 'month')),
            period_label      text NOT NULL,
            period_start      date NOT NULL,
            period_end        date NOT NULL,
            currency          text NOT NULL,
            synthetic         boolean NOT NULL,
            payload_json      text NOT NULL,
            payload           jsonb NOT NULL,
            payload_sha256    text NOT NULL CHECK (payload_sha256 ~ {HEX64}),
            payload_bytes     integer NOT NULL CHECK (payload_bytes > 0),
            created_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT reports_payload_sha256_matches
                CHECK (payload_sha256 = encode(sha256(convert_to(payload_json, 'UTF8')), 'hex')),
            CONSTRAINT reports_payload_bytes_matches
                CHECK (payload_bytes = octet_length(convert_to(payload_json, 'UTF8'))),
            CONSTRAINT reports_period_ordered CHECK (period_end >= period_start),
            CONSTRAINT reports_run_key UNIQUE (organization_id, analysis_run_id),
            CONSTRAINT reports_tenant_key UNIQUE (organization_id, store_id, id),
            CONSTRAINT reports_run_fkey FOREIGN KEY (organization_id, store_id, analysis_run_id, snapshot_id)
                REFERENCES analysis_runs (organization_id, store_id, id, snapshot_id) ON DELETE RESTRICT,
            CONSTRAINT reports_snapshot_fkey FOREIGN KEY (organization_id, store_id, snapshot_id)
                REFERENCES data_snapshots (organization_id, store_id, id) ON DELETE RESTRICT
        )
    """)
    # "dernier rapport d'une boutique" et historique par periode
    op.execute("CREATE INDEX reports_store_recent_idx ON reports (organization_id, store_id, period_end DESC, created_at DESC)")
    op.execute("CREATE INDEX reports_snapshot_idx ON reports (organization_id, store_id, snapshot_id)")

    op.execute("""
        CREATE FUNCTION reports_forbid_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'a persisted report is immutable';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER reports_forbid_update BEFORE UPDATE ON reports
            FOR EACH ROW EXECUTE FUNCTION reports_forbid_update()
    """)

    for table in ("analysis_runs", "reports"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
                USING (organization_id = app_current_organization_id())
                WITH CHECK (organization_id = app_current_organization_id())
        """)
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (status, finished_at, failure_code) ON analysis_runs TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE reports")
    op.execute("DROP TABLE analysis_runs")
    op.execute("DROP FUNCTION reports_forbid_update()")
    op.execute("DROP FUNCTION analysis_runs_guard_finished()")
    op.execute("DROP FUNCTION analysis_runs_require_sealed_snapshot()")
