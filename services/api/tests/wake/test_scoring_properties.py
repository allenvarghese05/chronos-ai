"""Hypothesis property-based tests for wake/scoring.py (T1.5 step 6).

Properties:
1. score_in_range: score always in [0, 100].
2. outside_window_always_zero: device_ts before window.start_utc -> score=0, outside_window.
3. deterministic: same inputs produce identical Evaluation (model_dump comparison).
4. monotone_in_steps: increasing steps_10m only -> score non-decreasing.
5. deduped_no_consecutive_advance: deduped tick never increments consecutive_high.
6. pedometer_unavailable_zeroes_pedometer_rules: when denied, steps/walking/steps_window -> 0pts.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chronos.enums import (
    AppState,
    BatteryState,
    DayState,
    EvaluationDecision,
    PedometerAuth,
    WakeSource,
    WalkingConf,
)
from chronos.wake.rules import WakeRules, load_yaml
from chronos.wake.scoring import EvalContext, PreviousTick, evaluate
from chronos.wake.signals import Signals
from chronos.wake.window import Window, local_date_for, window_for

RULES_PATH = Path(__file__).parents[2] / "config" / "wake_rules.yaml"

_UTC = datetime.UTC
_LA = ZoneInfo("America/Los_Angeles")

# Fixed in-window base timestamp: 06:30 LA on 2026-10-06 = 13:30 UTC.
_IN_WINDOW_BASE = datetime.datetime(2026, 10, 6, 13, 30, 0, tzinfo=_UTC)
# Fixed before-window timestamp: 05:30 LA = 12:30 UTC.
_BEFORE_WINDOW = datetime.datetime(2026, 10, 6, 12, 30, 0, tzinfo=_UTC)


def _rules() -> WakeRules:
    return load_yaml(RULES_PATH)


def _window_for(ts: datetime.datetime) -> Window:
    rules = _rules()
    cfg = rules.window.to_window_config()
    ld = local_date_for(ts, _LA, cfg.day_boundary)
    return window_for(ld, _LA, cfg)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


@st.composite
def signals_st(draw: st.DrawFn, *, device_ts: datetime.datetime = _IN_WINDOW_BASE) -> Signals:
    """Generate a valid Signals instance (device_ts fixed to keep window stable)."""
    battery_level_st = st.one_of(st.none(), st.floats(0.0, 1.0, allow_nan=False))
    return Signals(
        device_ts=device_ts,
        tz="America/Los_Angeles",
        wake_source=draw(st.sampled_from(list(WakeSource))),
        steps_10m=draw(st.integers(0, 5000)),
        steps_since_window=draw(st.integers(0, 5000)),
        walking_sec_10m=draw(st.integers(0, 600)),
        walking_conf=draw(st.sampled_from(list(WalkingConf))),
        stationary_sec_10m=draw(st.integers(0, 600)),
        is_unlocked=draw(st.booleans()),
        battery_state=draw(st.sampled_from(list(BatteryState))),
        battery_level=draw(battery_level_st),
        app_state=draw(st.sampled_from(list(AppState))),
        low_power_mode=draw(st.booleans()),
        pedometer_auth=draw(st.sampled_from(list(PedometerAuth))),
        seq=draw(st.integers(0, 10000)),
        app_version="1.0.0",
        ios_version="18.0",
    )


@st.composite
def optional_prev_st(draw: st.DrawFn, ts: datetime.datetime) -> PreviousTick | None:
    """Generate an optional PreviousTick that is guaranteed to be before ts."""
    if not draw(st.booleans()):
        return None
    gap_s = draw(st.integers(10, 3600))
    prev_ts = ts - datetime.timedelta(seconds=gap_s)
    rules = _rules()
    cfg = rules.window.to_window_config()
    ld = local_date_for(prev_ts, _LA, cfg.day_boundary)
    return PreviousTick(
        device_ts=prev_ts,
        local_date=ld,
        battery_state=draw(st.sampled_from(list(BatteryState))),
        is_unlocked=draw(st.booleans()),
        score=draw(st.integers(0, 100)),
        decision=draw(st.sampled_from(list(EvaluationDecision))),
        consecutive_high=draw(st.integers(0, 10)),
    )


def _ctx(
    ts: datetime.datetime = _IN_WINDOW_BASE,
    prev: PreviousTick | None = None,
    day_state: DayState = DayState.monitoring,
) -> EvalContext:
    return EvalContext(
        window=_window_for(ts),
        day_state=day_state,
        rule_version=1,
        previous=prev,
        ticks_received_today=0,
    )


# ---------------------------------------------------------------------------
# Property 1: score in [0, 100]
# ---------------------------------------------------------------------------


@given(signals_st())
@settings(max_examples=200, deadline=None)
def test_score_in_range(signals: Signals) -> None:
    """Score is always in [0, 100] for any valid signals."""
    rules = _rules()
    ev = evaluate(signals, rules, _IN_WINDOW_BASE, _ctx())
    assert 0 <= ev.score <= 100, f"score={ev.score} out of [0, 100]"


# ---------------------------------------------------------------------------
# Property 2: outside window -> score=0, decision=outside_window
# ---------------------------------------------------------------------------


@given(signals_st(device_ts=_BEFORE_WINDOW))
@settings(max_examples=100, deadline=None)
def test_outside_window_always_zero(signals: Signals) -> None:
    """device_ts before window.start_utc -> score=0 and decision=outside_window."""
    rules = _rules()
    ctx = EvalContext(
        window=_window_for(_BEFORE_WINDOW),
        day_state=DayState.monitoring,
        rule_version=1,
        previous=None,
        ticks_received_today=0,
    )
    ev = evaluate(signals, rules, _BEFORE_WINDOW, ctx)
    assert ev.score == 0, f"Expected score=0 outside window, got {ev.score}"
    assert ev.decision == EvaluationDecision.outside_window, (
        f"Expected outside_window, got {ev.decision}"
    )


# ---------------------------------------------------------------------------
# Property 3: deterministic
# ---------------------------------------------------------------------------


@given(signals_st(), optional_prev_st(ts=_IN_WINDOW_BASE))
@settings(max_examples=100, deadline=None)
def test_deterministic(signals: Signals, prev: PreviousTick | None) -> None:
    """Same inputs produce identical Evaluation (deterministic pure function)."""
    rules = _rules()
    ctx = _ctx(prev=prev)
    ev1 = evaluate(signals, rules, _IN_WINDOW_BASE, ctx)
    ev2 = evaluate(signals, rules, _IN_WINDOW_BASE, ctx)
    assert ev1.model_dump() == ev2.model_dump(), "evaluate is not deterministic"


# ---------------------------------------------------------------------------
# Property 4: monotone in steps_10m (holding all else fixed, no penalty)
# ---------------------------------------------------------------------------


@given(
    signals_st(),
    st.integers(0, 4999),
)
@settings(max_examples=200, deadline=None)
def test_monotone_in_steps(base_signals: Signals, extra_steps: int) -> None:
    """Increasing steps_10m only -> score non-decreasing (when no penalty changes).

    We fix stationary_sec_10m=0 to ensure the stationary_dominant penalty
    cannot fire (requires stationary_sec >= 540), so the only effect is
    the steps_10m rule contribution.
    """
    rules = _rules()
    # Build a signals pair: low_steps and high_steps (same in all other fields).
    low_steps = min(base_signals.steps_10m, 5000 - extra_steps)
    high_steps = low_steps + extra_steps

    # Override: fix stationary to 0 and pedometer to authorized to isolate steps.
    low_s = base_signals.model_copy(
        update={
            "steps_10m": low_steps,
            "stationary_sec_10m": 0,
            "pedometer_auth": PedometerAuth.authorized,
        }
    )
    high_s = base_signals.model_copy(
        update={
            "steps_10m": high_steps,
            "stationary_sec_10m": 0,
            "pedometer_auth": PedometerAuth.authorized,
        }
    )

    ctx = _ctx()
    ev_low = evaluate(low_s, rules, _IN_WINDOW_BASE, ctx)
    ev_high = evaluate(high_s, rules, _IN_WINDOW_BASE, ctx)
    assert ev_high.score >= ev_low.score, (
        f"Monotonicity violated: steps_low={low_steps} score={ev_low.score} > "
        f"steps_high={high_steps} score={ev_high.score}"
    )


# ---------------------------------------------------------------------------
# Property 5: deduped tick never advances consecutive_high
# ---------------------------------------------------------------------------


@given(
    signals_st(),
    st.integers(0, 10),
)
@settings(max_examples=100, deadline=None)
def test_deduped_no_consecutive_advance(signals: Signals, prev_consecutive_high: int) -> None:
    """Deduped tick preserves (never increases) consecutive_high from prev."""
    rules = _rules()
    # Force deduplication: prev.device_ts = signals.device_ts - 30s (< dedupe_min=2min).
    prev_ts = _IN_WINDOW_BASE - datetime.timedelta(seconds=30)
    cfg = rules.window.to_window_config()
    ld = local_date_for(prev_ts, _LA, cfg.day_boundary)
    prev = PreviousTick(
        device_ts=prev_ts,
        local_date=ld,
        battery_state=BatteryState.unplugged,
        is_unlocked=signals.is_unlocked,
        score=50,
        decision=EvaluationDecision.high,
        consecutive_high=prev_consecutive_high,
    )
    ctx = _ctx(prev=prev)
    ev = evaluate(signals, rules, _IN_WINDOW_BASE, ctx)
    if ev.decision == EvaluationDecision.ignored_dedupe:
        assert ev.consecutive_high <= prev_consecutive_high, (
            f"Deduped tick advanced consecutive_high from {prev_consecutive_high} "
            f"to {ev.consecutive_high}"
        )


# ---------------------------------------------------------------------------
# Property 6: pedometer-unavailable rules yield 0 pts in breakdown
# ---------------------------------------------------------------------------


_PEDOMETER_UNAVAILABLE_VALUES = [
    PedometerAuth.denied,
    PedometerAuth.restricted,
    PedometerAuth.not_determined,
]

_PEDOMETER_RULE_IDS = {"steps_10m", "walking_10m", "steps_window"}


@pytest.mark.parametrize("auth", _PEDOMETER_UNAVAILABLE_VALUES)
@given(signals_st())
@settings(max_examples=50, deadline=None)
def test_pedometer_unavailable_zeroes_pedometer_rules(
    auth: PedometerAuth, signals: Signals
) -> None:
    """When pedometer unavailable, steps/walking/steps_window rules contribute 0 pts."""
    rules = _rules()
    # Override pedometer_auth to the unavailable value.
    s = signals.model_copy(update={"pedometer_auth": auth})
    ctx = _ctx()
    ev = evaluate(s, rules, _IN_WINDOW_BASE, ctx)
    for bd in ev.breakdown:
        if bd.rule_id in _PEDOMETER_RULE_IDS:
            assert bd.points == 0, (
                f"pedometer_auth={auth}: rule {bd.rule_id!r} gave {bd.points} pts, "
                "expected 0 when pedometer unavailable"
            )
            assert bd.note == "pedometer unavailable", (
                f"rule {bd.rule_id!r} note expected 'pedometer unavailable', got {bd.note!r}"
            )


# ---------------------------------------------------------------------------
# Property 7: pedometer-unavailable score max ≤ 100
# ---------------------------------------------------------------------------
# With reweight values unlocked=35, charger_disconnect=25, foreground=40,
# the sum is exactly 100, so score ≤ 100 holds.  This is already enforced by
# test_score_in_range; this targeted test makes the intent explicit.


@pytest.mark.parametrize("auth", _PEDOMETER_UNAVAILABLE_VALUES)
@given(signals_st())
@settings(max_examples=50, deadline=None)
def test_pedometer_unavailable_score_max_100(auth: PedometerAuth, signals: Signals) -> None:
    """When pedometer unavailable, reweighted scores never exceed 100.

    Sum of reweight caps: unlocked=35 + charger_disconnect=25 + foreground=40 = 100.
    """
    rules = _rules()
    s = signals.model_copy(update={"pedometer_auth": auth})
    ctx = _ctx()
    ev = evaluate(s, rules, _IN_WINDOW_BASE, ctx)
    assert ev.score <= 100, (
        f"pedometer_auth={auth}: score={ev.score} exceeds 100; "
        "reweight caps unlocked=35+charger=25+foreground=40 must sum to ≤ 100"
    )
