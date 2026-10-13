"""Un sujet d'identite n'est plus enumerable par le role applicatif (SEC-05 / TD-27, 004.6).

SEC-05 est ouvert depuis la revision `0001`: `SELECT` etait accorde a `mervio_app` sur TOUTES
les colonnes de `users`, donc tout sujet d'identite etait enumerable par du SQL de role
applicatif. Selon le fournisseur, un sujet peut contenir une adresse.

CE QUE LA REVISION `0018` GARANTIT, ET CE QU'ELLE NE GARANTIT PAS
Le mecanisme est le privilege de COLONNE, pas RLS -- l'en-tete de `0018` donne la mesure qui
l'impose. Consequence a enoncer franchement: la LIGNE d'un autre utilisateur n'est pas cachee,
son `id` reste lisible. Un UUID v4 opaque n'identifie personne et sert de cle etrangere a
`memberships`, `audit_events` et `jobs`. Ce qui est ferme, c'est l'acces a la colonne qui
IDENTIFIE: `idp_subject`. La menace de SEC-05 est l'enumeration des sujets, et c'est exactement
cette menace qui est fermee.

Tous les acces legitimes sont des lectures a CLE EXACTE, et ils continuent de fonctionner.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

pytestmark = pytest.mark.usefixtures("clean")


def _make_user(owner, subject: str) -> uuid.UUID:
    """Cree un utilisateur par le proprietaire, hors du chemin applicatif."""
    user_id = uuid.uuid4()
    owner.execute("INSERT INTO users (id, idp_subject) VALUES (%s, %s)", (user_id, subject))
    return user_id


def _context(conn, *, organization_id=None, user_id=None) -> None:
    conn.execute("SELECT set_config('app.organization_id', %s, false), set_config('app.user_id', %s, false)",
                 (str(organization_id) if organization_id else "", str(user_id) if user_id else ""))


def test_a_user_row_is_readable_only_by_its_owner(owner, app_conn):
    """En contexte de l'utilisateur A, le sujet de B est illisible -- et celui de A aussi.

    Le role applicatif n'a plus AUCUN privilege sur `idp_subject`: ni pour un autre, ni pour
    soi. Lire son propre sujet passe par `app_find_user`, a cle exacte.
    """
    alice = _make_user(owner, "idp:alice")
    bob = _make_user(owner, "idp:bob")
    _context(app_conn, user_id=alice)

    for target in (bob, alice):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute("SELECT idp_subject FROM users WHERE id = %s", (target,)).fetchall()


def test_the_application_role_cannot_enumerate_identity_subjects(owner, app_conn):
    """La menace exacte de SEC-05: un balayage de la table ne rend aucun sujet."""
    _make_user(owner, "idp:alice")
    _make_user(owner, "idp:bob")
    _context(app_conn)

    for query in ("SELECT idp_subject FROM users",
                  "SELECT * FROM users",
                  "SELECT count(*) FROM users WHERE idp_subject LIKE 'idp:%'"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute(query).fetchall()


def test_the_opaque_columns_stay_readable(owner, app_conn):
    """`id`, `kind` et `created_at` restent lisibles: les declencheurs de `0007`/`0009` en
    dependent, et aucune des trois n'identifie une personne."""
    alice = _make_user(owner, "idp:alice")
    _context(app_conn)
    row = app_conn.execute("SELECT id, kind FROM users WHERE id = %s", (alice,)).fetchone()
    assert row == (alice, "human")


def test_an_exact_subject_lookup_still_resolves(owner, app_conn):
    """L'acces legitime survit: resolution d'UN sujet connu, sans balayage possible."""
    alice = _make_user(owner, "idp:alice")
    _context(app_conn)

    assert app_conn.execute("SELECT * FROM app_find_user(%s)", ("idp:alice",)).fetchone() \
        == (alice, "idp:alice", "human")
    assert app_conn.execute("SELECT * FROM app_find_user(%s)", ("idp:nobody",)).fetchone() is None


def test_provisioning_is_idempotent_and_refuses_a_reserved_subject(app_conn):
    """`app_ensure_user` provisionne sans contexte tenant -- le cas que RLS ne pouvait pas servir."""
    _context(app_conn)
    first = app_conn.execute("SELECT app_ensure_user(%s)", ("idp:new",)).fetchone()[0]
    again = app_conn.execute("SELECT app_ensure_user(%s)", ("idp:new",)).fetchone()[0]
    assert first == again

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("SELECT app_ensure_user(%s)", ("service:someone",)).fetchone()


def test_the_application_role_can_no_longer_insert_a_user_directly(app_conn):
    """Le provisionnement passe par la fonction: l'insertion directe est retiree."""
    _context(app_conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("INSERT INTO users (id, idp_subject) VALUES (%s, %s)",
                         (uuid.uuid4(), "idp:smuggled"))
