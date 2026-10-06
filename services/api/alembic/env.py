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
from sqlalchemy import engine_from_config, pool

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
    """Run migrations in "online" mode (connect directly to the DB)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
