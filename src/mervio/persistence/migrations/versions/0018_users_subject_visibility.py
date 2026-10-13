"""Un sujet d'identite n'est plus enumerable par le role applicatif (Mission 004.6, SEC-05/TD-27).

Revision ID: 0018_users_subject_visibility
Revises: 0017_purge_recovery_audit
Create Date: 2026-10-13

CE QUE FAIT CETTE REVISION
`users` etait la seule table de tenant sans restriction de lecture: `SELECT` etait accorde a
`mervio_app` sur TOUTES ses colonnes depuis `0001`, donc tout sujet d'identite etait enumerable
par du SQL de role applicatif. Selon le fournisseur d'identite, un sujet peut contenir une
adresse. Cette revision retire la colonne `idp_subject` de la portee du role applicatif et
rend les acces legitimes par des fonctions a CLE EXACTE.

    REVOKE SELECT ON users           le role applicatif perd la lecture de table
    GRANT  SELECT (id, kind, ...)    il garde ce qui n'identifie personne
    4 fonctions SECURITY DEFINER     les acces legitimes, tous a cle exacte
    1 fonction redeclaree            app_current_service_id, voir plus bas

    AUCUNE table    AUCUNE colonne    AUCUN declencheur    AUCUN role
    AUCUNE politique RLS              AUCUNE contrainte

POURQUOI PAS UNE POLITIQUE RLS -- MESURE, PAS SUPPOSE
TD-27 proposait << restrict via view or policy on own row >>. La politique est IMPOSSIBLE, pour
trois raisons independantes, chacune verifiee:

1. `tenancy.ensure_user` ouvre `database.transaction()` SANS argument: le provisionnement
   precede l'identite, par construction. Le GUC est vide, `app_current_user_id()` vaut NULL,
   et toute politique qui s'y adosse refuse. Mesure sur base jetable, politique
   `USING (id = app_current_user_id())` appliquee: `ensure_user` leve
   `InsufficientPrivilege: new row violates row-level security policy for table "users"`,
   pour un sujet nouveau comme pour un sujet existant.
2. Une trentaine de lectures de `users` vivent dans des declencheurs
   (`audit_events_guard_actors`, `jobs_enqueued_by_human`, `service_authorizations_guard`,
   `memberships_forbid_service_users`) qui s'executent comme l'APPELANT, donc soumis a RLS.
3. RLS ne distingue pas une lecture a cle d'un balayage. La menace de SEC-05 est
   l'ENUMERATION, et tous les acces legitimes sont des lectures a cle exacte. RLS est le
   mauvais instrument pour cette menace; le privilege de COLONNE est le bon.

C'est pourquoi SEC-05 est reste ouvert depuis `0001`: ce n'est pas une politique d'une ligne.

CE QUI SURVIT SANS CHANGEMENT, ET POURQUOI
  declencheurs de `0007`/`0009`   ils lisent `id` et `kind`, tous deux encore accordes
  `app_delegate_rank()`           joint `users` pour `u.kind` seulement
  `app_purge_guard()`             SECURITY INVOKER deliberement, mais appele depuis un
                                  SECURITY DEFINER: son corps lit avec les privileges du
                                  DEFINISSEUR, hors de portee de ce REVOKE
  fonctions de `0014`/`0015`/`0016` qui lisent `idp_subject`: toutes SECURITY DEFINER

LA SEULE FONCTION QUI CASSAIT, ET LE SOIN QU'ELLE DEMANDE
`app_current_service_id()` (`0009`) est `LANGUAGE sql STABLE`, NON SECURITY DEFINER, et lit
`u.idp_subject`. Elle alimente `app_service_authorized()`, donc les politiques RLS du worker:
la laisser casser aurait coupe toute l'autorisation du worker. Elle est redeclaree SECURITY
DEFINER. Son corps passe de `current_user` a `session_user` -- OBLIGATOIRE: dans un
SECURITY DEFINER, `current_user` vaut le PROPRIETAIRE, et la fonction aurait cherche le
principal du proprietaire. `app_ensure_service_principal()` (`0009`) prend deja cette
precaution, et le depot ne fait aucun `SET ROLE`, donc `session_user` et `current_user`
coincidaient avant ce changement: il ne change aucun comportement, il le preserve.
Le garde-fou `pg_has_role(session_user, 'mervio_worker')` est CONSERVE, et la fonction ne rend
toujours qu'UNE ligne, celle determinee par le role connecte: SECURITY DEFINER n'elargit rien.

PORTEE DES QUATRE NOUVELLES FONCTIONS -- aucune n'accepte un balayage
  app_ensure_user(text)        provisionne ou rend l'identifiant d'UN sujet exact. Refuse un
                               sujet reserve `service:` (contrainte `users_service_subject_reserved`).
  app_find_user(text)          rend (id, sujet, nature) d'UN sujet exact. Rendre le sujet que
                               l'appelant vient de fournir ne lui apprend rien.
  app_user_identities(uuid[])  rend les identites d'identifiants DEJA detenus, et seulement
                               s'ils sont: un principal de service, l'utilisateur courant, ou
                               un co-membre d'une de ses organisations. Les UUID v4 ne sont pas
                               enumerables, et ce filtre ferme le reste.
  app_organization_members()   la liste des membres de l'organisation DU CONTEXTE. Elle verifie
                               elle-meme l'appartenance ET le rang `owner` de l'appelant: un
                               SECURITY DEFINER contourne RLS, donc il ne peut pas se reposer
                               sur le controle Python de `TenantSession`, qui pose le GUC AVANT
                               de verifier l'appartenance.

`REVOKE ALL ... FROM PUBLIC` precede chaque `GRANT EXECUTE`, convention de `0009`/`0010`:
`EXECUTE` est accorde a PUBLIC par defaut sur toute fonction nouvelle.

DESCENTE
Exactement reciproque: les quatre fonctions sont supprimees, `app_current_service_id()` est
rendue a son corps de `0009` (SECURITY INVOKER, `current_user`), et les privileges de `users`
sont rendus a leur etat de `0007` -- `SELECT` de table, `INSERT (id, idp_subject)`.
`UP -> DOWN -> UP` rend un schema identique: aucune donnee n'est touchee, aucune contrainte
n'est amendee.
"""
from alembic import op

revision = "0018_users_subject_visibility"
down_revision = "0017_purge_recovery_audit"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"
WORKER_ROLE = "mervio_worker"

#: colonnes de `users` qui n'identifient personne: un UUID opaque, une nature, une date.
VISIBLE_COLUMNS = "id, kind, created_at"

#: corps de `app_current_service_id()` tel que `0009` le laisse: SECURITY INVOKER, `current_user`.
SERVICE_ID_0009 = f"""
    CREATE OR REPLACE FUNCTION public.app_current_service_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $body$
        SELECT u.id FROM public.users u
        WHERE u.kind = 'service' AND u.idp_subject = 'service:' || current_user
          AND pg_catalog.pg_has_role(current_user, '{WORKER_ROLE}', 'USAGE')
    $body$
"""

#: meme fonction, SECURITY DEFINER et `session_user`. Voir l'en-tete pour l'obligation.
SERVICE_ID_0018 = f"""
    CREATE OR REPLACE FUNCTION public.app_current_service_id() RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
    AS $body$
        SELECT u.id FROM public.users u
        WHERE u.kind = 'service' AND u.idp_subject = 'service:' || session_user
          AND pg_catalog.pg_has_role(session_user, '{WORKER_ROLE}', 'USAGE')
    $body$
"""

ENSURE_USER = """
    CREATE FUNCTION public.app_ensure_user(p_subject text) RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public
    AS $body$
    DECLARE
        v_id uuid;
    BEGIN
        IF p_subject IS NULL OR pg_catalog.char_length(p_subject) NOT BETWEEN 1 AND 255 THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                MESSAGE = 'invalid identity subject';
        END IF;
        IF pg_catalog.starts_with(p_subject, 'service:') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'insufficient_privilege',
                MESSAGE = 'a service subject is reserved to app_ensure_service_principal';
        END IF;
        INSERT INTO public.users (id, idp_subject, kind)
            VALUES (pg_catalog.gen_random_uuid(), p_subject, 'human')
            ON CONFLICT (idp_subject) DO NOTHING;
        SELECT u.id INTO v_id FROM public.users u WHERE u.idp_subject = p_subject;
        RETURN v_id;
    END
    $body$
"""

FIND_USER = """
    CREATE FUNCTION public.app_find_user(p_subject text)
    RETURNS TABLE (id uuid, idp_subject text, kind text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
    AS $body$
        SELECT u.id, u.idp_subject, u.kind FROM public.users u
        WHERE p_subject IS NOT NULL
          AND pg_catalog.char_length(p_subject) BETWEEN 1 AND 255
          AND u.idp_subject = p_subject
    $body$
"""

USER_IDENTITIES = """
    CREATE FUNCTION public.app_user_identities(p_ids uuid[])
    RETURNS TABLE (id uuid, idp_subject text, kind text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
    AS $body$
        SELECT u.id, u.idp_subject, u.kind FROM public.users u
        WHERE u.id = ANY (p_ids)
          AND (u.kind = 'service'
               OR u.id = public.app_current_user_id()
               OR EXISTS (SELECT 1 FROM public.memberships mine
                          JOIN public.memberships theirs
                            ON theirs.organization_id = mine.organization_id
                          WHERE mine.user_id = public.app_current_user_id()
                            AND theirs.user_id = u.id))
    $body$
"""

ORGANIZATION_MEMBERS = """
    CREATE FUNCTION public.app_organization_members()
    RETURNS TABLE (membership_id uuid, user_id uuid, idp_subject text, role text,
                   created_at timestamptz)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
    AS $body$
        SELECT m.id, m.user_id, u.idp_subject, m.role, m.created_at
        FROM public.memberships m JOIN public.users u ON u.id = m.user_id
        WHERE m.organization_id = public.app_current_organization_id()
          AND EXISTS (SELECT 1 FROM public.memberships me
                      WHERE me.organization_id = public.app_current_organization_id()
                        AND me.user_id = public.app_current_user_id()
                        AND me.role = 'owner')
        ORDER BY m.created_at, m.id
    $body$
"""

NEW_FUNCTIONS = (
    ("app_ensure_user(text)", ENSURE_USER),
    ("app_find_user(text)", FIND_USER),
    ("app_user_identities(uuid[])", USER_IDENTITIES),
    ("app_organization_members()", ORGANIZATION_MEMBERS),
)


def upgrade() -> None:
    # 1. les fonctions d'acces a cle exacte
    for signature, body in NEW_FUNCTIONS:
        op.execute(body)
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {APP_ROLE}")

    # 2. la seule fonction existante qui lisait `idp_subject` en SECURITY INVOKER
    op.execute(SERVICE_ID_0018)

    # 3. le role applicatif perd `idp_subject`, en lecture comme en ecriture
    op.execute(f"REVOKE SELECT ON public.users FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT ({VISIBLE_COLUMNS}) ON public.users TO {APP_ROLE}")
    op.execute(f"REVOKE INSERT ON public.users FROM {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"GRANT INSERT (id, idp_subject) ON public.users TO {APP_ROLE}")
    op.execute(f"REVOKE SELECT ({VISIBLE_COLUMNS}) ON public.users FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT ON public.users TO {APP_ROLE}")

    op.execute(SERVICE_ID_0009)

    for signature, _ in reversed(NEW_FUNCTIONS):
        op.execute(f"DROP FUNCTION public.{signature}")
