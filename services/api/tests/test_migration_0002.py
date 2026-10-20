"""Migration 0002 - push_notifications table: parity and drift-probe tests.

Tests
-----
- Table exists after upgrade head.
- All 13 expected columns present with correct nullability.
- reel_id has no FK constraint (FK deferred to T4.1).
- Autogenerate drift probe: running alembic autogenerate produces an empty diff
  (no un-migrated model changes).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect

from chronos.db import create_db_engine

_ALEMBIC_INI = Path(__file__).parents[1] / "alembic.ini"


def _make_engine(tmp_path: Path) -> Engine:
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_db_engine(db_url)
    return engine


def _run_upgrade(tmp_path: Path) -> tuple[Engine, str]:
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    engine = create_db_engine(db_url)
    return engine, db_url


class TestMigration0002:
    def test_push_notifications_table_exists(self, tmp_path: Path) -> None:
        """push_notifications table must exist after upgrade head."""
        engine, _ = _run_upgrade(tmp_path)
        insp = inspect(engine)
        assert "push_notifications" in insp.get_table_names()

    def test_push_notifications_columns(self, tmp_path: Path) -> None:
        """All 13 columns must exist with correct nullability."""
        engine, _ = _run_upgrade(tmp_path)
        insp = inspect(engine)
        cols = {c["name"]: c for c in insp.get_columns("push_notifications")}

        required_not_null = [
            "id",
            "device_id",
            "kind",
            "local_date",
            "payload_json",
            "ticket_status",
            "sent_at",
        ]
        required_nullable = [
            "reel_id",
            "tick_seq",
            "ticket_id",
            "receipt_status",
            "receipt_checked_at",
            "error",
        ]

        for col in required_not_null:
            assert col in cols, f"column {col!r} missing"
            assert not cols[col]["nullable"], f"column {col!r} should be NOT NULL"

        for col in required_nullable:
            assert col in cols, f"column {col!r} missing"
            assert cols[col]["nullable"], f"column {col!r} should be nullable"

    def test_reel_id_no_fk(self, tmp_path: Path) -> None:
        """reel_id must have no FK constraint (reels table deferred to T4.1)."""
        engine, _ = _run_upgrade(tmp_path)
        insp = inspect(engine)
        fks = insp.get_foreign_keys("push_notifications")
        reel_fks = [fk for fk in fks if "reel_id" in fk.get("constrained_columns", [])]
        assert reel_fks == [], f"Expected no FK on reel_id; found: {reel_fks}"

    def test_device_id_fk_exists(self, tmp_path: Path) -> None:
        """device_id must FK to devices.id."""
        engine, _ = _run_upgrade(tmp_path)
        insp = inspect(engine)
        fks = insp.get_foreign_keys("push_notifications")
        device_fks = [fk for fk in fks if "device_id" in fk.get("constrained_columns", [])]
        assert len(device_fks) == 1
        assert device_fks[0]["referred_table"] == "devices"

    def test_indexes_exist(self, tmp_path: Path) -> None:
        """Composite index (device_id, local_date) and kind index must exist."""
        engine, _ = _run_upgrade(tmp_path)
        insp = inspect(engine)
        indexes = {idx["name"]: idx for idx in insp.get_indexes("push_notifications")}
        assert "ix_push_notifications_device_local_date" in indexes
        assert "ix_push_notifications_kind" in indexes

    def test_autogenerate_drift_probe(self, tmp_path: Path) -> None:
        """Running alembic autogenerate must produce an empty diff (no model drift)."""
        from alembic.autogenerate import compare_metadata
        from alembic.runtime.migration import MigrationContext
        from sqlmodel import SQLModel

        _engine, db_url = _run_upgrade(tmp_path)

        # Import all models to register them with SQLModel.metadata
        import chronos.models  # noqa: F401 - side-effect: registers all table classes

        cfg = Config(str(_ALEMBIC_INI))
        cfg.set_main_option("sqlalchemy.url", db_url)

        raw_engine = create_engine(db_url)
        with raw_engine.connect() as conn:
            context = MigrationContext.configure(conn)
            diff = compare_metadata(context, SQLModel.metadata)

        assert diff == [], f"Autogenerate drift detected:\n{diff}"

    def test_downgrade_drops_table(self, tmp_path: Path) -> None:
        """Downgrade from head to 0001 must remove push_notifications."""
        _engine, db_url = _run_upgrade(tmp_path)
        cfg = Config(str(_ALEMBIC_INI))
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.downgrade(cfg, "0001")
        engine2 = create_db_engine(db_url)
        insp = inspect(engine2)
        assert "push_notifications" not in insp.get_table_names()
