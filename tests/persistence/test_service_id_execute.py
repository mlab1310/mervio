"""`app_current_service_id()` n'est executable que par le worker (revision 0020).

POURQUOI CE MODULE EXISTE
`0018` a redeclare cette fonction en `SECURITY DEFINER` -- elle lit `idp_subject`, desormais
hors de portee du role applicatif -- et a OUBLIE de lui appliquer le `REVOKE ALL ... FROM
PUBLIC` qu'elle applique aux quatre fonctions qu'elle cree. Le privilege lui-meme preexistait
(`proacl IS NULL` depuis `0007`: PostgreSQL accorde `EXECUTE` a PUBLIC par defaut).

L'ARGUMENT N'EST PAS << ELLE REND NULL >>. Deux sondes montraient qu'un appelant non worker
obtenait `NULL`; ce n'est pas une preuve de surete suffisante. L'argument est le PRIVILEGE
MINIMAL: une fonction `SECURITY DEFINER` qui lit une colonne retiree au role applicatif ne doit
pas etre appelable par un role qui n'en a aucun usage. La garde interne reste, elle ne remplace
rien -- et ce module verifie les deux.
"""
from __future__ import annotations

import psycopg
import pytest

from mervio.persistence import migrate

SIGNATURE = "public.app_current_service_id()"


# -- 1. privileges effectifs ---------------------------------------------------------------

@pytest.mark.parametrize("role, expected", [("public", False), ("mervio_app", False),
                                            ("mervio_worker", True)])
def test_only_the_worker_role_may_execute_the_service_identity_function(owner, role, expected):
    got = owner.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, SIGNATURE)).fetchone()[0]
    assert got is expected


def test_it_is_the_only_definer_function_that_was_ever_open_to_public(owner):
    """Releve, pas suppose: apres `0020`, AUCUN definisseur n'est executable par PUBLIC."""
    open_to_public = [r[0] for r in owner.execute(
        "SELECT p.proname FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
        "AND p.prosecdef AND has_function_privilege('public', p.oid, 'EXECUTE') ORDER BY 1").fetchall()]
    assert open_to_public == []


# -- 2. negatif: les roles sans usage legitime sont REFUSES --------------------------------

def test_the_application_role_is_refused(app_conn):
    """Avant `0020` il recevait `NULL`; il est desormais refuse, ce qui est strictement plus
    fort: l'identite de service n'est plus seulement infalsifiable, elle est inatteignable."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(f"SELECT {SIGNATURE}").fetchone()


@pytest.mark.parametrize("wrapper", ["app_service_authorized('00000000-0000-0000-0000-000000000000'::uuid)",
                                     "app_service_organizations()"])
def test_the_invoker_wrappers_close_with_it_for_the_application_role(app_conn, wrapper):
    """Effet de bord MESURE, et souhaitable: `app_service_authorized` et
    `app_service_organizations` sont `SECURITY INVOKER` et appellent la fonction retiree, donc
    un role applicatif ne les traverse plus non plus."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(f"SELECT public.{wrapper}").fetchone()


# -- 3. positif: le worker garde TOUT ce dont il a besoin ----------------------------------

def test_the_worker_keeps_every_function_of_its_authorization_path(worker_conn, principal):
    """Les 23 politiques RLS qui citent la fonction, directement ou par les deux enveloppes,
    sont scopees `{mervio_worker}`: c'est ce chemin qui doit rester entier."""
    assert worker_conn.execute(f"SELECT {SIGNATURE}").fetchone()[0] == principal.id
    assert worker_conn.execute(
        "SELECT public.app_service_authorized('00000000-0000-0000-0000-000000000000'::uuid)"
    ).fetchone()[0] is False
    assert worker_conn.execute("SELECT public.app_service_organizations()").fetchone()[0] == []


def test_the_worker_still_reads_its_authorized_organization(worker_db, tenant_a, principal):
    """Le chemin COMPLET: autorisation de service -> politiques RLS -> lecture effective.

    C'est la verification qui compte vraiment: les 23 politiques traversent la fonction, et un
    `REVOKE` trop large les aurait toutes cassees sans qu'un appel direct ne le montre.
    """
    from mervio.persistence.service import ServiceSession, authorize_service
    from mervio.persistence.tenancy import Permission

    authorize_service(tenant_a.session, principal.id)
    session = ServiceSession(worker_db, tenant_a.organization_id, principal)
    with session.transaction(Permission.RUN_JOBS) as conn:
        assert conn.execute("SELECT count(*) FROM stores").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM organizations").fetchone()[0] == 1


def test_an_unauthorized_worker_is_refused_as_a_missing_organization(worker_db, tenant_a, principal):
    """La moitie negative: le `GRANT` au role worker n'accorde PAS l'acces aux donnees.

    Il n'accorde que la resolution de son propre principal. Sans autorisation de service,
    l'organisation est refusee -- et le refus est indiscernable d'une organisation inexistante
    (ADR-004-003), ce qui est plus fort que de rendre zero ligne.
    """
    from mervio.persistence.errors import TenantAccessDenied
    from mervio.persistence.service import ServiceSession
    from mervio.persistence.tenancy import Permission

    session = ServiceSession(worker_db, tenant_a.organization_id, principal)
    with pytest.raises(TenantAccessDenied):
        with session.transaction(Permission.RUN_JOBS):
            pass


# -- 4. le role applicatif garde ses lectures normales -------------------------------------

def test_the_application_role_keeps_its_own_path_intact(app_conn):
    """Les politiques qui invoquent la fonction ne s'evaluent pas pour un role non worker, donc
    rien de son chemin ne doit se fermer."""
    for query in ("SELECT count(*) FROM organizations", "SELECT count(*) FROM jobs",
                  "SELECT count(*) FROM public.app_find_user('inconnu')",
                  "SELECT public.app_schema_revision()"):
        app_conn.execute(query).fetchone()


# -- 5. place dans la chaine et aller-retour de migration ----------------------------------

def test_0020_is_the_head_and_follows_0019():
    assert migrate.revisions()[-1] == "0020_service_id_execute"
    assert migrate.revisions()[-2] == "0019_readyz_revision_grant"


def test_the_downgrade_restores_the_effective_privileges_and_the_upgrade_closes_again(pg):
    """`UP -> DOWN -> UP` sur les PRIVILEGES EFFECTIFS, et identite de schema telle que le
    depot la mesure.

    La REPRESENTATION de l'ACL ne peut pas redevenir `NULL` -- PostgreSQL materialise un ACL
    des le premier `GRANT`/`REVOKE`. C'est sans consequence, et c'est verifie ici: l'empreinte
    de `scripts/ci_migrations.py` releve `pg_get_functiondef` pour les fonctions et `relacl`
    pour les RELATIONS, jamais `proacl`.
    """
    name = pg.create_empty_database("m0020")
    try:
        url = pg.url("migrator", name)
        migrate.upgrade(url)

        def privileges():
            with psycopg.connect(url) as conn:
                return tuple(conn.execute(
                    "SELECT has_function_privilege(r, %s, 'EXECUTE') FROM "
                    "unnest(ARRAY['public','mervio_app','mervio_worker']) AS r", (SIGNATURE,)
                ).fetchall())

        at_head = privileges()
        assert at_head == ((False,), (False,), (True,))
        head_fingerprint = _fingerprint(url)

        migrate.downgrade(url, "0019_readyz_revision_grant")
        assert privileges() == ((True,), (True,), (True,)), "la descente rend les privileges effectifs"

        migrate.upgrade(url)
        assert privileges() == at_head
        assert _fingerprint(url) == head_fingerprint, "identite de schema apres aller-retour"
    finally:
        pg.drop_database(name)


def _fingerprint(url):
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "ci_migrations.py"
    spec = importlib.util.spec_from_file_location("mervio_ci_migrations_fp_0020", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.PostgresTarget(url).fingerprint()
