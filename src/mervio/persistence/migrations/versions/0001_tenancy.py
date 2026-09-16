"""Tenancy: organisations, utilisateurs, appartenances, boutiques, connexions + RLS.

Revision ID: 0001_tenancy
Revises:
Create Date: 2026-09-15

Conventions de toutes les migrations Mervio:
- SQL explicite, jamais d'autogeneration; aucun caractere pourcent dans le SQL
  (aucune interpolation possible par le driver);
- toute table metier porte organization_id et une politique RLS FORCEE: le
  proprietaire des tables y est soumis aussi, seul un superutilisateur y echappe;
- cles etrangeres composites (organization_id, ...) pour qu'une reference vers
  la ligne d'un autre tenant soit impossible meme si la ligne existe
  (les controles de cle etrangere ignorent RLS);
- le role applicatif mervio_app n'est ni proprietaire, ni superutilisateur, ni
  BYPASSRLS; ses droits UPDATE sont limites colonne par colonne.
"""
from alembic import op

revision = "0001_tenancy"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"


def upgrade() -> None:
    # Role de groupe applicatif. Global au cluster: cree s'il manque, jamais supprime
    # par une migration (d'autres bases du cluster peuvent l'utiliser).
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mervio_app') THEN
                CREATE ROLE mervio_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
            END IF;
        END
        $$
    """)

    # Contexte tenant: positionne par transaction (set_config(..., true) = SET LOCAL).
    # Absent -> NULL -> aucune ligne visible, aucune ecriture possible (echec ferme).
    op.execute("""
        CREATE FUNCTION app_current_organization_id() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT NULLIF(current_setting('app.organization_id', true), '')::uuid $$
    """)
    op.execute("""
        CREATE FUNCTION app_current_user_id() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$
    """)

    op.execute("""
        CREATE TABLE organizations (
            id          uuid PRIMARY KEY,
            name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
            created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)

    # Identite plateforme (sujet OIDC opaque, 004.3). Pas une donnee de tenant, pas de PII:
    # aucun email ni nom ici. Seule table sans organization_id ni RLS (voir DECISIONS).
    op.execute("""
        CREATE TABLE users (
            id           uuid PRIMARY KEY,
            idp_subject  text NOT NULL UNIQUE CHECK (char_length(idp_subject) BETWEEN 1 AND 255),
            created_at   timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE memberships (
            id               uuid PRIMARY KEY,
            organization_id  uuid NOT NULL REFERENCES organizations (id) ON DELETE RESTRICT,
            user_id          uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
            role             text NOT NULL CHECK (role IN ('owner', 'admin', 'analyst', 'viewer')),
            created_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT memberships_organization_user_key UNIQUE (organization_id, user_id)
        )
    """)
    op.execute("CREATE INDEX memberships_user_idx ON memberships (user_id)")

    op.execute("""
        CREATE TABLE stores (
            id               uuid PRIMARY KEY,
            organization_id  uuid NOT NULL REFERENCES organizations (id) ON DELETE RESTRICT,
            name             text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
            currency         text NULL CHECK (currency ~ '^[A-Z]{3}$'),
            created_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT stores_organization_id_key UNIQUE (organization_id, id)
        )
    """)

    op.execute("""
        CREATE TABLE connections (
            id               uuid PRIMARY KEY,
            organization_id  uuid NOT NULL,
            store_id         uuid NOT NULL,
            kind             text NOT NULL CHECK (kind IN ('csv_upload')),
            label            text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 200),
            status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
            created_at       timestamptz NOT NULL DEFAULT now(),
            revoked_at       timestamptz NULL,
            CONSTRAINT connections_revocation_consistent CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
            CONSTRAINT connections_tenant_key UNIQUE (organization_id, store_id, id),
            CONSTRAINT connections_store_fkey FOREIGN KEY (organization_id, store_id)
                REFERENCES stores (organization_id, id) ON DELETE RESTRICT
        )
    """)

    for table in ("organizations", "memberships", "stores", "connections"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute("""
        CREATE POLICY organizations_tenant_isolation ON organizations
            USING (id = app_current_organization_id())
            WITH CHECK (id = app_current_organization_id())
    """)
    # Appartenances: celles de l'organisation courante, plus celles de l'utilisateur
    # courant dans toutes ses organisations (liste "mes organisations"), en lecture seule.
    op.execute("""
        CREATE POLICY memberships_tenant_isolation ON memberships
            USING (organization_id = app_current_organization_id())
            WITH CHECK (organization_id = app_current_organization_id())
    """)
    op.execute("""
        CREATE POLICY memberships_own_rows_readable ON memberships FOR SELECT
            USING (user_id = app_current_user_id())
    """)
    for table in ("stores", "connections"):
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
                USING (organization_id = app_current_organization_id())
                WITH CHECK (organization_id = app_current_organization_id())
        """)

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON organizations, users, memberships, stores, connections TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (name) ON organizations TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (role) ON memberships TO {APP_ROLE}")
    op.execute(f"GRANT DELETE ON memberships TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (name, currency) ON stores TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (label, status, revoked_at) ON connections TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON organizations, users, memberships, stores, connections FROM {APP_ROLE}")
    op.execute("DROP TABLE connections")
    op.execute("DROP TABLE stores")
    op.execute("DROP TABLE memberships")
    op.execute("DROP TABLE users")
    op.execute("DROP TABLE organizations")
    op.execute("DROP FUNCTION app_current_user_id()")
    op.execute("DROP FUNCTION app_current_organization_id()")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
