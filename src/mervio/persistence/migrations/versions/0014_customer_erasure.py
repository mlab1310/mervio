"""Effacement client controle: chemin privilegie, preuve et tombstone (Mission 004.4.5, D-052, D-056).

Revision ID: 0014_customer_erasure
Revises: 0013_raw_objects
Create Date: 2026-09-24

CE QUE FAIT CETTE REVISION
- Type de travail `redact_customer` (D-056, roadmap 004.4 item 3).
- `customer_redactions`: la PREUVE d'un effacement, sans aucune PII. Elle ne porte que des
  references DEJA non reversibles: le `customer_ref` efface (un HMAC, jamais un e-mail) et le
  tombstone qui l'a remplace. RLS activee ET forcee; le role applicatif n'a que `SELECT`:
  personne n'ecrit cette table hors de la fonction privilegiee ci-dessous.
- `raw_objects.purge_reason`: le MOTIF de purge exige par D-056 ("SHA-256, taille, type, dates,
  motif de purge"), avec une contrainte qui le lie a l'etat.
- Relachement CIBLE de `canonical_rows_forbid_update` (D-052): `orders.customer_ref`, vers un
  tombstone, par le SEUL proprietaire de la table. Tout le reste demeure immuable.
- `app_redact_customer(job_id)`: fonction `SECURITY DEFINER` etroite, declenchee par un travail.
- Vocabulaire d'audit `customer.redacted` / `customer_redaction`, par le mecanisme de 0008.

PRIVILEGE MINIMAL (D-052, D-049)
La fonction est possedee par LE PROPRIETAIRE DU SCHEMA, comme `app_ensure_service_principal`, et
n'introduit AUCUN role. Le roadmap 004.4 prevoyait un role dedie `mervio_redactor`; D-052 a
tranche l'inverse APRES lui ("sans nouveau role"), pour les motifs de D-050 -- un role global au
cluster et un transfert de propriete font echouer la migration quand le role preexiste sans que
le role de migration en soit membre. L'historique du roadmap n'est pas reecrit.

Elle ne s'execute que pour UN travail: du type attendu, `running`, dont `attempts` correspond
(jeton d'exclusion de 0006), de l'organisation du contexte, que le service CONNECTE est autorise
a traiter, et mis en file par un humain qui a ENCORE le rang `admin` au moins. L'objet de
l'operation -- le `customer_ref` -- est lu dans `jobs.payload`, JAMAIS recu en parametre: le seul
parametre est l'identifiant du travail. Il n'existe donc pas de `f(customer_ref) -> UPDATE`.

`SECURITY DEFINER` NE CONTOURNE PAS LA RLS: toutes les tables touchees sont en RLS FORCEE, donc
le proprietaire y est soumis comme les autres. La fonction est confinee a l'organisation du
contexte par PostgreSQL, pas par une condition Python. Ce qu'elle gagne est exactement ce qui
manque au worker: le droit d'ecrire ces lignes -- rien de plus, et dans une seule organisation.

IDENTITE DU SERVICE SOUS `SECURITY DEFINER`
`app_current_service_id()` lit `current_user`, qui vaut LE PROPRIETAIRE a l'interieur d'une
fonction `SECURITY DEFINER`. La fonction resout donc son principal depuis `session_user` -- le
role reellement connecte -- comme le fait deja `app_ensure_service_principal()` (0007), et
verifie l'autorisation de service pour CE principal, sans passer par `app_service_authorized()`.

ETATS DES OBJETS BRUTS (D-056)
L'effacement rend les objets porteurs d'identite (`shopify_orders`, `stripe`) IMMEDIATEMENT
illisibles: `available -> purging`, EN BASE, DANS LA TRANSACTION de l'effacement. La destruction
des octets et la transition `purging -> purged` appartiennent au gestionnaire de travail (E4), et
ne sont PAS livrees ici. Les objets sans identite client sont laisses intacts.

CONTRAT DE SEQUENCE -- CE QUE CETTE REVISION NE LIVRE PAS
Deux frontieres sont laissees ouvertes VOLONTAIREMENT. Elles sont ecrites ici parce que c'est
ici qu'un implementeur de E4 viendra les chercher.

1. TYPE DE TRAVAIL DECLARE EN BASE SEULEMENT
   `redact_customer` existe dans `jobs_job_type_check` et nulle part en Python. C'est
   TEMPORAIRE et INTENTIONNEL: le depot impose qu'un type declare dans `JobType` ait un
   gestionnaire (`tests/test_jobs_unit.py`), et ce gestionnaire est E4. Plutot que d'affaiblir
   cet invariant, le type reste cote base jusqu'a ce que son execution existe.
       E2  le type existe en base;
       E3  l'operation privilegiee sait agir sur un travail de ce type;
       E4  `JobType.REDACT_CUSTOMER`, le gestionnaire, la permission de mise en file et
           l'execution par le worker arrivent ENSEMBLE.
   Consequence verifiee: aucun chemin applicatif ne peut traiter un tel travail aujourd'hui.
   `enqueue_job` refuse le type (`JobType(...)` leve), `claim_next_job` construit son filtre
   depuis `list(JobType)` et ne le voit donc jamais, `HandlerRegistry.resolve` leve avant meme
   d'atteindre `no_handler`, et `settings.JOB_TYPES` interdit de configurer un worker pour lui.
   La sonde du dispatcher, elle, ne filtre PAS par type: une organisation dont le seul travail
   pret serait un `redact_customer` serait donc signalee "prete", puis la prise ne rendrait
   rien et le worker repasserait en attente (`run_forever`, `idle_sleep`) -- inoffensif, et de
   toute facon inatteignable tant qu'aucun chemin ne peut en mettre un en file.

2. ETAT DES OBJETS BRUTS: QUI POSSEDE QUELLE TRANSITION
       E3 (ici)  available -> purging   dans la transaction de l'effacement;
       E4        purging   -> purged    apres destruction reelle des octets.
   `raw_objects` ne porte AUCUN declencheur: sa machine a etats n'est tenue que par les
   contraintes `CHECK` et par l'absence de droit `UPDATE` (seul `mervio_app` existe, en
   `SELECT, INSERT`). Les contraintes seules n'interdisent donc pas un retour en arriere --
   c'est le privilege qui l'interdit. La fonction de E4 devra porter elle-meme cette garde:
     - QUI: une fonction `SECURITY DEFINER` etroite de plus, possedee par le proprietaire du
       schema, `EXECUTE` au seul `mervio_worker`, sur le modele exact de `app_redact_customer`.
       Aucun `UPDATE` general sur `raw_objects` ne doit etre accorde a quiconque.
     - VERIFICATIONS: les memes que D-052 impose ici -- travail `redact_customer`, `running`,
       jeton d'exclusion concordant, organisation du contexte (la RLS s'en charge, elle n'est
       pas contournee), service autorise, demandeur de rang `admin` au moins.
     - CHAMPS MODIFIES: `state` -> 'purged' et `purged_at` -> now(), et RIEN d'autre.
       `purge_reason` est CONSERVE (`raw_objects_purge_reason_consistent` l'exige des que
       l'etat est 'purging' ou 'purged'); `sha256`, `byte_size`, `source_kind`, `object_key`,
       `created_at` et `retain_until` restent intacts -- D-056 veut la ligne conservee.
     - TRANSITIONS INTERDITES: tout sauf `purging -> purged`. En particulier `purged -> *`
       (une destruction ne se defait pas), `available -> purged` (sauter l'etat illisible), et
       `purging -> available` (faire revivre un objet efface).
     - IDEMPOTENCE ET ORDRE: les octets d'abord, la ligne ensuite. `ObjectStore.delete` est
       idempotente exactement pour cela (E1): un arret entre les deux laisse l'objet en
       'purging' -- illisible, et un rejeu redetruit sans erreur. Marquer 'purged' AVANT de
       supprimer affirmerait une destruction qui n'a pas eu lieu. L'ecriture doit etre
       conditionnee a `state = 'purging'`: un second passage touche alors zero ligne au lieu
       d'echouer.

DESCENTE
Fonction, table, colonne, type de travail et listes d'audit sont retires; le declencheur
d'immuabilite retrouve son texte de 0002, mot pour mot. La liste des types de travaux et les
deux listes d'audit redescendent en `NOT VALID`: les lignes deja ecrites sont CONSERVEES, seules
les nouvelles sont refusees -- meme mecanisme qu'en 0008 et 0013.
"""
from alembic import op

revision = "0014_customer_erasure"
down_revision = "0013_raw_objects"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"
WORKER_ROLE = "mervio_worker"
TABLE = "customer_redactions"

#: memes expressions que 0007, 0011 et 0013 (un booleen par instruction, aucun cout par ligne)
AUTHORIZED_CONTEXT = "(SELECT app_service_authorized(app_current_organization_id()))"
DELEGATE_RANK = "(SELECT app_delegate_rank())"

#: forme d'une reference client EFFACABLE: un HMAC, jamais un e-mail, jamais un tombstone.
#: Identique au premier terme de `orders_customer_ref_keyed` (revision 0011).
KEYED_REF = "'^(c1|g1):[0-9a-f]{32}$'"
#: forme du tombstone, identique au second terme de `orders_customer_ref_keyed` (revision 0011)
TOMBSTONE = "'^redacted:[0-9a-f-]{36}$'"
#: rang minimal du DEMANDEUR (D-052: `admin` ou plus pour l'effacement)
ADMIN_RANK = 2
#: objets bruts porteurs d'identite client (D-056); les autres sont conserves
IDENTITY_BEARING = ("shopify_orders", "stripe")
PURGE_REASONS = ("customer_erasure",)

JOB_TYPES = ("import", "analysis", "purge")
REDACT_JOB_TYPE = "redact_customer"

#: listes d'audit apres 0013
BASE_ACTIONS = (
    "job.enqueued", "job.claimed", "job.succeeded", "job.failed", "job.requeued",
    "job.recovered", "job.cancelled",
    "import.started", "import.succeeded", "import.failed",
    "analysis.started", "analysis.succeeded", "analysis.failed",
    "purge.started", "purge.completed",
    "organization.created", "member.added", "store.created", "connection.created",
    "service.authorized", "service.revoked",
    "object.uploaded",
)
BASE_RESOURCES = ("job", "snapshot", "analysis_run", "report", "organization",
                  "membership", "store", "connection", "service_authorization", "raw_object")
#: ajouts de 004.4.5
ERASURE_ACTIONS = ("customer.redacted",)
ERASURE_RESOURCES = ("customer_redaction",)


def _in_list(values) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace_check(table: str, column: str, values, *, validated: bool) -> None:
    """Mecanisme de 0008: seule la liste s'allonge ou se raccourcit."""
    name = f"{table}_{column}_check"
    op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {name}")
    suffix = "" if validated else " NOT VALID"
    op.execute(f"ALTER TABLE public.{table} ADD CONSTRAINT {name} "
               f"CHECK ({column} IN ({_in_list(values)})){suffix}")


def _worker_policies():
    """Worker: organisation autorisee ET delegue humain, en LECTURE SEULE.

    La preuve d'effacement est lue par le worker (rejeu des effacements a l'import, E5); elle
    n'est jamais ecrite par lui. Il herite pourtant des privileges de `mervio_app` (membre du
    role de groupe), d'ou des refus EXPLICITES d'ecriture, comme en 0011 et 0013.
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


# --- declencheur d'immuabilite ------------------------------------------------------------
#: Texte de 0002, restaure tel quel a la descente.
#: L'INDENTATION fait partie du corps stocke par PostgreSQL (`pg_proc.prosrc`): elle est
#: reprise EXACTEMENT de 0002, sinon la descente laisserait une fonction subtilement differente.
FORBID_UPDATE_0002 = """
        CREATE OR REPLACE FUNCTION public.canonical_rows_forbid_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'canonical records are immutable',
                DETAIL = 'table ' || TG_TABLE_NAME;
        END
        $$
"""

#: Meme fonction, avec l'UNIQUE exception de D-052. Le `search_path` est epingle et toutes les
#: relations sont qualifiees (D-049): `pg_temp` ne peut ni fournir une fausse table `pg_class`,
#: ni redefinir les operateurs jsonb, car `pg_catalog` est cherche EN PREMIER.
#:
#: Les colonnes sont comparees en jsonb plutot que nommees une a une: la garantie "rien d'autre
#: n'a change" reste vraie si une colonne est ajoutee un jour a `orders`, et l'acces aux champs
#: ne depend pas de la table qui declenche (la meme fonction sert les sept tables canoniques).
FORBID_UPDATE_0014 = f"""
    CREATE OR REPLACE FUNCTION public.canonical_rows_forbid_update() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_owner text;
    BEGIN
        -- EXCEPTION UNIQUE (D-052): orders.customer_ref -> tombstone, par le proprietaire seul.
        IF TG_TABLE_SCHEMA = 'public' AND TG_TABLE_NAME = 'orders' THEN
            SELECT pg_catalog.pg_get_userbyid(c.relowner) INTO v_owner
            FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID;
            IF current_user = v_owner
               AND pg_catalog.to_jsonb(NEW) - 'customer_ref'
                   IS NOT DISTINCT FROM pg_catalog.to_jsonb(OLD) - 'customer_ref'
               AND (pg_catalog.to_jsonb(NEW) ->> 'customer_ref') ~ {TOMBSTONE}
               AND (pg_catalog.to_jsonb(OLD) ->> 'customer_ref') ~ {KEYED_REF}
            THEN
                RETURN NEW;
            END IF;
        END IF;
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = 'canonical records are immutable',
            DETAIL = 'table ' || TG_TABLE_NAME;
    END
    $$
"""


# --- operation privilegiee -----------------------------------------------------------------
REDACT_FUNCTION = f"""
    CREATE FUNCTION public.app_redact_customer(p_job_id uuid)
    RETURNS TABLE (redaction_id uuid, redacted_ref text,
                   orders_tombstoned bigint, raw_objects_marked bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_organization uuid := public.app_current_organization_id();
        v_service      uuid;
        v_job          public.jobs%ROWTYPE;
        v_token        text := pg_catalog.current_setting('app.job_lease_token', true);
        v_rank         integer;
        v_ref          text;
        v_tombstone    text;
        v_redaction    uuid;
        v_orders       bigint := 0;
        v_objects      bigint := 0;
    BEGIN
        IF v_organization IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'insufficient_privilege',
                MESSAGE = 'a customer erasure runs in the context of one organization';
        END IF;

        -- IDENTITE DU SERVICE: `session_user`, jamais `current_user` (voir l'en-tete).
        IF NOT pg_catalog.pg_has_role(session_user, '{WORKER_ROLE}', 'USAGE') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'insufficient_privilege',
                MESSAGE = 'the connected role is not a service role';
        END IF;
        SELECT u.id INTO v_service FROM public.users u
        WHERE u.kind = 'service' AND u.idp_subject = 'service:' || session_user;
        IF v_service IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'insufficient_privilege',
                MESSAGE = 'the connected service has no principal';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM public.service_authorizations sa
                       WHERE sa.organization_id = v_organization
                         AND sa.service_user_id = v_service
                         AND sa.revoked_at IS NULL) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'insufficient_privilege',
                MESSAGE = 'the service is not authorized for this organization';
        END IF;

        -- LE TRAVAIL: visible seulement dans l'organisation du contexte (RLS, non contournee).
        SELECT * INTO v_job FROM public.jobs j WHERE j.id = p_job_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = 'no_data_found',
                MESSAGE = 'no such job in this organization';
        END IF;
        IF v_job.job_type <> '{REDACT_JOB_TYPE}' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'this operation only runs for a customer erasure job';
        END IF;
        IF v_job.status <> 'running' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'a customer erasure only runs for a claimed job';
        END IF;
        -- JETON D'EXCLUSION (0006): un ancien detenteur ne peut plus rien effacer.
        IF v_token IS NULL OR v_token = ''
           OR v_token <> v_job.attempts::text || ':' || v_job.locked_by THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'only the lease holder erases a customer';
        END IF;
        -- LE DEMANDEUR A-T-IL ENCORE LE RANG? (relu maintenant, pas a la mise en file)
        SELECT CASE m.role WHEN 'viewer' THEN 0 WHEN 'analyst' THEN 1
                           WHEN 'admin' THEN 2 WHEN 'owner' THEN 3 END
        INTO v_rank
        FROM public.memberships m JOIN public.users u ON u.id = m.user_id
        WHERE m.organization_id = v_organization AND m.user_id = v_job.enqueued_by
          AND u.kind = 'human';
        IF v_rank IS NULL OR v_rank < {ADMIN_RANK} THEN
            RAISE EXCEPTION USING
                ERRCODE = 'insufficient_privilege',
                MESSAGE = 'the requester no longer holds the rank required to erase a customer';
        END IF;

        -- L'OBJET DE L'OPERATION VIENT DE LA CHARGE (D-052), jamais d'un parametre.
        v_ref := v_job.payload ->> 'customer_ref';
        IF v_ref IS NULL OR v_ref !~ {KEYED_REF} THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'the job payload carries no erasable customer reference';
        END IF;

        -- PREUVE, et IDEMPOTENCE: un rejeu retrouve le meme tombstone, n'en cree pas un second.
        SELECT r.id, r.redacted_ref INTO v_redaction, v_tombstone
        FROM public.{TABLE} r
        WHERE r.organization_id = v_organization AND r.customer_ref = v_ref;
        IF v_redaction IS NULL THEN
            v_tombstone := 'redacted:' || pg_catalog.gen_random_uuid()::text;
            INSERT INTO public.{TABLE}
                (id, organization_id, customer_ref, redacted_ref, origin, requested_by, job_id)
            VALUES (pg_catalog.gen_random_uuid(), v_organization, v_ref, v_tombstone,
                    'operator_request', v_job.enqueued_by, v_job.id)
            RETURNING id INTO v_redaction;
        END IF;

        -- TOMBSTONE: un jeton ALEATOIRE par client, jamais derive de son identite (D-056).
        UPDATE public.orders o SET customer_ref = v_tombstone
        WHERE o.organization_id = v_organization AND o.customer_ref = v_ref;
        GET DIAGNOSTICS v_orders = ROW_COUNT;

        -- OBJETS BRUTS PORTEURS D'IDENTITE: illisibles IMMEDIATEMENT (D-056). La destruction
        -- des octets et `purging -> purged` appartiennent au gestionnaire de travail (E4).
        --
        -- PORTEE VOLONTAIREMENT LARGE, et independante de `v_orders`: TOUS les objets
        -- `shopify_orders` et `stripe` de l'organisation sont marques, meme si AUCUNE ligne
        -- canonique ne portait la reference. D-056 a explicitement REJETE "ne purger que les
        -- fichiers qui contiennent le client", impossible pour Stripe sans conserver l'e-mail:
        -- l'absence de commande ne prouve donc pas l'absence de l'identite dans les octets.
        -- Consequence assumee (D-056): une reference qui n'existe pas dans l'organisation rend
        -- quand meme ses objets bruts illisibles. L'impact est borne -- les instantanes restent
        -- analysables, seul le rejeu brut est perdu, et la retention est de 30 jours.
        UPDATE public.raw_objects ro
        SET state = 'purging', purge_reason = 'customer_erasure'
        WHERE ro.organization_id = v_organization AND ro.state = 'available'
          AND ro.source_kind IN ({_in_list(IDENTITY_BEARING)});
        GET DIAGNOSTICS v_objects = ROW_COUNT;

        -- AUDIT, dans LA MEME transaction (D-052). Aucune identite, aucune valeur effacee:
        -- la trace designe la ligne de preuve, qui porte elle-meme la reference sous RLS.
        INSERT INTO public.audit_events
            (id, organization_id, actor_type, actor_id, action, resource_type, resource_id,
             correlation_id, outcome, on_behalf_of, metadata)
        VALUES (pg_catalog.gen_random_uuid(), v_organization, 'worker', v_service,
                'customer.redacted', 'customer_redaction', v_redaction,
                v_job.correlation_id, 'succeeded', v_job.enqueued_by,
                pg_catalog.jsonb_build_object('job_id', v_job.id,
                                              'orders_tombstoned', v_orders,
                                              'raw_objects_marked', v_objects));

        RETURN QUERY SELECT v_redaction, v_tombstone, v_orders, v_objects;
    END
    $$
"""


def upgrade() -> None:
    # --- type de travail --------------------------------------------------------------------
    _replace_check("jobs", "job_type", JOB_TYPES + (REDACT_JOB_TYPE,), validated=True)

    # --- preuve d'effacement, sans PII -------------------------------------------------------
    op.execute(f"""
        CREATE TABLE public.{TABLE} (
            id               uuid PRIMARY KEY,
            organization_id  uuid NOT NULL REFERENCES public.organizations (id) ON DELETE RESTRICT,
            customer_ref     text NOT NULL
                CONSTRAINT {TABLE}_ref_keyed CHECK (customer_ref ~ {KEYED_REF}),
            redacted_ref     text NOT NULL
                CONSTRAINT {TABLE}_tombstone_format CHECK (redacted_ref ~ {TOMBSTONE}),
            origin           text NOT NULL
                CONSTRAINT {TABLE}_origin_known CHECK (origin IN ({_in_list(("operator_request",))})),
            requested_by     uuid NOT NULL REFERENCES public.users (id) ON DELETE RESTRICT,
            job_id           uuid NOT NULL,
            created_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT {TABLE}_once UNIQUE (organization_id, customer_ref),
            CONSTRAINT {TABLE}_tombstone_uniq UNIQUE (organization_id, redacted_ref),
            CONSTRAINT {TABLE}_tenant_key UNIQUE (organization_id, id),
            CONSTRAINT {TABLE}_job_fkey FOREIGN KEY (organization_id, job_id)
                REFERENCES public.jobs (organization_id, id) ON DELETE RESTRICT
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
    # LECTURE SEULE: la preuve n'est ecrite que par `app_redact_customer`, jamais par un appelant
    op.execute(f"GRANT SELECT ON public.{TABLE} TO {APP_ROLE}")

    # --- motif de purge des objets bruts (D-056) ---------------------------------------------
    op.execute(f"""
        ALTER TABLE public.raw_objects
            ADD COLUMN purge_reason text NULL
                CONSTRAINT raw_objects_purge_reason_known
                CHECK (purge_reason IS NULL OR purge_reason IN ({_in_list(PURGE_REASONS)})),
            ADD CONSTRAINT raw_objects_purge_reason_consistent
                CHECK ((purge_reason IS NOT NULL) = (state IN ('purging', 'purged')))
    """)

    # --- exception d'immuabilite, et le chemin privilegie qui l'emprunte ---------------------
    op.execute(FORBID_UPDATE_0014)
    op.execute(REDACT_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION public.app_redact_customer(uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.app_redact_customer(uuid) TO {WORKER_ROLE}")

    # --- vocabulaire d'audit -----------------------------------------------------------------
    _replace_check("audit_events", "action", BASE_ACTIONS + ERASURE_ACTIONS, validated=True)
    _replace_check("audit_events", "resource_type", BASE_RESOURCES + ERASURE_RESOURCES, validated=True)


def downgrade() -> None:
    _replace_check("audit_events", "resource_type", BASE_RESOURCES, validated=False)
    _replace_check("audit_events", "action", BASE_ACTIONS, validated=False)
    op.execute(f"REVOKE ALL ON FUNCTION public.app_redact_customer(uuid) FROM {WORKER_ROLE}")
    op.execute("DROP FUNCTION public.app_redact_customer(uuid)")
    op.execute(FORBID_UPDATE_0002)  # texte de 0002, mot pour mot
    op.execute("""
        ALTER TABLE public.raw_objects
            DROP CONSTRAINT raw_objects_purge_reason_consistent,
            DROP COLUMN purge_reason
    """)
    op.execute(f"REVOKE ALL ON public.{TABLE} FROM {APP_ROLE}")
    op.execute(f"DROP TABLE public.{TABLE}")
    _replace_check("jobs", "job_type", JOB_TYPES, validated=False)
