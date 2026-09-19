"""Identite client a cle et retrait de l'e-mail persiste (Mission 004.4.2, D-053, D-057, D-058).

Revision ID: 0011_identity_pii_schema
Revises: 0010_qualify_residual_security
Create Date: 2026-09-18

CE QUE FAIT CETTE REVISION
- Refuse de s'appliquer si un instantane NON synthetique existe (D-057): ses references client
  sont des e-mails et aucune conversion n'est faite en migration (la cle maitre n'y est jamais).
- Supprime `orders.customer_email` et `payments.customer_email`: plus aucun e-mail persiste.
- `orders_customer_ref_keyed` (NOT VALID): toute NOUVELLE reference client a la forme d'un HMAC
  (`c1:`/`g1:` + 32 hex) ou d'un tombstone d'effacement (`redacted:<uuid>`, 004.4.5). Un e-mail est
  refuse par la base. NOT VALID: les anciennes lignes synthetiques (reference = e-mail synthetique,
  normalization_version "mervio-ingestion/0.1.0") ne sont pas revalidees et restent analysables.
- `organization_identity_keys`: le SEL d'identite de chaque organisation (32 octets), jamais la cle.
  Creee VIDE: la ligne apparait a la premiere utilisation (une migration ne connait pas la cle
  maitre). RLS activee et forcee; worker soumis a l'autorisation de service et a un delegue de rang
  analyst au moins; SELECT et INSERT seulement: ni UPDATE ni DELETE accordes. Un declencheur
  n'autorise qu'UNE transition: sel present -> detruit (004.4.6). Il ne lit aucune table.
- `stores.timezone` (defaut 'UTC') et `stores.timezone_source` ('default' tant que non choisi):
  schema seulement; la validation IANA et l'usage viennent en 004.4.3 / 004.5.
- `connections.credential_ref`: reference future a un secret stocke ailleurs (004.11); aucune valeur
  n'est possible sur une connexion `csv_upload`. Aucun secret en base.

GARDE D-057 ET RLS FORCEE
Le proprietaire est soumis a la RLS forcee: sans contexte d'organisation, `SELECT ... FROM
data_snapshots` ne voit AUCUNE ligne et le garde passerait en silence. La RLS est donc levee
(NO FORCE) le temps du controle puis retablie (FORCE) dans la meme transaction (Alembic:
`transaction_per_migration`). En cas de refus, `RAISE` annule toute la transaction, NO FORCE compris.
Le message ne contient aucune donnee.

DESCENTE
Refusee si une cle d'identite existe: detruire des sels casserait la continuite des references.
Sinon, les colonnes `customer_email` sont recreees VIDES (text NULL): les anciennes valeurs ne sont
PAS restaurees (elles n'existent plus nulle part).

Aucune fonction SECURITY DEFINER, aucun role, aucun index sur customer_ref.
"""
from alembic import op

revision = "0011_identity_pii_schema"
down_revision = "0010_qualify_residual_security"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"
WORKER_ROLE = "mervio_worker"
KEYS = "organization_identity_keys"
#: memes expressions que 0007 (un booleen par instruction, aucun cout par ligne)
AUTHORIZED_CONTEXT = "(SELECT app_service_authorized(app_current_organization_id()))"
DELEGATE_RANK = "(SELECT app_delegate_rank())"
CUSTOMER_REF_CHECK = (
    "customer_ref ~ '^(c1|g1):[0-9a-f]{32}$' OR customer_ref ~ '^redacted:[0-9a-f-]{36}$'"
)


def _refuse_if_rows(table: str, condition: str, message: str, hint: str) -> None:
    """Controle vu par le proprietaire SANS RLS, puis RLS forcee retablie (meme transaction)."""
    op.execute(f"ALTER TABLE public.{table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.{table} WHERE {condition}) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = '{message}',
                    HINT = '{hint}';
            END IF;
        END
        $$
    """)
    op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    # --- D-057: aucune conversion silencieuse d'une identite reelle ------------------------
    _refuse_if_rows(
        "data_snapshots", "NOT synthetic",
        "0011_identity_pii_schema refused: non-synthetic data snapshots exist (customer references are e-mails)",
        "D-057: no silent re-keying. Purge or re-import these snapshots in a development database, then migrate.",
    )

    # --- plus aucun e-mail persiste ---------------------------------------------------------
    op.execute("ALTER TABLE public.orders DROP COLUMN customer_email")
    op.execute("ALTER TABLE public.payments DROP COLUMN customer_email")
    op.execute(f"ALTER TABLE public.orders ADD CONSTRAINT orders_customer_ref_keyed CHECK ({CUSTOMER_REF_CHECK}) "
               "NOT VALID")

    # --- contrat boutique et connexion (schema seulement) ----------------------------------
    op.execute("""
        ALTER TABLE public.stores
            ADD COLUMN timezone text NOT NULL DEFAULT 'UTC'
                CONSTRAINT stores_timezone_format
                CHECK (char_length(timezone) BETWEEN 1 AND 64 AND timezone ~ '^[A-Za-z0-9_+./-]+$'),
            ADD COLUMN timezone_source text NOT NULL DEFAULT 'default'
                CONSTRAINT stores_timezone_source_known CHECK (timezone_source IN ('default', 'explicit'))
    """)
    op.execute("""
        ALTER TABLE public.connections
            ADD COLUMN credential_ref text NULL,
            ADD CONSTRAINT connections_csv_has_no_credential CHECK (credential_ref IS NULL OR kind <> 'csv_upload')
    """)

    # --- sel d'identite d'organisation (D-053) ---------------------------------------------
    op.execute(f"""
        CREATE TABLE public.{KEYS} (
            organization_id  uuid PRIMARY KEY REFERENCES public.organizations (id) ON DELETE RESTRICT,
            scheme           text NOT NULL CONSTRAINT {KEYS}_scheme_known CHECK (scheme = 'hmac-sha256-v1'),
            salt             bytea NULL CONSTRAINT {KEYS}_salt_length CHECK (salt IS NULL OR octet_length(salt) = 32),
            master_key_id    text NOT NULL CONSTRAINT {KEYS}_master_key_id_format
                                 CHECK (master_key_id ~ '^[0-9a-f]{{16}}$'),
            created_at       timestamptz NOT NULL DEFAULT now(),
            destroyed_at     timestamptz NULL,
            CONSTRAINT {KEYS}_destruction_consistent CHECK ((salt IS NULL) = (destroyed_at IS NOT NULL))
        )
    """)
    # Une seule transition: sel present -> detruit. Aucune lecture de table (aucun ombrage pg_temp).
    op.execute(f"""
        CREATE FUNCTION public.{KEYS}_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'an organization identity key is never deleted, only destroyed';
            END IF;
            IF OLD.salt IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'a destroyed organization identity key is immutable';
            END IF;
            IF NEW.organization_id <> OLD.organization_id OR NEW.scheme <> OLD.scheme
               OR NEW.master_key_id <> OLD.master_key_id OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'organization identity key identity is immutable';
            END IF;
            IF NEW.salt IS NOT NULL OR NEW.destroyed_at IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'an organization identity key only changes by being destroyed';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute(f"""
        CREATE TRIGGER {KEYS}_guard BEFORE UPDATE OR DELETE ON public.{KEYS}
            FOR EACH ROW EXECUTE FUNCTION public.{KEYS}_guard()
    """)
    op.execute(f"ALTER TABLE public.{KEYS} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{KEYS} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY {KEYS}_tenant_isolation ON public.{KEYS}
            USING (organization_id = app_current_organization_id())
            WITH CHECK (organization_id = app_current_organization_id())
    """)
    for name, definition in _worker_policies():
        op.execute(f"CREATE POLICY {name} ON public.{KEYS} {definition}")
    op.execute(f"GRANT SELECT, INSERT ON public.{KEYS} TO {APP_ROLE}")


def _worker_policies():
    """Le worker (0007): organisation autorisee ET delegue humain de rang analyst au moins."""
    return [
        (f"{KEYS}_service_authorized", f"AS RESTRICTIVE FOR ALL TO {WORKER_ROLE} USING ({AUTHORIZED_CONTEXT})"),
        (f"{KEYS}_service_delegate_read",
         f"AS RESTRICTIVE FOR SELECT TO {WORKER_ROLE} USING ({DELEGATE_RANK} >= 1)"),
        (f"{KEYS}_service_delegate_insert",
         f"AS RESTRICTIVE FOR INSERT TO {WORKER_ROLE} WITH CHECK ({DELEGATE_RANK} >= 1)"),
        (f"{KEYS}_service_no_update", f"AS RESTRICTIVE FOR UPDATE TO {WORKER_ROLE} USING (false)"),
        (f"{KEYS}_service_no_delete", f"AS RESTRICTIVE FOR DELETE TO {WORKER_ROLE} USING (false)"),
    ]


def downgrade() -> None:
    _refuse_if_rows(
        KEYS, "true",
        "0011_identity_pii_schema downgrade refused: organization identity keys exist",
        "Dropping identity salts would break customer reference continuity (D-053). Downgrade only an unused schema.",
    )
    op.execute(f"REVOKE ALL ON public.{KEYS} FROM {APP_ROLE}")
    op.execute(f"DROP TABLE public.{KEYS}")
    op.execute(f"DROP FUNCTION public.{KEYS}_guard()")
    op.execute("""
        ALTER TABLE public.connections
            DROP CONSTRAINT connections_csv_has_no_credential,
            DROP COLUMN credential_ref
    """)
    op.execute("ALTER TABLE public.stores DROP COLUMN timezone_source, DROP COLUMN timezone")
    op.execute("ALTER TABLE public.orders DROP CONSTRAINT orders_customer_ref_keyed")
    # colonnes recreees VIDES: les e-mails supprimes ne sont pas (et ne peuvent pas etre) restaures
    op.execute("ALTER TABLE public.payments ADD COLUMN customer_email text NULL")
    op.execute("ALTER TABLE public.orders ADD COLUMN customer_email text NULL")
