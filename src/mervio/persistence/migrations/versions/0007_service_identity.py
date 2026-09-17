"""Identite de service et autorisations explicites par organisation (Mission 004.3).

Revision ID: 0007_service_identity
Revises: 0006_job_leases
Create Date: 2026-09-16

Un worker n'est plus un humain deguise (SEC-06). Toutes les garanties ci-dessous sont
portees par PostgreSQL, pas par une classe Python:

IDENTITE
- `users.kind` distingue `human` et `service`. Un sujet `service:` est reserve aux
  principaux de service (CHECK), et le role applicatif ne peut pas choisir `kind`
  (droit INSERT limite a `id, idp_subject`).
- Le principal d'un service est DERIVE DU ROLE DE CONNEXION: `service:<current_user>`,
  pour un role qui herite de `mervio_worker` (NOLOGIN, ni superutilisateur ni
  BYPASSRLS). Aucun reglage de session ne permet de s'en attribuer un autre.
  `app_ensure_service_principal()` (SECURITY DEFINER, executable par mervio_worker
  seulement) cree ce principal au premier demarrage.
- Un principal de service ne peut etre membre d'aucune organisation (trigger).

AUTORISATION EXPLICITE
- `service_authorizations`: liste des organisations qu'un service peut traiter,
  accordee et revoquee par un humain (table de tenant, RLS forcee, revocation tracee,
  jamais supprimee). Un service ne lit que SES lignes et n'en ecrit aucune.
- Politiques RESTRICTIVES `TO mervio_worker` sur toutes les tables de tenant: sans
  autorisation active pour l'organisation du contexte, aucune ligne n'est lisible ni
  modifiable, meme avec un `app.organization_id` forge. La revocation vaut des
  l'instruction suivante.
- Sans contexte d'organisation, le worker voit les travaux en file ou en cours de ses
  seules organisations autorisees: c'est le dispatcher, sans SECURITY DEFINER.

DELEGATION (controle a l'execution)
- Les donnees metier (instantanes, lignes canoniques, executions, rapports) ne sont
  lisibles par le worker que sous un delegue humain membre de l'organisation
  (`app.user_id`), et modifiables seulement si ce delegue est au moins `analyst`; la
  purge exige un delegue `owner`. Le role est relu a CHAQUE instruction: une
  revocation ou un changement de role entre la mise en file et l'execution s'applique.
- Le worker ne cree aucun travail et n'ecrit ni organisation, ni appartenance, ni
  boutique, ni connexion, ni autorisation.

AUDIT
- `audit_events.on_behalf_of`: l'acteur metier, toujours un humain.
- Une trace `user` nomme toujours un humain (trigger).
- Sous le role worker, une trace est `worker` ou `system`, son acteur est le principal
  du service connecte, et `on_behalf_of` est le delegue courant ou le demandeur
  (`enqueued_by`) du travail concerne: ni l'un ni l'autre ne se forge.
- `jobs.enqueued_by` est toujours un humain (trigger).

Limite assumee: DANS une organisation autorisee, le service peut choisir comme delegue
n'importe quel membre (le reglage `app.user_id` est pose par l'application, SEC-04);
c'est le code du worker qui prend `enqueued_by`, et la base qui verifie que ce membre
existe encore avec le role requis.
"""
from alembic import op

revision = "0007_service_identity"
down_revision = "0006_job_leases"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"
WORKER_ROLE = "mervio_worker"

#: Tables de tenant a colonne organization_id (hors jobs, traite a part).
TENANT_TABLES = (
    "memberships", "stores", "connections", "data_snapshots", "snapshot_sources", "products", "orders",
    "order_lines", "payments", "refunds", "campaigns", "ad_daily_performance", "analysis_runs", "reports",
    "audit_events",
)
#: Le worker ne les modifie jamais.
READ_ONLY_FOR_WORKER = ("organizations", "memberships", "stores", "connections")
#: Donnees metier: lecture et ecriture seulement sous un delegue humain.
BUSINESS_TABLES = (
    "data_snapshots", "snapshot_sources", "products", "orders", "order_lines", "payments", "refunds",
    "campaigns", "ad_daily_performance", "analysis_runs", "reports",
)
#: L'organisation du contexte est-elle autorisee? Un booleen calcule UNE fois par instruction
#: (InitPlan, filtre unique): aucun cout par ligne, meme sur un million de lignes canoniques.
AUTHORIZED_CONTEXT = "(SELECT app_service_authorized(app_current_organization_id()))"
#: Organisations autorisees du service connecte (celle du contexte s'il y en a une), calculees
#: une fois par instruction. `IS TRUE` en fait un simple filtre: une condition d'index sur
#: plusieurs organisations casserait l'ordre de `jobs_ready_idx` dont la prise et la sonde du
#: dispatcher ont besoin pour s'arreter au premier travail.
AUTHORIZED_JOB_ROW = "(organization_id = ANY ((SELECT app_service_organizations())::uuid[])) IS TRUE"
DELEGATE_RANK = "(SELECT app_delegate_rank())"


def _policies():
    """(nom, table, definition) de toutes les politiques ajoutees; la descente les supprime."""
    policies = []
    for table in TENANT_TABLES + ("organizations",):
        policies.append((f"{table}_service_authorized", table,
                         f"AS RESTRICTIVE FOR ALL TO {WORKER_ROLE} USING ({AUTHORIZED_CONTEXT})"))
    # jobs: seule table lisible sans contexte (dispatcher), donc filtree ligne a ligne
    policies.append(("jobs_service_authorized", "jobs",
                     f"AS RESTRICTIVE FOR ALL TO {WORKER_ROLE} USING ({AUTHORIZED_JOB_ROW})"))
    for table in READ_ONLY_FOR_WORKER:
        policies += [
            (f"{table}_service_no_insert", table, f"AS RESTRICTIVE FOR INSERT TO {WORKER_ROLE} WITH CHECK (false)"),
            (f"{table}_service_no_update", table, f"AS RESTRICTIVE FOR UPDATE TO {WORKER_ROLE} USING (false)"),
            (f"{table}_service_no_delete", table, f"AS RESTRICTIVE FOR DELETE TO {WORKER_ROLE} USING (false)"),
        ]
    for table in BUSINESS_TABLES:
        policies += [
            (f"{table}_service_delegate_read", table,
             f"AS RESTRICTIVE FOR SELECT TO {WORKER_ROLE} USING ({DELEGATE_RANK} >= 0)"),
            (f"{table}_service_delegate_insert", table,
             f"AS RESTRICTIVE FOR INSERT TO {WORKER_ROLE} WITH CHECK ({DELEGATE_RANK} >= 1)"),
            (f"{table}_service_delegate_update", table,
             f"AS RESTRICTIVE FOR UPDATE TO {WORKER_ROLE} USING ({DELEGATE_RANK} >= 1)"),
        ]
    policies += [
        ("jobs_service_dispatch", "jobs",
         f"AS PERMISSIVE FOR SELECT TO {WORKER_ROLE} "
         "USING (app_current_organization_id() IS NULL AND status IN ('queued', 'running'))"),
        ("jobs_service_no_insert", "jobs", f"AS RESTRICTIVE FOR INSERT TO {WORKER_ROLE} WITH CHECK (false)"),
        ("jobs_service_purge_by_owner", "jobs",
         f"AS RESTRICTIVE FOR DELETE TO {WORKER_ROLE} USING ({DELEGATE_RANK} >= 3)"),
        ("audit_events_service_actor", "audit_events",
         f"AS RESTRICTIVE FOR INSERT TO {WORKER_ROLE} WITH CHECK ("
         "actor_type IN ('worker', 'system') AND actor_id = app_current_service_id() "
         "AND (on_behalf_of IS NULL OR on_behalf_of = app_current_user_id() "
         "     OR (resource_type = 'job' AND on_behalf_of = (SELECT j.enqueued_by FROM jobs j "
         "         WHERE j.organization_id = audit_events.organization_id AND j.id = audit_events.resource_id))))"),
        ("audit_events_service_purge_by_owner", "audit_events",
         f"AS RESTRICTIVE FOR DELETE TO {WORKER_ROLE} USING ({DELEGATE_RANK} >= 3)"),
        ("service_authorizations_tenant_isolation", "service_authorizations",
         "USING (organization_id = app_current_organization_id()) "
         "WITH CHECK (organization_id = app_current_organization_id())"),
        # sans contexte seulement: dans le contexte d'une organisation, l'isolation reste stricte
        ("service_authorizations_own_rows", "service_authorizations",
         f"AS PERMISSIVE FOR SELECT TO {WORKER_ROLE} "
         "USING (app_current_organization_id() IS NULL AND service_user_id = app_current_service_id())"),
        ("service_authorizations_service_own_only", "service_authorizations",
         f"AS RESTRICTIVE FOR ALL TO {WORKER_ROLE} USING (service_user_id = app_current_service_id()) "
         "WITH CHECK (false)"),
    ]
    return policies


def upgrade() -> None:
    # Role de groupe des workers. Global au cluster: cree s'il manque, jamais supprime.
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
                CREATE ROLE {WORKER_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
            END IF;
        END
        $$
    """)

    # --- identite ----------------------------------------------------------------
    op.execute("""
        ALTER TABLE users ADD COLUMN kind text NOT NULL DEFAULT 'human'
            CONSTRAINT users_kind_known CHECK (kind IN ('human', 'service'))
    """)
    op.execute("""
        ALTER TABLE users ADD CONSTRAINT users_service_subject_reserved
            CHECK ((kind = 'service') = starts_with(idp_subject, 'service:'))
    """)
    op.execute(f"REVOKE INSERT ON users FROM {APP_ROLE}")
    op.execute(f"GRANT INSERT (id, idp_subject) ON users TO {APP_ROLE}")

    op.execute(f"""
        CREATE FUNCTION app_current_service_id() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$
            SELECT u.id FROM users u
            WHERE u.kind = 'service' AND u.idp_subject = 'service:' || current_user
              AND pg_has_role(current_user, '{WORKER_ROLE}', 'USAGE')
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION app_ensure_service_principal() RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
        AS $$
        DECLARE
            subject text := 'service:' || session_user;
            principal uuid;
        BEGIN
            IF NOT pg_has_role(session_user, '{WORKER_ROLE}', 'USAGE') THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'insufficient_privilege',
                    MESSAGE = 'the connected role is not a service role';
            END IF;
            INSERT INTO users (id, idp_subject, kind) VALUES (gen_random_uuid(), subject, 'service')
                ON CONFLICT (idp_subject) DO NOTHING;
            SELECT id INTO principal FROM users WHERE idp_subject = subject AND kind = 'service';
            IF principal IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'the service subject belongs to a human';
            END IF;
            RETURN principal;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION app_ensure_service_principal() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION app_ensure_service_principal() TO {WORKER_ROLE}")

    op.execute("""
        CREATE FUNCTION memberships_forbid_service_users() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id AND kind = 'service') THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'a service principal is never a member';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER memberships_forbid_service_users BEFORE INSERT OR UPDATE ON memberships
            FOR EACH ROW EXECUTE FUNCTION memberships_forbid_service_users()
    """)

    # --- autorisations explicites ------------------------------------------------
    op.execute("""
        CREATE TABLE service_authorizations (
            id               uuid PRIMARY KEY,
            organization_id  uuid NOT NULL REFERENCES organizations (id) ON DELETE RESTRICT,
            service_user_id  uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
            granted_by       uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
            granted_at       timestamptz NOT NULL DEFAULT now(),
            revoked_by       uuid NULL REFERENCES users (id) ON DELETE RESTRICT,
            revoked_at       timestamptz NULL,
            CONSTRAINT service_authorizations_revocation_consistent
                CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
            CONSTRAINT service_authorizations_revoked_after_granted
                CHECK (revoked_at IS NULL OR revoked_at >= granted_at),
            CONSTRAINT service_authorizations_tenant_key UNIQUE (organization_id, id)
        )
    """)
    # une seule autorisation active par couple; l'historique des revocations est garde
    op.execute("""
        CREATE UNIQUE INDEX service_authorizations_active_uniq
            ON service_authorizations (organization_id, service_user_id) WHERE revoked_at IS NULL
    """)
    # liste des organisations d'UN service (dispatcher): la seule recherche transverse, par conception
    op.execute("""
        CREATE INDEX service_authorizations_service_idx
            ON service_authorizations (service_user_id, organization_id) WHERE revoked_at IS NULL
    """)
    op.execute("""
        CREATE FUNCTION service_authorizations_guard() RETURNS trigger
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
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.service_user_id AND kind = 'service') THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'only a service principal is authorized';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.granted_by AND kind = 'human')
               OR (NEW.revoked_by IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.revoked_by AND kind = 'human')) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'only a human grants or revokes an authorization';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER service_authorizations_guard BEFORE INSERT OR UPDATE ON service_authorizations
            FOR EACH ROW EXECUTE FUNCTION service_authorizations_guard()
    """)
    op.execute("ALTER TABLE service_authorizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE service_authorizations FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT ON service_authorizations TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (revoked_by, revoked_at) ON service_authorizations TO {APP_ROLE}")

    op.execute("""
        CREATE FUNCTION app_service_authorized(p_organization_id uuid) RETURNS boolean
        LANGUAGE sql STABLE
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM service_authorizations sa
                WHERE sa.organization_id = p_organization_id
                  AND sa.service_user_id = app_current_service_id()
                  AND sa.revoked_at IS NULL)
        $$
    """)
    op.execute("""
        CREATE FUNCTION app_service_organizations() RETURNS uuid[]
        LANGUAGE sql STABLE
        AS $$
            SELECT COALESCE(array_agg(sa.organization_id), '{}'::uuid[])
            FROM service_authorizations sa
            WHERE sa.service_user_id = app_current_service_id()
              AND sa.revoked_at IS NULL
              AND (app_current_organization_id() IS NULL OR sa.organization_id = app_current_organization_id())
        $$
    """)
    # rang du delegue humain dans l'organisation du contexte; NULL s'il n'en est pas membre
    op.execute("""
        CREATE FUNCTION app_delegate_rank() RETURNS integer
        LANGUAGE sql STABLE
        AS $$
            SELECT CASE m.role WHEN 'viewer' THEN 0 WHEN 'analyst' THEN 1 WHEN 'admin' THEN 2 WHEN 'owner' THEN 3 END
            FROM memberships m JOIN users u ON u.id = m.user_id
            WHERE m.organization_id = app_current_organization_id()
              AND m.user_id = app_current_user_id()
              AND u.kind = 'human'
        $$
    """)

    # --- audit et travaux: acteurs ------------------------------------------------
    op.execute("ALTER TABLE audit_events ADD COLUMN on_behalf_of uuid NULL REFERENCES users (id) ON DELETE RESTRICT")
    op.execute("""
        CREATE FUNCTION audit_events_guard_actors() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            -- acteur absent: laisse a la contrainte audit_events_user_actor_identified (0005)
            IF NEW.actor_type = 'user' AND NEW.actor_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.actor_id AND kind = 'human') THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'a user event names a human actor';
            END IF;
            IF NEW.on_behalf_of IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.on_behalf_of AND kind = 'human') THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'work is done on behalf of a human';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER audit_events_guard_actors BEFORE INSERT ON audit_events
            FOR EACH ROW EXECUTE FUNCTION audit_events_guard_actors()
    """)
    op.execute("""
        CREATE FUNCTION jobs_enqueued_by_human() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.enqueued_by IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.enqueued_by AND kind = 'human') THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'a job is requested by a human';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER jobs_enqueued_by_human BEFORE INSERT ON jobs
            FOR EACH ROW EXECUTE FUNCTION jobs_enqueued_by_human()
    """)

    # --- politiques ---------------------------------------------------------------
    for name, table, definition in _policies():
        op.execute(f"CREATE POLICY {name} ON {table} {definition}")


def downgrade() -> None:
    for name, table, _ in reversed(_policies()):
        op.execute(f"DROP POLICY {name} ON {table}")
    op.execute("DROP TRIGGER jobs_enqueued_by_human ON jobs")
    op.execute("DROP FUNCTION jobs_enqueued_by_human()")
    op.execute("DROP TRIGGER audit_events_guard_actors ON audit_events")
    op.execute("DROP FUNCTION audit_events_guard_actors()")
    op.execute("ALTER TABLE audit_events DROP COLUMN on_behalf_of")
    op.execute("DROP FUNCTION app_delegate_rank()")
    op.execute("DROP FUNCTION app_service_organizations()")
    op.execute("DROP FUNCTION app_service_authorized(uuid)")
    op.execute(f"REVOKE ALL ON service_authorizations FROM {APP_ROLE}")
    op.execute("DROP TABLE service_authorizations")
    op.execute("DROP FUNCTION service_authorizations_guard()")
    op.execute("DROP TRIGGER memberships_forbid_service_users ON memberships")
    op.execute("DROP FUNCTION memberships_forbid_service_users()")
    op.execute(f"REVOKE ALL ON FUNCTION app_ensure_service_principal() FROM {WORKER_ROLE}")
    op.execute("DROP FUNCTION app_ensure_service_principal()")
    op.execute("DROP FUNCTION app_current_service_id()")
    op.execute(f"REVOKE INSERT (id, idp_subject) ON users FROM {APP_ROLE}")
    op.execute(f"GRANT INSERT ON users TO {APP_ROLE}")
    # les principaux de service deja crees redeviennent des lignes `users` ordinaires
    op.execute("ALTER TABLE users DROP COLUMN kind")
