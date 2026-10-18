"""Tests for database-level constraints in migration 0001.

Each test:
1. Upgrades a fresh temp DB.
2. Opens a session with PRAGMA foreign_keys=ON (via build_migration_engine).
3. Inserts the minimal prerequisite rows.
4. Attempts the constraint-violating insert.
5. Asserts IntegrityError is raised.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from chronos.db import build_migration_engine
from chronos.time import utc_now_iso


def _alembic_cfg(db_url: str) -> Config:
    ini_path = Path(__file__).parents[1] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _migrated_engine(tmp_path: Path, name: str = "constraints_test.db") -> Engine:
    """Return a build_migration_engine pointing at a freshly migrated temp DB."""
    db_url = f"sqlite:///{tmp_path / name}"
    command.upgrade(_alembic_cfg(db_url), "head")
    return build_migration_engine(db_url)


# ---------------------------------------------------------------------------
# Minimal insert helpers (raw SQL to avoid triggering ORM-level validation)
# ---------------------------------------------------------------------------

_INSERT_DEVICE = text(
    "INSERT INTO devices (public_id, name, token_hash, tz, status, created_at, last_seen_at)"
    " VALUES (:pub, :name, :th, :tz, 'active', :ts, :ts)"
)

_INSERT_RULE_VERSION = text(
    "INSERT INTO rule_versions"
    " (version, source, rules_json, sha256, created_at, created_by, active)"
    " VALUES (:ver, 'yaml', '{}', 'abc', :ts, 'cli', :active)"
)

_INSERT_TELEMETRY = text(
    "INSERT INTO telemetry_events"
    " (device_id, received_at, device_ts, tz, local_date, wake_source, seq,"
    "  steps_10m, steps_since_window, walking_sec_10m, stationary_sec_10m,"
    "  walking_conf, is_unlocked, battery_state, app_state, low_power_mode,"
    "  pedometer_auth, app_version, ios_version, raw_json)"
    " VALUES (:did, :ts, :ts, 'UTC', '2026-10-06', 'foreground', :seq,"
    "         0, 0, 0, 0, 'low', 0, 'unplugged', 'background', 0,"
    "         'authorized', '1.0', '17.0', '{}')"
)

_INSERT_RULE_VER_MIN = text(
    "INSERT INTO rule_versions"
    " (version, source, rules_json, sha256, created_at, created_by, active)"
    " VALUES (1, 'yaml', '{}', 'abc', :ts, 'cli', 0)"
)

_INSERT_WAKE_EVAL = text(
    "INSERT INTO wake_evaluations"
    " (telemetry_event_id, rule_version_id, local_date, gate, score,"
    "  breakdown_json, decision, consecutive_high, evaluated_at)"
    " VALUES (:eid, :rid, '2026-10-06', 'open', 50, '[]', 'low', 0, :ts)"
)

_INSERT_DAY = text(
    "INSERT INTO days"
    " (local_date, device_id, tz, kind, window_start_utc, window_end_utc,"
    "  state, fallback_used, ticks_sent, ticks_received, created_at, updated_at)"
    " VALUES (:ld, :did, 'UTC', 'weekday', :ts, :ts, 'idle', 0, 0, 0, :ts, :ts)"
)


class TestForeignKeys:
    def test_device_fk_on_telemetry_events(self, tmp_path: Path) -> None:
        """Inserting a telemetry event with a non-existent device_id must fail."""
        engine = _migrated_engine(tmp_path, "fk_telemetry.db")
        ts = utc_now_iso()
        with Session(engine) as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    _INSERT_TELEMETRY,
                    {"did": 9999, "seq": 1, "ts": ts},
                )
                session.commit()
        engine.dispose()

    def test_days_confirm_evaluation_fk_null_ok(self, tmp_path: Path) -> None:
        """A day with confirm_evaluation_id=NULL must insert cleanly."""
        engine = _migrated_engine(tmp_path, "fk_days_null.db")
        ts = utc_now_iso()
        with Session(engine) as session:
            session.execute(
                _INSERT_DEVICE,
                {"pub": "PUB1", "name": "Phone", "th": "hash1", "tz": "UTC", "ts": ts},
            )
            session.execute(_INSERT_DAY, {"ld": "2026-10-06", "did": 1, "ts": ts})
            session.commit()
        engine.dispose()

    def test_days_confirm_evaluation_fk_invalid(self, tmp_path: Path) -> None:
        """A day with a bogus confirm_evaluation_id must fail with IntegrityError."""
        engine = _migrated_engine(tmp_path, "fk_days_bad_eval.db")
        ts = utc_now_iso()
        with Session(engine) as session:
            session.execute(
                _INSERT_DEVICE,
                {"pub": "PUB1", "name": "Phone", "th": "hash1", "tz": "UTC", "ts": ts},
            )
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        "INSERT INTO days"
                        " (local_date, device_id, tz, kind, window_start_utc, window_end_utc,"
                        "  state, fallback_used, ticks_sent, ticks_received,"
                        "  confirm_evaluation_id, created_at, updated_at)"
                        " VALUES ('2026-10-06', 1, 'UTC', 'weekday', :ts, :ts,"
                        "         'idle', 0, 0, 0, 9999, :ts, :ts)"
                    ),
                    {"ts": ts},
                )
                session.commit()
        engine.dispose()


class TestUniqueConstraints:
    def test_two_active_rule_versions_fails(self, tmp_path: Path) -> None:
        """Inserting two rule_versions rows with active=1 must fail (partial unique index)."""
        engine = _migrated_engine(tmp_path, "partial_idx.db")
        ts = utc_now_iso()
        with Session(engine) as session:
            session.execute(_INSERT_RULE_VERSION, {"ver": 1, "active": 1, "ts": ts})
            session.commit()

            with pytest.raises(IntegrityError):
                session.execute(_INSERT_RULE_VERSION, {"ver": 2, "active": 1, "ts": ts})
                session.commit()
        engine.dispose()

    def test_two_inactive_rule_versions_ok(self, tmp_path: Path) -> None:
        """Multiple rows with active=0 must be allowed (partial index ignores them)."""
        engine = _migrated_engine(tmp_path, "partial_idx_ok.db")
        ts = utc_now_iso()
        with Session(engine) as session:
            session.execute(_INSERT_RULE_VERSION, {"ver": 1, "active": 0, "ts": ts})
            session.execute(_INSERT_RULE_VERSION, {"ver": 2, "active": 0, "ts": ts})
            session.execute(_INSERT_RULE_VERSION, {"ver": 3, "active": 0, "ts": ts})
            session.commit()
        engine.dispose()

    def test_telemetry_device_seq_unique(self, tmp_path: Path) -> None:
        """Duplicate (device_id, seq) on telemetry_events must fail."""
        engine = _migrated_engine(tmp_path, "seq_unique.db")
        ts = utc_now_iso()
        with Session(engine) as session:
            session.execute(
                _INSERT_DEVICE,
                {"pub": "PUB1", "name": "Phone", "th": "hash1", "tz": "UTC", "ts": ts},
            )
            session.commit()

            session.execute(_INSERT_TELEMETRY, {"did": 1, "seq": 42, "ts": ts})
            session.commit()

            with pytest.raises(IntegrityError):
                session.execute(_INSERT_TELEMETRY, {"did": 1, "seq": 42, "ts": ts})
                session.commit()
        engine.dispose()

    def test_wake_evaluation_telemetry_event_id_unique(self, tmp_path: Path) -> None:
        """Two wake_evaluations for the same telemetry_event_id must fail (1:1)."""
        engine = _migrated_engine(tmp_path, "eval_unique.db")
        ts = utc_now_iso()
        with Session(engine) as session:
            session.execute(
                _INSERT_DEVICE,
                {"pub": "PUB1", "name": "Phone", "th": "hash1", "tz": "UTC", "ts": ts},
            )
            session.execute(_INSERT_RULE_VER_MIN, {"ts": ts})
            session.commit()

            session.execute(_INSERT_TELEMETRY, {"did": 1, "seq": 1, "ts": ts})
            session.commit()

            session.execute(_INSERT_WAKE_EVAL, {"eid": 1, "rid": 1, "ts": ts})
            session.commit()

            with pytest.raises(IntegrityError):
                session.execute(_INSERT_WAKE_EVAL, {"eid": 1, "rid": 1, "ts": ts})
                session.commit()
        engine.dispose()

    def test_days_composite_pk_unique(self, tmp_path: Path) -> None:
        """Duplicate (local_date, device_id) on days must fail."""
        engine = _migrated_engine(tmp_path, "days_pk.db")
        ts = utc_now_iso()
        with Session(engine) as session:
            session.execute(
                _INSERT_DEVICE,
                {"pub": "PUB1", "name": "Phone", "th": "hash1", "tz": "UTC", "ts": ts},
            )
            session.commit()

            session.execute(_INSERT_DAY, {"ld": "2026-10-06", "did": 1, "ts": ts})
            session.commit()

            with pytest.raises(IntegrityError):
                session.execute(_INSERT_DAY, {"ld": "2026-10-06", "did": 1, "ts": ts})
                session.commit()
        engine.dispose()


class TestCheckConstraints:
    def test_wake_source_check_constraint(self, tmp_path: Path) -> None:
        """Inserting an invalid wake_source enum value must fail."""
        engine = _migrated_engine(tmp_path, "ck_wake_source.db")
        ts = utc_now_iso()
        with Session(engine) as session:
            session.execute(
                _INSERT_DEVICE,
                {"pub": "PUB1", "name": "Phone", "th": "hash1", "tz": "UTC", "ts": ts},
            )
            session.commit()

            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        "INSERT INTO telemetry_events"
                        " (device_id, received_at, device_ts, tz, local_date,"
                        "  wake_source, seq, steps_10m, steps_since_window,"
                        "  walking_sec_10m, stationary_sec_10m, walking_conf,"
                        "  is_unlocked, battery_state, app_state, low_power_mode,"
                        "  pedometer_auth, app_version, ios_version, raw_json)"
                        " VALUES (1, :ts, :ts, 'UTC', '2026-10-06',"
                        "  'INVALID_SOURCE', 1, 0, 0, 0, 0, 'low',"
                        "  0, 'unplugged', 'background', 0,"
                        "  'authorized', '1.0', '17.0', '{}')"
                    ),
                    {"ts": ts},
                )
                session.commit()
        engine.dispose()

    def test_rule_version_source_check_constraint(self, tmp_path: Path) -> None:
        """Inserting an invalid rule_version source value must fail."""
        engine = _migrated_engine(tmp_path, "ck_rv_source.db")
        ts = utc_now_iso()
        with Session(engine) as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        "INSERT INTO rule_versions"
                        " (version, source, rules_json, sha256, created_at, created_by, active)"
                        " VALUES (1, 'INVALID', '{}', 'abc', :ts, 'cli', 0)"
                    ),
                    {"ts": ts},
                )
                session.commit()
        engine.dispose()
