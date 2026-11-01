"""Tests for wake/rules.py — WakeRules schema, loader, cache, versioning (T1.4)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from sqlmodel import Session, create_engine

from chronos.models.wake import RuleVersion
from chronos.wake.rules import (
    ActiveRuleVersion,
    WakeRules,
    WakeRulesWindow,
    activate_new_version,
    canonical_json,
    ensure_initial_version,
    get_rules_cache,
    load_yaml,
    reset_rules_cache,
    rules_sha256,
    yaml_stale,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_RULES: dict[str, Any] = {
    "version": 1,
    "window": {
        "weekday": {"start": "06:00", "end": "11:00"},
        "weekend": {"start": "08:00", "end": "14:00"},
        "day_boundary": "03:00",
        "holidays_as_weekend": [],
        "tick_interval_min": 5,
    },
    "thresholds": {"confirm": 70, "strong": 85, "dedupe_min": 2},
    "rules": [
        {"id": "steps_10m", "signal": "steps_10m", "points": [[35, 30], [20, 18]], "cap": 30},
        {
            "id": "unlocked",
            "signal": "is_unlocked",
            "points": [[True, 15]],
            "cap": 25,
            "bonus_consecutive": {"ticks": 2, "points": 10},
        },
        {
            "id": "foreground",
            "signal": "wake_source",
            "points": [["foreground", 40]],
            "cap": 40,
        },
    ],
    "penalties": [],
    "reweight_without_pedometer": {},
}


def make_valid(**overrides: Any) -> dict[str, Any]:
    """Return VALID_RULES with overrides applied at the top level."""
    import copy

    base = copy.deepcopy(VALID_RULES)
    base.update(overrides)
    return base


def make_session(tmp_path: Path) -> Session:
    """Create a fresh SQLite session with migrations applied."""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    ini_path = Path(__file__).parents[2] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    return Session(engine)


@pytest.fixture()
def tmp_session(tmp_path: Path) -> Session:
    return make_session(tmp_path)


@pytest.fixture()
def valid_rules() -> WakeRules:
    return WakeRules.model_validate(VALID_RULES)


@pytest.fixture()
def yaml_path(tmp_path: Path) -> Path:
    """Copy the real config/wake_rules.yaml to a temp directory and return its path."""
    real = Path(__file__).parents[2] / "config" / "wake_rules.yaml"
    dest = tmp_path / "wake_rules.yaml"
    dest.write_text(real.read_text())
    return dest


# ---------------------------------------------------------------------------
# Signal registry
# ---------------------------------------------------------------------------


class TestSignalsRegistry:
    def test_signals_registry_complete(self) -> None:
        from chronos.wake.signals import SIGNALS

        expected_phone = {
            "device_ts",
            "tz",
            "wake_source",
            "steps_10m",
            "steps_since_window",
            "walking_sec_10m",
            "walking_conf",
            "stationary_sec_10m",
            "is_unlocked",
            "battery_state",
            "battery_level",
            "app_state",
            "low_power_mode",
            "pedometer_auth",
            "seq",
            "app_version",
            "ios_version",
        }
        expected_derived = {
            "charger_disconnected",
            "consecutive_unlocked_ticks",
            "minutes_since_window_start",
            "ticks_received_today",
        }
        assert expected_phone <= set(SIGNALS.keys())
        assert expected_derived <= set(SIGNALS.keys())

    def test_signal_kinds_correct(self) -> None:
        from chronos.wake.signals import SIGNALS, SignalKind

        assert SIGNALS["steps_10m"].kind is SignalKind.numeric
        assert SIGNALS["is_unlocked"].kind is SignalKind.bool_
        assert SIGNALS["wake_source"].kind is SignalKind.enum_
        assert SIGNALS["charger_disconnected"].kind is SignalKind.bool_


# ---------------------------------------------------------------------------
# v1 YAML loads successfully
# ---------------------------------------------------------------------------


class TestV1YamlLoads:
    def test_v1_yaml_loads_successfully(self, yaml_path: Path) -> None:
        rules = load_yaml(yaml_path)
        assert rules.version == 1
        assert len(rules.rules) == 6
        assert rules.thresholds.confirm == 70
        assert rules.thresholds.strong == 85
        assert rules.window.weekday["start"] == "06:00"

    def test_v1_yaml_reweight_keys_valid(self, yaml_path: Path) -> None:
        rules = load_yaml(yaml_path)
        assert "unlocked" in rules.reweight_without_pedometer
        assert "charger_disconnect" in rules.reweight_without_pedometer
        assert "foreground" in rules.reweight_without_pedometer

    def test_v1_yaml_to_window_config(self, yaml_path: Path) -> None:
        import datetime

        rules = load_yaml(yaml_path)
        wc = rules.window.to_window_config()
        assert wc.weekday_start == datetime.time(6, 0)
        assert wc.weekend_end == datetime.time(14, 0)
        assert wc.tick_interval_min == 5


# ---------------------------------------------------------------------------
# Edge case 1: cap < max points
# ---------------------------------------------------------------------------


class TestInvalidCapLessThanPoints:
    def test_invalid_cap_less_than_points(self) -> None:
        d = make_valid(
            rules=[
                {"id": "steps_10m", "signal": "steps_10m", "points": [[35, 30]], "cap": 25},
            ]
        )
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("cap" in m and "25" in m and "30" in m for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 2: duplicate rule ids
# ---------------------------------------------------------------------------


class TestInvalidDuplicateRuleId:
    def test_invalid_duplicate_rule_id(self) -> None:
        d = make_valid(
            rules=[
                {"id": "steps_10m", "signal": "steps_10m", "points": [[35, 30]], "cap": 30},
                {"id": "steps_10m", "signal": "steps_since_window", "points": [[100, 5]], "cap": 5},
            ]
        )
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("duplicate" in m and "steps_10m" in m for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 3: unknown signal
# ---------------------------------------------------------------------------


class TestInvalidUnknownSignal:
    def test_invalid_unknown_signal(self) -> None:
        d = make_valid(
            rules=[
                {
                    "id": "bad_rule",
                    "signal": "unknown_signal",
                    "points": [[10, 5]],
                    "cap": 5,
                },
            ]
        )
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("unknown signal" in m and "unknown_signal" in m for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 4: numeric thresholds not strictly descending
# ---------------------------------------------------------------------------


class TestInvalidPointsNotDescending:
    def test_invalid_points_not_descending(self) -> None:
        d = make_valid(
            rules=[
                {
                    "id": "steps_10m",
                    "signal": "steps_10m",
                    "points": [[100, 10], [50, 8], [60, 5]],
                    "cap": 10,
                },
            ]
        )
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("descending" in m for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 5: bool rule with more than one row
# ---------------------------------------------------------------------------


class TestInvalidBoolRuleMultipleRows:
    def test_invalid_bool_rule_multiple_rows(self) -> None:
        d = make_valid(
            rules=[
                {
                    "id": "unlocked",
                    "signal": "is_unlocked",
                    "points": [[True, 15], [False, 0]],
                    "cap": 15,
                },
            ]
        )
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("exactly one" in m or "bool" in m for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 6: thresholds.strong < thresholds.confirm (violates strong >= confirm)
# ---------------------------------------------------------------------------


class TestInvalidStrongLtConfirm:
    def test_invalid_strong_lt_confirm(self) -> None:
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["thresholds"] = {"confirm": 70, "strong": 60, "dedupe_min": 2}
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("strong" in m and "confirm" in m for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 7: window weekend start >= end
# ---------------------------------------------------------------------------


class TestInvalidWindowStartGteEnd:
    def test_invalid_window_start_gte_end(self) -> None:
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["window"]["weekend"] = {"start": "14:00", "end": "14:00"}
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("weekend" in m and "<" in m for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 8 & 9: tick_interval_min out of range
# ---------------------------------------------------------------------------


class TestInvalidTickIntervalMin:
    def test_invalid_tick_interval_min_too_small(self) -> None:
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["window"]["tick_interval_min"] = 1
        with pytest.raises(ValidationError):
            WakeRules.model_validate(d)

    def test_invalid_tick_interval_min_too_large(self) -> None:
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["window"]["tick_interval_min"] = 31
        with pytest.raises(ValidationError):
            WakeRules.model_validate(d)


# ---------------------------------------------------------------------------
# Edge case 10: empty rules list
# ---------------------------------------------------------------------------


class TestInvalidEmptyRulesList:
    def test_invalid_empty_rules_list(self) -> None:
        d = make_valid(rules=[])
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("must not be empty" in m or "rules" in m for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 11: invalid holiday ISO date
# ---------------------------------------------------------------------------


class TestInvalidHolidayDate:
    def test_invalid_holiday_date(self) -> None:
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["window"]["holidays_as_weekend"] = ["2026-13-01"]
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("2026-13-01" in m or "ISO date" in m for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 12: reweight key not a rule id
# ---------------------------------------------------------------------------


class TestInvalidReweightUnknownRule:
    def test_invalid_reweight_unknown_rule(self) -> None:
        d = make_valid(reweight_without_pedometer={"unknown_rule": 35})
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("unknown_rule" in m for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 13: reweight sum > 100
# ---------------------------------------------------------------------------


class TestInvalidReweightSumTooLarge:
    def test_invalid_reweight_sum_too_large(self) -> None:
        # Use rules that include all three IDs used in reweight.
        d = make_valid(
            reweight_without_pedometer={
                "unlocked": 50,
                "foreground": 60,  # sum = 110
            }
        )
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("≤ 100" in m or "<= 100" in m or "sum" in m.lower() for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 14: penalty when condition cannot be parsed
# ---------------------------------------------------------------------------


class TestInvalidPenaltyCondition:
    def test_invalid_penalty_condition(self) -> None:
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["penalties"] = [{"id": "bad", "when": {"steps_10m": ">= notanumber"}, "points": -5}]
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = [e["msg"] for e in errors]
        assert any("condition" in m.lower() or "invalid" in m.lower() for m in msgs)


# ---------------------------------------------------------------------------
# Edge case 15: Penalty.points must be <= 0 (Field(le=0))
# ---------------------------------------------------------------------------


class TestInvalidPenaltyPointsPositive:
    def test_penalty_points_positive_rejected(self) -> None:
        """Positive penalty points are rejected; loc path must include penalties.0.points."""
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["penalties"] = [
            {"id": "bad", "when": {"steps_10m": ">= 10"}, "points": 5},  # positive \ invalid
        ]
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        locs = [e["loc"] for e in errors]
        # At least one error must point to penalties -> 0 -> points.
        assert any(loc[0] == "penalties" and loc[1] == 0 and loc[2] == "points" for loc in locs), (
            f"Expected penalties.0.points in error locs; got {locs}"
        )

    def test_penalty_points_zero_accepted(self) -> None:
        """Zero penalty points (mark-only) must be accepted."""
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["penalties"] = [
            {"id": "mark_only", "when": {"steps_10m": ">= 10"}, "points": 0},
        ]
        rules = WakeRules.model_validate(d)  # must not raise
        assert rules.penalties[0].points == 0

    def test_penalty_points_negative_accepted(self) -> None:
        """Negative penalty points must be accepted."""
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["penalties"] = [
            {"id": "deduct", "when": {"steps_10m": ">= 10"}, "points": -20},
        ]
        rules = WakeRules.model_validate(d)  # must not raise
        assert rules.penalties[0].points == -20


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    def test_canonical_json_deterministic(self, valid_rules: WakeRules) -> None:
        j1 = canonical_json(valid_rules)
        j2 = canonical_json(valid_rules)
        j3 = canonical_json(valid_rules)
        assert j1 == j2 == j3

    def test_canonical_json_is_valid_json(self, valid_rules: WakeRules) -> None:
        j = canonical_json(valid_rules)
        parsed = json.loads(j)
        assert isinstance(parsed, dict)

    def test_canonical_json_roundtrip(self, valid_rules: WakeRules) -> None:
        j = canonical_json(valid_rules)
        roundtripped = WakeRules.model_validate_json(j)
        assert canonical_json(roundtripped) == j

    def test_sha256_stable(self, valid_rules: WakeRules) -> None:
        sha1 = rules_sha256(valid_rules)
        sha2 = rules_sha256(valid_rules)
        assert sha1 == sha2
        assert len(sha1) == 64  # hex SHA-256

    def test_sha256_matches_manual(self, valid_rules: WakeRules) -> None:
        j = canonical_json(valid_rules)
        expected = hashlib.sha256(j.encode()).hexdigest()
        assert rules_sha256(valid_rules) == expected

    def test_different_rules_different_sha(self, valid_rules: WakeRules) -> None:
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["thresholds"]["confirm"] = 75
        other = WakeRules.model_validate(d)
        assert rules_sha256(valid_rules) != rules_sha256(other)


# ---------------------------------------------------------------------------
# DB versioning
# ---------------------------------------------------------------------------


class TestActivateNewVersion:
    def test_activate_new_version_increments(
        self, tmp_session: Session, valid_rules: WakeRules
    ) -> None:
        with tmp_session:
            rv, created = activate_new_version(
                tmp_session, valid_rules, source="yaml", created_by="test"
            )
            tmp_session.commit()
            assert rv.version == 1
            assert created is True
            assert rv.active == 1

    def test_activate_second_version_increments(
        self, tmp_session: Session, valid_rules: WakeRules
    ) -> None:
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["thresholds"]["confirm"] = 75
        other_rules = WakeRules.model_validate(d)

        with tmp_session:
            rv1, _ = activate_new_version(
                tmp_session, valid_rules, source="yaml", created_by="test"
            )
            tmp_session.commit()
            reset_rules_cache()

            rv2, created = activate_new_version(
                tmp_session, other_rules, source="api", created_by="test"
            )
            tmp_session.commit()

            # Capture values inside the session context to avoid DetachedInstanceError.
            rv1_version = rv1.version
            rv2_version = rv2.version

        assert rv2_version == rv1_version + 1
        assert created is True

    def test_activate_new_version_previous_inactive(
        self, tmp_session: Session, valid_rules: WakeRules
    ) -> None:
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["thresholds"]["confirm"] = 75
        other_rules = WakeRules.model_validate(d)

        with tmp_session:
            rv1, _ = activate_new_version(
                tmp_session, valid_rules, source="yaml", created_by="test"
            )
            tmp_session.commit()
            reset_rules_cache()

            activate_new_version(tmp_session, other_rules, source="api", created_by="test")
            tmp_session.commit()

            tmp_session.refresh(rv1)
            assert rv1.active == 0

    def test_activate_identical_sha_noop(
        self, tmp_session: Session, valid_rules: WakeRules
    ) -> None:
        with tmp_session:
            rv1, _ = activate_new_version(
                tmp_session, valid_rules, source="yaml", created_by="test"
            )
            tmp_session.commit()
            reset_rules_cache()

            rv2, created = activate_new_version(
                tmp_session, valid_rules, source="api", created_by="test"
            )
            tmp_session.commit()

            # Capture values inside the session context to avoid DetachedInstanceError.
            rv1_id = rv1.id
            rv2_id = rv2.id

        assert created is False
        assert rv1_id == rv2_id

    def test_activate_failing_insert_leaves_previous_active(
        self, tmp_session: Session, valid_rules: WakeRules, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import copy

        d = copy.deepcopy(VALID_RULES)
        d["thresholds"]["confirm"] = 75
        other_rules = WakeRules.model_validate(d)

        with tmp_session:
            rv1, _ = activate_new_version(
                tmp_session, valid_rules, source="yaml", created_by="test"
            )
            tmp_session.commit()
            reset_rules_cache()
            # Capture rv1.id inside the session to avoid DetachedInstanceError.
            rv1_id = rv1.id

        # Monkeypatch session.flush to raise when new objects are pending
        # (i.e., when the INSERT of the new RuleVersion is about to be flushed).
        # activate_new_version suppresses autoflush with no_autoflush, so this
        # failing_flush is only called for the explicit session.flush() calls:
        #   call 1: flush the deactivation UPDATE — no new objects, succeeds
        #   call 2: flush the INSERT of new_rv — new objects pending, RAISES
        original_flush = tmp_session.flush

        def failing_flush(objects: Any = None) -> None:
            if tmp_session.new:  # new objects pending => INSERT path => fail
                raise RuntimeError("Simulated DB failure")
            return original_flush(objects)

        monkeypatch.setattr(tmp_session, "flush", failing_flush)

        with tmp_session:
            with pytest.raises(RuntimeError, match="Simulated DB failure"):
                activate_new_version(tmp_session, other_rules, source="api", created_by="test")
            tmp_session.rollback()

            # Check previous version is still active.
            # Use no_autoflush so the patched flush is not triggered by the verify SELECT.
            with tmp_session.no_autoflush:
                from sqlmodel import select as sel

                active = tmp_session.exec(sel(RuleVersion).where(RuleVersion.active == 1)).first()
            assert active is not None
            assert active.id == rv1_id


# ---------------------------------------------------------------------------
# ensure_initial_version
# ---------------------------------------------------------------------------


class TestEnsureInitialVersion:
    def test_startup_no_active_loads_yaml(self, tmp_session: Session, yaml_path: Path) -> None:
        with tmp_session:
            rv = ensure_initial_version(tmp_session, yaml_path)
            assert rv.version == 1
            assert rv.source == "yaml"
            assert rv.created_by == "startup"
            assert rv.active == 1

    def test_startup_idempotent(self, tmp_session: Session, yaml_path: Path) -> None:
        with tmp_session:
            rv1 = ensure_initial_version(tmp_session, yaml_path)
            reset_rules_cache()
            rv2 = ensure_initial_version(tmp_session, yaml_path)
        assert rv1.id == rv2.id

    def test_startup_invalid_yaml_raises(self, tmp_path: Path) -> None:
        db_session = make_session(tmp_path)
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("{unclosed: [")
        with db_session:
            with pytest.raises((yaml.YAMLError, ValueError)):
                ensure_initial_version(db_session, bad_yaml)


# ---------------------------------------------------------------------------
# yaml_stale
# ---------------------------------------------------------------------------


class TestYamlStale:
    def test_yaml_stale_false_when_same(self, tmp_session: Session, yaml_path: Path) -> None:
        with tmp_session:
            ensure_initial_version(tmp_session, yaml_path)
            reset_rules_cache()
            assert yaml_stale(tmp_session, yaml_path) is False

    def test_yaml_stale_true_when_changed(
        self, tmp_session: Session, yaml_path: Path, tmp_path: Path
    ) -> None:
        with tmp_session:
            ensure_initial_version(tmp_session, yaml_path)
            reset_rules_cache()

        # Modify the yaml (change threshold).
        raw = yaml.safe_load(yaml_path.read_text())
        raw["thresholds"]["confirm"] = 75
        yaml_path.write_text(yaml.dump(raw))

        with tmp_session:
            assert yaml_stale(tmp_session, yaml_path) is True

    def test_yaml_stale_false_no_yaml_version(
        self, tmp_session: Session, yaml_path: Path, valid_rules: WakeRules
    ) -> None:
        # Insert an API-sourced version (no yaml version exists).
        with tmp_session:
            activate_new_version(tmp_session, valid_rules, source="api", created_by="test")
            tmp_session.commit()
            reset_rules_cache()
            # No yaml-sourced version → stale returns False
            assert yaml_stale(tmp_session, yaml_path) is False


# ---------------------------------------------------------------------------
# RulesCache
# ---------------------------------------------------------------------------


class TestRulesCache:
    def test_cache_get_effective(self, tmp_session: Session, yaml_path: Path) -> None:
        cache = get_rules_cache()
        with tmp_session:
            ensure_initial_version(tmp_session, yaml_path)
            reset_rules_cache()
            rules, rv = cache.get_effective(tmp_session)
        assert rules.version == 1
        # rv is a frozen snapshot — always represents the active version
        assert isinstance(rv, ActiveRuleVersion)
        assert rv.version == 1

    def test_cache_invalidate(
        self, tmp_session: Session, yaml_path: Path, valid_rules: WakeRules
    ) -> None:
        import copy

        cache = get_rules_cache()
        d = copy.deepcopy(VALID_RULES)
        d["thresholds"]["confirm"] = 75
        other_rules = WakeRules.model_validate(d)

        with tmp_session:
            ensure_initial_version(tmp_session, yaml_path)
            reset_rules_cache()

            # Warm up cache.
            rules1, rv1 = cache.get_effective(tmp_session)
            assert rules1.thresholds.confirm == 70

            # Activate a new version.
            activate_new_version(tmp_session, other_rules, source="api", created_by="test")
            tmp_session.commit()

            # Cache was invalidated by activate_new_version.
            rules2, rv2 = cache.get_effective(tmp_session)
            assert rules2.thresholds.confirm == 75
            assert rv2.version > rv1.version

    def test_cache_miss_no_active_raises(self, tmp_session: Session) -> None:
        cache = get_rules_cache()
        reset_rules_cache()
        with tmp_session, pytest.raises(RuntimeError, match="No active rule version"):
            cache.get_effective(tmp_session)

    def test_snapshot_readable_after_session_closes(self, tmp_path: Path, yaml_path: Path) -> None:
        """Regression: ActiveRuleVersion snapshot must not raise DetachedInstanceError.

        Two calls from different sessions both read ``.version`` outside their
        session scope — previously this would raise ``DetachedInstanceError``
        because the cached ORM object was bound to the first session.
        """
        from sqlmodel import SQLModel, create_engine

        engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
        SQLModel.metadata.create_all(engine)

        cache = get_rules_cache()
        reset_rules_cache()

        # Session 1 — warms the cache.
        with Session(engine) as s1:
            ensure_initial_version(s1, yaml_path)
            reset_rules_cache()
            _, rv1 = cache.get_effective(s1)
        # Session 1 is now closed.
        version_from_s1 = rv1.version  # must not raise DetachedInstanceError

        # Session 2 — cache hit; same snapshot is returned.
        with Session(engine) as s2:
            _, rv2 = cache.get_effective(s2)
        version_from_s2 = rv2.version  # must not raise DetachedInstanceError

        assert version_from_s1 == version_from_s2 == 1


# ---------------------------------------------------------------------------
# WakeRulesWindow.to_window_config
# ---------------------------------------------------------------------------


class TestWakeRulesWindowToWindowConfig:
    def test_to_window_config_basic(self) -> None:
        import datetime

        w = WakeRulesWindow.model_validate(
            {
                "weekday": {"start": "06:00", "end": "11:00"},
                "weekend": {"start": "08:00", "end": "14:00"},
            }
        )
        wc = w.to_window_config()
        assert wc.weekday_start == datetime.time(6, 0)
        assert wc.weekday_end == datetime.time(11, 0)
        assert wc.weekend_start == datetime.time(8, 0)
        assert wc.weekend_end == datetime.time(14, 0)
        assert wc.tick_interval_min == 5

    def test_to_window_config_with_holidays(self) -> None:
        import datetime

        w = WakeRulesWindow.model_validate(
            {
                "weekday": {"start": "06:00", "end": "11:00"},
                "weekend": {"start": "08:00", "end": "14:00"},
                "holidays_as_weekend": ["2026-12-25", "2026-01-01"],
            }
        )
        wc = w.to_window_config()
        assert datetime.date(2026, 12, 25) in wc.holidays
        assert datetime.date(2026, 1, 1) in wc.holidays


# ---------------------------------------------------------------------------
# T1.5 additions: confirm_max_gap_min and needs_pedometer
# ---------------------------------------------------------------------------


class TestConfirmMaxGapMin:
    """Tests for Thresholds.confirm_max_gap_min (T1.5 schema addition)."""

    def test_default_is_12(self) -> None:
        from chronos.wake.rules import Thresholds

        t = Thresholds.model_validate({"confirm": 70, "strong": 85, "dedupe_min": 2})
        assert t.confirm_max_gap_min == 12

    def test_explicit_value_accepted(self) -> None:
        from chronos.wake.rules import Thresholds

        t = Thresholds.model_validate(
            {"confirm": 70, "strong": 85, "dedupe_min": 2, "confirm_max_gap_min": 20}
        )
        assert t.confirm_max_gap_min == 20

    def test_lower_bound_2_accepted(self) -> None:
        from chronos.wake.rules import Thresholds

        t = Thresholds.model_validate(
            {"confirm": 70, "strong": 85, "dedupe_min": 2, "confirm_max_gap_min": 2}
        )
        assert t.confirm_max_gap_min == 2

    def test_upper_bound_60_accepted(self) -> None:
        from chronos.wake.rules import Thresholds

        t = Thresholds.model_validate(
            {"confirm": 70, "strong": 85, "dedupe_min": 2, "confirm_max_gap_min": 60}
        )
        assert t.confirm_max_gap_min == 60

    def test_below_lower_bound_rejected(self) -> None:
        from pydantic import ValidationError

        from chronos.wake.rules import Thresholds

        with pytest.raises(ValidationError):
            Thresholds.model_validate(
                {"confirm": 70, "strong": 85, "dedupe_min": 2, "confirm_max_gap_min": 1}
            )

    def test_above_upper_bound_rejected(self) -> None:
        from pydantic import ValidationError

        from chronos.wake.rules import Thresholds

        with pytest.raises(ValidationError):
            Thresholds.model_validate(
                {"confirm": 70, "strong": 85, "dedupe_min": 2, "confirm_max_gap_min": 61}
            )

    def test_confirm_max_gap_min_in_yaml(self) -> None:
        """The live YAML file must include confirm_max_gap_min: 12."""
        rules_path = Path(__file__).parents[2] / "config" / "wake_rules.yaml"
        rules = load_yaml(rules_path)
        assert rules.thresholds.confirm_max_gap_min == 12


class TestNeedsPedometer:
    """Tests for SignalSpec.needs_pedometer flag (T1.5 addition)."""

    def test_steps_10m_needs_pedometer(self) -> None:
        from chronos.wake.signals import SIGNALS

        assert SIGNALS["steps_10m"].needs_pedometer is True

    def test_walking_sec_10m_needs_pedometer(self) -> None:
        from chronos.wake.signals import SIGNALS

        assert SIGNALS["walking_sec_10m"].needs_pedometer is True

    def test_stationary_sec_10m_needs_pedometer(self) -> None:
        from chronos.wake.signals import SIGNALS

        assert SIGNALS["stationary_sec_10m"].needs_pedometer is True

    def test_steps_since_window_needs_pedometer(self) -> None:
        from chronos.wake.signals import SIGNALS

        assert SIGNALS["steps_since_window"].needs_pedometer is True

    def test_is_unlocked_no_pedometer(self) -> None:
        from chronos.wake.signals import SIGNALS

        assert SIGNALS["is_unlocked"].needs_pedometer is False

    def test_charger_disconnected_no_pedometer(self) -> None:
        from chronos.wake.signals import SIGNALS

        assert SIGNALS["charger_disconnected"].needs_pedometer is False


# ---------------------------------------------------------------------------
# Edge case 15: reweight value must exceed bonus.points for bonus rules
# ---------------------------------------------------------------------------


class TestReweightBonusConstraint:
    """Validator: for reweighted rules with bonus_consecutive, value > bonus.points."""

    def _rules_with_reweight(self, unlocked_reweight: int) -> dict[str, Any]:
        """Build a valid base dict then override the unlocked reweight value."""
        import copy

        base = copy.deepcopy(VALID_RULES)
        # Add charger_disconnect and foreground rules so the reweight keys exist.
        base["rules"] = [
            {
                "id": "unlocked",
                "signal": "is_unlocked",
                "points": [[True, 15]],
                "cap": 25,
                "bonus_consecutive": {"ticks": 2, "points": 10},
            },
            {
                "id": "foreground",
                "signal": "wake_source",
                "points": [["foreground", 40]],
                "cap": 40,
            },
        ]
        base["reweight_without_pedometer"] = {
            "unlocked": unlocked_reweight,
            "foreground": 40,
        }
        return base

    def test_reweight_value_gt_bonus_valid(self) -> None:
        """reweight_value (35) > bonus.points (10) → valid."""
        d = self._rules_with_reweight(35)
        rules = WakeRules.model_validate(d)
        assert rules.reweight_without_pedometer["unlocked"] == 35

    def test_reweight_value_equals_bonus_invalid(self) -> None:
        """reweight_value (10) == bonus.points (10) → ValidationError."""
        # The error location should reference reweight_without_pedometer.unlocked.
        d = self._rules_with_reweight(10)
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = " ".join(e["msg"] for e in errors)
        assert "reweight_without_pedometer.unlocked" in msgs

    def test_reweight_value_less_than_bonus_invalid(self) -> None:
        """reweight_value (5) < bonus.points (10) → ValidationError."""
        d = self._rules_with_reweight(5)
        with pytest.raises(ValidationError) as exc_info:
            WakeRules.model_validate(d)
        errors = exc_info.value.errors()
        msgs = " ".join(e["msg"] for e in errors)
        assert "reweight_without_pedometer.unlocked" in msgs

    def test_reweight_bonus_rule_live_yaml_valid(self) -> None:
        """The live YAML (unlocked: 35, bonus.points: 10) satisfies 35 > 10."""
        rules_path = Path(__file__).parents[2] / "config" / "wake_rules.yaml"
        rules = load_yaml(rules_path)
        unlocked_rule = next(r for r in rules.rules if r.id == "unlocked")
        rw_val = rules.reweight_without_pedometer["unlocked"]
        assert unlocked_rule.bonus_consecutive is not None
        assert rw_val > unlocked_rule.bonus_consecutive.points


@pytest.mark.parametrize("value", [-1, 0.5, float("nan"), float("inf"), True, "15"])
def test_awards_are_strict_nonnegative_integers(value: object) -> None:
    body = make_valid()
    body["rules"][0]["points"][0][1] = value
    with pytest.raises(ValidationError):
        WakeRules.model_validate(body)


@pytest.mark.parametrize("value", [-1, 0.5, True, "15"])
def test_reweights_are_strict_nonnegative_integers(value: object) -> None:
    body = make_valid()
    body["reweight_without_pedometer"] = {"steps_10m": value}
    with pytest.raises(ValidationError):
        WakeRules.model_validate(body)


def test_award_strictness_preserves_threshold_types() -> None:
    body = make_valid()
    body["rules"][0]["points"] = [[35.5, 30], [20, 18]]
    rules = WakeRules.model_validate(body)
    assert rules.rules[0].points[0][0] == 35.5
    assert rules.rules[1].points[0][0] is True
    assert rules.rules[2].points[0][0] == "foreground"
