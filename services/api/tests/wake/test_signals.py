"""Tests for the Signals pydantic model (T1.5 step 2)."""

from __future__ import annotations

import datetime

import pytest
from pydantic import ValidationError

from chronos.enums import (
    AppState,
    BatteryState,
    PedometerAuth,
    WakeSource,
    WalkingConf,
)
from chronos.wake.signals import Signals

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = datetime.UTC
_BASE_TS = datetime.datetime(2026, 10, 6, 13, 30, 0, tzinfo=_UTC)

_VALID: dict[str, object] = {
    "device_ts": _BASE_TS,
    "tz": "America/Los_Angeles",
    "wake_source": WakeSource.silent_push,
    "steps_10m": 48,
    "steps_since_window": 112,
    "walking_sec_10m": 95,
    "walking_conf": WalkingConf.high,
    "stationary_sec_10m": 30,
    "is_unlocked": True,
    "battery_state": BatteryState.unplugged,
    "battery_level": 0.82,
    "app_state": AppState.background,
    "low_power_mode": False,
    "pedometer_auth": PedometerAuth.authorized,
    "seq": 5,
    "app_version": "1.0.0",
    "ios_version": "18.1",
}


def _make(**overrides: object) -> Signals:
    return Signals.model_validate({**_VALID, **overrides})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_signals_valid() -> None:
    """Round-trip through model_validate with all 17 fields."""
    s = _make()
    assert s.steps_10m == 48
    assert s.walking_conf == WalkingConf.high
    assert s.battery_level == pytest.approx(0.82)


def test_naive_device_ts_rejected() -> None:
    """Naive datetime must raise ValidationError with 'timezone-aware' in message."""
    naive = datetime.datetime(2026, 10, 6, 13, 30, 0)  # no tzinfo
    with pytest.raises(ValidationError, match="timezone-aware"):
        _make(device_ts=naive)


def test_extra_field_rejected() -> None:
    """Extra fields must raise ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        Signals.model_validate({**_VALID, "unexpected_field": 42})


def test_steps_10m_bound_upper() -> None:
    """steps_10m=5001 must raise; 5000 must pass."""
    with pytest.raises(ValidationError):
        _make(steps_10m=5001)
    s = _make(steps_10m=5000)
    assert s.steps_10m == 5000


def test_steps_10m_bound_lower() -> None:
    """steps_10m=-1 must raise; 0 must pass."""
    with pytest.raises(ValidationError):
        _make(steps_10m=-1)
    s = _make(steps_10m=0)
    assert s.steps_10m == 0


def test_walking_sec_bound() -> None:
    """walking_sec_10m=601 must raise; 600 must pass."""
    with pytest.raises(ValidationError):
        _make(walking_sec_10m=601)
    s = _make(walking_sec_10m=600)
    assert s.walking_sec_10m == 600


def test_stationary_sec_bound() -> None:
    """stationary_sec_10m=601 must raise; 600 must pass."""
    with pytest.raises(ValidationError):
        _make(stationary_sec_10m=601)
    s = _make(stationary_sec_10m=600)
    assert s.stationary_sec_10m == 600


def test_battery_level_bounds() -> None:
    """battery_level=1.1 → error; 0.0 → OK; None → OK."""
    with pytest.raises(ValidationError):
        _make(battery_level=1.1)
    s_zero = _make(battery_level=0.0)
    assert s_zero.battery_level == pytest.approx(0.0)
    s_none = _make(battery_level=None)
    assert s_none.battery_level is None


def test_seq_bound() -> None:
    """seq=-1 must raise; 0 must pass."""
    with pytest.raises(ValidationError):
        _make(seq=-1)
    s = _make(seq=0)
    assert s.seq == 0


def test_string_enum_coercion() -> None:
    """String values must coerce to enum members."""
    s = Signals.model_validate({**_VALID, "wake_source": "foreground"})
    assert s.wake_source == WakeSource.foreground


def test_device_ts_string_coercion() -> None:
    """ISO-8601 strings with UTC offset must parse to tz-aware datetimes."""
    s = Signals.model_validate({**_VALID, "device_ts": "2026-10-06T06:12:00-07:00"})
    assert s.device_ts.tzinfo is not None
