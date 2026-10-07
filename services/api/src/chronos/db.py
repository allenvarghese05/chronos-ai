"""Chronos-AI database setup.

Creates a **synchronous** SQLAlchemy engine (D10 owner answer 2) backed by
SQLite.  Three PRAGMAs are applied on every new connection via the SQLAlchemy
``connect`` event:

- ``journal_mode = WAL`` — allows concurrent readers alongside a writer.
- ``foreign_keys = ON`` — enforces referential integrity (SQLite default is OFF).
- ``busy_timeout = 5000`` — wait up to 5 s instead of returning SQLITE_BUSY.

All SQLModel/SQLAlchemy models share the same :data:`metadata` object so that
Alembic can auto-generate migrations.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator

from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import ConnectionPoolEntry, NullPool
from sqlmodel import SQLModel, create_engine

# SQLModel exposes SQLAlchemy's metadata via SQLModel.metadata.
metadata = SQLModel.metadata

_engine: Engine | None = None


def _apply_pragmas(dbapi_conn: sqlite3.Connection, _connection_record: ConnectionPoolEntry) -> None:
    """SQLAlchemy ``connect`` event handler — set PRAGMAs on every new connection.

    This fires for every new DBAPI connection, including those created by
    connection-pool replenishment, so the PRAGMAs are never lost.
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def create_db_engine(database_url: str) -> Engine:
    """Create a synchronous SQLAlchemy engine with PRAGMAs applied on connect.

    Args:
        database_url: A ``sqlite:///`` URL (relative or absolute path).

    Returns:
        A configured :class:`sqlalchemy.Engine`.
    """
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def build_migration_engine(database_url: str) -> Engine:
    """Create an engine suitable for Alembic migrations.

    Uses :class:`~sqlalchemy.pool.NullPool` so each migration step gets a
    fresh DBAPI connection (required for ``render_as_batch=True`` on SQLite).
    PRAGMAs are applied via the same ``_apply_pragmas`` event listener as the
    application engine, so ``foreign_keys`` and ``journal_mode=WAL`` are
    enforced during migrations too.

    Args:
        database_url: A ``sqlite:///`` URL.

    Returns:
        A configured :class:`sqlalchemy.Engine` with :class:`NullPool`.
    """
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def init_engine(database_url: str) -> Engine:
    """Initialise (or replace) the module-level engine.

    Should be called once at application startup via the lifespan hook in
    :mod:`chronos.main`.
    """
    global _engine
    _engine = create_db_engine(database_url)
    return _engine


def get_engine() -> Engine:
    """Return the module-level engine.

    Raises:
        RuntimeError: if :func:`init_engine` has not been called yet.
    """
    if _engine is None:
        raise RuntimeError("DB engine not initialised — call init_engine() first")
    return _engine


def get_session(engine: Engine | None = None) -> Generator[Session, None, None]:
    """Yield a :class:`sqlalchemy.orm.Session`, rolling back on exception.

    Args:
        engine: Engine to use; falls back to the module-level engine.

    Yields:
        An open :class:`Session` that is closed after the generator exits.
    """
    resolved = engine if engine is not None else get_engine()
    with Session(resolved) as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise


def check_db(engine: Engine | None = None) -> str:
    """Run a lightweight connectivity probe.

    Returns:
        ``"ok"`` if the DB is reachable, or an error string starting with
        ``"error: "`` if not.
    """
    try:
        resolved = engine if engine is not None else get_engine()
        with Session(resolved) as session:
            session.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        return f"error: {exc}"
