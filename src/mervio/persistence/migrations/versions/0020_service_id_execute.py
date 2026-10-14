"""`app_current_service_id()` n'est plus executable par PUBLIC (Mission 004.6, correctif).

Revision ID: 0020_service_id_execute
Revises: 0019_readyz_revision_grant
Create Date: 2026-10-13

CE QUE FAIT CETTE REVISION
Une seule chose: retirer `EXECUTE` a PUBLIC sur `app_current_service_id()`, et l'accorder au
seul role qui en a un usage legitime.

    AUCUNE table    AUCUNE colonne    AUCUNE fonction creee ou modifiee
    AUCUNE politique                  AUCUN role        AUCUNE contrainte

POURQUOI -- ET POURQUOI CE N'EST PAS << DEJA SUR >>
`0018` a redeclare cette fonction en `SECURITY DEFINER` (elle lisait `idp_subject`, desormais
hors de portee du role applicatif, et elle alimente les politiques RLS du worker). Elle a
applique `REVOKE ALL ... FROM PUBLIC` aux QUATRE fonctions qu'elle creait, et PAS a celle
qu'elle convertissait: un oubli, et un oubli de la convention que son propre en-tete enonce.

Le privilege lui-meme PREEXISTE a `0018`: `proacl IS NULL`, donc la fonction n'a jamais eu
d'ACL explicite depuis `0007` et PostgreSQL accorde `EXECUTE` a PUBLIC par defaut sur toute
fonction nouvelle. Ce que `0018` a change est le STATUT de la fonction, pas son ACL. Mesure sur
deux bases -- montee depuis zero et montee depuis `0017` puis a la tete -- resultat identique:
sur 17 definisseurs, celle-ci est la SEULE executable par PUBLIC.

Deux sondes ont montre qu'un appelant non worker obtient `NULL`. Ce n'est PAS une preuve de
surete suffisante, et ce n'est pas l'argument retenu. L'argument est le PRIVILEGE MINIMAL: une
fonction `SECURITY DEFINER` qui lit une colonne retiree au role applicatif ne doit pas etre
appelable par un role qui n'en a aucun usage. La garde interne reste, elle ne remplace rien.

CE QUI RESTE GARDE A L'INTERIEUR, ET POURQUOI C'EST SOLIDE
Le corps filtre sur `session_user`, PAS sur `current_user`. Les deux moitiees de cette
distinction sont MESUREES, et elles vont dans le meme sens -- `SET ROLE` ne peut que PERDRE
l'acces, jamais deplacer l'identite:

    SET ROLE mervio_app     le controle de PRIVILEGE suit `current_user`, donc l'appel est
                            REFUSE: un worker qui bascule vers un role sans le privilege
                            perd la fonction.
    SET ROLE mervio_worker  le privilege est conserve, et le principal rendu est INCHANGE:
                            le corps resout `session_user`, qui reste le role CONNECTE.

`pg_has_role(session_user, 'mervio_worker', 'USAGE')` reste la deuxieme condition, et la
fonction ne rend jamais qu'UNE ligne: celle du principal du role connecte. Le depot ne fait
par ailleurs aucun `SET ROLE` -- c'est un invariant d'architecture; ces mesures montrent ce
qu'il se passerait si quelqu'un en introduisait un.

QUI EN A UN USAGE LEGITIME -- RELEVE, NON SUPPOSE
    23 politiques RLS la citent, directement ou via `app_service_authorized` et
    `app_service_organizations`: TOUTES scopees `{mervio_worker}`.
    2 appels Python (`persistence/service.py`): tous deux dans le chemin de service.
Un role purement applicatif n'en a donc AUCUN usage: les politiques qui l'invoquent ne
s'evaluent pas pour lui. `EXECUTE` est accorde a `mervio_worker` seul; les roles de worker
reels en heritent, etant membres de `mervio_app` ET `mervio_worker`.

CONSEQUENCE ASSUMEE, ET ELLE RENFORCE UNE GARANTIE
Un role applicatif qui appelait cette fonction recevait `NULL`; il est desormais REFUSE.
`test_the_service_identity_cannot_be_forged_by_a_setting` le verifiait par `NULL`: il le
verifie maintenant par le refus, ce qui est strictement plus fort -- l'identite de service
n'est plus seulement infalsifiable, elle est inatteignable.

DESCENTE
`EXECUTE` est rendu a PUBLIC, donc les privileges EFFECTIFS reviennent a l'etat de `0019`.
La REPRESENTATION de l'ACL, elle, ne peut pas redevenir `NULL`: PostgreSQL materialise un ACL
des le premier `GRANT`/`REVOKE` et ne revient jamais a l'implicite. C'est sans consequence ici,
et c'est verifie: l'empreinte de schema du depot (`scripts/ci_migrations.py`) releve
`pg_get_functiondef` pour les fonctions et `relacl` pour les RELATIONS -- jamais `proacl`.
`UP -> DOWN -> UP` reste donc identique telle que le depot la mesure, et les privileges
effectifs sont exactement restaures. Dit explicitement plutot que laisse a decouvrir.
"""
from alembic import op

revision = "0020_service_id_execute"
down_revision = "0019_readyz_revision_grant"
branch_labels = None
depends_on = None

WORKER_ROLE = "mervio_worker"
SIGNATURE = "app_current_service_id()"


def upgrade() -> None:
    op.execute(f"REVOKE ALL ON FUNCTION public.{SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.{SIGNATURE} TO {WORKER_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON FUNCTION public.{SIGNATURE} FROM {WORKER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.{SIGNATURE} TO PUBLIC")
