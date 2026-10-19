"""Unit tests for wake/state.py (T1.6 step 1).

Covers: get_or_create_day, apply() transitions, on_confirmed hook.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event as sa_event
from sqlmodel import Session, create_engine, select

from chronos.enums import (
    DayState,
    EvaluationDecision,
    EvaluationGate,
)
from chronos.models.devices import Device
from chronos.models.wake import Day, DayTransition, RuleVersion, WakeEvaluation
from chronos.wake.rules import WakeRules, load_yaml
from chronos.wake.scoring import (
    DerivedFacts,
    Evaluation,
    ThresholdsEcho,
)
from chronos.wake.state import apply, get_or_create_day, on_confirmed

_UTC = datetime.UTC
_LA = ZoneInfo("America/Los_Angeles")
_RULES_PATH = Path(__file__).parents[2] / "config" / "wake_rules.yaml"

# 2026-10-06 is a Tuesday (weekday); window is 06:00-11:00 LA = 13:00-18:00 UTC.
_NOW_IN_WINDOW = datetime.datetime(2026, 10, 6, 13, 30, 0, tzinfo=_UTC)  # 06:30 LA
_NOW_BEFORE = datetime.datetime(2026, 10, 6, 12, 30, 0, tzinfo=_UTC)  # 05:30 LA
_LOCAL_DATE = datetime.date(2026, 10, 6)
_TZ_LA = "America/Los_Angeles"


def _rules() -> WakeRules:
    return load_yaml(_RULES_PATH)


@pytest.fixture()
def db_session(tmp_path: Path) -> Iterator[Session]:
    """Create a fresh migrated SQLite DB and yield an open Session."""
    db_url = f"sqlite:///{tmp_path / 'test_state.db'}"
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


def _evaluation(
    gate: EvaluationGate,
    decision: EvaluationDecision,
    consecutive_high: int = 0,
    confirm_reason: str | None = None,
) -> Evaluation:
    rules = _rules()
    return Evaluation(
        gate=gate,
        score=50,
        decision=decision,
        confirm_reason=confirm_reason,  # type: ignore[arg-type]
        consecutive_high=consecutive_high,
        thresholds=ThresholdsEcho(
            confirm=rules.thresholds.confirm,
            strong=rules.thresholds.strong,
            confirm_max_gap_min=rules.thresholds.confirm_max_gap_min,
        ),
        rule_version=1,
        breakdown=(),
        penalties=(),
        derived=DerivedFacts(
            charger_disconnected=False,
            consecutive_unlocked_ticks=0,
            minutes_since_window_start=30,
            ticks_received_today=1,
            pedometer_unavailable=False,
        ),
        notes=(),
    )


def _make_wake_eval(
    session: Session, device: Device, rule_version: RuleVersion, *, seq: int = 999
) -> WakeEvaluation:
    from tests.factories import make_telemetry_event, make_wake_evaluation

    te = make_telemetry_event(session, device, seq=seq)
    session.commit()
    we = make_wake_evaluation(session, te, rule_version)
    session.commit()
    return we


def _get_or_create(
    session: Session,
    device: Device,
    rules: WakeRules,
    tz: str = _TZ_LA,
    now: datetime.datetime = _NOW_IN_WINDOW,
) -> Day:
    return get_or_create_day(session, device, _LOCAL_DATE, tz, rules, now)


# ---------------------------------------------------------------------------
# Tests: get_or_create_day
# ---------------------------------------------------------------------------


def test_get_or_create_day_creates_new_row(db_session: Session) -> None:
    device = _make_device(db_session)
    rules = _rules()

    _get_or_create(db_session, device, rules)
    db_session.commit()

    stmt = select(Day).where(Day.local_date == "2026-10-06", Day.device_id == device.id)
    fetched = db_session.exec(stmt).first()  # type: ignore[call-overload]
    assert fetched is not None
    assert fetched.state == "idle"
    assert fetched.tz == _TZ_LA
    assert fetched.kind == "weekday"
    assert fetched.ticks_received == 0
    assert fetched.confirmed_at is None


def test_get_or_create_day_returns_existing_row(db_session: Session) -> None:
    device = _make_device(db_session)
    rules = _rules()

    day1 = _get_or_create(db_session, device, rules)
    db_session.commit()
    day2 = _get_or_create(db_session, device, rules)
    db_session.commit()

    assert day1.local_date == day2.local_date
    assert day1.device_id == day2.device_id

    rows = list(db_session.exec(select(Day).where(Day.device_id == device.id)))  # type: ignore[call-overload]
    assert len(rows) == 1


def test_get_or_create_day_tz_frozen_on_existing(db_session: Session) -> None:
    device = _make_device(db_session)
    rules = _rules()

    _get_or_create(db_session, device, rules, tz=_TZ_LA)
    db_session.commit()

    day2 = _get_or_create(db_session, device, rules, tz="America/New_York")
    db_session.commit()

    assert day2.tz == _TZ_LA  # frozen; New_York ignored


# ---------------------------------------------------------------------------
# Tests: apply()
# ---------------------------------------------------------------------------


def test_apply_idle_closed_gate_no_transition(db_session: Session) -> None:
    device = _make_device(db_session)
    rules = _rules()
    rv = _make_rv(db_session)

    day = _get_or_create(db_session, device, rules, now=_NOW_BEFORE)
    db_session.commit()
    we = _make_wake_eval(db_session, device, rv)
    assert we.id is not None

    evaluation = _evaluation(EvaluationGate.closed, EvaluationDecision.outside_window)
    transitions = apply(db_session, day, evaluation, _NOW_BEFORE, evaluation_id=we.id)
    db_session.commit()

    assert transitions == []
    assert day.state == "idle"
    dt_rows = list(  # type: ignore[call-overload]
        db_session.exec(select(DayTransition).where(DayTransition.device_id == device.id))
    )
    assert dt_rows == []


def test_apply_idle_open_gate_becomes_monitoring(db_session: Session) -> None:
    device = _make_device(db_session)
    rules = _rules()
    rv = _make_rv(db_session)

    day = _get_or_create(db_session, device, rules)
    db_session.commit()
    we = _make_wake_eval(db_session, device, rv)
    assert we.id is not None

    evaluation = _evaluation(EvaluationGate.open, EvaluationDecision.low)
    transitions = apply(db_session, day, evaluation, _NOW_IN_WINDOW, evaluation_id=we.id)
    db_session.commit()

    assert len(transitions) == 1
    assert transitions[0].from_state == "idle"
    assert transitions[0].to_state == "monitoring"
    assert transitions[0].cause == "scheduler_window_start"
    assert transitions[0].caused_by_evaluation_id is None
    assert day.state == "monitoring"


def test_apply_monitoring_low_no_transition(db_session: Session) -> None:
    device = _make_device(db_session)
    rules = _rules()
    rv = _make_rv(db_session)

    day = _get_or_create(db_session, device, rules)
    day.state = DayState.monitoring.value
    db_session.commit()
    we = _make_wake_eval(db_session, device, rv)
    assert we.id is not None

    evaluation = _evaluation(EvaluationGate.open, EvaluationDecision.low)
    transitions = apply(db_session, day, evaluation, _NOW_IN_WINDOW, evaluation_id=we.id)
    db_session.commit()

    assert transitions == []
    assert day.state == "monitoring"


def test_apply_monitoring_confirm_becomes_confirmed(db_session: Session) -> None:
    device = _make_device(db_session)
    rules = _rules()
    rv = _make_rv(db_session)

    day = _get_or_create(db_session, device, rules)
    day.state = DayState.monitoring.value
    db_session.commit()
    we = _make_wake_eval(db_session, device, rv)
    assert we.id is not None

    evaluation = _evaluation(
        EvaluationGate.open,
        EvaluationDecision.confirm,
        consecutive_high=2,
        confirm_reason="consecutive",
    )
    transitions = apply(db_session, day, evaluation, _NOW_IN_WINDOW, evaluation_id=we.id)
    db_session.commit()

    assert len(transitions) == 1
    assert transitions[0].from_state == "monitoring"
    assert transitions[0].to_state == "confirmed"
    assert transitions[0].cause == "evaluation"
    assert transitions[0].caused_by_evaluation_id == we.id
    assert day.state == "confirmed"
    assert day.confirmed_at is not None
    assert day.confirm_evaluation_id == we.id


def test_apply_idle_then_confirm_two_transitions(db_session: Session) -> None:
    """idle + open gate + confirm → 2 audit rows in one apply call."""
    device = _make_device(db_session)
    rules = _rules()
    rv = _make_rv(db_session)

    day = _get_or_create(db_session, device, rules)
    db_session.commit()
    assert day.state == "idle"
    we = _make_wake_eval(db_session, device, rv)
    assert we.id is not None

    evaluation = _evaluation(
        EvaluationGate.open,
        EvaluationDecision.confirm,
        consecutive_high=2,
        confirm_reason="both",
    )
    transitions = apply(db_session, day, evaluation, _NOW_IN_WINDOW, evaluation_id=we.id)
    db_session.commit()

    assert len(transitions) == 2
    assert transitions[0].from_state == "idle"
    assert transitions[0].to_state == "monitoring"
    assert transitions[0].cause == "scheduler_window_start"
    assert transitions[1].from_state == "monitoring"
    assert transitions[1].to_state == "confirmed"
    assert transitions[1].cause == "evaluation"
    assert day.state == "confirmed"
    assert day.confirmed_at is not None


def test_apply_confirmed_no_further_transition(db_session: Session) -> None:
    device = _make_device(db_session)
    rules = _rules()
    rv = _make_rv(db_session)

    day = _get_or_create(db_session, device, rules)
    day.state = DayState.confirmed.value
    db_session.commit()
    we = _make_wake_eval(db_session, device, rv)
    assert we.id is not None

    transitions = apply(
        db_session,
        day,
        _evaluation(EvaluationGate.open, EvaluationDecision.confirm),
        _NOW_IN_WINDOW,
        evaluation_id=we.id,
    )
    db_session.commit()

    assert transitions == []
    assert day.state == "confirmed"


def test_apply_fallback_no_transition(db_session: Session) -> None:
    device = _make_device(db_session)
    rules = _rules()
    rv = _make_rv(db_session)

    day = _get_or_create(db_session, device, rules)
    day.state = DayState.fallback.value
    db_session.commit()
    we = _make_wake_eval(db_session, device, rv)
    assert we.id is not None

    transitions = apply(
        db_session,
        day,
        _evaluation(EvaluationGate.open, EvaluationDecision.confirm),
        _NOW_IN_WINDOW,
        evaluation_id=we.id,
    )
    db_session.commit()

    assert transitions == []
    assert day.state == "fallback"


def test_apply_delivered_no_transition(db_session: Session) -> None:
    device = _make_device(db_session)
    rules = _rules()
    rv = _make_rv(db_session)

    day = _get_or_create(db_session, device, rules)
    day.state = DayState.delivered.value
    db_session.commit()
    we = _make_wake_eval(db_session, device, rv)
    assert we.id is not None

    transitions = apply(
        db_session,
        day,
        _evaluation(EvaluationGate.open, EvaluationDecision.confirm),
        _NOW_IN_WINDOW,
        evaluation_id=we.id,
    )
    db_session.commit()

    assert transitions == []
    assert day.state == "delivered"


def test_transition_audit_rows_match_state(db_session: Session) -> None:
    """Full idle→monitoring→confirmed sequence produces correct audit rows."""
    from tests.factories import make_telemetry_event, make_wake_evaluation

    device = _make_device(db_session)
    rules = _rules()
    rv = _make_rv(db_session)

    day = _get_or_create(db_session, device, rules)
    db_session.commit()

    # Tick 1: open gate, low → idle→monitoring only.
    we1 = _make_wake_eval(db_session, device, rv, seq=1)
    assert we1.id is not None
    transitions1 = apply(
        db_session,
        day,
        _evaluation(EvaluationGate.open, EvaluationDecision.low),
        _NOW_IN_WINDOW,
        evaluation_id=we1.id,
    )
    db_session.commit()
    assert len(transitions1) == 1
    assert day.state == "monitoring"

    # Tick 2: open gate, confirm → monitoring→confirmed.
    te2 = make_telemetry_event(db_session, device, seq=2)
    db_session.commit()
    we2 = make_wake_evaluation(db_session, te2, rv, decision="confirm")
    db_session.commit()
    assert we2.id is not None
    transitions2 = apply(
        db_session,
        day,
        _evaluation(
            EvaluationGate.open,
            EvaluationDecision.confirm,
            consecutive_high=2,
            confirm_reason="consecutive",
        ),
        _NOW_IN_WINDOW,
        evaluation_id=we2.id,
    )
    db_session.commit()
    assert len(transitions2) == 1
    assert day.state == "confirmed"

    rows = list(  # type: ignore[call-overload]
        db_session.exec(
            select(DayTransition)
            .where(DayTransition.device_id == device.id)
            .order_by(DayTransition.id)  # type: ignore[arg-type]
        )
    )
    assert len(rows) == 2
    assert rows[0].from_state == "idle"
    assert rows[0].to_state == "monitoring"
    assert rows[0].cause == "scheduler_window_start"
    assert rows[0].caused_by_evaluation_id is None
    assert rows[1].from_state == "monitoring"
    assert rows[1].to_state == "confirmed"
    assert rows[1].cause == "evaluation"
    assert rows[1].caused_by_evaluation_id == we2.id


def test_on_confirmed_is_noop(db_session: Session) -> None:
    """on_confirmed does not raise and produces no side effects."""
    device = _make_device(db_session)
    rules = _rules()

    day = _get_or_create(db_session, device, rules)
    db_session.commit()

    on_confirmed(db_session, day)
    db_session.commit()

    rows = list(  # type: ignore[call-overload]
        db_session.exec(select(DayTransition).where(DayTransition.device_id == device.id))
    )
    assert rows == []
