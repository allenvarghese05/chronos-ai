"""Shared EvalContext-building helpers for service.py and replay.py (T1.8).

Extracted so that both ingest_telemetry (service.py) and replay_events
(replay.py) build EvalContext and PreviousTick with structural parity — the
same helpers are called in the same order, so scoring is deterministically
reproducible from the same inputs.

Public API
----------
- :func:`make_previous_tick_from_row` — build PreviousTick from ORM rows.
- :func:`build_eval_context`          — assemble EvalContext from ingredients (no I/O).
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from chronos.enums import BatteryState, DayState, EvaluationDecision
from chronos.models.wake import TelemetryEvent, WakeEvaluation
from chronos.wake.rules import WakeRules
from chronos.wake.scoring import EvalContext, PreviousTick
from chronos.wake.signals import Signals
from chronos.wake.window import local_date_for, window_for


def make_previous_tick_from_row(
    we: WakeEvaluation,
    te: TelemetryEvent,
) -> PreviousTick:
    """Build a :class:`~chronos.wake.scoring.PreviousTick` from an ORM row pair.

    Args:
        we: The WakeEvaluation row for the previous tick.
        te: The TelemetryEvent row for the previous tick.

    Returns:
        A frozen :class:`PreviousTick` snapshot.
    """
    try:
        dt = datetime.datetime.fromisoformat(te.device_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.UTC)
    except ValueError:
        dt = datetime.datetime.now(datetime.UTC)
    return PreviousTick(
        device_ts=dt,
        local_date=datetime.date.fromisoformat(te.local_date),
        battery_state=BatteryState(te.battery_state),
        is_unlocked=bool(te.is_unlocked),
        score=we.score,
        decision=EvaluationDecision(we.decision),
        consecutive_high=we.consecutive_high,
    )


def build_eval_context(
    signals: Signals,
    rules: WakeRules,
    rule_version_number: int,
    previous: PreviousTick | None,
    ticks_received_today: int,
    day_state: DayState,
) -> EvalContext:
    """Assemble an :class:`~chronos.wake.scoring.EvalContext` from ingredients (no I/O).

    Derives the :class:`~chronos.wake.window.Window` for ``signals.device_ts``
    using ``signals.tz`` and the active rule's window config, then returns a
    frozen :class:`EvalContext`.

    Args:
        signals:               Validated phone telemetry snapshot.
        rules:                 Active rule set used to derive the window.
        rule_version_number:   Version number echoed inside the :class:`EvalContext`.
        previous:              Previous-tick snapshot, or ``None`` for the first tick.
        ticks_received_today:  Total ticks received so far today (used for
                               ``ticks_received_today`` inside derived facts).
        day_state:             Current state-machine state.

    Returns:
        A frozen :class:`EvalContext`.
    """
    tz_obj = ZoneInfo(signals.tz)
    window_cfg = rules.window.to_window_config()
    local_date = local_date_for(signals.device_ts, tz_obj, window_cfg.day_boundary)
    window = window_for(local_date, tz_obj, window_cfg)
    return EvalContext(
        window=window,
        day_state=day_state,
        rule_version=rule_version_number,
        previous=previous,
        ticks_received_today=ticks_received_today,
    )
