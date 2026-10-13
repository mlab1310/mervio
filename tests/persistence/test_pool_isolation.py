"""Invariant de securite du pool de connexions de l'API (Mission 004.6, D-099).

INVARIANT GARANTI
    Aucune connexion rendue au pool ne peut porter un contexte tenant, et aucune requete
    sans contexte ne peut lire une ligne.

Il est garanti, et non espere, parce que DEUX mecanismes independants le produisent:

1. `set_config('app.organization_id', ..., true)` -- le troisieme argument est `is_local`:
   le GUC meurt au `COMMIT`/`ROLLBACK`. Un `checkin` de pool ne peut pas le transporter,
   puisque la transaction est deja fermee quand la connexion revient.
2. `app_current_organization_id()` vaut `NULLIF(current_setting(..., true), '')::uuid`, donc
   NULL en l'absence de contexte. Les politiques comparent `colonne = NULL`, qui n'est jamais
   TRUE: ZERO ligne. Le mode de defaillance est REFUS, jamais MAUVAIS TENANT.

Le seul vecteur residuel est un GUC positionne a une MAUVAISE valeur DANS une transaction --
un defaut de `TenantSession`, que sa verification d'appartenance en base couvre deja.

Les cinq proprietes de ce module sont BLOQUANTES: aucune route ne se livre sans elles.
"""
from __future__ import annotations

import threading

import psycopg
import pytest

from mervio.persistence.errors import UnsafeDatabaseConfiguration
from mervio.persistence.pool import DatabasePool

from .persistence_support import make_tenant


@pytest.fixture
def pool(clean):
    with DatabasePool(clean.url("app"), min_size=1, max_size=4, application_name="mervio-api-test") as p:
        yield p


def _setting(conn, name: str):
    return conn.execute("SELECT current_setting(%s, true)", (name,)).fetchone()[0]


# -- 1. le contexte ne survit pas a la remise au pool -------------------------------------------

def test_the_tenant_context_is_gone_after_a_connection_is_reused(pool, db):
    """Mecanisme (1): `is_local = true`. Le GUC meurt avec la transaction."""
    tenant = make_tenant(db, "pool-a")
    with pool.transaction(organization_id=tenant.organization_id, user_id=tenant.owner_id) as conn:
        assert _setting(conn, "app.organization_id") == str(tenant.organization_id)
        used = id(conn)

    # min_size=1: la meme connexion physique revient
    with pool.transaction() as conn:
        assert id(conn) == used, "le test veut prouver la REUTILISATION de la connexion"
        assert _setting(conn, "app.organization_id") in ("", None)
        assert _setting(conn, "app.user_id") in ("", None)


# -- 2. sans contexte, rien ne sort -----------------------------------------------------------

def test_a_query_without_a_tenant_context_reads_no_row(pool, db):
    """Mecanisme (2): defaillance FERMEE. Absence de contexte => zero ligne, jamais une autre."""
    tenant = make_tenant(db, "pool-b")
    with pool.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM stores").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM organizations").fetchone()[0] == 0
    # et en contexte, la meme requete voit sa boutique: le refus vient du contexte, pas d'un vide
    with pool.transaction(organization_id=tenant.organization_id, user_id=tenant.owner_id) as conn:
        assert conn.execute("SELECT count(*) FROM stores").fetchone()[0] == 1


# -- 3. concurrence: deux locataires entrelaces ne se croisent jamais ---------------------------

def test_concurrent_tenants_never_cross(pool, db):
    """La propriete sous charge: chaque fil ne voit QUE ses lignes, sur un pool partage."""
    a = make_tenant(db, "pool-concurrent-a")
    b = make_tenant(db, "pool-concurrent-b")
    seen: dict = {}
    errors: list = []
    barrier = threading.Barrier(2)

    def read(label, tenant):
        try:
            rows = set()
            for _ in range(12):
                with pool.transaction(organization_id=tenant.organization_id,
                                      user_id=tenant.owner_id) as conn:
                    barrier.wait(timeout=10)
                    rows |= {r[0] for r in conn.execute("SELECT id FROM stores").fetchall()}
            seen[label] = rows
        except BaseException as exc:  # noqa: BLE001 - remonte dans le fil principal
            errors.append(exc)
            try:
                barrier.abort()
            except BaseException:  # noqa: BLE001
                pass

    threads = [threading.Thread(target=read, args=("a", a)), threading.Thread(target=read, args=("b", b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert seen["a"] == {a.store_id}
    assert seen["b"] == {b.store_id}


# -- 4. un role qui contournerait RLS est refuse a la connexion --------------------------------

def test_the_pool_refuses_a_superuser_or_bypassrls_role(clean):
    """`configure` execute la verification de `Database`, une fois par connexion PHYSIQUE."""
    with pytest.raises(UnsafeDatabaseConfiguration):
        with DatabasePool(clean.url("bypass"), min_size=1, max_size=1) as pool:
            with pool.transaction():
                pass


# -- 5. le fuseau UTC survit a la reutilisation -------------------------------------------------

def test_utc_survives_pool_reuse(pool):
    """`SET TIME ZONE 'UTC'` est de portee SESSION. Un `DISCARD ALL` a la remise l'effacerait,
    et les dates civiles du rapport changeraient d'octets -- les sept goldens et
    `test_determinism_f01` tomberaient sans que la cause soit lisible dans l'echec.
    D'ou l'interdiction explicite de `DISCARD ALL` dans `pool.py`."""
    for _ in range(3):
        with pool.transaction() as conn:
            assert conn.execute("SHOW TIME ZONE").fetchone()[0] == "UTC"


# -- garde de forme: le pool n'est pas une porte derobee ----------------------------------------

def test_the_pool_exposes_nothing_but_the_transaction_seam(pool):
    """`TenantSession` n'utilise que `transaction(organization_id=, user_id=)`. Le pool ne doit
    pas offrir de chemin qui contourne la pose du contexte."""
    public = {name for name in dir(pool) if not name.startswith("_")}
    assert public == {"transaction", "open", "close", "statistics"}, sorted(public)


def test_a_tenant_session_works_on_the_pool(pool, db):
    """Le seam tient: `TenantSession` fonctionne sur le pool sans modification."""
    from mervio.persistence.tenancy import Permission, TenantContext, TenantSession
    tenant = make_tenant(db, "pool-session")
    session = TenantSession(pool, TenantContext(tenant.organization_id, tenant.owner_id))
    with session.transaction(Permission.READ) as conn:
        assert conn.execute("SELECT count(*) FROM stores").fetchone()[0] == 1
