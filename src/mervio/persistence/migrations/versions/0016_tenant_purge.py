"""Fondation PostgreSQL de la purge de boutique et d'organisation (004.4.6, D-051, D-065, D-066).

Revision ID: 0016_tenant_purge
Revises: 0015_raw_object_purge
Create Date: 2026-09-26

CE QUE FAIT CETTE REVISION
La couche BASE de la purge tenant, et rien d'autre: statuts et pierres tombales, types de
travaux cote base, vocabulaire d'audit, motifs de purge, declencheur de cloture, exception
D-066 au plancher de retention, et les huit operations privilegiees. Aucun gestionnaire,
aucune commande, aucune orchestration de magasin d'objets: c'est la phase suivante.

CONTRAT DE SEQUENCE -- CE QUE CETTE REVISION NE LIVRE PAS
Comme `0014` avant elle, et pour la meme raison, les deux types de travaux existent ici EN
BASE SEULEMENT. Le depot impose qu'un type declare dans `JobType` ait un gestionnaire
(`tests/test_jobs_unit.py`: `set(default_registry().types()) == set(JobType)`), et ce
gestionnaire appartient a la phase worker. Plutot que d'affaiblir cet invariant, le type
reste cote base jusqu'a ce que son execution existe.
    ici        les types existent en base; les operations privilegiees savent agir sur eux;
    phase 3    `JobType.PURGE_STORE`/`PURGE_ORGANIZATION`, les gestionnaires, la liaison
               `ENQUEUE_PERMISSION` et l'execution par le worker arrivent ENSEMBLE.
Consequence, identique a celle verifiee en `0014`: aucun chemin applicatif ne peut traiter un
tel travail aujourd'hui -- `enqueue_job` refuse le type, `claim_next_job` construit son filtre
depuis `list(JobType)`, et `settings.JOB_TYPES` interdit de configurer un worker pour lui.
`Permission.PURGE_TENANT` est en revanche livree des maintenant (D-065 Q6): elle ne depend
d'aucun gestionnaire et fixe des maintenant le rang `owner` exige.

GARDE PARTAGEE ENTRE LES SEULES FONCTIONS DE PURGE (revue d'architecture 004.4.6)
`0014` et `0015` inlinent chacune leur batterie D-052, pour ne pas rouvrir une fonction deja
ratifiee. Ce motif est PRESERVE: `app_redact_customer` et `app_finalize_raw_object_purge` ne
sont pas touchees, et restent auditables seules. Entre les HUIT fonctions nouvelles, en
revanche, la batterie est portee par `app_purge_guard`, pour une raison mesuree: a huit corps,
la propriete "les batteries sont equivalentes" n'est garantie par rien de structurel, et
D-065 consignait deja la derive comme risque residuel a DEUX corps.

Le helper n'ajoute AUCUNE surface privilegiee, et c'est ce qui rend l'arbitrage soutenable:
  - il est `SECURITY INVOKER`: appele depuis un `SECURITY DEFINER`, il s'execute avec les
    privileges du DEFINISSEUR (mesure: `current_user` vaut le proprietaire dans le helper
    aussi), et `session_user` y est preserve -- la garde d'identite de service, la plus
    subtile de D-052, fonctionne donc a l'identique;
  - il porte son propre `SET search_path` et qualifie toutes ses relations, bien que le
    `SET` de l'appelant soit deja herite (mesure): une relecture ne doit pas avoir a
    raisonner sur l'heritage;
  - il est `REVOKE ALL ... FROM PUBLIC` et ne recoit AUCUN `GRANT`: `mervio_app` et
    `mervio_worker` obtiennent `permission denied` a l'appel direct (mesure), tandis que les
    definisseurs continuent de l'appeler, le proprietaire conservant son `EXECUTE` implicite.

DEPENDANCE NON ENREGISTREE -- DISCIPLINE D'ORDRE A LA DESCENTE
Mesure: PostgreSQL n'enregistre AUCUNE entree `pg_depend` entre une fonction plpgsql et les
fonctions qu'elle appelle -- un corps plpgsql est une chaine opaque. Supprimer le helper
avant ses appelants REUSSIT donc sans erreur, et laisse des fonctions privilegiees pointant
dans le vide, detectables seulement au premier appel. La descente ci-dessous retire les huit
definisseurs AVANT le helper, et un test verrouille cet ordre: la garantie vient de la
migration et du test, jamais du moteur.

D-066 -- EXCEPTION BORNEE AU SEUL PLANCHER TEMPOREL
La politique `jobs_purge_terminal_only` n'est ni supprimee ni desactivee. Sa clause historique
devient le PREMIER TERME d'une disjonction, et la disjonction porte EXCLUSIVEMENT sur le
plancher d'une heure. Les trois garanties de `0004` deviennent:
    I1  `queued`/`running` jamais supprimables   -> INTACTE, sans exception
    I2  `finished_at` renseigne                  -> INTACTE, sans exception
    I3  plancher d'une heure                     -> seule garantie amendee
Placer la disjonction sur la condition ENTIERE rendrait supprimables les travaux `queued` et
`running` pendant la purge, detruisant I1 et la garantie de D-065 Q7 sur un `redact_customer`
`running`. Consequence utile et non accidentelle: le travail de purge est lui-meme `running`,
donc jamais supprimable par sa propre exception -- l'exigence de D-051 "travaux AUTRES que la
purge en cours" est satisfaite PAR CONSTRUCTION.
L'ancre est l'etat durable `organizations.status`, colonne SANS `GRANT UPDATE`: `mervio_app`
ne peut pas ouvrir l'exception lui-meme. Aucun GUC n'intervient -- ils sont forgeables et ne
portent qu'un contexte, jamais une autorisation.

C1 / C2 -- CE QUE LE SCHEMA REEL IMPOSE A LA SUPPRESSION
Mesures sur le schema migre, sous le proprietaire:
  C1  un `DELETE` DIRECT sur une table canonique ou sur `snapshot_sources` d'un instantane
      SCELLE est REFUSE par `canonical_rows_guard_sealed`; le `DELETE` sur `data_snapshots`
      REUSSIT et le CASCADE emporte sources et lignes canoniques. La purge doit donc passer
      par `data_snapshots`, et ne JAMAIS supprimer une canonique directement.
  C2  `data_snapshots` porte une FK AUTO-REFERENTE `supersedes` en RESTRICT: supprimer un
      instantane superseded SEUL echoue; les supprimer TOUS en UN SEUL `DELETE` reussit.
      D'ou un `DELETE` unique couvrant toute la portee, jamais un traitement par lots.

DESCENTE
Les huit definisseurs, puis le helper, puis le declencheur, puis la politique D-066 remplacee
par la clause de `0004` reproduite LITTERALEMENT, puis les domaines rendus a leur liste
d'origine, puis les colonnes.

Aller-retour MESURE sur une empreinte riche (fonctions avec md5 du corps, ACL et `proconfig`;
politiques avec expression complete; contraintes; declencheurs avec leurs arguments; colonnes;
privileges de colonne; RLS): `UP` est DETERMINISTE (UP1 == UP2) et la politique
`jobs_purge_terminal_only` revient IDENTIQUE au caractere pres, comme D-066 l'exige.
Une seule difference subsiste entre l'etat d'origine et l'etat descendu, et elle est VOULUE:
les trois domaines rendus a leur liste d'origine reviennent marques `NOT VALID`. C'est le
mecanisme de `0008`, repris tel quel par `0013` et `0014`, dont la descente fait deja
exactement cela -- une descente ne peut pas VALIDER une contrainte contre des lignes qui
peuvent la violer (un travail `purge_store` subsistant, par exemple). La contrainte est donc
appliquee aux nouvelles lignes, sans relire les anciennes. Le test d'aller-retour l'affirme
explicitement plutot que de le masquer.
"""
from alembic import op

revision = "0016_tenant_purge"
down_revision = "0015_raw_object_purge"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"
WORKER_ROLE = "mervio_worker"

#: plancher de retention de `0004`. Reproduit a l'IDENTIQUE: la descente doit restaurer la
#: politique d'origine au caractere pres (exigence de D-066).
PURGE_FLOOR = "1 hour"

#: rang minimal du DEMANDEUR. D-065 Q6: `owner` pour une purge, la ou l'effacement se contente
#: d'`admin`. Le rang est RELU a l'execution, jamais celui de la mise en file.
OWNER_RANK = 3

STORE_JOB = "purge_store"
ORGANIZATION_JOB = "purge_organization"
PURGE_JOB_TYPES = (STORE_JOB, ORGANIZATION_JOB)

#: etat des lignes `jobs` du depot avant cette revision
JOB_TYPES = ("import", "analysis", "purge", "redact_customer")

#: `0014` n'en connaissait qu'un
PURGE_REASONS = ("customer_erasure",)
TENANT_PURGE_REASONS = ("store_purge", "organization_purge")

TENANT_STATUSES = ("active", "purging", "purged")

#: Colonnes que `mervio_app` peut inserer sur les deux tables de locataire, APRES cette
#: revision. `status` et `purged_at` en sont EXCLUES: ce sont les colonnes de cycle de vie, et
#: l'ancre de D-066 en particulier ne doit etre ecrite que par une fonction privilegiee.
#:
#: Pourquoi enumerer, et pourquoi ne pas se contenter d'un `REVOKE` de colonne: MESURE --
#: `REVOKE INSERT (status) ... FROM role` est SANS EFFET quand `INSERT` a ete accorde au niveau
#: TABLE (`0001`). `has_column_privilege` reste vrai et l'insertion passe. La seule forme qui
#: fonctionne est de retirer le privilege de table, puis de le re-accorder colonne par colonne.
#: Consequence assumee: une colonne ajoutee plus tard ne sera PAS inserable par `mervio_app`
#: tant qu'on ne l'aura pas ajoutee ici. C'est fail-closed, et c'est voulu sur ces deux tables.
INSERTABLE_COLUMNS = {
    "organizations": ("id", "name", "created_at"),
    "stores": ("id", "organization_id", "name", "currency", "created_at",
               "timezone", "timezone_source"),
}

#: vocabulaire d'audit du depot avant cette revision (releve du catalogue, pas de memoire)
BASE_ACTIONS = (
    "job.enqueued", "job.claimed", "job.succeeded", "job.failed", "job.requeued",
    "job.recovered", "job.cancelled", "import.started", "import.succeeded", "import.failed",
    "analysis.started", "analysis.succeeded", "analysis.failed", "purge.started",
    "purge.completed", "organization.created", "member.added", "store.created",
    "connection.created", "service.authorized", "service.revoked", "object.uploaded",
    "customer.redacted",
)
#: D-065 Q1: vocabulaire DEDIE. `purge.started`/`purge.completed` appartiennent a la purge de
#: RETENTION (004.2) et ne sont PAS reutilises: les reemployer rendrait une purge D-051
#: indistinguable d'une expiration de retention dans une revue de conformite.
TENANT_PURGE_ACTIONS = ("store.purge_started", "store.purged",
                        "organization.purge_started", "organization.purged")

#: tables dont l'INSERT est refuse des que le tenant n'est plus `active`. Les SEPT tables
#: canoniques n'y figurent pas, et c'est DELIBERE: une ligne canonique n'entre que dans un
#: instantane `ingesting` (`canonical_rows_guard_sealed`), et un instantane ne se cree que
#: dans une organisation active (`data_snapshots` ci-dessous). Elles sont donc protegees
#: TRANSITIVEMENT, sans payer un declencheur par ligne sur le chemin chaud de l'import.
CLOSURE_GUARDED = ("stores", "connections", "data_snapshots", "snapshot_sources",
                   "analysis_runs", "reports", "raw_objects", "customer_redactions", "jobs")

#: tables de `CLOSURE_GUARDED` qui portent un `store_id` et exigent donc aussi le statut de la
#: boutique. `stores` n'en fait pas partie: elle EST la boutique.
STORE_SCOPED = ("connections", "data_snapshots", "snapshot_sources", "analysis_runs",
                "reports", "raw_objects")


def _in_list(values) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace_check(table: str, column: str, values, *, validated: bool) -> None:
    """Mecanisme de `0008`, repris tel quel par `0013` et `0014`: seule la liste change."""
    name = f"{table}_{column}_check"
    op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {name}")
    suffix = "" if validated else " NOT VALID"
    op.execute(f"ALTER TABLE public.{table} ADD CONSTRAINT {name} "
               f"CHECK ({column} IN ({_in_list(values)})){suffix}")


def _restrict_lifecycle_insert(table: str) -> None:
    """Retire `INSERT` de la table, puis le rend colonne par colonne -- sauf le cycle de vie."""
    columns = ", ".join(INSERTABLE_COLUMNS[table])
    op.execute(f"REVOKE INSERT ON public.{table} FROM {APP_ROLE}")
    op.execute(f"GRANT INSERT ({columns}) ON public.{table} TO {APP_ROLE}")


def _restore_table_insert(table: str) -> None:
    """Rend l'etat d'avant `0016`: un unique `INSERT` de TABLE, sans aucun droit de colonne.

    Verifie: apres ce couple, `relacl` ne porte plus aucune entree de colonne residuelle -- la
    descente restaure donc le catalogue a l'identique, ce que l'aller-retour controle.
    """
    columns = ", ".join(INSERTABLE_COLUMNS[table])
    op.execute(f"REVOKE INSERT ({columns}) ON public.{table} FROM {APP_ROLE}")
    op.execute(f"GRANT INSERT ON public.{table} TO {APP_ROLE}")


def _replace_purge_reason(values, *, validated: bool) -> None:
    """`0014` a nomme cette contrainte `_known` et lui a donne la forme `IS NULL OR IN (...)`.

    Elle n'entre donc pas dans le moule de `_replace_check`, et la reproduire de memoire
    changerait la chaine rendue par `pg_get_constraintdef` -- ce que l'aller-retour verifie.
    """
    name = "raw_objects_purge_reason_known"
    op.execute(f"ALTER TABLE public.raw_objects DROP CONSTRAINT {name}")
    suffix = "" if validated else " NOT VALID"
    op.execute(f"ALTER TABLE public.raw_objects ADD CONSTRAINT {name} "
               f"CHECK (purge_reason IS NULL OR purge_reason IN ({_in_list(values)})){suffix}")


# -- garde partagee des huit operations privilegiees ------------------------------------------

PURGE_GUARD = f"""
    CREATE FUNCTION public.app_purge_guard(p_job_id uuid, p_job_type text)
    RETURNS public.jobs
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_organization uuid := public.app_current_organization_id();
        v_service      uuid;
        v_job          public.jobs%ROWTYPE;
        v_token        text := pg_catalog.current_setting('app.job_lease_token', true);
        v_rank         integer;
    BEGIN
        -- SECURITY INVOKER, DELIBEREMENT. Appele depuis un `SECURITY DEFINER`, ce corps
        -- s'execute avec les privileges du DEFINISSEUR et voit `session_user` inchange; appele
        -- directement, il est refuse faute d'`EXECUTE` (REVOKE ALL ... FROM PUBLIC, aucun
        -- GRANT). Il n'ajoute donc aucune surface privilegiee.
        IF v_organization IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'insufficient_privilege',
                MESSAGE = 'a tenant purge runs in the context of one organization';
        END IF;

        -- IDENTITE DU SERVICE: `session_user`, jamais `current_user`, qui vaut le
        -- proprietaire a l'interieur d'un `SECURITY DEFINER` (0014).
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
        IF v_job.job_type <> p_job_type THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'this operation does not run for this job type';
        END IF;
        IF v_job.status <> 'running' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'a tenant purge only runs for a claimed job';
        END IF;
        -- JETON D'EXCLUSION (0006): un ancien detenteur de bail ne purge plus rien.
        IF v_token IS NULL OR v_token = ''
           OR v_token <> v_job.attempts::text || ':' || v_job.locked_by THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'only the lease holder purges a tenant';
        END IF;
        -- LE DEMANDEUR A-T-IL ENCORE LE RANG? Relu MAINTENANT (D-052), et `owner` ici (D-065
        -- Q6), la ou l'effacement se contente d'`admin`.
        SELECT CASE m.role WHEN 'viewer' THEN 0 WHEN 'analyst' THEN 1
                           WHEN 'admin' THEN 2 WHEN 'owner' THEN 3 END
        INTO v_rank
        FROM public.memberships m JOIN public.users u ON u.id = m.user_id
        WHERE m.organization_id = v_organization AND m.user_id = v_job.enqueued_by
          AND u.kind = 'human';
        IF v_rank IS NULL OR v_rank < {OWNER_RANK} THEN
            RAISE EXCEPTION USING
                ERRCODE = 'insufficient_privilege',
                MESSAGE = 'the requester no longer holds the rank required to purge a tenant';
        END IF;

        RETURN v_job;
    END
    $$
"""


# -- declencheur de cloture -------------------------------------------------------------------

CLOSURE_GUARD = """
    CREATE FUNCTION public.tenant_closure_guard() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_status text;
    BEGIN
        -- Relations QUALIFIEES `public.` (D-049): un corps plpgsql re-resout ses noms a
        -- l'execution, contrairement a une expression de politique.
        SELECT o.status INTO v_status FROM public.organizations o
        WHERE o.id = NEW.organization_id;
        IF v_status IS NOT NULL AND v_status <> 'active' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'this organization is closed and accepts no new record',
                DETAIL  = 'table ' || TG_TABLE_NAME;
        END IF;
        -- Tables portant un `store_id`: la boutique doit l'etre aussi. `TG_ARGV[0]` dit si la
        -- colonne existe, plutot que de deviner depuis `TG_TABLE_NAME`.
        IF TG_ARGV[0] = 'store' THEN
            SELECT s.status INTO v_status FROM public.stores s
            WHERE s.organization_id = NEW.organization_id AND s.id = NEW.store_id;
            IF v_status IS NOT NULL AND v_status <> 'active' THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'this store is closed and accepts no new record',
                    DETAIL  = 'table ' || TG_TABLE_NAME;
            END IF;
        END IF;
        RETURN NEW;
    END
    $$
"""


# -- operations privilegiees (D-052) ----------------------------------------------------------
#
# Les huit partagent `app_purge_guard`. Ce qui suit chaque appel est ce qui leur est PROPRE:
# la garde ne decide de rien, elle refuse. Chacune verifie en outre son propre invariant de
# portee, en deux lignes lisibles, plutot que de le confier au helper.

#: verrou consultatif D-063. Construction de cle RATIFIEE, identique a `persistence/concurrency.py`:
#: `hashtextextended(organization_id || ':' || portee, 0)`, portee `org`. Aucune autre portee n'est
#: creee -- `purge_store` prend LUI AUSSI le verrou d'ORGANISATION, D-063 l'exigeant explicitement.
#: Le litteral est SCINDE (`':' || 'org'`) et non ecrit `':org'`: Alembic passe le SQL a
#: SQLAlchemy, qui prendrait `:org` pour un parametre lie et refuserait la migration.
_LOCK = ("PERFORM pg_catalog.pg_advisory_xact_lock("
         "pg_catalog.hashtextextended(v_job.organization_id::text || ':' || 'org', 0));")

#: le principal de service, deja PROUVE existant et autorise par la garde.
_SERVICE = ("(SELECT u.id FROM public.users u "
            "WHERE u.kind = 'service' AND u.idp_subject = 'service:' || session_user)")

CLOSE_ORGANIZATION = f"""
    CREATE FUNCTION public.app_close_organization(p_job_id uuid)
    RETURNS TABLE (jobs_cancelled bigint, raw_objects_marked bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_job     public.jobs%ROWTYPE;
        v_status  text;
        v_jobs    bigint := 0;
        v_objects bigint := 0;
    BEGIN
        v_job := public.app_purge_guard(p_job_id, '{ORGANIZATION_JOB}');
        -- D-063: verrou EXCLUSIF, dans CETTE transaction. Il attend les imports en vol au lieu
        -- de les faire echouer a mi-parcours, et ordonne totalement purge et effacement.
        {_LOCK}

        SELECT o.status INTO v_status FROM public.organizations o
        WHERE o.id = v_job.organization_id;
        IF v_status = 'purged' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'this organization is already purged';
        END IF;

        -- CLOTURE. A partir d'ici le declencheur refuse toute nouvelle ecriture metier, et
        -- l'exception D-066 s'ouvre pour les seuls travaux TERMINAUX de cette organisation.
        UPDATE public.organizations o SET status = 'purging'
        WHERE o.id = v_job.organization_id AND o.status = 'active';

        -- D-065 Q7: les travaux EN FILE sont annules, dans CETTE transaction. Un travail
        -- `running` n'est PAS annule -- la transition le refuse (`0004`) et D-063 le serialise.
        -- Le travail de purge lui-meme est `running`: il n'est donc jamais sa propre victime.
        UPDATE public.jobs j
        SET status = 'cancelled', finished_at = pg_catalog.now(), updated_at = pg_catalog.now()
        WHERE j.organization_id = v_job.organization_id AND j.status = 'queued';
        GET DIAGNOSTICS v_jobs = ROW_COUNT;

        -- Les octets deviennent ILLISIBLES des maintenant. Leur DESTRUCTION appartient au
        -- worker, et la suppression de la ligne a `app_purge_finalize_raw_object` (D-065 Q3).
        UPDATE public.raw_objects ro
        SET state = 'purging', purge_reason = 'organization_purge'
        WHERE ro.organization_id = v_job.organization_id AND ro.state = 'available';
        GET DIAGNOSTICS v_objects = ROW_COUNT;

        INSERT INTO public.audit_events
            (id, organization_id, actor_type, actor_id, action, resource_type, resource_id,
             correlation_id, outcome, on_behalf_of, metadata)
        VALUES (pg_catalog.gen_random_uuid(), v_job.organization_id, 'worker', {_SERVICE},
                'organization.purge_started', 'organization', v_job.organization_id,
                v_job.correlation_id, 'started', v_job.enqueued_by,
                pg_catalog.jsonb_build_object('job_id', v_job.id,
                                              'jobs_cancelled', v_jobs,
                                              'raw_objects_marked', v_objects));

        RETURN QUERY SELECT v_jobs, v_objects;
    END
    $$
"""

CLOSE_STORE = f"""
    CREATE FUNCTION public.app_close_store(p_job_id uuid)
    RETURNS TABLE (jobs_cancelled bigint, raw_objects_marked bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_job     public.jobs%ROWTYPE;
        v_status  text;
        v_jobs    bigint := 0;
        v_objects bigint := 0;
    BEGIN
        v_job := public.app_purge_guard(p_job_id, '{STORE_JOB}');
        {_LOCK}

        -- L'OBJET DE L'OPERATION vient du TRAVAIL (D-052), jamais d'un parametre d'appel.
        -- `jobs.store_id` porte en outre une FK vers `stores (organization_id, id)`: la
        -- boutique appartient a l'organisation du contexte par le SCHEMA, pas par une
        -- condition qu'il faudrait relire.
        IF v_job.store_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'a store purge job names no store';
        END IF;
        SELECT s.status INTO v_status FROM public.stores s
        WHERE s.organization_id = v_job.organization_id AND s.id = v_job.store_id;
        IF v_status = 'purged' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'this store is already purged';
        END IF;

        UPDATE public.stores s SET status = 'purging'
        WHERE s.organization_id = v_job.organization_id AND s.id = v_job.store_id
          AND s.status = 'active';

        -- Seuls les travaux EN FILE DE CETTE BOUTIQUE. Ceux de l'organisation qui ne la
        -- nomment pas continuent: l'organisation, elle, reste vivante.
        UPDATE public.jobs j
        SET status = 'cancelled', finished_at = pg_catalog.now(), updated_at = pg_catalog.now()
        WHERE j.organization_id = v_job.organization_id AND j.store_id = v_job.store_id
          AND j.status = 'queued';
        GET DIAGNOSTICS v_jobs = ROW_COUNT;

        UPDATE public.raw_objects ro
        SET state = 'purging', purge_reason = 'store_purge'
        WHERE ro.organization_id = v_job.organization_id AND ro.store_id = v_job.store_id
          AND ro.state = 'available';
        GET DIAGNOSTICS v_objects = ROW_COUNT;

        INSERT INTO public.audit_events
            (id, organization_id, actor_type, actor_id, action, resource_type, resource_id,
             correlation_id, outcome, on_behalf_of, metadata)
        VALUES (pg_catalog.gen_random_uuid(), v_job.organization_id, 'worker', {_SERVICE},
                'store.purge_started', 'store', v_job.store_id,
                v_job.correlation_id, 'started', v_job.enqueued_by,
                pg_catalog.jsonb_build_object('job_id', v_job.id,
                                              'jobs_cancelled', v_jobs,
                                              'raw_objects_marked', v_objects));

        RETURN QUERY SELECT v_jobs, v_objects;
    END
    $$
"""

DESTROY_IDENTITY_KEY = f"""
    CREATE FUNCTION public.app_destroy_identity_key(p_job_id uuid)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_job     public.jobs%ROWTYPE;
        v_updated integer;
    BEGIN
        -- D-065 Q4: UNIQUEMENT une purge d'ORGANISATION. `purge_store` ne detruit JAMAIS le
        -- sel, meme si la boutique purgee est la derniere: le sel a pour cle primaire
        -- l'`organization_id`, et une organisation sans boutique reste vivante.
        v_job := public.app_purge_guard(p_job_id, '{ORGANIZATION_JOB}');

        -- D-053: le sel ne se DETRUIT, jamais ne se supprime -- le declencheur de `0011`
        -- interdit le `DELETE`, fige `master_key_id` et rend immuable une ligne deja detruite.
        -- Le rejeu est donc sans effet, et c'est la seule facon d'etre idempotent ici.
        UPDATE public.organization_identity_keys k
        SET salt = NULL, destroyed_at = pg_catalog.now()
        WHERE k.organization_id = v_job.organization_id AND k.salt IS NOT NULL;
        GET DIAGNOSTICS v_updated = ROW_COUNT;
        RETURN v_updated > 0;
    END
    $$
"""

PURGE_FINALIZE_RAW_OBJECT = f"""
    CREATE FUNCTION public.app_purge_finalize_raw_object(p_job_id uuid, p_raw_object_id uuid)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_job     public.jobs%ROWTYPE;
        v_type    text;
        v_state   text;
        v_deleted integer;
    BEGIN
        -- D-065 Q2: fonction DISTINCTE de `app_finalize_raw_object_purge`, qui reste dediee a
        -- `redact_customer` et n'est PAS elargie. Les deux sorts different d'ailleurs: un
        -- effacement CONSERVE la ligne en `purged` avec ses metadonnees; une purge la RETIRE,
        -- la boutique ou l'organisation disparaissant avec elle (D-051).
        SELECT j.job_type INTO v_type FROM public.jobs j WHERE j.id = p_job_id;
        IF v_type IS NULL OR v_type NOT IN ({_in_list(PURGE_JOB_TYPES)}) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'this operation only runs for a tenant purge job';
        END IF;
        v_job := public.app_purge_guard(p_job_id, v_type);

        SELECT ro.state INTO v_state FROM public.raw_objects ro WHERE ro.id = p_raw_object_id;
        IF NOT FOUND THEN
            -- REJEU: la ligne a deja ete retiree par un passage precedent, interrompu apres le
            -- commit. Aucune ecriture, aucune erreur -- c'est ce qui rend la reprise sure.
            RETURN false;
        END IF;
        -- D-065 Q3: SEULE transition autorisee. `available -> supprime` sauterait l'etat
        -- illisible et, surtout, supprimerait la SEULE trace de ce qu'il faut detruire: un
        -- echec du magasin deviendrait alors des octets orphelins que plus rien ne designe.
        IF v_state <> 'purging' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'only a raw object being purged is removed';
        END IF;

        -- L'APPELANT GARANTIT que les octets ont deja disparu. Cette fonction ne sait pas, et
        -- ne peut pas savoir, si `ObjectStore.delete` a reussi: elle garantit le reste --
        -- qu'aucune ligne ne disparaisse sans etre passee par `purging`, et qu'un travail
        -- autorise le demande.
        DELETE FROM public.raw_objects ro
        WHERE ro.id = p_raw_object_id AND ro.state = 'purging';
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        RETURN v_deleted > 0;
    END
    $$
"""

PURGE_TENANT_DATA = f"""
    CREATE FUNCTION public.app_purge_tenant_data(p_job_id uuid)
    RETURNS TABLE (reports bigint, analysis_runs bigint, data_snapshots bigint,
                   connections bigint, customer_redactions bigint, jobs bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_job    public.jobs%ROWTYPE;
        v_type   text;
        v_store  uuid;
        v_reports bigint := 0; v_runs bigint := 0; v_snaps bigint := 0;
        v_conns bigint := 0;   v_reds bigint := 0; v_jobs bigint := 0;
    BEGIN
        SELECT j.job_type INTO v_type FROM public.jobs j WHERE j.id = p_job_id;
        IF v_type IS NULL OR v_type NOT IN ({_in_list(PURGE_JOB_TYPES)}) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'this operation only runs for a tenant purge job';
        END IF;
        v_job := public.app_purge_guard(p_job_id, v_type);
        {_LOCK}
        v_store := v_job.store_id;

        -- La portee DOIT deja etre close: detruire avant d'avoir ferme laisserait un import en
        -- vol recreer ce qu'on vient de supprimer.
        IF v_type = '{ORGANIZATION_JOB}' THEN
            IF NOT EXISTS (SELECT 1 FROM public.organizations o
                           WHERE o.id = v_job.organization_id AND o.status = 'purging') THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'the organization must be closed before its data is destroyed';
            END IF;
        ELSE
            IF NOT EXISTS (SELECT 1 FROM public.stores s
                           WHERE s.organization_id = v_job.organization_id
                             AND s.id = v_store AND s.status = 'purging') THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'the store must be closed before its data is destroyed';
            END IF;
        END IF;

        -- ORDRE IMPOSE PAR LES FK REELLES (D-051), reconstruit depuis le catalogue et non
        -- depuis le roadmap: `reports -> analysis_runs -> data_snapshots` sont en RESTRICT.
        DELETE FROM public.reports r
        WHERE r.organization_id = v_job.organization_id
          AND (v_store IS NULL OR r.store_id = v_store);
        GET DIAGNOSTICS v_reports = ROW_COUNT;

        DELETE FROM public.analysis_runs a
        WHERE a.organization_id = v_job.organization_id
          AND (v_store IS NULL OR a.store_id = v_store);
        GET DIAGNOSTICS v_runs = ROW_COUNT;

        -- C1: on ne supprime JAMAIS une table canonique directement -- un instantane SCELLE
        -- les rend immuables (`canonical_rows_guard_sealed`), et le refus porte aussi sur le
        -- DELETE. Le CASCADE depuis `data_snapshots`, lui, passe.
        -- C2: UN SEUL `DELETE`, jamais par lots -- la FK AUTO-REFERENTE `supersedes` est en
        -- RESTRICT, donc un instantane superseded ne se supprime pas seul.
        DELETE FROM public.data_snapshots d
        WHERE d.organization_id = v_job.organization_id
          AND (v_store IS NULL OR d.store_id = v_store);
        GET DIAGNOSTICS v_snaps = ROW_COUNT;

        DELETE FROM public.connections c
        WHERE c.organization_id = v_job.organization_id
          AND (v_store IS NULL OR c.store_id = v_store);
        GET DIAGNOSTICS v_conns = ROW_COUNT;

        -- D-065 Q5: `customer_redactions` est organization-scoped et ne porte AUCUN
        -- `store_id`. Une purge de BOUTIQUE ne la touche donc pas: la preuve atteste d'un
        -- effacement demande par une personne, pas d'un fait de boutique.
        IF v_type = '{ORGANIZATION_JOB}' THEN
            DELETE FROM public.customer_redactions cr
            WHERE cr.organization_id = v_job.organization_id;
            GET DIAGNOSTICS v_reds = ROW_COUNT;
        END IF;

        -- D-066: les travaux TERMINAUX partent sans attendre le plancher d'une heure, parce
        -- que l'organisation est `purging`. `queued` et `running` restent hors de portee (I1),
        -- donc le travail de purge -- `running` -- ne peut pas se supprimer lui-meme. La
        -- clause `id <> p_job_id` est une ceinture, pas la bretelle.
        DELETE FROM public.jobs j
        WHERE j.organization_id = v_job.organization_id
          AND j.id <> v_job.id
          AND (v_store IS NULL OR j.store_id = v_store);
        GET DIAGNOSTICS v_jobs = ROW_COUNT;

        RETURN QUERY SELECT v_reports, v_runs, v_snaps, v_conns, v_reds, v_jobs;
    END
    $$
"""

TOMBSTONE_ORGANIZATION_STORES = f"""
    CREATE FUNCTION public.app_tombstone_organization_stores(p_job_id uuid)
    RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_job       public.jobs%ROWTYPE;
        v_tombstoned bigint := 0;
    BEGIN
        -- RESERVEE a la purge d'ORGANISATION. `app_tombstone_store` reste, elle, reservee a la
        -- purge de BOUTIQUE et n'est pas elargie: la separation des deux voies est portee par
        -- la GARDE, exactement comme pour les sept autres operations de purge (D-065 Q2).
        v_job := public.app_purge_guard(p_job_id, '{ORGANIZATION_JOB}');
        {_LOCK}

        -- BARRIERE, avant toute ecriture. Une pierre tombale de boutique affirme que la
        -- boutique n'a plus rien; la poser sur des donnees encore presentes serait la meme
        -- faute que marquer un objet `purged` avant d'en detruire les octets.
        IF EXISTS (SELECT 1 FROM public.raw_objects ro
                   WHERE ro.organization_id = v_job.organization_id) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'raw objects remain: their bytes must be destroyed first';
        END IF;
        IF EXISTS (SELECT 1 FROM public.jobs j
                   WHERE j.organization_id = v_job.organization_id AND j.id <> v_job.id) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'jobs remain: they must be removed before the stores are tombstoned';
        END IF;
        IF EXISTS (SELECT 1 FROM public.data_snapshots d
                   WHERE d.organization_id = v_job.organization_id)
           OR EXISTS (SELECT 1 FROM public.analysis_runs a
                      WHERE a.organization_id = v_job.organization_id)
           OR EXISTS (SELECT 1 FROM public.reports r
                      WHERE r.organization_id = v_job.organization_id)
           OR EXISTS (SELECT 1 FROM public.connections c
                      WHERE c.organization_id = v_job.organization_id) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'store data remains: it must be destroyed before the tombstone';
        END IF;

        -- UN SEUL `UPDATE`, jamais une boucle: une boucle interrompue laisserait une partie des
        -- boutiques tombstonees et l'autre vivante, etat que rien ne saurait interpreter. Ici
        -- c'est tout ou rien, et un rejeu ne trouve plus rien a faire.
        --
        -- `active -> purged` est ACCEPTE autant que `purging -> purged`: la cloture de
        -- l'ORGANISATION est deja la barriere d'ecriture qui protege l'ensemble, et exiger un
        -- passage prealable par `purging` n'ajouterait aucune garantie -- seulement une
        -- transition artificielle a la charge de `app_close_organization`.
        UPDATE public.stores s
        SET name = '[purged]', currency = NULL, status = 'purged',
            -- `coalesce` est une construction de la GRAMMAIRE SQL, pas une fonction du
            -- catalogue: elle n'est pas resolue par `search_path`, donc pas ombrable,
            -- et ne peut pas etre qualifiee (`pg_catalog.coalesce` n'existe pas).
            purged_at = coalesce(s.purged_at, pg_catalog.now())
        WHERE s.organization_id = v_job.organization_id AND s.status <> 'purged';
        GET DIAGNOSTICS v_tombstoned = ROW_COUNT;
        RETURN v_tombstoned;
    END
    $$
"""

TOMBSTONE_ORGANIZATION = f"""
    CREATE FUNCTION public.app_tombstone_organization(p_job_id uuid)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_job     public.jobs%ROWTYPE;
        v_updated integer;
    BEGIN
        v_job := public.app_purge_guard(p_job_id, '{ORGANIZATION_JOB}');
        {_LOCK}

        -- D-065 Q3 / D-066: on ne pose pas la pierre tombale tant qu'il reste des octets a
        -- detruire, ni tant que des travaux restent a retirer. Apres `purged`, l'exception
        -- D-066 SE REFERME et l'etape 10 de D-051 redevient impossible: cet ordre n'est donc
        -- pas une convention, c'est la condition pour que la purge soit complete.
        IF EXISTS (SELECT 1 FROM public.raw_objects ro
                   WHERE ro.organization_id = v_job.organization_id) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'raw objects remain: their bytes must be destroyed first';
        END IF;
        IF EXISTS (SELECT 1 FROM public.jobs j
                   WHERE j.organization_id = v_job.organization_id AND j.id <> v_job.id) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'jobs remain: they must be removed before the tombstone closes D-066';
        END IF;
        IF EXISTS (SELECT 1 FROM public.stores s
                   WHERE s.organization_id = v_job.organization_id AND s.status <> 'purged') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'every store must be tombstoned before its organization';
        END IF;

        -- PIERRE TOMBALE (D-051): la ligne RESTE, videe. Elle porte les FK d'audit, de
        -- `memberships` et de `service_authorizations`, toutes en RESTRICT: la supprimer
        -- rendrait l'audit illisible, ce que D-051 a explicitement refuse.
        UPDATE public.organizations o
        SET name = '[purged]', status = 'purged', purged_at = pg_catalog.now()
        WHERE o.id = v_job.organization_id AND o.status = 'purging';
        GET DIAGNOSTICS v_updated = ROW_COUNT;

        INSERT INTO public.audit_events
            (id, organization_id, actor_type, actor_id, action, resource_type, resource_id,
             correlation_id, outcome, on_behalf_of, metadata)
        VALUES (pg_catalog.gen_random_uuid(), v_job.organization_id, 'worker', {_SERVICE},
                'organization.purged', 'organization', v_job.organization_id,
                v_job.correlation_id, 'succeeded', v_job.enqueued_by,
                pg_catalog.jsonb_build_object('job_id', v_job.id));

        RETURN v_updated > 0;
    END
    $$
"""

TOMBSTONE_STORE = f"""
    CREATE FUNCTION public.app_tombstone_store(p_job_id uuid)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        v_job     public.jobs%ROWTYPE;
        v_updated integer;
    BEGIN
        v_job := public.app_purge_guard(p_job_id, '{STORE_JOB}');
        {_LOCK}
        IF v_job.store_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'a store purge job names no store';
        END IF;
        IF EXISTS (SELECT 1 FROM public.raw_objects ro
                   WHERE ro.organization_id = v_job.organization_id
                     AND ro.store_id = v_job.store_id) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'raw objects remain: their bytes must be destroyed first';
        END IF;

        -- D-065 Q4, rappel a l'endroit ou la tentation serait la plus grande: meme si cette
        -- boutique est la DERNIERE de l'organisation, le sel d'identite n'est PAS detruit ici.
        -- L'organisation reste vivante, et peut recreer une boutique et importer.
        UPDATE public.stores s
        SET name = '[purged]', currency = NULL, status = 'purged', purged_at = pg_catalog.now()
        WHERE s.organization_id = v_job.organization_id AND s.id = v_job.store_id
          AND s.status = 'purging';
        GET DIAGNOSTICS v_updated = ROW_COUNT;

        INSERT INTO public.audit_events
            (id, organization_id, actor_type, actor_id, action, resource_type, resource_id,
             correlation_id, outcome, on_behalf_of, metadata)
        VALUES (pg_catalog.gen_random_uuid(), v_job.organization_id, 'worker', {_SERVICE},
                'store.purged', 'store', v_job.store_id,
                v_job.correlation_id, 'succeeded', v_job.enqueued_by,
                pg_catalog.jsonb_build_object('job_id', v_job.id));

        RETURN v_updated > 0;
    END
    $$
"""

#: les HUIT, avec leur signature exacte -- l'ordre compte a la descente (voir l'en-tete).
PURGE_FUNCTIONS = (
    ("app_close_organization", "uuid"),
    ("app_close_store", "uuid"),
    ("app_destroy_identity_key", "uuid"),
    ("app_purge_finalize_raw_object", "uuid, uuid"),
    ("app_purge_tenant_data", "uuid"),
    ("app_tombstone_organization_stores", "uuid"),
    ("app_tombstone_organization", "uuid"),
    ("app_tombstone_store", "uuid"),
)


# -- politique de suppression des travaux (D-066) ---------------------------------------------
#
# Reproduite depuis `0004`, mot pour mot, avant modification. La descente la restaure a
# l'identique: `pg_get_expr(polqual, polrelid)` doit rendre exactement la meme chaine.

JOBS_DELETE_POLICY_ORIGINAL = f"""
    CREATE POLICY jobs_purge_terminal_only ON jobs AS RESTRICTIVE FOR DELETE
        USING (status IN ('succeeded', 'failed', 'cancelled')
               AND finished_at IS NOT NULL
               AND finished_at < now() - interval '{PURGE_FLOOR}')
"""

#: D-066. La disjonction porte sur le SEUL plancher temporel. I1 et I2 restent en facteur, donc
#: `queued` et `running` demeurent indestructibles -- y compris le travail de purge lui-meme.
JOBS_DELETE_POLICY_D066 = f"""
    CREATE POLICY jobs_purge_terminal_only ON jobs AS RESTRICTIVE FOR DELETE
        USING (status IN ('succeeded', 'failed', 'cancelled')
               AND finished_at IS NOT NULL
               AND (finished_at < now() - interval '{PURGE_FLOOR}'
                    OR EXISTS (SELECT 1 FROM public.organizations o
                               WHERE o.id = jobs.organization_id AND o.status = 'purging')))
"""


def _add_tenant_state(table: str) -> None:
    op.execute(f"""
        ALTER TABLE public.{table}
            ADD COLUMN status text NOT NULL DEFAULT 'active'
                CONSTRAINT {table}_status_known CHECK (status IN ({_in_list(TENANT_STATUSES)})),
            ADD COLUMN purged_at timestamptz NULL,
            ADD CONSTRAINT {table}_purge_consistent
                CHECK ((status = 'purged') = (purged_at IS NOT NULL))
    """)


def _drop_tenant_state(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {table}_purge_consistent")
    op.execute(f"ALTER TABLE public.{table} DROP COLUMN purged_at")
    op.execute(f"ALTER TABLE public.{table} DROP COLUMN status")


def upgrade() -> None:
    # 1. etat des tenants. Aucun `GRANT UPDATE` sur ces colonnes: `mervio_app` ne peut donc pas
    #    ouvrir l'exception D-066 lui-meme, et c'est ce qui la borne.
    for table in ("organizations", "stores"):
        _add_tenant_state(table)
        # Un `GRANT INSERT` de TABLE (`0001`) couvre AUTOMATIQUEMENT les colonnes ajoutees
        # ensuite: sans ceci, `mervio_app` pourrait creer une organisation deja `purging`.
        # Aucun autre privilege n'est retire -- `SELECT` et l'`INSERT` des autres colonnes
        # restent inchanges, et `UPDATE (name)` / `UPDATE (name, currency)` ne sont pas touches.
        _restrict_lifecycle_insert(table)

    # 2. types de travaux, EN BASE SEULEMENT (voir le contrat de sequence en tete)
    _replace_check("jobs", "job_type", JOB_TYPES + PURGE_JOB_TYPES, validated=True)

    # 3. motifs de purge: la semantique `customer_erasure` est INCHANGEE
    _replace_purge_reason(PURGE_REASONS + TENANT_PURGE_REASONS, validated=True)

    # 4. vocabulaire d'audit DEDIE (D-065 Q1). Les types de ressource `store` et
    #    `organization` existent deja: aucun n'est ajoute.
    _replace_check("audit_events", "action", BASE_ACTIONS + TENANT_PURGE_ACTIONS, validated=True)

    # 5. D-066: la politique est REMPLACEE, jamais supprimee ni desactivee.
    op.execute("DROP POLICY jobs_purge_terminal_only ON public.jobs")
    op.execute(JOBS_DELETE_POLICY_D066)

    # 6. cloture: plus aucune ecriture metier dans un tenant qui n'est plus `active`
    op.execute(CLOSURE_GUARD)
    for table in CLOSURE_GUARDED:
        scope = "store" if table in STORE_SCOPED else "org"
        op.execute(f"""
            CREATE TRIGGER {table}_closure_guard BEFORE INSERT ON public.{table}
                FOR EACH ROW EXECUTE FUNCTION public.tenant_closure_guard('{scope}')
        """)

    # 7. la garde partagee, PUIS les huit operations qui l'appellent
    op.execute(PURGE_GUARD)
    op.execute("REVOKE ALL ON FUNCTION public.app_purge_guard(uuid, text) FROM PUBLIC")
    for body in (CLOSE_ORGANIZATION, CLOSE_STORE, DESTROY_IDENTITY_KEY,
                 PURGE_FINALIZE_RAW_OBJECT, PURGE_TENANT_DATA,
                 TOMBSTONE_ORGANIZATION_STORES, TOMBSTONE_ORGANIZATION, TOMBSTONE_STORE):
        op.execute(body)
    for name, args in PURGE_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION public.{name}({args}) FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{name}({args}) TO {WORKER_ROLE}")


def downgrade() -> None:
    # ORDRE IMPOSE, ET NON PROTEGE PAR LE MOTEUR: PostgreSQL n'enregistre aucune dependance
    # `pg_depend` entre une fonction plpgsql et celles qu'elle appelle. Retirer la garde avant
    # ses appelants REUSSIRAIT, en laissant huit fonctions privilegiees pointer dans le vide.
    # Les definisseurs partent donc D'ABORD, la garde ENSUITE. Un test verrouille cet ordre.
    for name, args in PURGE_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION public.{name}({args}) FROM {WORKER_ROLE}")
        op.execute(f"DROP FUNCTION public.{name}({args})")
    op.execute("DROP FUNCTION public.app_purge_guard(uuid, text)")

    for table in CLOSURE_GUARDED:
        op.execute(f"DROP TRIGGER {table}_closure_guard ON public.{table}")
    op.execute("DROP FUNCTION public.tenant_closure_guard()")

    # D-066 exige la restauration EXACTE de la politique de `0004`.
    op.execute("DROP POLICY jobs_purge_terminal_only ON public.jobs")
    op.execute(JOBS_DELETE_POLICY_ORIGINAL)

    _replace_check("audit_events", "action", BASE_ACTIONS, validated=False)
    _replace_purge_reason(PURGE_REASONS, validated=False)
    _replace_check("jobs", "job_type", JOB_TYPES, validated=False)

    for table in ("stores", "organizations"):
        _restore_table_insert(table)
        _drop_tenant_state(table)
