"""`GET /readyz` contre une VRAIE base (Mission 004.6, tranche E).

Ces deux proprietes vivent sous `tests/persistence/` parce qu'elles ont besoin de la fixture
`pg`: une base reelle, montee revision par revision. Le reste du squelette d'API n'a besoin
d'aucune base et vit sous `tests/api/`.
"""
from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from mervio.api.app import create_app
from mervio.persistence import migrate


def test_readyz_is_ready_against_a_database_at_head(pg):
    """Pret quand la base repond ET que son schema est a la tete attendue."""
    app = create_app(database_url=pg.url("app"))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    document = response.json()
    assert document["ready"] is True
    assert document["migration"]["revision"] == migrate.head_revision()


def test_readyz_refuses_a_schema_behind_head_even_when_the_revision_is_readable(pg):
    """La propriete sharp: revision LISIBLE mais differente de la tete => `schema_not_at_head`.

    Servir des lectures contre un schema qui n'est pas celui du code produirait des reponses
    fausses plutot qu'une panne franche. La fonction de `0019` est posee a la main ici, parce
    qu'aucune revision livree n'est a la fois >= 0019 et < tete: c'est la tete.
    """
    name = pg.create_empty_database("api_readyz_behind")
    try:
        migrate.upgrade(pg.url("migrator", name), "0016_tenant_purge")
        with psycopg.connect(pg.url("migrator", name), autocommit=True) as conn:
            conn.execute("CREATE FUNCTION public.app_schema_revision() RETURNS text "
                         "LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public "
                         "AS $$ SELECT version_num FROM public.alembic_version $$")
            conn.execute("GRANT EXECUTE ON FUNCTION public.app_schema_revision() TO mervio_app")
        app = create_app(database_url=pg.url("app", name))
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/readyz")
        assert response.status_code == 503
        document = response.json()
        assert document["reason"] == "schema_not_at_head"
        assert document["migration"] == {"revision": "0016_tenant_purge",
                                         "expected": migrate.head_revision()}
    finally:
        pg.drop_database(name)


def test_readyz_refuses_a_schema_too_old_to_even_report_its_revision(pg):
    """Et le cas reel d'un schema anterieur a `0019`: le privilege de lecture POST-DATE le
    controle, donc la revision est illisible et le motif est `database_unreachable`.

    Dit franchement: le diagnostic est alors moins precis. Ce qui compte est preserve --
    `readyz` ne repond JAMAIS `ready` sur un schema qui n'est pas celui du code.
    """
    name = pg.create_empty_database("api_readyz_old")
    try:
        migrate.upgrade(pg.url("migrator", name), "0016_tenant_purge")
        app = create_app(database_url=pg.url("app", name))
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/readyz")
        assert response.status_code == 503
        document = response.json()
        assert document["ready"] is False
        assert document["reason"] == "database_unreachable"
    finally:
        pg.drop_database(name)
