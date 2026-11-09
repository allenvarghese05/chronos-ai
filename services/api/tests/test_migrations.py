"""Tests for Alembic migration correctness.

Verifies that:
- ``alembic upgrade head`` runs without error against a temp file database.
- ``alembic downgrade base`` runs without error (no-op on schema when there
  are no revisions, but env.py must not blow up).
- The SQLite PRAGMAs applied by :func:`~chronos.db.build_migration_engine`
  (``foreign_keys=ON``, ``journal_mode=WAL``) are visible on the connection
  used by the migration path.
- All 8 tables are created by migration 0001.
- Downgrade base drops all 8 tables.
- SQLModel.metadata matches the migrated schema (parity test).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import UniqueConstraint, inspect, text
from sqlmodel import SQLModel

import chronos.models  # side-effect: populates SQLModel.metadata
from chronos.db import build_migration_engine

_ = chronos.models  # keep reference so pyright doesn't flag unused import


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


class TestMigration0001:
    """Detailed tests for migration 0001 tables, downgrade and parity."""

    EXPECTED_TABLES = frozenset(
        {
            "devices",
            "pairing_codes",
            "rule_versions",
            "telemetry_events",
            "wake_evaluations",
            "days",
            "day_transitions",
            "settings",
            "push_notifications",
            "sources",
            "items",
            "briefs",
            "brief_items",
            "jobs",
            "reels",
            "reel_assets",
        }
    )

    def test_upgrade_creates_all_head_tables(self, tmp_path: Path) -> None:
        """After ``upgrade head`` all application tables must exist."""
        db_url = f"sqlite:///{tmp_path / 'tables_test.db'}"
        cfg = _alembic_cfg(db_url)
        command.upgrade(cfg, "head")

        engine = build_migration_engine(db_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT name FROM sqlite_master"
                        " WHERE type='table' AND name NOT LIKE 'alembic%'"
                    )
                ).fetchall()
                actual = {r[0] for r in rows}
            assert self.EXPECTED_TABLES == actual, f"Missing: {self.EXPECTED_TABLES - actual}"
        finally:
            engine.dispose()

    def test_downgrade_drops_all_tables(self, tmp_path: Path) -> None:
        """After ``downgrade base`` none of the 8 application tables must exist."""
        db_url = f"sqlite:///{tmp_path / 'downgrade_test.db'}"
        cfg = _alembic_cfg(db_url)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")

        engine = build_migration_engine(db_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT name FROM sqlite_master"
                        " WHERE type='table' AND name NOT LIKE 'alembic%'"
                    )
                ).fetchall()
                actual = {r[0] for r in rows}
            assert actual == set(), f"Tables not dropped: {actual}"
        finally:
            engine.dispose()

    def test_metadata_vs_inspect_parity(self, tmp_path: Path) -> None:
        """SQLModel.metadata must match the migrated schema.

        Checks: table names, column names, nullability, and PK columns.
        """
        db_url = f"sqlite:///{tmp_path / 'parity_test.db'}"
        cfg = _alembic_cfg(db_url)
        command.upgrade(cfg, "head")

        engine = build_migration_engine(db_url)
        try:
            inspector = inspect(engine)
            db_tables = set(inspector.get_table_names())
            db_tables.discard("alembic_version")

            meta_tables = set(SQLModel.metadata.tables.keys())
            assert meta_tables == db_tables, (
                f"Table mismatch -- metadata: {meta_tables}, DB: {db_tables}"
            )

            for table_name in meta_tables:
                meta_table = SQLModel.metadata.tables[table_name]

                # Column names and nullability
                db_cols = {c["name"]: c["nullable"] for c in inspector.get_columns(table_name)}
                for col in meta_table.columns:
                    assert col.name in db_cols, f"{table_name}.{col.name} missing from DB"
                    assert db_cols[col.name] == col.nullable, (
                        f"{table_name}.{col.name} nullability mismatch: "
                        f"meta={col.nullable} db={db_cols[col.name]}"
                    )

                # Primary key column names
                db_pk = inspector.get_pk_constraint(table_name)
                db_pk_cols = set(db_pk.get("constrained_columns", []))
                meta_pk_cols = {col.name for col in meta_table.primary_key.columns}
                assert meta_pk_cols == db_pk_cols, (
                    f"{table_name} PK mismatch: meta={meta_pk_cols} db={db_pk_cols}"
                )

                # Unique constraints
                db_uqs = {
                    tuple(sorted(u["column_names"]))
                    for u in inspector.get_unique_constraints(table_name)
                }
                meta_uqs = {
                    tuple(sorted(c.columns.keys()))  # type: ignore[union-attr]
                    for c in meta_table.constraints
                    if isinstance(c, UniqueConstraint)
                }
                assert meta_uqs == db_uqs, (
                    f"{table_name} unique constraints mismatch: meta={meta_uqs} db={db_uqs}"
                )
        finally:
            engine.dispose()
