"""Fermer le risque residuel de D-049: gardes d'instantanes et politique d'audit (Mission 004.4.1).

Revision ID: 0010_qualify_residual_security
Revises: 0009_qualify_security_functions
Create Date: 2026-09-17

PROBLEME (meme classe que 0009, laisse en risque residuel documente par D-049)
PostgreSQL recherche IMPLICITEMENT `pg_temp` EN PREMIER pour un nom de relation non qualifie
tant que `pg_temp` n'est pas nomme dans `search_path`. Un corps de fonction est reinterprete a
l'execution: deux gardes lisaient encore une table par un nom nu, et un role qui possede le
privilege `TEMPORARY` (laisse a PUBLIC hors du bootstrap Docker) pouvait les detourner avec une
table temporaire forgee (reproduit a 0009 sur une base neuve):

- `analysis_runs_require_sealed_snapshot()` (0003): `FROM data_snapshots`. Une table temporaire
  `data_snapshots` qui declare `completed` un instantane en cours d'ingestion ou en echec laisse
  inserer une execution d'analyse sur une entree non scellee.
- `canonical_rows_guard_sealed()` (0002): `JOIN data_snapshots`. Une table temporaire qui declare
  `ingesting` un instantane scelle laisse ajouter des lignes canoniques a un instantane immuable.

Portee: integrite INTRA-locataire (la RLS des tables reelles reste appliquee); aucune lecture
inter-locataire. Irrealisable dans le deploiement Docker (TEMPORARY retire a PUBLIC), realisable
dans tout deploiement Alembic-seul et dans la configuration des tests.

Troisieme element de la liste de D-049, la sous-requete `FROM jobs` de la politique
`audit_events_service_actor` (0007) n'etait PAS detournable: une expression de politique est
stockee deja resolue (OID de relation fixe au `CREATE POLICY`), pas reinterpretee a l'execution
(prouve a 0009: une table temporaire `jobs` ne permet pas de forger `on_behalf_of`). Elle est
reecrite avec `public.jobs` par coherence de source; son arbre stocke, donc son comportement,
est identique avant et apres.

CORRECTIF (identique a la strategie de 0009)
- Qualifier en `public.` chaque relation reelle lue par ces gardes. La qualification est immune a
  `search_path`; aucun `SET search_path` n'est ajoute aux fonctions de declencheur (0009 ne l'a
  pose que sur la fonction SECURITY DEFINER; ici aucune ne l'est).
- `changed_rows` reste NON qualifie: c'est la table de transition du declencheur (relation nommee
  ephemere), resolue AVANT le catalogue, donc avant `pg_temp`; elle ne peut pas etre qualifiee.
  Un test fige cette propriete.
- `CREATE OR REPLACE FUNCTION` conserve proprietaire, privileges et declencheurs; `ALTER POLICY`
  ne change que l'expression (roles, commande et caractere RESTRICTIVE conserves).
- Aucune table, aucun privilege, aucun comportement legitime modifie.

Descente: corps et expression d'origine restaures a l'identique (prosrc de 0002/0003, expression
de 0007).
"""
from alembic import op

revision = "0010_qualify_residual_security"
down_revision = "0009_qualify_security_functions"
branch_labels = None
depends_on = None

# --- definitions QUALIFIEES (correctif) --------------------------------------------
QUALIFIED_FUNCTIONS = [
    """
    CREATE OR REPLACE FUNCTION analysis_runs_require_sealed_snapshot() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM public.data_snapshots s
            WHERE s.id = NEW.snapshot_id AND s.organization_id = NEW.organization_id AND s.status = 'completed'
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'an analysis run requires a completed data snapshot';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    # `changed_rows`: table de transition du declencheur, non qualifiable et resolue avant pg_temp.
    """
    CREATE OR REPLACE FUNCTION canonical_rows_guard_sealed() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM changed_rows c JOIN public.data_snapshots s ON s.id = c.snapshot_id
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
    """,
]

QUALIFIED_AUDIT_CHECK = (
    "actor_type IN ('worker', 'system') AND actor_id = app_current_service_id() "
    "AND (on_behalf_of IS NULL OR on_behalf_of = app_current_user_id() "
    "     OR (resource_type = 'job' AND on_behalf_of = (SELECT j.enqueued_by FROM public.jobs j "
    "         WHERE j.organization_id = audit_events.organization_id AND j.id = audit_events.resource_id)))"
)

# --- definitions d'origine (0003, 0002, 0007), pour la descente ---------------------
ORIGINAL_FUNCTIONS = [
    ("CREATE OR REPLACE FUNCTION analysis_runs_require_sealed_snapshot() RETURNS trigger\n"
     "    LANGUAGE plpgsql AS $body$\n        BEGIN\n            IF NOT EXISTS (\n"
     "                SELECT 1 FROM data_snapshots s\n"
     "                WHERE s.id = NEW.snapshot_id AND s.organization_id = NEW.organization_id"
     " AND s.status = 'completed'\n"
     "            ) THEN\n                RAISE EXCEPTION USING\n"
     "                    ERRCODE = 'restrict_violation',\n"
     "                    MESSAGE = 'an analysis run requires a completed data snapshot';\n"
     "            END IF;\n            RETURN NEW;\n        END\n        $body$\n"),
    ("CREATE OR REPLACE FUNCTION canonical_rows_guard_sealed() RETURNS trigger\n"
     "    LANGUAGE plpgsql AS $body$\n        BEGIN\n            IF EXISTS (\n"
     "                SELECT 1 FROM changed_rows c JOIN data_snapshots s ON s.id = c.snapshot_id\n"
     "                WHERE s.status <> 'ingesting'\n"
     "            ) THEN\n                RAISE EXCEPTION USING\n"
     "                    ERRCODE = 'restrict_violation',\n"
     "                    MESSAGE = 'records of a sealed data snapshot are immutable',\n"
     "                    DETAIL = 'table ' || TG_TABLE_NAME || ', operation ' || TG_OP;\n"
     "            END IF;\n            RETURN NULL;\n        END\n        $body$\n"),
]

ORIGINAL_AUDIT_CHECK = (
    "actor_type IN ('worker', 'system') AND actor_id = app_current_service_id() "
    "AND (on_behalf_of IS NULL OR on_behalf_of = app_current_user_id() "
    "     OR (resource_type = 'job' AND on_behalf_of = (SELECT j.enqueued_by FROM jobs j "
    "         WHERE j.organization_id = audit_events.organization_id AND j.id = audit_events.resource_id)))"
)


def upgrade() -> None:
    for statement in QUALIFIED_FUNCTIONS:
        op.execute(statement)
    op.execute(f"ALTER POLICY audit_events_service_actor ON audit_events WITH CHECK ({QUALIFIED_AUDIT_CHECK})")


def downgrade() -> None:
    op.execute(f"ALTER POLICY audit_events_service_actor ON audit_events WITH CHECK ({ORIGINAL_AUDIT_CHECK})")
    for statement in ORIGINAL_FUNCTIONS:
        op.execute(statement)
