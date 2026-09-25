"""Finalisation de la destruction d'un objet brut (Mission 004.4.5 E4, D-052, D-056).

Revision ID: 0015_raw_object_purge
Revises: 0014_customer_erasure
Create Date: 2026-09-25

CE QUE FAIT CETTE REVISION
Une seule chose: `app_finalize_raw_object_purge(job_id, raw_object_id)`, la deuxieme et
derniere moitie du cycle de vie d'un objet brut efface. Aucune table, aucune colonne, aucun
role, aucun privilege de table, aucun changement de vocabulaire d'audit.

    E3 (revision 0014)  available -> purging   dans la transaction de l'effacement
    E4 (ici)            purging   -> purged    APRES destruction reelle des octets

C'est le contrat ecrit en tete de 0014, applique tel quel.

L'ORDRE EST LA PROPRIETE DE SECURITE
Les octets partent d'abord, la ligne ensuite. Marquer `purged` avant d'avoir supprime
affirmerait une destruction qui n'a pas eu lieu -- et cette affirmation est exactement ce
qu'un auditeur de conformite lirait. La fonction ne sait pas, et ne peut pas savoir, si les
octets ont disparu: c'est l'appelant qui le garantit en ne l'appelant qu'apres un
`ObjectStore.delete` revenu sans erreur. La fonction garantit le reste: qu'aucune ligne ne
passe `purged` sans etre passee par `purging`, et qu'un travail autorise le demande.

PAS DE TRANSACTION ATOMIQUE POSTGRESQL + MAGASIN D'OBJETS
Le magasin est EXTERNE a PostgreSQL (systeme de fichiers ou S3): il n'existe aucune
transaction commune, et cette revision n'en simule pas une. La securite vient de l'ORDRE et
de l'IDEMPOTENCE, pas d'une atomicite qu'on ne peut pas avoir:
  - octets detruits, puis arret avant le commit -> la ligne reste `purging`, le rejeu
    redetruit (sans erreur, E1) et finalise; aucun octet ne revient;
  - `delete` echoue -> la ligne reste `purging`, donc illisible (l'import refuse tout objet
    non `available`), et le travail peut etre repris;
  - ligne deja `purged` -> la fonction rend `false` sans rien ecrire.
La seule fenetre est donc "octets detruits, ligne encore `purging`": un etat ou l'objet est
deja illisible et deja vide. C'est le sens SUR de la fenetre, et c'est pourquoi l'ordre
inverse est interdit.

GARDES DUPLIQUEES, DELIBEREMENT
Les verifications de D-052 sont reecrites ici au lieu d'etre factorisees avec
`app_redact_customer` (0014). `CREATE OR REPLACE` sur une fonction privilegiee deja acceptee
et couverte par sa CI elargirait le perimetre de E4 a du code ratifie. Chaque fonction
privilegiee reste donc lisible et auditable seule -- au prix d'une repetition assumee. La
protection contre la derive est BEHAVIORALE: la suite adversariale de E4 rejoue sur cette
fonction exactement les memes refus que celle de E3 (organisation etrangere, mauvais type,
service non autorise, service revoque, jeton perime, demandeur retrograde).

DESCENTE
La fonction disparait. Rien d'autre n'a ete touche, donc rien d'autre n'est restaure.
"""
from alembic import op

revision = "0015_raw_object_purge"
down_revision = "0014_customer_erasure"
branch_labels = None
depends_on = None

WORKER_ROLE = "mervio_worker"
REDACT_JOB_TYPE = "redact_customer"
#: rang minimal du DEMANDEUR (D-052: `admin` ou plus pour l'effacement), comme en 0014
ADMIN_RANK = 2

FINALIZE_FUNCTION = f"""
    CREATE FUNCTION public.app_finalize_raw_object_purge(p_job_id uuid, p_raw_object_id uuid)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_organization uuid := public.app_current_organization_id();
        v_service      uuid;
        v_job          public.jobs%ROWTYPE;
        v_token        text := pg_catalog.current_setting('app.job_lease_token', true);
        v_rank         integer;
        v_state        text;
        v_updated      integer;
    BEGIN
        IF v_organization IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'insufficient_privilege',
                MESSAGE = 'a raw object purge runs in the context of one organization';
        END IF;

        -- IDENTITE DU SERVICE: `session_user`, jamais `current_user` (voir 0014).
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
                MESSAGE = 'a raw object purge only runs for a claimed job';
        END IF;
        IF v_token IS NULL OR v_token = ''
           OR v_token <> v_job.attempts::text || ':' || v_job.locked_by THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'only the lease holder purges a raw object';
        END IF;
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

        -- L'OBJET: introuvable hors de l'organisation, sans oracle d'existence (RLS).
        SELECT ro.state INTO v_state FROM public.raw_objects ro WHERE ro.id = p_raw_object_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = 'no_data_found',
                MESSAGE = 'no such raw object in this organization';
        END IF;

        -- REJEU: deja finalise. Aucune ecriture, aucune erreur (l'appelant a pu etre
        -- interrompu entre la destruction des octets et le commit).
        IF v_state = 'purged' THEN
            RETURN false;
        END IF;
        -- SEULE TRANSITION AUTORISEE. `available -> purged` sauterait l'etat illisible;
        -- `pending -> purged` n'a jamais ete efface. Les deux sont refuses ici.
        IF v_state <> 'purging' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'only a raw object being purged is finalized';
        END IF;

        -- `state` et `purged_at`, RIEN d'autre: `purge_reason` reste (la contrainte
        -- `raw_objects_purge_reason_consistent` l'exige), et l'empreinte, la taille, le type,
        -- la cle et les dates sont conservees -- D-056 veut la ligne, pas les octets.
        UPDATE public.raw_objects ro SET state = 'purged', purged_at = now()
        WHERE ro.id = p_raw_object_id AND ro.state = 'purging';
        GET DIAGNOSTICS v_updated = ROW_COUNT;
        RETURN v_updated > 0;
    END
    $$
"""


def upgrade() -> None:
    op.execute(FINALIZE_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION public.app_finalize_raw_object_purge(uuid, uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.app_finalize_raw_object_purge(uuid, uuid) "
               f"TO {WORKER_ROLE}")


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION public.app_finalize_raw_object_purge(uuid, uuid) "
               f"FROM {WORKER_ROLE}")
    op.execute("DROP FUNCTION public.app_finalize_raw_object_purge(uuid, uuid)")
