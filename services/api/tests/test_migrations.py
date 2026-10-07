"""Tests for Alembic migration correctness.

Verifies that:
- ``alembic upgrade head`` runs without error against a temp file database.
- ``alembic downgrade base`` runs without error (no-op on schema when there
  are no revisions, but env.py must not blow up).
- The SQLite PRAGMAs applied by :func:`~chronos.db.build_migration_engine`
  (``foreign_keys=ON``, ``journal_mode=WAL``) are visible on the connection
  used by the migration path.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from chronos.db import build_migration_engine


def _alembic_cfg(db_url: str) -> Config:
    """Return an Alembic :class:`~alembic.config.Config` pointing at the real
    ``alembic.ini`` but overriding the database URL for the temp database.

    The ``services/api`` directory is set as the working directory so that
    the ``script_location = alembic`` entry in ``alembic.ini`` resolves
    correctly regardless of where pytest is invoked from.
    """
    ini_path = Path(__file__).parents[1] / "alembic.ini"
    cfg = Config(str(ini_path))
    # Override the URL so env.py picks it up via config.get_main_option.
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


class TestMigrations:
    def test_upgrade_head_no_error(self, tmp_path: Path) -> None:
        """``alembic upgrade head`` must complete without raising."""
        db_url = f"sqlite:///{tmp_path / 'migrate_test.db'}"
        cfg = _alembic_cfg(db_url)
        # Should not raise even when there are no revision scripts yet.
        command.upgrade(cfg, "head")

    def test_downgrade_base_no_error(self, tmp_path: Path) -> None:
        """``alembic downgrade base`` must complete without raising."""
        db_url = f"sqlite:///{tmp_path / 'migrate_down.db'}"
        cfg = _alembic_cfg(db_url)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")

    def test_pragmas_applied_via_build_migration_engine(self, tmp_path: Path) -> None:
        """PRAGMAs must be set on connections opened through build_migration_engine.

        This mirrors exactly the engine path that env.py uses.
        """
        db_url = f"sqlite:///{tmp_path / 'pragma_test.db'}"
        # Run migrations first so the DB file is created.
        cfg = _alembic_cfg(db_url)
        command.upgrade(cfg, "head")

        engine = build_migration_engine(db_url)
        try:
            with engine.connect() as conn:
                fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
                wm = conn.execute(text("PRAGMA journal_mode")).scalar()
            assert fk == 1, f"foreign_keys should be 1, got {fk!r}"
            assert wm == "wal", f"journal_mode should be 'wal', got {wm!r}"
        finally:
            engine.dispose()
