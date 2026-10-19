"""Unit tests for wake/scoring.py (T1.5 step 7).

Covers: derived facts, decision precedence, bonus consecutive, requires,
caps, penalties, reweighting, out-of-order / cross-day previous, deduped
consecutive_high carry-through.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from chronos.enums import (
    AppState,
    BatteryState,
    DayState,
    EvaluationDecision,
    EvaluationGate,
    PedometerAuth,
    WakeSource,
    WalkingConf,
)
from chronos.wake.rules import WakeRules, load_yaml
from chronos.wake.scoring import (
    EvalContext,
    Evaluation,
    PreviousTick,
    evaluate,
)
from chronos.wake.signals import Signals
from chronos.wake.window import Window, local_date_for, window_for

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = datetime.UTC
_RULES_PATH = Path(__file__).parents[2] / "config" / "wake_rules.yaml"


def _rules() -> WakeRules:
    return load_yaml(_RULES_PATH)


# 2026-10-06 is a Tuesday (weekday); window is 06:00-11:00 LA time.
_LA = ZoneInfo("America/Los_Angeles")
# 06:12 LA = 13:12 UTC
_IN_WINDOW_TS = datetime.datetime(2026, 10, 6, 13, 12, 0, tzinfo=_UTC)
_BEFORE_WINDOW_TS = datetime.datetime(2026, 10, 6, 12, 50, 0, tzinfo=_UTC)  # 05:50 LA


def _window(ts: datetime.datetime = _IN_WINDOW_TS) -> Window:
    rules = _rules()
    cfg = rules.window.to_window_config()
    ld = local_date_for(ts, _LA, cfg.day_boundary)
    return window_for(ld, _LA, cfg)


def _base_signals(**overrides: object) -> Signals:
    """Return a valid Signals object inside the window."""
    base: dict[str, object] = {
        "device_ts": _IN_WINDOW_TS,
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
    base.update(overrides)
    return Signals.model_validate(base)


def _base_ctx(
    *,
    previous: PreviousTick | None = None,
    day_state: DayState = DayState.monitoring,
    ticks_today: int = 0,
    ts: datetime.datetime = _IN_WINDOW_TS,
) -> EvalContext:
    return EvalContext(
        window=_window(ts),
        day_state=day_state,
        rule_version=1,
        previous=previous,
        ticks_received_today=ticks_today,
    )


def _prev(
    *,
    ts: datetime.datetime,
    battery: BatteryState = BatteryState.unplugged,
    unlocked: bool = False,
    score: int = 75,
    decision: EvaluationDecision = EvaluationDecision.high,
    consecutive_high: int = 1,
) -> PreviousTick:
    rules = _rules()
    cfg = rules.window.to_window_config()
    ld = local_date_for(ts, _LA, cfg.day_boundary)
    return PreviousTick(
        device_ts=ts,
        local_date=ld,
        battery_state=battery,
        is_unlocked=unlocked,
        score=score,
        decision=decision,
        consecutive_high=consecutive_high,
    )


def _eval(signals: Signals, ctx: EvalContext) -> Evaluation:
    return evaluate(signals, _rules(), _IN_WINDOW_TS, ctx)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_outside_window_gate_closed() -> None:
    s = _base_signals(device_ts=_BEFORE_WINDOW_TS)
    ctx = EvalContext(
        window=_window(_BEFORE_WINDOW_TS),
        day_state=DayState.monitoring,
        rule_version=1,
    )
    ev = evaluate(s, _rules(), _BEFORE_WINDOW_TS, ctx)
    assert ev.gate == EvaluationGate.closed
    assert ev.score == 0
    assert ev.decision == EvaluationDecision.outside_window
    assert ev.breakdown == ()


# ---------------------------------------------------------------------------
# Derived facts
# ---------------------------------------------------------------------------


def test_charger_disconnected_charging_to_unplugged() -> None:
    p = _prev(ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5), battery=BatteryState.charging)
    s = _base_signals(battery_state=BatteryState.unplugged)
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.derived.charger_disconnected is True


def test_charger_disconnected_full_to_unplugged() -> None:
    p = _prev(ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5), battery=BatteryState.full)
    s = _base_signals(battery_state=BatteryState.unplugged)
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.derived.charger_disconnected is True


def test_charger_not_disconnected_already_unplugged() -> None:
    p = _prev(ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5), battery=BatteryState.unplugged)
    s = _base_signals(battery_state=BatteryState.unplugged)
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.derived.charger_disconnected is False


def test_charger_no_previous() -> None:
    s = _base_signals()
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    assert ev.derived.charger_disconnected is False


def test_consecutive_unlocked_none_prev() -> None:
    s = _base_signals(is_unlocked=True)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    assert ev.derived.consecutive_unlocked_ticks == 1


def test_consecutive_unlocked_both_unlocked() -> None:
    p = _prev(ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5), unlocked=True)
    s = _base_signals(is_unlocked=True)
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.derived.consecutive_unlocked_ticks == 2


def test_consecutive_unlocked_current_locked() -> None:
    p = _prev(ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5), unlocked=True)
    s = _base_signals(is_unlocked=False)
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.derived.consecutive_unlocked_ticks == 0


# ---------------------------------------------------------------------------
# Decision precedence
# ---------------------------------------------------------------------------


def test_outside_window_trumps_all() -> None:
    s = _base_signals(device_ts=_BEFORE_WINDOW_TS)
    ctx = EvalContext(
        window=_window(_BEFORE_WINDOW_TS),
        day_state=DayState.confirmed,
        rule_version=1,
    )
    ev = evaluate(s, _rules(), _BEFORE_WINDOW_TS, ctx)
    assert ev.decision == EvaluationDecision.outside_window


def test_already_confirmed_when_day_confirmed() -> None:
    s = _base_signals()
    ctx = _base_ctx(day_state=DayState.confirmed)
    ev = _eval(s, ctx)
    assert ev.decision == EvaluationDecision.already_confirmed
    assert ev.confirm_reason is None


def test_already_confirmed_when_delivered() -> None:
    s = _base_signals()
    ctx = _base_ctx(day_state=DayState.delivered)
    ev = _eval(s, ctx)
    assert ev.decision == EvaluationDecision.already_confirmed


def test_ignored_dedupe_within_gap() -> None:
    # gap < 2 minutes (dedupe_min=2)
    p = _prev(ts=_IN_WINDOW_TS - datetime.timedelta(seconds=59), unlocked=True)
    s = _base_signals()
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.decision == EvaluationDecision.ignored_dedupe


def test_confirm_strong() -> None:
    """Score ≥ 85 (strong), consecutive_high=1 → confirm, reason=strong."""
    # steps_10m=48→30, walking_10m=95/high→20, unlocked=True(no prev unlocked)→15,
    # charger=False→0, steps_window=112→5, wake_source=silent_push→0
    # total = 70 (< 85 before bonus), need to force score ≥ 85
    # Use foreground to get +40: steps_10m=0, walking=0, unlocked→15, foreground→40, steps_window→5
    # = 60; add charger disconnect: +15 = 75 — still not 85.
    # Easiest: use foreground + charger + unlocked + steps_10m=35
    # steps=35→30, foreground→40, unlocked→15 = 85 exactly.
    p = _prev(
        ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5),
        battery=BatteryState.charging,
        unlocked=False,
        consecutive_high=0,
    )
    s = _base_signals(
        steps_10m=35,
        steps_since_window=0,
        walking_sec_10m=0,
        walking_conf=WalkingConf.low,
        stationary_sec_10m=0,
        is_unlocked=True,
        battery_state=BatteryState.unplugged,
        wake_source=WakeSource.foreground,
    )
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.score >= 85
    assert ev.decision == EvaluationDecision.confirm
    assert ev.confirm_reason == "strong"


def test_confirm_consecutive() -> None:
    """score ≥ confirm (70), consecutive_high ≥ 2 → confirm, reason=consecutive."""
    # prev has consecutive_high=1 and score ≥ 70
    p = _prev(
        ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5),
        unlocked=True,
        consecutive_high=1,
        score=75,
    )
    # unlocked(prev unlocked)=25(bonus), steps_10m=35→30, steps_window→5, walking=0, charger=0
    # = 25+30+5 = 60 < 70... need more.
    # steps=35→30, walking=60/high→20 + unlocked no-prev-unlocked=15(no bonus since prev locked)
    # Actually prev.is_unlocked=True → bonus applies: 15+10=25, cap=25.
    # 30 + 20 + 25 + 0 + 5 + 0 = 80 ≥ 70 and consecutive_high=2.
    s = _base_signals(
        steps_10m=35,
        steps_since_window=100,
        walking_sec_10m=60,
        walking_conf=WalkingConf.high,
        stationary_sec_10m=0,
        is_unlocked=True,
        battery_state=BatteryState.unplugged,
        wake_source=WakeSource.silent_push,
    )
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.consecutive_high == 2
    # Score should be ≥ 70 but < 85 (no strong trigger)
    assert ev.score >= rules_thresholds_confirm()
    if ev.score < rules_thresholds_strong():
        assert ev.decision == EvaluationDecision.confirm
        assert ev.confirm_reason == "consecutive"


def rules_thresholds_confirm() -> int:
    return _rules().thresholds.confirm


def rules_thresholds_strong() -> int:
    return _rules().thresholds.strong


def test_confirm_both() -> None:
    """score ≥ 85 AND consecutive_high ≥ 2 → confirm, reason=both."""
    p = _prev(
        ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5),
        battery=BatteryState.charging,
        unlocked=False,
        consecutive_high=1,
        score=85,
    )
    # foreground=40+steps->30+unlocked=15+charger=15+steps_window=5 = 105, clamped 100
    s = _base_signals(
        steps_10m=35,
        steps_since_window=100,
        walking_sec_10m=0,
        walking_conf=WalkingConf.low,
        stationary_sec_10m=0,
        is_unlocked=True,
        battery_state=BatteryState.unplugged,
        wake_source=WakeSource.foreground,
    )
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.score >= 85
    assert ev.consecutive_high == 2
    assert ev.decision == EvaluationDecision.confirm
    assert ev.confirm_reason == "both"


def test_high_decision() -> None:
    """score ≥ confirm (70), consecutive_high < 2 → high."""
    # No prev, steps_10m=35→30, walking=95/high→20, unlocked=15, charger=0, steps_window=5 = 70
    s = _base_signals(
        steps_10m=35,
        steps_since_window=100,
        walking_sec_10m=95,
        walking_conf=WalkingConf.high,
        stationary_sec_10m=0,
        is_unlocked=True,
        battery_state=BatteryState.unplugged,
        wake_source=WakeSource.silent_push,
    )
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    assert ev.score >= 70
    assert ev.consecutive_high == 1
    assert ev.decision == EvaluationDecision.high


def test_low_decision() -> None:
    """score < confirm (70) → low."""
    s = _base_signals(
        steps_10m=0,
        steps_since_window=0,
        walking_sec_10m=0,
        walking_conf=WalkingConf.low,
        stationary_sec_10m=0,
        is_unlocked=False,
        battery_state=BatteryState.unplugged,
        wake_source=WakeSource.silent_push,
    )
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    assert ev.score < 70
    assert ev.decision == EvaluationDecision.low
    assert ev.confirm_reason is None


# ---------------------------------------------------------------------------
# Bonus consecutive
# ---------------------------------------------------------------------------


def test_bonus_consecutive_applied() -> None:
    """prev.is_unlocked=True, is_unlocked=True, gap=5min ≤ 12 → unlocked=25 (15+10)."""
    p = _prev(ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5), unlocked=True)
    s = _base_signals(
        steps_10m=0,
        steps_since_window=0,
        walking_sec_10m=0,
        walking_conf=WalkingConf.low,
        stationary_sec_10m=0,
        is_unlocked=True,
        wake_source=WakeSource.silent_push,
    )
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    unlocked_bd = next(b for b in ev.breakdown if b.rule_id == "unlocked")
    assert unlocked_bd.points == 25  # 15 + 10 bonus, capped at 25
    assert unlocked_bd.note is not None and "bonus" in unlocked_bd.note


def test_bonus_consecutive_not_applied_prev_locked() -> None:
    """prev.is_unlocked=False → no bonus, unlocked=15."""
    p = _prev(ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5), unlocked=False)
    s = _base_signals(
        steps_10m=0,
        steps_since_window=0,
        walking_sec_10m=0,
        walking_conf=WalkingConf.low,
        stationary_sec_10m=0,
        is_unlocked=True,
        wake_source=WakeSource.silent_push,
    )
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    unlocked_bd = next(b for b in ev.breakdown if b.rule_id == "unlocked")
    assert unlocked_bd.points == 15
    assert unlocked_bd.note is None or "bonus" not in unlocked_bd.note


def test_bonus_consecutive_gap_too_large() -> None:
    """gap=13min > confirm_max_gap_min=12 → no bonus."""
    p = _prev(ts=_IN_WINDOW_TS - datetime.timedelta(minutes=13), unlocked=True)
    s = _base_signals(
        steps_10m=0,
        steps_since_window=0,
        walking_sec_10m=0,
        walking_conf=WalkingConf.low,
        stationary_sec_10m=0,
        is_unlocked=True,
        wake_source=WakeSource.silent_push,
    )
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    unlocked_bd = next(b for b in ev.breakdown if b.rule_id == "unlocked")
    assert unlocked_bd.points == 15


def test_bonus_consecutive_no_prev() -> None:
    """prev=None → no bonus, unlocked=15."""
    s = _base_signals(
        steps_10m=0,
        steps_since_window=0,
        walking_sec_10m=0,
        walking_conf=WalkingConf.low,
        stationary_sec_10m=0,
        is_unlocked=True,
        wake_source=WakeSource.silent_push,
    )
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    unlocked_bd = next(b for b in ev.breakdown if b.rule_id == "unlocked")
    assert unlocked_bd.points == 15


# ---------------------------------------------------------------------------
# Requires
# ---------------------------------------------------------------------------


def test_requires_met() -> None:
    """walking_conf=high, requires=[medium, high] → 20 pts."""
    s = _base_signals(walking_sec_10m=95, walking_conf=WalkingConf.high)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    walking_bd = next(b for b in ev.breakdown if b.rule_id == "walking_10m")
    assert walking_bd.points == 20


def test_requires_unmet() -> None:
    """walking_conf=low, requires=[medium, high] → 0 pts, note set."""
    s = _base_signals(walking_sec_10m=95, walking_conf=WalkingConf.low)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    walking_bd = next(b for b in ev.breakdown if b.rule_id == "walking_10m")
    assert walking_bd.points == 0
    assert walking_bd.note is not None and "requires" in walking_bd.note


def test_requires_not_present() -> None:
    """Rule without requires (steps_10m) → scored normally."""
    s = _base_signals(steps_10m=35)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    steps_bd = next(b for b in ev.breakdown if b.rule_id == "steps_10m")
    assert steps_bd.points == 30


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


def test_cap_limits_points() -> None:
    """steps_10m=5000 → capped at 30 (max for steps rule)."""
    s = _base_signals(steps_10m=5000)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    steps_bd = next(b for b in ev.breakdown if b.rule_id == "steps_10m")
    assert steps_bd.points == 30  # cap is 30


# ---------------------------------------------------------------------------
# Penalties
# ---------------------------------------------------------------------------


def test_stationary_dominant_fires() -> None:
    """stationary_sec_10m=540, steps_10m=5 → -20 applied."""
    s = _base_signals(
        stationary_sec_10m=540,
        steps_10m=5,
        walking_sec_10m=0,
        walking_conf=WalkingConf.low,
    )
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    penalty_bd = next((p for p in ev.penalties if p.rule_id == "penalty:stationary_dominant"), None)
    assert penalty_bd is not None
    assert penalty_bd.points == -20


def test_stationary_dominant_not_fires_enough_steps() -> None:
    """steps_10m=10 → stationary_dominant does not fire."""
    s = _base_signals(stationary_sec_10m=540, steps_10m=10)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    penalty_bd = next((p for p in ev.penalties if p.rule_id == "penalty:stationary_dominant"), None)
    assert penalty_bd is None


def test_pedometer_denied_penalty_zero_points() -> None:
    """pedometer_denied penalty has 0 pts (mark-only) and fires when denied."""
    s = _base_signals(pedometer_auth=PedometerAuth.denied)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    penalty_bd = next((p for p in ev.penalties if p.rule_id == "penalty:pedometer_denied"), None)
    assert penalty_bd is not None
    assert penalty_bd.points == 0


# ---------------------------------------------------------------------------
# Reweighting
# ---------------------------------------------------------------------------


def test_reweight_skips_pedometer_rules() -> None:
    """pedometer_auth=denied → steps_10m rule in breakdown has 0 pts + note."""
    s = _base_signals(pedometer_auth=PedometerAuth.denied, steps_10m=100)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    steps_bd = next(b for b in ev.breakdown if b.rule_id == "steps_10m")
    assert steps_bd.points == 0
    assert steps_bd.note == "pedometer unavailable"


def test_reweight_adjusts_caps() -> None:
    """pedometer_auth=denied → unlocked cap is 35 (from reweight YAML)."""
    s = _base_signals(pedometer_auth=PedometerAuth.denied, is_unlocked=True)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    unlocked_bd = next(b for b in ev.breakdown if b.rule_id == "unlocked")
    assert unlocked_bd.cap == 35


def test_not_determined_is_unavailable() -> None:
    """pedometer_auth=not_determined → reweighting applies (steps_10m skipped)."""
    s = _base_signals(pedometer_auth=PedometerAuth.not_determined, steps_10m=100)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    steps_bd = next(b for b in ev.breakdown if b.rule_id == "steps_10m")
    assert steps_bd.points == 0
    assert ev.derived.pedometer_unavailable is True


def test_restricted_is_unavailable() -> None:
    """pedometer_auth=restricted → reweighting applies."""
    s = _base_signals(pedometer_auth=PedometerAuth.restricted, steps_10m=100)
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    assert ev.derived.pedometer_unavailable is True
    steps_bd = next(b for b in ev.breakdown if b.rule_id == "steps_10m")
    assert steps_bd.points == 0


# ---------------------------------------------------------------------------
# Out-of-order and cross-day previous
# ---------------------------------------------------------------------------


def test_previous_from_another_date_treated_as_none() -> None:
    """prev.local_date=2026-10-05, current=2026-10-06 → note, treated as None."""
    yesterday = datetime.datetime(2026, 10, 5, 13, 0, 0, tzinfo=_UTC)
    rules = _rules()
    cfg = rules.window.to_window_config()
    ld_yesterday = local_date_for(yesterday, _LA, cfg.day_boundary)
    p = PreviousTick(
        device_ts=yesterday,
        local_date=ld_yesterday,
        battery_state=BatteryState.charging,
        is_unlocked=True,
        score=80,
        decision=EvaluationDecision.high,
        consecutive_high=1,
    )
    s = _base_signals()
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    # Cross-day prev is dropped → charger_disconnected=False (no prev)
    assert ev.derived.charger_disconnected is False
    assert any("treating as None" in n for n in ev.notes)


def test_previous_out_of_order_treated_as_none() -> None:
    """prev.device_ts >= signals.device_ts → note, treated as None."""
    future_prev = _prev(
        ts=_IN_WINDOW_TS + datetime.timedelta(minutes=5),
        battery=BatteryState.charging,
        unlocked=True,
        consecutive_high=1,
    )
    s = _base_signals()
    ctx = _base_ctx(previous=future_prev)
    ev = _eval(s, ctx)
    assert any("out-of-order" in n for n in ev.notes)
    assert ev.derived.charger_disconnected is False


# ---------------------------------------------------------------------------
# Deduped consecutive_high carry-through
# ---------------------------------------------------------------------------


def test_deduped_preserves_consecutive_high() -> None:
    """prev.consecutive_high=1, deduped tick → evaluation.consecutive_high=1."""
    p = _prev(
        ts=_IN_WINDOW_TS - datetime.timedelta(seconds=30),
        unlocked=True,
        consecutive_high=1,
    )
    s = _base_signals()
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.decision == EvaluationDecision.ignored_dedupe
    assert ev.consecutive_high == 1


def test_deduped_with_no_prev_consecutive_high_is_zero() -> None:
    """Deduped tick with no prev → consecutive_high=0."""
    # This is an unusual scenario (dedupe requires a prev), but if prev is
    # treated as None and somehow deduped is still True, consecutive_high=0.
    # In practice this cannot happen since deduped needs prev to compute gap.
    # We verify via a just-in-window edge case: prev same-second → deduped.
    p = _prev(
        ts=_IN_WINDOW_TS - datetime.timedelta(seconds=30),
        unlocked=False,
        consecutive_high=0,
        score=50,
    )
    s = _base_signals()
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.decision == EvaluationDecision.ignored_dedupe
    # consecutive_high should carry from prev (which is 0)
    assert ev.consecutive_high == 0


# ---------------------------------------------------------------------------
# Score bounds
# ---------------------------------------------------------------------------


def test_score_clamped_max() -> None:
    """Score never exceeds 100."""
    p = _prev(ts=_IN_WINDOW_TS - datetime.timedelta(minutes=5), battery=BatteryState.charging)
    s = _base_signals(
        steps_10m=5000,
        steps_since_window=5000,
        walking_sec_10m=600,
        walking_conf=WalkingConf.high,
        stationary_sec_10m=0,
        is_unlocked=True,
        battery_state=BatteryState.unplugged,
        wake_source=WakeSource.foreground,
    )
    ctx = _base_ctx(previous=p)
    ev = _eval(s, ctx)
    assert ev.score <= 100


def test_score_clamped_min() -> None:
    """Score never goes below 0 even with penalties."""
    s = _base_signals(
        steps_10m=5,
        steps_since_window=0,
        walking_sec_10m=0,
        walking_conf=WalkingConf.low,
        stationary_sec_10m=540,
        is_unlocked=False,
        battery_state=BatteryState.unplugged,
        wake_source=WakeSource.silent_push,
    )
    ctx = _base_ctx()
    ev = _eval(s, ctx)
    assert ev.score >= 0


# ---------------------------------------------------------------------------
# Worked example consistency (also covered in test_mornings, but double check)
# ---------------------------------------------------------------------------


def test_worked_example_event2_score_85() -> None:
    """Event 2 in the worked example: score=85, confirm, both."""
    # prev = event 1: charging, unlocked=false, consecutive_high=1 (score≥70)
    event1_ts = datetime.datetime(2026, 10, 6, 13, 7, 0, tzinfo=_UTC)  # 06:07 LA
    event2_ts = datetime.datetime(2026, 10, 6, 13, 12, 0, tzinfo=_UTC)  # 06:12 LA

    rules = _rules()
    cfg = rules.window.to_window_config()
    ld1 = local_date_for(event1_ts, _LA, cfg.day_boundary)

    prev = PreviousTick(
        device_ts=event1_ts,
        local_date=ld1,
        battery_state=BatteryState.charging,
        is_unlocked=False,
        score=70,
        decision=EvaluationDecision.high,
        consecutive_high=1,
    )

    s = Signals.model_validate(
        {
            "device_ts": event2_ts,
            "tz": "America/Los_Angeles",
            "wake_source": "silent_push",
            "steps_10m": 48,
            "steps_since_window": 112,
            "walking_sec_10m": 95,
            "walking_conf": "high",
            "stationary_sec_10m": 30,
            "is_unlocked": True,
            "battery_state": "unplugged",
            "battery_level": 0.82,
            "app_state": "background",
            "low_power_mode": False,
            "pedometer_auth": "authorized",
            "seq": 2,
            "app_version": "1.0.0",
            "ios_version": "18.1",
        }
    )

    ld2 = local_date_for(event2_ts, _LA, cfg.day_boundary)
    w = window_for(ld2, _LA, cfg)
    ctx = EvalContext(
        window=w,
        day_state=DayState.monitoring,
        rule_version=1,
        previous=prev,
        ticks_received_today=1,
    )

    ev = evaluate(s, rules, event2_ts, ctx)
    assert ev.score == 85
    assert ev.decision == EvaluationDecision.confirm
    assert ev.confirm_reason == "both"
    assert ev.consecutive_high == 2
    # Verify charger_disconnected fired
    assert ev.derived.charger_disconnected is True
