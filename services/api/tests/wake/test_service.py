"""Unit tests for wake/service.py (T1.6 step 2).

Covers: clock-skew, idempotency (identical + different payload), out-of-window,
tz-change frozen, late past-date, rollback, monitoring→confirmed two ticks.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event as sa_event
from sqlmodel import Session, create_engine, select

from chronos.enums import DayState, EvaluationDecision, EvaluationGate
from chronos.errors import ProblemException
from chronos.models.devices import Device
from chronos.models.wake import Day, RuleVersion, TelemetryEvent, WakeEvaluation
from chronos.wake.rules import WakeRules, load_yaml
from chronos.wake.service import CLOCK_SKEW_MAX_SEC, ingest_telemetry
from chronos.wake.signals import Signals

_UTC = datetime.UTC
_LA = ZoneInfo("America/Los_Angeles")
_RULES_PATH = Path(__file__).parents[2] / "config" / "wake_rules.yaml"

# 2026-10-06 Tuesday; window 06:00-11:00 LA = 13:00-18:00 UTC.
_IN_WINDOW = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)  # 06:48 LA
_BEFORE_WINDOW = datetime.datetime(2026, 10, 6, 12, 30, 0, tzinfo=_UTC)  # 05:30 LA


def _rules() -> WakeRules:
    return load_yaml(_RULES_PATH)


@pytest.fixture()
def db_session(tmp_path: Path) -> Iterator[Session]:
    db_url = f"sqlite:///{tmp_path / 'test_svc.db'}"
    ini_path = Path(__file__).parents[2] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")

    engine = create_engine(db_url, connect_args={"check_same_thread": False})

    @sa_event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: sqlite3.Connection, _record: object) -> None:
        dbapi_conn.cursor().execute("PRAGMA foreign_keys=ON")

    with Session(engine) as session:
        yield session

    engine.dispose()


def _make_device(session: Session) -> Device:
    from tests.factories import make_device

    d = make_device(session)
    session.commit()
    return d


def _make_rv(session: Session) -> RuleVersion:
    from tests.factories import make_rule_version

    rv = make_rule_version(session, active=1)
    session.commit()
    return rv


def _signals(
    *,
    device_ts: datetime.datetime = _IN_WINDOW,
    tz: str = "America/Los_Angeles",
    seq: int = 0,
    steps_10m: int = 35,
    is_unlocked: bool = True,
) -> Signals:
    return Signals.model_validate(
        {
            "device_ts": device_ts,
            "tz": tz,
            "wake_source": "silent_push",
            "steps_10m": steps_10m,
            "steps_since_window": 120,
            "walking_sec_10m": 95,
            "walking_conf": "high",
            "stationary_sec_10m": 30,
            "is_unlocked": is_unlocked,
            "battery_state": "unplugged",
            "battery_level": 0.78,
            "app_state": "background",
            "low_power_mode": False,
            "pedometer_auth": "authorized",
            "seq": seq,
            "app_version": "1.0.0",
            "ios_version": "18.1",
        }
    )


def _raw(sig: Signals) -> bytes:
    return sig.model_dump_json().encode()


# ---------------------------------------------------------------------------
# Clock-skew tests
# ---------------------------------------------------------------------------


def test_ingest_clock_skew_exactly_300_ok(db_session: Session) -> None:
    """Exactly 300 s skew is within tolerance → no exception."""
    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()

    device_ts = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)
    now = device_ts + datetime.timedelta(seconds=CLOCK_SKEW_MAX_SEC)  # +300 exactly
    sig = _signals(device_ts=device_ts, seq=1)

    result = ingest_telemetry(db_session, device, sig, _raw(sig), now, rules, rv)
    assert result.evaluation_id > 0


def test_ingest_clock_skew_301_future_raises(db_session: Session) -> None:
    """301 s in the future → 422 clock_skew, no rows written."""
    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()

    device_ts = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)
    now = device_ts + datetime.timedelta(seconds=301)
    sig = _signals(device_ts=device_ts, seq=10)

    with pytest.raises(ProblemException) as exc_info:
        ingest_telemetry(db_session, device, sig, _raw(sig), now, rules, rv)

    assert exc_info.value.status_code == 422
    assert exc_info.value.problem_code == "clock_skew"
    # server_time is a structured extension member, not embedded in detail text.
    assert exc_info.value.extensions is not None
    st = exc_info.value.extensions["server_time"]
    assert isinstance(st, str) and st.endswith("Z"), f"server_time not ISO/Z: {st!r}"
    # The detail text still describes the skew.
    assert "seconds" in exc_info.value.detail

    # No telemetry row must have been written.
    db_session.rollback()
    rows = list(  # type: ignore[call-overload]
        db_session.exec(select(TelemetryEvent).where(TelemetryEvent.device_id == device.id))
    )
    assert rows == []


def test_ingest_clock_skew_301_past_raises(db_session: Session) -> None:
    """301 s in the past → 422 clock_skew."""
    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()

    device_ts = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)
    now = device_ts - datetime.timedelta(seconds=301)
    sig = _signals(device_ts=device_ts, seq=11)

    with pytest.raises(ProblemException) as exc_info:
        ingest_telemetry(db_session, device, sig, _raw(sig), now, rules, rv)

    assert exc_info.value.status_code == 422
    assert exc_info.value.problem_code == "clock_skew"


def test_ingest_clock_skew_no_rows_written(db_session: Session) -> None:
    """On clock_skew rejection, zero telemetry_events and wake_evaluations rows exist."""
    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()

    device_ts = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)
    now = device_ts + datetime.timedelta(seconds=400)
    sig = _signals(device_ts=device_ts, seq=12)

    with pytest.raises(ProblemException):
        ingest_telemetry(db_session, device, sig, _raw(sig), now, rules, rv)

    db_session.rollback()
    te_rows = list(  # type: ignore[call-overload]
        db_session.exec(select(TelemetryEvent).where(TelemetryEvent.device_id == device.id))
    )
    we_rows = list(  # type: ignore[call-overload]
        db_session.exec(select(WakeEvaluation))
    )
    assert te_rows == []
    assert we_rows == []


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


def test_ingest_duplicate_seq_identical_returns_first(db_session: Session) -> None:
    """Same (device_id, seq) → is_duplicate=True, no new rows."""
    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()
    now = _IN_WINDOW
    sig = _signals(seq=0)

    r1 = ingest_telemetry(db_session, device, sig, _raw(sig), now, rules, rv)
    assert not r1.is_duplicate

    r2 = ingest_telemetry(db_session, device, sig, _raw(sig), now, rules, rv)
    assert r2.is_duplicate
    assert r2.evaluation_id == r1.evaluation_id

    te_rows = list(  # type: ignore[call-overload]
        db_session.exec(select(TelemetryEvent).where(TelemetryEvent.device_id == device.id))
    )
    assert len(te_rows) == 1


def test_ingest_duplicate_seq_different_returns_first_with_warning(
    db_session: Session,
) -> None:
    """Same seq, different payload → first evaluation returned, warning logged."""

    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()
    now = _IN_WINDOW

    sig1 = _signals(seq=0, steps_10m=35)
    r1 = ingest_telemetry(db_session, device, sig1, _raw(sig1), now, rules, rv)
    assert not r1.is_duplicate

    sig2 = _signals(seq=0, steps_10m=60)  # different payload
    with patch("chronos.wake.service.log") as mock_log:
        r2 = ingest_telemetry(db_session, device, sig2, _raw(sig2), now, rules, rv)
        mock_log.warning.assert_called_once()
        warning_call = mock_log.warning.call_args
        assert "different_payload" in warning_call.args[0]

    assert r2.is_duplicate
    assert r2.evaluation_id == r1.evaluation_id


# ---------------------------------------------------------------------------
# Out-of-window / day state tests
# ---------------------------------------------------------------------------


def test_ingest_out_of_window_idle_day(db_session: Session) -> None:
    """Before-window event → gate=closed, decision=outside_window, day stays idle."""
    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()

    # 05:30 LA = before window (06:00 LA).
    device_ts = _BEFORE_WINDOW
    now = device_ts + datetime.timedelta(seconds=10)
    sig = _signals(device_ts=device_ts, seq=0)

    result = ingest_telemetry(db_session, device, sig, _raw(sig), now, rules, rv)

    assert result.evaluation.gate == EvaluationGate.closed
    assert result.evaluation.decision == EvaluationDecision.outside_window
    assert result.day_state == DayState.idle


def test_ingest_tz_change_day_frozen(db_session: Session) -> None:
    """After day created with LA tz, a tick with NY tz keeps day.tz=LA."""
    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()

    now = _IN_WINDOW
    sig1 = _signals(device_ts=_IN_WINDOW, seq=0, tz="America/Los_Angeles")
    ingest_telemetry(db_session, device, sig1, _raw(sig1), now, rules, rv)

    # Second tick with New York tz but same UTC time → same local_date LA → same day row.
    ny_ts = _IN_WINDOW  # same UTC instant
    now2 = ny_ts + datetime.timedelta(seconds=30)
    sig2 = _signals(device_ts=ny_ts, seq=1, tz="America/New_York")
    ingest_telemetry(db_session, device, sig2, _raw(sig2), now2, rules, rv)

    # The day created for 2026-10-06 LA date should keep tz=LA.
    days = list(  # type: ignore[call-overload]
        db_session.exec(
            select(Day).where(Day.device_id == device.id, Day.local_date == "2026-10-06")
        )
    )
    la_day = next((d for d in days if d.tz == "America/Los_Angeles"), None)
    assert la_day is not None, "day with tz=America/Los_Angeles should exist"


def test_ingest_late_past_date(db_session: Session) -> None:
    """A late outbox retry with a past device_ts is stored under the past local_date."""
    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()

    # Original event: 2026-10-05 LA, 07:00 UTC = within yesterday's window.
    past_ts = datetime.datetime(2026, 10, 5, 14, 0, 0, tzinfo=_UTC)  # 07:00 LA Oct 5
    # Retried "today" but device_ts is the original old value.
    # |past_ts - today_now| = 24 h -> would exceed skew limit.
    # Use a close_now near past_ts so skew check passes.
    sig = _signals(device_ts=past_ts, seq=0, tz="America/Los_Angeles")
    close_now = past_ts + datetime.timedelta(seconds=10)
    result = ingest_telemetry(db_session, device, sig, _raw(sig), close_now, rules, rv)

    assert result.local_date.isoformat() == "2026-10-05"

    te_rows = list(  # type: ignore[call-overload]
        db_session.exec(
            select(TelemetryEvent).where(
                TelemetryEvent.device_id == device.id,
                TelemetryEvent.local_date == "2026-10-05",
            )
        )
    )
    assert len(te_rows) == 1


def test_ingest_rollback_on_failure(db_session: Session) -> None:
    """If apply raises after TelemetryEvent+WakeEvaluation flush, the full transaction
    is rolled back: no te/we/day_transitions rows for that seq, ticks_received unchanged."""
    from chronos.models.wake import DayTransition

    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()
    now = _IN_WINDOW

    # ── First tick committed successfully (establishes Day with ticks_received=1). ──
    sig0 = _signals(seq=0)
    ingest_telemetry(db_session, device, sig0, _raw(sig0), now, rules, rv)

    day_before = db_session.exec(  # type: ignore[call-overload]
        select(Day).where(Day.device_id == device.id)
    ).first()
    assert day_before is not None
    ticks_before = day_before.ticks_received

    dt_count_before = len(
        list(  # type: ignore[call-overload]
            db_session.exec(select(DayTransition).where(DayTransition.device_id == device.id))
        )
    )

    # ── Second tick: apply raises AFTER TE+WE are flushed but BEFORE commit. ──
    sig1 = _signals(seq=1)
    with patch("chronos.wake.service.apply", side_effect=RuntimeError("simulated failure")):
        with pytest.raises(RuntimeError, match="simulated failure"):
            ingest_telemetry(db_session, device, sig1, _raw(sig1), now, rules, rv)

    db_session.rollback()

    # No TelemetryEvent for seq=1.
    te_seq1 = list(  # type: ignore[call-overload]
        db_session.exec(
            select(TelemetryEvent).where(
                TelemetryEvent.device_id == device.id,
                TelemetryEvent.seq == 1,
            )
        )
    )
    assert te_seq1 == [], "telemetry_events row for failed seq must be rolled back"

    # No extra WakeEvaluation rows (only the one from seq=0 remains).
    we_rows = list(  # type: ignore[call-overload]
        db_session.exec(select(WakeEvaluation))
    )
    assert len(we_rows) == 1, "wake_evaluations from failed tick must be rolled back"

    # No extra DayTransition rows beyond those from the first tick.
    dt_rows = list(  # type: ignore[call-overload]
        db_session.exec(select(DayTransition).where(DayTransition.device_id == device.id))
    )
    assert len(dt_rows) == dt_count_before, "day_transitions must not grow on rollback"

    # Day.ticks_received is unchanged from what the first tick committed.
    db_session.expire_all()
    day_after = db_session.exec(  # type: ignore[call-overload]
        select(Day).where(Day.device_id == device.id)
    ).first()
    assert day_after is not None
    assert day_after.ticks_received == ticks_before, "ticks_received must not change on rollback"


def test_ingest_monitoring_to_confirmed_two_ticks(db_session: Session) -> None:
    """Two consecutive high ticks → monitoring→confirmed with transition rows."""
    from chronos.models.wake import DayTransition

    device = _make_device(db_session)
    rv = _make_rv(db_session)
    rules = _rules()

    # Tick 1: 06:48 LA, score=70, decision=high.
    ts1 = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)
    now1 = ts1 + datetime.timedelta(seconds=5)
    sig1 = _signals(device_ts=ts1, seq=0, steps_10m=35)
    r1 = ingest_telemetry(db_session, device, sig1, _raw(sig1), now1, rules, rv)
    assert r1.evaluation.gate == EvaluationGate.open
    assert r1.evaluation.decision in {EvaluationDecision.high, EvaluationDecision.confirm}
    # Day should be monitoring now.
    assert r1.day_state in {DayState.monitoring, DayState.confirmed}

    # Tick 2: 06:53 LA (5 min later), score=80, decision=confirm.
    ts2 = ts1 + datetime.timedelta(minutes=5)
    now2 = ts2 + datetime.timedelta(seconds=5)
    sig2 = _signals(device_ts=ts2, seq=1, steps_10m=40)
    r2 = ingest_telemetry(db_session, device, sig2, _raw(sig2), now2, rules, rv)
    assert r2.evaluation.gate == EvaluationGate.open
    # Final day state should be confirmed.
    assert r2.day_state == DayState.confirmed

    # Transition audit rows.
    dt_rows = list(  # type: ignore[call-overload]
        db_session.exec(
            select(DayTransition)
            .where(DayTransition.device_id == device.id)
            .order_by(DayTransition.id)  # type: ignore[arg-type]
        )
    )
    causes = [r.cause for r in dt_rows]
    assert "scheduler_window_start" in causes
    assert "evaluation" in causes
    # Day.confirmed_at must be set.
    day_row = db_session.exec(  # type: ignore[call-overload]
        select(Day).where(Day.device_id == device.id)
    ).first()
    assert day_row is not None
    assert day_row.confirmed_at is not None
    assert day_row.confirm_evaluation_id is not None
