"""Day state machine for Chronos-AI (T1.6).

Manages Day row lifecycle: creation (get_or_create_day), lazy idle→monitoring
transition when an in-window event arrives, and monitoring→confirmed on a
confirm evaluation decision.  All transitions are audited to day_transitions.

Public API
----------
- :func:`get_or_create_day`  — get or create a Day row for (device, local_date).
- :func:`apply`              — apply an Evaluation to a Day; may transition state.
- :func:`on_confirmed`       — no-op hook called after monitoring→confirmed (T1.7/T5.2).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import structlog
from sqlmodel import Session, select

from chronos.enums import DayKind, DayState, EvaluationGate, TransitionCause
from chronos.models.devices import Device
from chronos.models.wake import Day, DayTransition
from chronos.wake.rules import WakeRules
from chronos.wake.window import window_for

if TYPE_CHECKING:
    from chronos.wake.scoring import Evaluation

log = structlog.get_logger(__name__)


def get_or_create_day(
    session: Session,
    device: Device,
    local_date: datetime.date,
    tz: str,
    rules: WakeRules,
    now: datetime.datetime,
) -> Day:
    """Get or create the Day row for (device, local_date).

    If no row exists, creates one with:
    - tz: frozen to ``tz`` argument (the signal's tz, never the device row's tz).
    - kind: derived via day_kind(local_date, window_config).
    - window_start_utc, window_end_utc: computed via window_for().
    - state: idle.
    - ticks_sent/received: 0.
    - created_at, updated_at: now.

    The day's tz and window bounds are frozen at creation; a tz change
    mid-day (owner travels) does not update an existing row.

    Args:
        session:     Open SQLModel Session (caller commits).
        device:      Authenticated device row.
        local_date:  Local calendar date (derived by the caller via local_date_for).
        tz:          IANA timezone string from the Signals payload.
        rules:       Active WakeRules (provides window config and day_kind).
        now:         UTC now (injected by caller; frozen in tests).

    Returns:
        The existing or newly-created Day row (flushed, not committed).
    """
    assert device.id is not None, "device must be persisted before get_or_create_day"

    local_date_str = local_date.isoformat()
    stmt = select(Day).where(Day.local_date == local_date_str, Day.device_id == device.id)
    existing = session.exec(stmt).first()  # type: ignore[call-overload]
    if existing is not None:
        return existing

    # Compute window for the new day.
    from zoneinfo import ZoneInfo

    window_cfg = rules.window.to_window_config()
    tz_obj = ZoneInfo(tz)
    window = window_for(local_date, tz_obj, window_cfg)
    kind: DayKind = window.kind

    # Format window bounds as ISO-8601 UTC strings.
    start_str = _fmt_utc(window.start_utc)
    end_str = _fmt_utc(window.end_utc)

    day = Day(
        local_date=local_date_str,
        device_id=device.id,
        tz=tz,
        kind=kind.value,
        window_start_utc=start_str,
        window_end_utc=end_str,
        state=DayState.idle.value,
        confirmed_at=None,
        confirm_evaluation_id=None,
        delivered_at=None,
        fallback_used=0,
        actual_wake_local=None,
        ticks_sent=0,
        ticks_received=0,
        brief_id=None,
        reel_id=None,
        created_at=_fmt_utc(now),
        updated_at=_fmt_utc(now),
    )
    session.add(day)
    session.flush()
    log.info("day_created", local_date=local_date_str, device_id=device.id, tz=tz, kind=kind.value)
    return day


def apply(
    session: Session,
    day: Day,
    evaluation: Evaluation,
    now: datetime.datetime,
    *,
    evaluation_id: int,
) -> list[DayTransition]:
    """Apply an Evaluation to a Day; insert DayTransition rows as needed.

    Transition rules (in order, short-circuit after first change):
    1. If day.state is idle AND evaluation.gate is open:
       - transition idle → monitoring (cause=scheduler_window_start,
         caused_by_evaluation_id=None); then fall through to rule 2.
    2. If day.state is monitoring AND evaluation.decision is confirm:
       - transition monitoring → confirmed (cause=evaluation,
         caused_by_evaluation_id=evaluation_id).
       - Set day.confirmed_at = now.isoformat() and day.confirm_evaluation_id = evaluation_id.
       - Call on_confirmed(session, day).
    3. If day.state is confirmed, fallback, or delivered: no transition.

    Both the Day update and DayTransition insert happen in the same
    session as the caller's TelemetryEvent + WakeEvaluation inserts;
    the caller commits the whole transaction.

    Args:
        session:       Open SQLModel Session.
        day:           The Day row (already in session).
        evaluation:    The scored Evaluation from evaluate().
        now:           UTC now (injected; frozen in tests).
        evaluation_id: PK of the WakeEvaluation row just inserted.

    Returns:
        List of DayTransition rows inserted (0, 1, or 2).
    """
    from chronos.enums import EvaluationDecision

    now_str = _fmt_utc(now)
    transitions: list[DayTransition] = []

    # Rule 1: idle + open gate → monitoring.
    if day.state == DayState.idle.value and evaluation.gate == EvaluationGate.open:
        dt = _insert_transition(
            session=session,
            day=day,
            from_state=DayState.idle,
            to_state=DayState.monitoring,
            cause=TransitionCause.scheduler_window_start,
            caused_by_evaluation_id=None,
            now_str=now_str,
        )
        transitions.append(dt)
        day.state = DayState.monitoring.value
        day.updated_at = now_str
        log.info(
            "day_transition",
            local_date=day.local_date,
            device_id=day.device_id,
            from_state="idle",
            to_state="monitoring",
            cause="scheduler_window_start",
        )

    # Rule 2: monitoring + confirm → confirmed.
    if day.state == DayState.monitoring.value and evaluation.decision == EvaluationDecision.confirm:
        dt = _insert_transition(
            session=session,
            day=day,
            from_state=DayState.monitoring,
            to_state=DayState.confirmed,
            cause=TransitionCause.evaluation,
            caused_by_evaluation_id=evaluation_id,
            now_str=now_str,
        )
        transitions.append(dt)
        day.state = DayState.confirmed.value
        day.confirmed_at = now_str
        day.confirm_evaluation_id = evaluation_id
        day.updated_at = now_str
        log.info(
            "day_transition",
            local_date=day.local_date,
            device_id=day.device_id,
            from_state="monitoring",
            to_state="confirmed",
            cause="evaluation",
            evaluation_id=evaluation_id,
        )
        on_confirmed(session, day)

    return transitions


def on_confirmed(session: Session, day: Day) -> None:
    """No-op hook called after a day transitions to confirmed.

    T1.7 replaces this with the delivery trigger; T5.2 wires the render job.
    """
    log.debug("on_confirmed_noop", local_date=day.local_date, device_id=day.device_id)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _fmt_utc(dt: datetime.datetime) -> str:
    """Format a UTC-aware datetime as ISO-8601 with 'Z' suffix."""
    if dt.tzinfo is not None:
        # Convert to UTC then format.
        utc = dt.astimezone(datetime.UTC)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    # Naive — assume UTC.
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _insert_transition(
    session: Session,
    day: Day,
    from_state: DayState,
    to_state: DayState,
    cause: TransitionCause,
    caused_by_evaluation_id: int | None,
    now_str: str,
) -> DayTransition:
    """Insert a DayTransition row and flush."""
    dt = DayTransition(
        local_date=day.local_date,
        device_id=day.device_id,
        from_state=from_state.value,
        to_state=to_state.value,
        cause=cause.value,
        caused_by_evaluation_id=caused_by_evaluation_id,
        at=now_str,
    )
    session.add(dt)
    session.flush()
    return dt
