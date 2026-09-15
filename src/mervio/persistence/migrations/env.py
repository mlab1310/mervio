"""Environnement Alembic de Mervio.

Les migrations sont ecrites en SQL explicite (aucun modele ORM, aucune
autogeneration): le schema versionne est exactement celui qui est relu en
revue. L'URL vient de `config.attributes['database_url']` (migrate.py) ou de
MERVIO_MIGRATION_DATABASE_URL; aucune valeur par defaut.
"""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config


def _url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("MERVIO_MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError("MERVIO_MIGRATION_DATABASE_URL non definie: aucune base de migration par defaut")
    # driver psycopg 3 (le seul installe); jamais psycopg2 implicite
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
