"""Instantanes de donnees immuables et entites canoniques.

Revision ID: 0002_snapshots
Revises: 0001_tenancy
Create Date: 2026-09-15

Un instantane (data_snapshots) est l'ensemble fige des entites canoniques
(domain.models) d'une boutique produit par UN import. Ses lignes canoniques lui
appartiennent (copie par import) et sont ordonnees par `position`: l'ordre des
listes du Dataset est restitue a l'identique, condition du rapport identique
octet pour octet (les sommes flottantes dependent de l'ordre).

Cycle de vie: 'ingesting' (dans la transaction d'import) -> 'completed' (scelle,
immuable) ; 'failed' (aucune ligne, motif conserve). Montants en numeric(19,4).

Toute cle naturelle (primaire ou unique) d'une donnee de tenant commence par
organization_id: un controle d'unicite ignore RLS et s'execute avant les cles
etrangeres; sans ce prefixe, une insertion portant les identifiants d'un autre
tenant echouerait en "doublon" et revelerait l'existence de sa ligne.

Seule la cle primaire (organization_id, snapshot_id, position) commence par
(organization_id, snapshot_id). Les cles uniques secondaires placent leur
identifiant metier avant snapshot_id: sinon, sur une table fraichement remplie
(sans statistiques), le plan generique du controle de cle etrangere des lignes de
commande choisissait un index unique secondaire filtre sur `position`, soit un
parcours de tout l'instantane par ligne inseree (import quadratique, mesure:
5,9 s au lieu de 0,6 s pour 10 000 commandes).
"""
from alembic import op

revision = "0002_snapshots"
down_revision = "0001_tenancy"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"
HEX64 = "'^[0-9a-f]{64}$'"

#: tables canoniques, dans l'ordre de creation (les lignes de commande dependent des commandes)
CANONICAL_TABLES = ("products", "orders", "order_lines", "payments", "refunds", "campaigns", "ad_daily_performance")

#: colonnes communes de provenance et d'appartenance d'une ligne canonique
_OWNERSHIP = """
    organization_id     uuid NOT NULL,
    store_id            uuid NOT NULL,
    snapshot_id         uuid NOT NULL,
    snapshot_source_id  uuid NOT NULL,
    position            integer NOT NULL CHECK (position >= 0),
    source              text NOT NULL,
    source_record_id    text NOT NULL,
"""

_OWNERSHIP_KEYS = """
    FOREIGN KEY (organization_id, store_id, snapshot_id)
        REFERENCES data_snapshots (organization_id, store_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, store_id, snapshot_id, snapshot_source_id)
        REFERENCES snapshot_sources (organization_id, store_id, snapshot_id, id) ON DELETE CASCADE,
    PRIMARY KEY (organization_id, snapshot_id, position),
    UNIQUE (organization_id, source_record_id, snapshot_id, source)
"""


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE data_snapshots (
            id                      uuid PRIMARY KEY,
            organization_id         uuid NOT NULL,
            store_id                uuid NOT NULL,
            connection_id           uuid NOT NULL,
            created_by              uuid NULL REFERENCES users (id) ON DELETE RESTRICT,
            source                  text NOT NULL CHECK (source IN ('csv')),
            connector               text NOT NULL,
            connector_version       text NOT NULL,
            schema_version          text NOT NULL,
            normalization_version   text NOT NULL,
            status                  text NOT NULL CHECK (status IN ('ingesting', 'completed', 'failed')),
            ingestion_started_at    timestamptz NOT NULL,
            ingested_at             timestamptz NULL,
            failure_code            text NULL,
            inputs_sha256           text NULL CHECK (inputs_sha256 ~ {HEX64}),
            currency                text NULL,
            source_period_start     timestamptz NULL,
            source_period_end       timestamptz NULL,
            row_count               bigint NULL CHECK (row_count >= 0),
            record_counts           jsonb NULL,
            quality                 jsonb NULL,
            synthetic               boolean NOT NULL,
            not_for_production      boolean NOT NULL,
            synthetic_manifest      jsonb NULL,
            supersedes_snapshot_id  uuid NULL,
            CONSTRAINT data_snapshots_synthetic_never_production CHECK (NOT synthetic OR not_for_production),
            CONSTRAINT data_snapshots_completed_is_complete CHECK (
                status <> 'completed' OR (
                    ingested_at IS NOT NULL AND inputs_sha256 IS NOT NULL AND currency IS NOT NULL
                    AND row_count IS NOT NULL AND record_counts IS NOT NULL AND quality IS NOT NULL)),
            CONSTRAINT data_snapshots_failed_has_code CHECK (status <> 'failed' OR failure_code IS NOT NULL),
            CONSTRAINT data_snapshots_period_ordered CHECK (source_period_end >= source_period_start),
            CONSTRAINT data_snapshots_tenant_key UNIQUE (organization_id, store_id, id),
            CONSTRAINT data_snapshots_store_fkey FOREIGN KEY (organization_id, store_id)
                REFERENCES stores (organization_id, id) ON DELETE RESTRICT,
            CONSTRAINT data_snapshots_connection_fkey FOREIGN KEY (organization_id, store_id, connection_id)
                REFERENCES connections (organization_id, store_id, id) ON DELETE RESTRICT,
            CONSTRAINT data_snapshots_supersedes_fkey FOREIGN KEY (organization_id, store_id, supersedes_snapshot_id)
                REFERENCES data_snapshots (organization_id, store_id, id) ON DELETE RESTRICT
        )
    """)
    # idempotence d'import: memes octets + memes versions de normalisation = meme instantane
    op.execute("""
        CREATE UNIQUE INDEX data_snapshots_inputs_uniq ON data_snapshots (organization_id, store_id, inputs_sha256)
            WHERE status = 'completed'
    """)
    # "derniers instantanes d'une boutique"
    op.execute("CREATE INDEX data_snapshots_store_recent_idx ON data_snapshots (organization_id, store_id, ingestion_started_at DESC)")
    op.execute("CREATE INDEX data_snapshots_connection_idx ON data_snapshots (organization_id, store_id, connection_id)")

    op.execute(f"""
        CREATE TABLE snapshot_sources (
            id               uuid PRIMARY KEY,
            organization_id  uuid NOT NULL,
            store_id         uuid NOT NULL,
            snapshot_id      uuid NOT NULL,
            source_kind      text NOT NULL CHECK (source_kind IN ('shopify_orders', 'shopify_products', 'stripe', 'google_ads')),
            file_sha256      text NOT NULL CHECK (file_sha256 ~ {HEX64}),
            byte_size        bigint NOT NULL CHECK (byte_size >= 0),
            rows_read        bigint NULL CHECK (rows_read >= 0),
            rows_accepted    bigint NULL CHECK (rows_accepted >= 0),
            CONSTRAINT snapshot_sources_kind_key UNIQUE (organization_id, snapshot_id, source_kind),
            CONSTRAINT snapshot_sources_tenant_key UNIQUE (organization_id, store_id, snapshot_id, id),
            CONSTRAINT snapshot_sources_snapshot_fkey FOREIGN KEY (organization_id, store_id, snapshot_id)
                REFERENCES data_snapshots (organization_id, store_id, id) ON DELETE CASCADE
        )
    """)

    op.execute(f"""
        CREATE TABLE products (
            {_OWNERSHIP}
            sku          text NOT NULL,
            product_ref  text NOT NULL,
            title        text NOT NULL,
            unit_cogs    numeric(19,4) NULL,
            {_OWNERSHIP_KEYS},
            UNIQUE (organization_id, sku, snapshot_id)
        )
    """)
    op.execute(f"""
        CREATE TABLE orders (
            {_OWNERSHIP}
            order_ref         text NOT NULL,
            customer_ref      text NOT NULL,
            customer_email    text NULL,
            created_at        timestamptz NOT NULL,
            currency          text NOT NULL,
            subtotal          numeric(19,4) NOT NULL,
            discount          numeric(19,4) NOT NULL,
            shipping          numeric(19,4) NOT NULL,
            tax               numeric(19,4) NOT NULL,
            total             numeric(19,4) NOT NULL,
            financial_status  text NOT NULL,
            {_OWNERSHIP_KEYS},
            UNIQUE (organization_id, order_ref, snapshot_id)
        )
    """)
    # Une ligne de commande n'a pas d'identifiant dans l'export CSV: son identite est
    # (commande, rang), rattachee a la commande du meme tenant et du meme instantane.
    op.execute("""
        CREATE TABLE order_lines (
            organization_id  uuid NOT NULL,
            store_id         uuid NOT NULL,
            snapshot_id      uuid NOT NULL,
            order_position   integer NOT NULL,
            position         integer NOT NULL CHECK (position >= 0),
            sku              text NOT NULL,
            title            text NOT NULL,
            quantity         bigint NOT NULL,
            unit_price       numeric(19,4) NOT NULL,
            product_ref      text NULL,
            PRIMARY KEY (organization_id, snapshot_id, order_position, position),
            FOREIGN KEY (organization_id, snapshot_id, order_position)
                REFERENCES orders (organization_id, snapshot_id, position) ON DELETE CASCADE,
            FOREIGN KEY (organization_id, store_id, snapshot_id)
                REFERENCES data_snapshots (organization_id, store_id, id) ON DELETE CASCADE
        )
    """)
    op.execute(f"""
        CREATE TABLE payments (
            {_OWNERSHIP}
            payment_ref     text NOT NULL,
            created_at      timestamptz NOT NULL,
            amount          numeric(19,4) NOT NULL,
            fee             numeric(19,4) NULL,
            net             numeric(19,4) NULL,
            status          text NOT NULL,
            order_ref       text NULL,
            customer_email  text NULL,
            {_OWNERSHIP_KEYS}
        )
    """)
    op.execute(f"""
        CREATE TABLE refunds (
            {_OWNERSHIP}
            refund_ref  text NOT NULL,
            created_at  timestamptz NOT NULL,
            amount      numeric(19,4) NOT NULL,
            order_ref   text NULL,
            {_OWNERSHIP_KEYS}
        )
    """)
    op.execute(f"""
        CREATE TABLE campaigns (
            {_OWNERSHIP}
            campaign_ref  text NOT NULL,
            name          text NOT NULL,
            channel       text NOT NULL,
            {_OWNERSHIP_KEYS},
            UNIQUE (organization_id, campaign_ref, snapshot_id)
        )
    """)
    # conversions: compte fractionnaire attribue par la regie, pas un montant -> double precision
    # (aller-retour float8 exact). conversion_value et spend sont des montants -> numeric.
    op.execute(f"""
        CREATE TABLE ad_daily_performance (
            {_OWNERSHIP}
            campaign_ref      text NOT NULL,
            day               date NOT NULL,
            spend             numeric(19,4) NOT NULL,
            impressions       bigint NOT NULL,
            clicks            bigint NOT NULL,
            conversions       double precision NOT NULL,
            conversion_value  numeric(19,4) NOT NULL,
            {_OWNERSHIP_KEYS},
            UNIQUE (organization_id, campaign_ref, day, snapshot_id)
        )
    """)

    # --- immuabilite ---------------------------------------------------------------
    # Un instantane scelle (completed/failed) ne change plus. Seule une purge explicite
    # (role proprietaire, non accorde a mervio_app) peut le supprimer, et jamais s'il
    # est reference par une analyse (RESTRICT).
    op.execute("""
        CREATE FUNCTION data_snapshots_guard_sealed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> 'ingesting' THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'data snapshot is sealed and immutable',
                    DETAIL = 'snapshot ' || OLD.id::text || ' has status ' || OLD.status;
            END IF;
            IF NEW.id <> OLD.id OR NEW.organization_id <> OLD.organization_id OR NEW.store_id <> OLD.store_id
               OR NEW.connection_id <> OLD.connection_id OR NEW.source <> OLD.source
               OR NEW.ingestion_started_at <> OLD.ingestion_started_at THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'data snapshot identity is immutable';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER data_snapshots_guard_sealed BEFORE UPDATE ON data_snapshots
            FOR EACH ROW EXECUTE FUNCTION data_snapshots_guard_sealed()
    """)
    # Lignes canoniques: jamais modifiees; ajoutees ou supprimees seulement tant que leur
    # instantane est en cours d'ingestion. Une suppression en cascade (instantane purge)
    # passe: l'instantane n'existe deja plus au moment du controle.
    op.execute("""
        CREATE FUNCTION canonical_rows_guard_sealed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM changed_rows c JOIN data_snapshots s ON s.id = c.snapshot_id
                WHERE s.status <> 'ingesting'
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'records of a sealed data snapshot are immutable',
                    DETAIL = 'table ' || TG_TABLE_NAME || ', operation ' || TG_OP;
            END IF;
            RETURN NULL;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION canonical_rows_forbid_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'canonical records are immutable',
                DETAIL = 'table ' || TG_TABLE_NAME;
        END
        $$
    """)
    for table in ("snapshot_sources",) + CANONICAL_TABLES:
        op.execute(f"""
            CREATE TRIGGER {table}_guard_insert AFTER INSERT ON {table}
                REFERENCING NEW TABLE AS changed_rows
                FOR EACH STATEMENT EXECUTE FUNCTION canonical_rows_guard_sealed()
        """)
        op.execute(f"""
            CREATE TRIGGER {table}_guard_delete AFTER DELETE ON {table}
                REFERENCING OLD TABLE AS changed_rows
                FOR EACH STATEMENT EXECUTE FUNCTION canonical_rows_guard_sealed()
        """)
        op.execute(f"""
            CREATE TRIGGER {table}_forbid_update BEFORE UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION canonical_rows_forbid_update()
        """)

    # --- RLS et droits -----------------------------------------------------------
    for table in ("data_snapshots", "snapshot_sources") + CANONICAL_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
                USING (organization_id = app_current_organization_id())
                WITH CHECK (organization_id = app_current_organization_id())
        """)
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
    op.execute(f"""
        GRANT UPDATE (status, ingested_at, failure_code, inputs_sha256, currency, source_period_start,
                      source_period_end, row_count, record_counts, quality)
        ON data_snapshots TO {APP_ROLE}
    """)


def downgrade() -> None:
    for table in ("ad_daily_performance", "campaigns", "refunds", "payments", "order_lines", "orders", "products",
                  "snapshot_sources", "data_snapshots"):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION canonical_rows_forbid_update()")
    op.execute("DROP FUNCTION canonical_rows_guard_sealed()")
    op.execute("DROP FUNCTION data_snapshots_guard_sealed()")
