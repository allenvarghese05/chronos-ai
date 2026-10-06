"""Alembic environment configuration for Chronos-AI.

Reads ``CHRONOS_DATABASE_URL`` from the environment (via
:class:`~chronos.config.Settings`) at migration time so that the database URL
is never hard-coded in ``alembic.ini``.

Both offline (SQL generation) and online (direct DB) migration modes are
supported.  ``target_metadata`` is set to :attr:`sqlmodel.SQLModel.metadata`
so Alembic can auto-generate migrations for all SQLModel-declared models.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Ensure the src layout is importable when running alembic from services/api/.
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

# Import SQLModel metadata so Alembic sees all declared models.
from sqlmodel import SQLModel

target_metadata = SQLModel.metadata

# Alembic Config object (gives access to alembic.ini values).
config = context.config

# Set up Python logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# Inject the database URL from CHRONOS_DATABASE_URL (or the pydantic Settings).
def _get_db_url() -> str:
    """Return the database URL, preferring (in order):

    1. A value already set on the Alembic config object (e.g. by a test or a
       CLI ``-x sqlalchemy.url=...`` flag).
    2. The ``CHRONOS_DATABASE_URL`` environment variable.
    3. The full :class:`~chronos.config.Settings` singleton (requires
       ``CHRONOS_SECRET_KEY`` etc. to be set).
    4. A safe SQLite fallback.
    """
    # Check if the config object already has a URL (set by test or CLI).
    existing = config.get_main_option("sqlalchemy.url")
    if existing:
        return existing
    # Environment variable.
    db_url = os.environ.get("CHRONOS_DATABASE_URL")
    if db_url:
        return db_url
    # Fallback: load full settings (requires CHRONOS_SECRET_KEY etc.)
    try:
        from chronos.config import get_settings

        return get_settings().database_url
    except Exception:
        return "sqlite:///data/chronos.db"


config.set_main_option("sqlalchemy.url", _get_db_url())


def run_migrations_offline() -> None:
    """Run migrations in "offline" mode (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # required for SQLite ALTER TABLE emulation
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in "online" mode (connect directly to the DB).

    Uses :func:`~chronos.db.build_migration_engine` so that the same
    PRAGMAs (``journal_mode=WAL``, ``foreign_keys=ON``, ``busy_timeout``)
    are applied during migrations as during normal app operation.
    """
    from chronos.db import build_migration_engine

    url = config.get_main_option("sqlalchemy.url")
    assert url is not None, "sqlalchemy.url must be set before run_migrations_online()"
    connectable = build_migration_engine(url)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
