"""Immuniser les fonctions de securite contre l'ombrage par pg_temp (Mission 004.3.10).

PROBLEME (reproduit)
- Les fonctions d'identite et d'autorisation de la revision 0007 referencent leurs tables
  par un nom NON qualifie (`users`, `service_authorizations`, `memberships`). PostgreSQL
  recherche IMPLICITEMENT le schema temporaire `pg_temp` EN PREMIER pour les noms de relation
  quand `pg_temp` n'est pas nomme explicitement dans `search_path` (verifie: avec
  `search_path = pg_catalog, public` un `SELECT ... FROM users` resout vers `pg_temp.users`).
- Un role de service (`mervio_worker`) qui possede le privilege `TEMPORARY` sur la base peut
  donc creer `CREATE TEMP TABLE service_authorizations(...)`, y inserer une ligne forgee, et
  les politiques RLS (via `app_service_authorized()` / `app_service_organizations()`, fonctions
  SQL SECURITY INVOKER sans `search_path` epingle) lisent CETTE table temporaire au lieu de la
  vraie. Resultat: lecture INTER-LOCATAIRE (stores, instantanes) d'une organisation qui n'a
  jamais autorise le service. `app_ensure_service_principal()` (SECURITY DEFINER) est ombrable
  de la meme facon malgre son `SET search_path = pg_catalog, public`, car `pg_temp` reste
  implicitement premier.

PORTEE REELLE
- Le bootstrap Docker retire `TEMPORARY` a PUBLIC (`REVOKE ALL ON DATABASE ... FROM PUBLIC`),
  ce qui rend l'attaque IRREALISABLE dans le deploiement livre (le worker ne peut creer aucune
  table temporaire). Elle reste realisable dans tout deploiement Alembic-seul qui ne reproduit
  pas ce REVOKE, et dans la configuration des tests. La garantie affichee par 0007 -- "toutes
  les garanties sont portees par PostgreSQL" -- ne doit pas dependre d'un GRANT de base.

CORRECTIF (defense en profondeur, minimal)
- Qualifier explicitement en `public.` chaque relation lue par une fonction de securite. La
  qualification est IMMUNE a `search_path` (aucun ordre a respecter) et, contrairement a un
  `SET search_path` sur une fonction SQL, elle NE desactive PAS l'inlining du planificateur:
  les plans de la file (jobs_ready_idx, evaluation InitPlan unique) restent identiques (verifie).
- `CREATE OR REPLACE` conserve le proprietaire (definisseur), les GRANT/REVOKE d'EXECUTE et le
  statut SECURITY DEFINER; on les redeclare a l'identique. Aucune table, aucune politique, aucun
  privilege modifie.

Hors portee (meme classe, severite moindre, NON reproduits comme escalade inter-locataire, laisses
en risque residuel documente): les gardes d'instantanes 0002/0003 (`data_snapshots`) et la
sous-requete `FROM jobs` inline de la politique `audit_events_service_actor` (0007).
"""
from alembic import op

revision = "0009_qualify_security_functions"
down_revision = "0008_admin_audit"
branch_labels = None
depends_on = None

WORKER_ROLE = "mervio_worker"

# --- definitions QUALIFIEES (correctif) --------------------------------------------
QUALIFIED = [
    f"""
    CREATE OR REPLACE FUNCTION app_current_service_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
        SELECT u.id FROM public.users u
        WHERE u.kind = 'service' AND u.idp_subject = 'service:' || current_user
          AND pg_catalog.pg_has_role(current_user, '{WORKER_ROLE}', 'USAGE')
    $$
    """,
    f"""
    CREATE OR REPLACE FUNCTION app_ensure_service_principal() RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        subject text := 'service:' || session_user;
        principal uuid;
    BEGIN
        IF NOT pg_catalog.pg_has_role(session_user, '{WORKER_ROLE}', 'USAGE') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'insufficient_privilege',
                MESSAGE = 'the connected role is not a service role';
        END IF;
        INSERT INTO public.users (id, idp_subject, kind)
            VALUES (pg_catalog.gen_random_uuid(), subject, 'service')
            ON CONFLICT (idp_subject) DO NOTHING;
        SELECT id INTO principal FROM public.users WHERE idp_subject = subject AND kind = 'service';
        IF principal IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'the service subject belongs to a human';
        END IF;
        RETURN principal;
    END
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION app_service_authorized(p_organization_id uuid) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
        SELECT EXISTS (
            SELECT 1 FROM public.service_authorizations sa
            WHERE sa.organization_id = p_organization_id
              AND sa.service_user_id = app_current_service_id()
              AND sa.revoked_at IS NULL)
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION app_service_organizations() RETURNS uuid[]
    LANGUAGE sql STABLE
    AS $$
        SELECT COALESCE(array_agg(sa.organization_id), '{}'::uuid[])
        FROM public.service_authorizations sa
        WHERE sa.service_user_id = app_current_service_id()
          AND sa.revoked_at IS NULL
          AND (app_current_organization_id() IS NULL OR sa.organization_id = app_current_organization_id())
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION app_delegate_rank() RETURNS integer
    LANGUAGE sql STABLE
    AS $$
        SELECT CASE m.role WHEN 'viewer' THEN 0 WHEN 'analyst' THEN 1 WHEN 'admin' THEN 2 WHEN 'owner' THEN 3 END
        FROM public.memberships m JOIN public.users u ON u.id = m.user_id
        WHERE m.organization_id = app_current_organization_id()
          AND m.user_id = app_current_user_id()
          AND u.kind = 'human'
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION memberships_forbid_service_users() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM public.users WHERE id = NEW.user_id AND kind = 'service') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'a service principal is never a member';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION service_authorizations_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.revoked_at IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'an authorization is granted active';
            END IF;
        ELSE
            IF OLD.revoked_at IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'a revoked authorization is immutable';
            END IF;
            IF NEW.id <> OLD.id OR NEW.organization_id <> OLD.organization_id
               OR NEW.service_user_id <> OLD.service_user_id OR NEW.granted_by <> OLD.granted_by
               OR NEW.granted_at <> OLD.granted_at THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'an authorization only changes by being revoked';
            END IF;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM public.users WHERE id = NEW.service_user_id AND kind = 'service') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'only a service principal is authorized';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM public.users WHERE id = NEW.granted_by AND kind = 'human')
           OR (NEW.revoked_by IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM public.users WHERE id = NEW.revoked_by AND kind = 'human')) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'only a human grants or revokes an authorization';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION audit_events_guard_actors() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.actor_type = 'user' AND NEW.actor_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM public.users WHERE id = NEW.actor_id AND kind = 'human') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'a user event names a human actor';
        END IF;
        IF NEW.on_behalf_of IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM public.users WHERE id = NEW.on_behalf_of AND kind = 'human') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'work is done on behalf of a human';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION jobs_enqueued_by_human() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.enqueued_by IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM public.users WHERE id = NEW.enqueued_by AND kind = 'human') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'a job is requested by a human';
        END IF;
        RETURN NEW;
    END
    $$
    """,
]

# --- definitions d'origine 0007 (NON qualifiees), pour la descente -----------------
ORIGINAL = [
    "CREATE OR REPLACE FUNCTION app_current_service_id() RETURNS uuid\n    LANGUAGE sql STABLE\n    AS $body$\n            SELECT u.id FROM users u\n            WHERE u.kind = 'service' AND u.idp_subject = 'service:' || current_user\n              AND pg_has_role(current_user, 'mervio_worker', 'USAGE')\n        $body$\n",
    "CREATE OR REPLACE FUNCTION app_ensure_service_principal() RETURNS uuid\n    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public\n    AS $body$\n        DECLARE\n            subject text := 'service:' || session_user;\n            principal uuid;\n        BEGIN\n            IF NOT pg_has_role(session_user, 'mervio_worker', 'USAGE') THEN\n                RAISE EXCEPTION USING\n                    ERRCODE = 'insufficient_privilege',\n                    MESSAGE = 'the connected role is not a service role';\n            END IF;\n            INSERT INTO users (id, idp_subject, kind) VALUES (gen_random_uuid(), subject, 'service')\n                ON CONFLICT (idp_subject) DO NOTHING;\n            SELECT id INTO principal FROM users WHERE idp_subject = subject AND kind = 'service';\n            IF principal IS NULL THEN\n                RAISE EXCEPTION USING\n                    ERRCODE = 'restrict_violation',\n                    MESSAGE = 'the service subject belongs to a human';\n            END IF;\n            RETURN principal;\n        END\n        $body$\n",
    'CREATE OR REPLACE FUNCTION app_service_authorized(p_organization_id uuid) RETURNS boolean\n    LANGUAGE sql STABLE\n    AS $body$\n            SELECT EXISTS (\n                SELECT 1 FROM service_authorizations sa\n                WHERE sa.organization_id = p_organization_id\n                  AND sa.service_user_id = app_current_service_id()\n                  AND sa.revoked_at IS NULL)\n        $body$\n',
    "CREATE OR REPLACE FUNCTION app_service_organizations() RETURNS uuid[]\n    LANGUAGE sql STABLE\n    AS $body$\n            SELECT COALESCE(array_agg(sa.organization_id), '{}'::uuid[])\n            FROM service_authorizations sa\n            WHERE sa.service_user_id = app_current_service_id()\n              AND sa.revoked_at IS NULL\n              AND (app_current_organization_id() IS NULL OR sa.organization_id = app_current_organization_id())\n        $body$\n",
    "CREATE OR REPLACE FUNCTION app_delegate_rank() RETURNS integer\n    LANGUAGE sql STABLE\n    AS $body$\n            SELECT CASE m.role WHEN 'viewer' THEN 0 WHEN 'analyst' THEN 1 WHEN 'admin' THEN 2 WHEN 'owner' THEN 3 END\n            FROM memberships m JOIN users u ON u.id = m.user_id\n            WHERE m.organization_id = app_current_organization_id()\n              AND m.user_id = app_current_user_id()\n              AND u.kind = 'human'\n        $body$\n",
    "CREATE OR REPLACE FUNCTION memberships_forbid_service_users() RETURNS trigger\n    LANGUAGE plpgsql VOLATILE\n    AS $body$\n        BEGIN\n            IF EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id AND kind = 'service') THEN\n                RAISE EXCEPTION USING\n                    ERRCODE = 'restrict_violation',\n                    MESSAGE = 'a service principal is never a member';\n            END IF;\n            RETURN NEW;\n        END\n        $body$\n",
    "CREATE OR REPLACE FUNCTION service_authorizations_guard() RETURNS trigger\n    LANGUAGE plpgsql VOLATILE\n    AS $body$\n        BEGIN\n            IF TG_OP = 'INSERT' THEN\n                IF NEW.revoked_at IS NOT NULL THEN\n                    RAISE EXCEPTION USING\n                        ERRCODE = 'restrict_violation',\n                        MESSAGE = 'an authorization is granted active';\n                END IF;\n            ELSE\n                IF OLD.revoked_at IS NOT NULL THEN\n                    RAISE EXCEPTION USING\n                        ERRCODE = 'restrict_violation',\n                        MESSAGE = 'a revoked authorization is immutable';\n                END IF;\n                IF NEW.id <> OLD.id OR NEW.organization_id <> OLD.organization_id\n                   OR NEW.service_user_id <> OLD.service_user_id OR NEW.granted_by <> OLD.granted_by\n                   OR NEW.granted_at <> OLD.granted_at THEN\n                    RAISE EXCEPTION USING\n                        ERRCODE = 'restrict_violation',\n                        MESSAGE = 'an authorization only changes by being revoked';\n                END IF;\n            END IF;\n            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.service_user_id AND kind = 'service') THEN\n                RAISE EXCEPTION USING\n                    ERRCODE = 'restrict_violation',\n                    MESSAGE = 'only a service principal is authorized';\n            END IF;\n            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.granted_by AND kind = 'human')\n               OR (NEW.revoked_by IS NOT NULL\n                   AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.revoked_by AND kind = 'human')) THEN\n                RAISE EXCEPTION USING\n                    ERRCODE = 'restrict_violation',\n                    MESSAGE = 'only a human grants or revokes an authorization';\n            END IF;\n            RETURN NEW;\n        END\n        $body$\n",
    "CREATE OR REPLACE FUNCTION audit_events_guard_actors() RETURNS trigger\n    LANGUAGE plpgsql VOLATILE\n    AS $body$\n        BEGIN\n            -- acteur absent: laisse a la contrainte audit_events_user_actor_identified (0005)\n            IF NEW.actor_type = 'user' AND NEW.actor_id IS NOT NULL\n               AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.actor_id AND kind = 'human') THEN\n                RAISE EXCEPTION USING\n                    ERRCODE = 'restrict_violation',\n                    MESSAGE = 'a user event names a human actor';\n            END IF;\n            IF NEW.on_behalf_of IS NOT NULL\n               AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.on_behalf_of AND kind = 'human') THEN\n                RAISE EXCEPTION USING\n                    ERRCODE = 'restrict_violation',\n                    MESSAGE = 'work is done on behalf of a human';\n            END IF;\n            RETURN NEW;\n        END\n        $body$\n",
    "CREATE OR REPLACE FUNCTION jobs_enqueued_by_human() RETURNS trigger\n    LANGUAGE plpgsql VOLATILE\n    AS $body$\n        BEGIN\n            IF NEW.enqueued_by IS NOT NULL\n               AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.enqueued_by AND kind = 'human') THEN\n                RAISE EXCEPTION USING\n                    ERRCODE = 'restrict_violation',\n                    MESSAGE = 'a job is requested by a human';\n            END IF;\n            RETURN NEW;\n        END\n        $body$\n",
]


def upgrade() -> None:
    for statement in QUALIFIED:
        op.execute(statement)


def downgrade() -> None:
    for statement in ORIGINAL:
        op.execute(statement)
