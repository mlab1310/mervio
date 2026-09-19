"""Sonde du dispatcher et prise: plans robustes sous RLS (D-060).

Revision ID: 0012_dispatch_probe_plan
Revises: 0011_identity_pii_schema
Create Date: 2026-09-19

PROBLEMES (preexistants, reproduits sur 3150b91, dfe1975 et 673ca4f, PostgreSQL 17.11)
1. Sous le role worker, les politiques PERMISSIVES de `jobs` sont combinees par OU:
   `organization_id = app_current_organization_id()` OU `jobs_service_dispatch`
   (`app_current_organization_id() IS NULL AND status IN (...)`). Dans ce OU, le test `IS NULL` sur
   une expression sans statistiques vaut 0,005: la sonde READY estimait ~3 travaux prets par
   organisation pour ~3 000 reels et basculait vers un bitmap suivi d'un tri de la file.
2. `jobs_ready_idx` ne contenait pas `available_at`: les travaux DIFFERES (reprises avec delai, qui
   gardent leur `created_at`, donc restent en tete de l'ordre de priorite) etaient sautes un par un
   avec une lecture de table chacun, par la prise comme par la sonde (90 000 differes: ~28 ms par
   prise). Sans statistiques, la prise choisissait par intermittence un bitmap suivi d'un tri
   externe de toute la file (~53 ms a 100 000 travaux, ~0,6 s a un million).

CORRECTIF
- `jobs_service_dispatch`: `(SELECT public.app_current_organization_id() IS NULL)`: le contexte est
  evalue UNE fois (booleen d'InitPlan, 0,5 au lieu de 0,005). Semantique identique: fonction
  STABLE, constante pour l'instruction. Seule l'expression change (`ALTER POLICY`): role
  `mervio_worker`, commande SELECT et caractere PERMISSIVE conserves. La politique RESTRICTIVE
  `jobs_service_authorized`, frontiere d'isolation, n'est pas modifiee.
- `jobs_ready_idx` devient `(organization_id, priority DESC, created_at, id, available_at)`, toujours
  partiel sur `status = 'queued'`. Le PREFIXE est inchange: l'index porte le meme ordre de prise
  (`priority DESC, created_at, id`), sans tri. `available_at`, derniere cle, devient une condition
  d'index verifiee sans lecture de table quand la requete compare a une horloge evaluee une fois
  (`(SELECT COALESCE(..., now()))`, voir `jobs._CLAIM_SQL` et `dispatch._READY_SQL`): `now()` n'est
  pas leakproof et, sous RLS, ne peut pas figurer dans une condition d'index.

Un second index `(organization_id, available_at) WHERE status = 'queued'` a ete ECARTE: sans
statistiques, la prise le choisissait suivi d'un tri de toute la file (~50 ms par prise).

Aucune fonction, aucun role, aucun privilege, aucun SECURITY DEFINER, aucun reglage du
planificateur. L'expression de politique est stockee resolue (OID fixes au `ALTER POLICY`), donc
pas ombrable par `pg_temp`; la fonction est qualifiee `public.` (D-049).

PRODUCTION
La reconstruction ci-dessous (DROP puis CREATE dans la transaction de la revision) verrouille
`jobs` en ecriture le temps de la construction: acceptable tant qu'aucune donnee marchande reelle
n'est stockee. Sur une table volumineuse en service, remplacer l'index hors transaction:
`CREATE INDEX CONCURRENTLY jobs_ready_idx_v2 ...`, puis `DROP INDEX CONCURRENTLY jobs_ready_idx`,
puis `ALTER INDEX jobs_ready_idx_v2 RENAME TO jobs_ready_idx`.

DESCENTE
Expression de politique de 0007 et definition d'index de 0004 restaurees a l'identique.
"""
from alembic import op

revision = "0012_dispatch_probe_plan"
down_revision = "0011_identity_pii_schema"
branch_labels = None
depends_on = None

POLICY = "jobs_service_dispatch"
#: expression de 0007, restauree a la descente
ORIGINAL_USING = "app_current_organization_id() IS NULL AND status IN ('queued', 'running')"
#: contexte evalue une fois par instruction (InitPlan booleen)
PLANNABLE_USING = "(SELECT public.app_current_organization_id() IS NULL) AND status IN ('queued', 'running')"
READY_INDEX = "jobs_ready_idx"
#: definition de 0004, restauree a la descente
ORIGINAL_READY_COLUMNS = "organization_id, priority DESC, created_at, id"
#: meme prefixe (ordre de prise), `available_at` en derniere cle
READY_COLUMNS = "organization_id, priority DESC, created_at, id, available_at"


def _ready_index(columns: str) -> None:
    op.execute(f"DROP INDEX public.{READY_INDEX}")
    op.execute(f"CREATE INDEX {READY_INDEX} ON public.jobs ({columns}) WHERE status = 'queued'")


def upgrade() -> None:
    op.execute(f"ALTER POLICY {POLICY} ON public.jobs USING ({PLANNABLE_USING})")
    _ready_index(READY_COLUMNS)


def downgrade() -> None:
    _ready_index(ORIGINAL_READY_COLUMNS)
    op.execute(f"ALTER POLICY {POLICY} ON public.jobs USING ({ORIGINAL_USING})")
