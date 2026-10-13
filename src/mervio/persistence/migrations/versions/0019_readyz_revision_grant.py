"""Le role applicatif peut lire LA REVISION, pas la table qui la porte (Mission 004.6).

Revision ID: 0019_readyz_revision_grant
Revises: 0018_users_subject_visibility
Create Date: 2026-10-13

CE QUE FAIT CETTE REVISION
Une seule chose: une fonction `app_schema_revision()`, `SECURITY DEFINER`, qui rend la revision
courante et rien d'autre.

    AUCUNE table    AUCUNE colonne    AUCUN declencheur    AUCUN role
    AUCUNE politique                  AUCUNE contrainte    AUCUN privilege de TABLE

POURQUOI ELLE EST OBLIGATOIRE
La roadmap 004.6 definit `GET /readyz` comme << base joignable, revision de migration = tete >>.
Mesure: le role applicatif n'a aucun privilege sur `alembic_version`, donc la lecture de la
revision levait `InsufficientPrivilege` et `readyz` ne pouvait rapporter que
`database_unreachable` -- un diagnostic FAUX, qui aurait envoye un operateur chercher un
probleme de reseau devant un probleme de privilege.

POURQUOI UNE FONCTION ET NON UN `GRANT SELECT` -- MESURE, PAS PREFERENCE
Un `GRANT SELECT ON alembic_version` fonctionne, mais sa DESCENTE n'est pas symetrique: en
PostgreSQL, un `GRANT` puis `REVOKE` laisse un ACL EXPLICITE (`{proprietaire=arwdDxtm/...}`) la
ou une base fraiche porte `NULL`. Le catalogue ne revient jamais a `NULL`. Mesure sur deux bases
jetables -- une montee fraiche a `0007`, une montee a la tete puis redescendue a `0007` --
l'empreinte differait sur cette seule ligne, et `test_0008_downgrades_to_the_0007_schema...`
le refusait a juste titre: l'identite de schema apres aller-retour est une garantie du depot.
Une fonction se cree et se supprime proprement, donc l'aller-retour est exact.

Accessoirement, c'est le mecanisme que `0018` vient d'etablir pour `users`, et pour la meme
raison: le role applicatif obtient LA VALEUR dont il a besoin, jamais la portee qui la contient.

CE QUE LA FONCTION N'ACCORDE PAS
Ni ecriture, ni lecture de la table. Seul le role de migration ecrit `alembic_version`, et il la
possede. Un role applicatif qui pourrait la reecrire mentirait sur l'etat du schema a tout le
monde, `readyz` compris. La fonction est `STABLE`, sans argument, et ne rend qu'une chaine: il
n'y a rien a en tirer d'autre.

DESCENTE
La fonction est supprimee. `UP -> DOWN -> UP` rend un schema identique, y compris les ACL.
"""
from alembic import op

revision = "0019_readyz_revision_grant"
down_revision = "0018_users_subject_visibility"
branch_labels = None
depends_on = None

APP_ROLE = "mervio_app"
SIGNATURE = "app_schema_revision()"

FUNCTION = """
    CREATE FUNCTION public.app_schema_revision() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
    AS $body$ SELECT version_num FROM public.alembic_version $body$
"""


def upgrade() -> None:
    op.execute(FUNCTION)
    op.execute(f"REVOKE ALL ON FUNCTION public.{SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.{SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION public.{SIGNATURE}")
