"""Ombrage par pg_temp des fonctions de securite (Mission 004.3.10, revision 0009).

Contexte PostgreSQL: quand `pg_temp` n'est PAS nomme dans `search_path`, il est recherche
IMPLICITEMENT EN PREMIER pour les noms de relation. Une fonction de securite qui lit une
table par un nom non qualifie (`users`, `service_authorizations`, `memberships`) lit donc la
table temporaire d'un attaquant si celui-ci peut en creer une (privilege `TEMPORARY`, laisse
par defaut a PUBLIC hors du bootstrap Docker qui le retire).

La revision 0009 qualifie ces references en `public.`, ce qui est immune a `search_path`. Ces
tests prouvent le COMPORTEMENT en base, sous le vrai role worker, avec une table temporaire
reellement forgee. Chaque test montre AUSSI que la meme reference NON qualifiee resterait
ombree: c'est la preuve du vecteur d'attaque, fige en regression.
"""
from __future__ import annotations

import uuid

import pytest

from .persistence_support import make_tenant


def _shadowing_is_possible(conn) -> bool:
    """Le role worker peut-il creer une table temporaire? (Vrai en config de test; faux sous Docker.)"""
    return conn.execute(
        "SELECT has_database_privilege(current_database(), 'TEMPORARY')").fetchone()[0]


def test_pg_temp_can_shadow_an_unqualified_name_but_not_a_qualified_one(worker_conn, principal):
    """Le mecanisme meme: pg_temp l'emporte sur un nom nu, jamais sur `public.`."""
    if not _shadowing_is_possible(worker_conn):
        pytest.skip("le role worker n'a pas TEMPORARY (config type Docker): ombrage impossible")
    worker_conn.execute("CREATE TEMP TABLE users (id uuid, idp_subject text, kind text)")
    worker_conn.execute("INSERT INTO pg_temp.users VALUES (%s, 'forged', 'service')", (uuid.uuid4(),))
    # nom NON qualifie: PostgreSQL lit la table temporaire (le vecteur)
    assert worker_conn.execute("SELECT idp_subject FROM users").fetchall() == [("forged",)]
    # nom qualifie (ce que fait 0009): PostgreSQL lit la vraie table
    real = worker_conn.execute("SELECT idp_subject FROM public.users WHERE id = %s", (principal.id,)).fetchone()
    assert real == (principal.subject,)


def test_forged_temp_authorization_does_not_grant_cross_tenant_read(worker_db, worker_conn, principal, db):
    """Vecteur inter-locataire principal: une `service_authorizations` temporaire forgee ne doit
    donner AUCUN acces a une organisation qui n'a pas autorise le service."""
    if not _shadowing_is_possible(worker_conn):
        pytest.skip("le role worker n'a pas TEMPORARY (config type Docker): ombrage impossible")
    victim = make_tenant(db, "victim")  # n'autorise JAMAIS le worker

    worker_conn.execute(
        "CREATE TEMP TABLE service_authorizations "
        "(organization_id uuid, service_user_id uuid, revoked_at timestamptz)")
    worker_conn.execute("INSERT INTO pg_temp.service_authorizations VALUES (%s, %s, NULL)",
                        (victim.organization_id, principal.id))
    worker_conn.execute("SELECT set_config('app.organization_id', %s, false)", (str(victim.organization_id),))

    # 0009 qualifie service_authorizations: la porte d'autorisation ignore la table temporaire.
    assert worker_conn.execute("SELECT app_service_authorized(%s)", (victim.organization_id,)).fetchone()[0] is False
    assert worker_conn.execute("SELECT app_service_organizations()").fetchone()[0] == []
    # et donc les politiques RESTRICTIVES ferment toujours les tables du locataire victime.
    assert worker_conn.execute("SELECT count(*) FROM stores").fetchone()[0] == 0
    assert worker_conn.execute("SELECT count(*) FROM data_snapshots").fetchone()[0] == 0


def test_forged_temp_users_cannot_make_the_definer_return_another_principal(worker_conn, principal, principal2):
    """app_ensure_service_principal (SECURITY DEFINER) ne doit pas resoudre `users` vers pg_temp."""
    if not _shadowing_is_possible(worker_conn):
        pytest.skip("le role worker n'a pas TEMPORARY (config type Docker): ombrage impossible")
    worker_conn.execute("CREATE TEMP TABLE users (id uuid, idp_subject text UNIQUE, kind text)")
    worker_conn.execute("GRANT ALL ON pg_temp.users TO PUBLIC")  # que le definisseur puisse la voir s'il l'utilisait
    worker_conn.execute("INSERT INTO pg_temp.users VALUES (%s, %s, 'service')",
                        (principal2.id, principal.subject))
    # la vraie identite du worker connecte, jamais celle forgee dans pg_temp
    assert worker_conn.execute("SELECT app_ensure_service_principal()").fetchone()[0] == principal.id
    assert worker_conn.execute("SELECT app_current_service_id()").fetchone()[0] == principal.id


def test_forged_temp_users_cannot_bypass_the_membership_guard(app_conn, worker_db, principal, tenant_a):
    """La garde `un service n'est jamais membre` lit `public.users`, pas une table temporaire."""
    principal  # force la creation du principal de service via worker_db
    if not app_conn.execute("SELECT has_database_privilege(current_database(), 'TEMPORARY')").fetchone()[0]:
        pytest.skip("le role app n'a pas TEMPORARY (config type Docker): ombrage impossible")
    # une table temporaire qui pretend que le principal de service est un humain
    app_conn.execute("CREATE TEMP TABLE users (id uuid, idp_subject text, kind text)")
    app_conn.execute("INSERT INTO pg_temp.users VALUES (%s, 'looks-human', 'human')", (principal.id,))
    app_conn.execute("SELECT set_config('app.organization_id', %s, false), set_config('app.user_id', %s, false)",
                    (str(tenant_a.organization_id), str(tenant_a.owner_id)))
    import psycopg
    with pytest.raises(psycopg.errors.RestrictViolation):
        app_conn.execute(
            "INSERT INTO memberships (id, organization_id, user_id, role) VALUES (%s, %s, %s, 'viewer')",
            (uuid.uuid4(), tenant_a.organization_id, principal.id))
