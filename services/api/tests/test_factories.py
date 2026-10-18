"""Smoke tests: every factory inserts cleanly on a migrated temp DB."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from chronos.db import build_migration_engine
from chronos.models import (
    Day,
    DayTransition,
    Device,
    PairingCode,
    RuleVersion,
    Setting,
    TelemetryEvent,
    WakeEvaluation,
)
from tests.factories import (
    make_day,
    make_day_transition,
    make_device,
    make_pairing_code,
    make_rule_version,
    make_setting,
    make_telemetry_event,
    make_wake_evaluation,
)


def _alembic_cfg(db_url: str) -> Config:
    ini_path = Path(__file__).parents[1] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


class TestFactoriesSmoke:
    """Each factory must insert exactly one row and return the correct type."""

    def test_all_factories_insert_cleanly(self, tmp_path: Path) -> None:
        """End-to-end: all factories insert cleanly on a migrated DB."""
        db_url = f"sqlite:///{tmp_path / 'factories_smoke.db'}"
        command.upgrade(_alembic_cfg(db_url), "head")
        engine = build_migration_engine(db_url)

        try:
            with Session(engine) as session:
                # Device
                device = make_device(session)
                session.commit()
                assert isinstance(device, Device)
                assert device.id is not None

                # PairingCode
                pc = make_pairing_code(session)
                session.commit()
                assert isinstance(pc, PairingCode)
                assert pc.id is not None

                # RuleVersion
                rv = make_rule_version(session, active=1, version=1)
                session.commit()
                assert isinstance(rv, RuleVersion)
                assert rv.id is not None

                # TelemetryEvent
                te = make_telemetry_event(session, device=device, seq=1)
                session.commit()
                assert isinstance(te, TelemetryEvent)
                assert te.id is not None

                # WakeEvaluation
                we = make_wake_evaluation(session, event=te, rule_version=rv)
                session.commit()
                assert isinstance(we, WakeEvaluation)
                assert we.id is not None

                # Day
                day = make_day(session, device=device)
                session.commit()
                assert isinstance(day, Day)
                assert day.local_date == "2026-10-06"
                assert day.device_id == device.id

                # DayTransition
                dt = make_day_transition(session, day=day)
                session.commit()
                assert isinstance(dt, DayTransition)
                assert dt.id is not None

                # Setting
                setting = make_setting(session)
                session.commit()
                assert isinstance(setting, Setting)
                assert setting.id is not None

            # Verify row counts
            with Session(engine) as session:
                assert session.query(Device).count() == 1
                assert session.query(PairingCode).count() == 1
                assert session.query(RuleVersion).count() == 1
                assert session.query(TelemetryEvent).count() == 1
                assert session.query(WakeEvaluation).count() == 1
                assert session.query(Day).count() == 1
                assert session.query(DayTransition).count() == 1
                assert session.query(Setting).count() == 1

        finally:
            engine.dispose()

    def test_make_device_returns_unique_ids(self, tmp_path: Path) -> None:
        """Two make_device calls must produce distinct public_id values."""
        db_url = f"sqlite:///{tmp_path / 'factories_unique.db'}"
        command.upgrade(_alembic_cfg(db_url), "head")
        engine = build_migration_engine(db_url)
        try:
            with Session(engine) as session:
                d1 = make_device(session, token_hash="hash_a")
                d2 = make_device(session, token_hash="hash_b")
                session.commit()
                # Access within session to avoid DetachedInstanceError.
                pub1, pub2 = d1.public_id, d2.public_id
            assert pub1 != pub2
        finally:
            engine.dispose()

    def test_make_day_transition_references_day(self, tmp_path: Path) -> None:
        """DayTransition must reference its parent Day via composite FK."""
        db_url = f"sqlite:///{tmp_path / 'factories_fk.db'}"
        command.upgrade(_alembic_cfg(db_url), "head")
        engine = build_migration_engine(db_url)
        try:
            with Session(engine) as session:
                device = make_device(session)
                session.commit()
                day = make_day(session, device=device, local_date="2026-10-07")
                session.commit()
                dt = make_day_transition(session, day=day)
                session.commit()
                # Access within session.
                ld, did, dt_ld, dt_did = (
                    day.local_date,
                    day.device_id,
                    dt.local_date,
                    dt.device_id,
                )
            assert dt_ld == ld
            assert dt_did == did
        finally:
            engine.dispose()
