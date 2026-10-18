"""Test data factories for Chronos-AI models.

Each ``make_<table>()`` function inserts one row into ``session`` and returns
the persisted instance.  Callers must ``session.commit()`` (or use the
``session`` as a context manager) after inserting.

All defaults use :func:`~chronos.time.utc_now_iso` and :func:`~chronos.ids.new_ulid`
so every factory is time-consistent and globally unique out of the box.

Usage::

    with Session(engine) as session:
        device = make_device(session)
        session.commit()
        rv = make_rule_version(session)
        session.commit()
        te = make_telemetry_event(session, device=device)
        session.commit()
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from chronos.ids import new_ulid
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
from chronos.time import utc_now_iso

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def make_device(session: Session, **kwargs: object) -> Device:
    """Insert and return a :class:`~chronos.models.Device` with sane defaults."""
    ts = utc_now_iso()
    token = new_ulid()
    defaults: dict[str, object] = {
        "public_id": new_ulid(),
        "name": "Allen's iPhone",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "expo_push_token": None,
        "tz": "America/Los_Angeles",
        "app_version": "1.0.0",
        "ios_version": "17.5",
        "model": "iPhone 15 Pro",
        "status": "active",
        "created_at": ts,
        "last_seen_at": ts,
        "revoked_at": None,
    }
    defaults.update(kwargs)
    device = Device.model_validate(defaults)
    session.add(device)
    session.flush()
    return device


def make_pairing_code(session: Session, **kwargs: object) -> PairingCode:
    """Insert and return a :class:`~chronos.models.PairingCode` with sane defaults."""
    ts = utc_now_iso()
    code = new_ulid()[:8].upper()
    defaults: dict[str, object] = {
        "code_hash": hashlib.sha256(code.encode()).hexdigest(),
        "expires_at": ts,
        "consumed_at": None,
        "attempts": 0,
    }
    defaults.update(kwargs)
    pc = PairingCode.model_validate(defaults)
    session.add(pc)
    session.flush()
    return pc


def make_rule_version(session: Session, *, active: int = 0, **kwargs: object) -> RuleVersion:
    """Insert and return a :class:`~chronos.models.RuleVersion`.

    By default ``active=0`` so multiple can coexist.  Pass ``active=1`` for
    the current version (ensure no other active row exists first).
    """
    ts = utc_now_iso()
    rules = '{"version":1}'
    defaults: dict[str, object] = {
        "version": 1,
        "source": "yaml",
        "rules_json": rules,
        "sha256": hashlib.sha256(rules.encode()).hexdigest(),
        "created_at": ts,
        "created_by": "cli",
        "active": active,
    }
    defaults.update(kwargs)
    rv = RuleVersion.model_validate(defaults)
    session.add(rv)
    session.flush()
    return rv


def make_telemetry_event(
    session: Session,
    device: Device,
    *,
    seq: int = 1,
    **kwargs: object,
) -> TelemetryEvent:
    """Insert and return a :class:`~chronos.models.TelemetryEvent`."""
    ts = utc_now_iso()
    assert device.id is not None, "device must be persisted (flush) before make_telemetry_event"
    defaults: dict[str, object] = {
        "device_id": device.id,
        "received_at": ts,
        "device_ts": ts,
        "tz": "America/Los_Angeles",
        "local_date": "2026-10-06",
        "wake_source": "foreground",
        "seq": seq,
        "steps_10m": 40,
        "steps_since_window": 200,
        "walking_sec_10m": 90,
        "stationary_sec_10m": 510,
        "walking_conf": "medium",
        "is_unlocked": 1,
        "battery_state": "unplugged",
        "battery_level": 0.75,
        "app_state": "background",
        "low_power_mode": 0,
        "pedometer_auth": "authorized",
        "tick_seq": None,
        "app_version": "1.0.0",
        "ios_version": "17.5",
        "raw_json": "{}",
    }
    defaults.update(kwargs)
    te = TelemetryEvent.model_validate(defaults)
    session.add(te)
    session.flush()
    return te


def make_wake_evaluation(
    session: Session,
    event: TelemetryEvent,
    rule_version: RuleVersion,
    **kwargs: object,
) -> WakeEvaluation:
    """Insert and return a :class:`~chronos.models.WakeEvaluation`."""
    ts = utc_now_iso()
    assert event.id is not None
    assert rule_version.id is not None
    defaults: dict[str, object] = {
        "telemetry_event_id": event.id,
        "rule_version_id": rule_version.id,
        "local_date": "2026-10-06",
        "gate": "open",
        "score": 55,
        "breakdown_json": "[]",
        "decision": "low",
        "consecutive_high": 0,
        "evaluated_at": ts,
    }
    defaults.update(kwargs)
    we = WakeEvaluation.model_validate(defaults)
    session.add(we)
    session.flush()
    return we


def make_day(
    session: Session,
    device: Device,
    *,
    local_date: str = "2026-10-06",
    **kwargs: object,
) -> Day:
    """Insert and return a :class:`~chronos.models.Day`."""
    ts = utc_now_iso()
    assert device.id is not None
    defaults: dict[str, object] = {
        "local_date": local_date,
        "device_id": device.id,
        "tz": "America/Los_Angeles",
        "kind": "weekday",
        "window_start_utc": ts,
        "window_end_utc": ts,
        "state": "idle",
        "confirmed_at": None,
        "confirm_evaluation_id": None,
        "delivered_at": None,
        "fallback_used": 0,
        "actual_wake_local": None,
        "ticks_sent": 0,
        "ticks_received": 0,
        "brief_id": None,
        "reel_id": None,
        "created_at": ts,
        "updated_at": ts,
    }
    defaults.update(kwargs)
    day = Day.model_validate(defaults)
    session.add(day)
    session.flush()
    return day


def make_day_transition(
    session: Session,
    day: Day,
    **kwargs: object,
) -> DayTransition:
    """Insert and return a :class:`~chronos.models.DayTransition`."""
    ts = utc_now_iso()
    defaults: dict[str, object] = {
        "local_date": day.local_date,
        "device_id": day.device_id,
        "from_state": "idle",
        "to_state": "monitoring",
        "cause": "scheduler_window_start",
        "caused_by_evaluation_id": None,
        "at": ts,
    }
    defaults.update(kwargs)
    dt = DayTransition.model_validate(defaults)
    session.add(dt)
    session.flush()
    return dt


def make_setting(session: Session, *, key: str = "render_mode", **kwargs: object) -> Setting:
    """Insert and return a :class:`~chronos.models.Setting`."""
    ts = utc_now_iso()
    defaults: dict[str, object] = {
        "key": key,
        "value": "standard",
        "updated_at": ts,
        "updated_by": "cli",
    }
    defaults.update(kwargs)
    setting = Setting.model_validate(defaults)
    session.add(setting)
    session.flush()
    return setting
