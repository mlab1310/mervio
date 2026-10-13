"""Execution des migrations Alembic sans fichier alembic.ini.

    python -m mervio.persistence upgrade            # jusqu'a head
    python -m mervio.persistence downgrade -1
    python -m mervio.persistence current

URL: MERVIO_MIGRATION_DATABASE_URL (role proprietaire du schema, distinct du role
applicatif). Aucune valeur par defaut.
"""
from __future__ import annotations

import io
import os

import psycopg
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_DATABASE_URL_ENV = "MERVIO_MIGRATION_DATABASE_URL"


def _sqlalchemy_url(url: str) -> str:
    return "postgresql+psycopg://" + url[len("postgresql://"):] if url.startswith("postgresql://") else url


def alembic_config(url: Optional[str] = None, *, stdout=None) -> Config:
    resolved = url or os.environ.get(MIGRATION_DATABASE_URL_ENV)
    if not resolved:
        raise RuntimeError(f"{MIGRATION_DATABASE_URL_ENV} non definie: aucune base de migration par defaut")
    config = Config(stdout=stdout or io.StringIO())
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # via attributes et non set_main_option: un mot de passe encode (%) casserait l'interpolation ini
    config.attributes["database_url"] = resolved
    return config


def upgrade(url: Optional[str] = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: Optional[str] = None, revision: str = "-1") -> None:
    command.downgrade(alembic_config(url), revision)


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config("postgresql://unused")).get_current_head()


def revisions() -> list:
    """Revisions de la plus ancienne a la plus recente."""
    script = ScriptDirectory.from_config(alembic_config("postgresql://unused"))
    return [r.revision for r in reversed(list(script.walk_revisions()))]


def current_revision(url: Optional[str] = None) -> Optional[str]:
    resolved = url or os.environ.get(MIGRATION_DATABASE_URL_ENV)
    if not resolved:
        raise RuntimeError(f"{MIGRATION_DATABASE_URL_ENV} non definie")
    engine = create_engine(_sqlalchemy_url(resolved), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def applied_revision(url: str, *, connect_timeout: int = 5) -> str:
    """Revision courante, lue par un role APPLICATIF (Mission 004.6, revision 0019).

    `current_revision` ci-dessus lit `alembic_version` par SQLAlchemy, ce qu'un role applicatif
    ne peut pas faire: il n'a aucun privilege sur cette table, et lui en donner rendrait la
    descente de migration non symetrique (voir l'en-tete de `0019`). La valeur passe donc par
    `app_schema_revision()`, `SECURITY DEFINER`, qui rend la revision et rien d'autre.

    Cette fonction vit ici et non dans `api/` parce que le pilote SQL est confine a la couche
    de persistance (`tests/test_persistence_boundaries.py`).
    """
    with psycopg.connect(url, autocommit=True, connect_timeout=connect_timeout) as conn:
        return conn.execute("SELECT public.app_schema_revision()").fetchone()[0]
