"""Golden fixture runner for wake scoring morning scenarios (T1.5 step 5).

Loads each JSON file from ``tests/fixtures/wake/mornings/``, replays the
events through :func:`~chronos.wake.scoring.evaluate` while threading
:class:`~chronos.wake.scoring.PreviousTick` between events, and asserts that
every ``expect`` field in each event matches the returned
:class:`~chronos.wake.scoring.Evaluation`.

The runner also tracks ``confirmed_at_event_index`` and compares it to
``expect_day.confirmed_at_event_index`` (or asserts ``None`` when
``expect_day.confirmed_at`` is ``null``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from chronos.enums import DayState, EvaluationDecision
from chronos.wake.rules import WakeRules, load_yaml
from chronos.wake.scoring import (
    EvalContext,
    PreviousTick,
    evaluate,
)
from chronos.wake.signals import Signals
from chronos.wake.window import local_date_for, window_for

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "wake" / "mornings"
RULES_PATH = Path(__file__).parents[2] / "config" / "wake_rules.yaml"

_ALL_FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))
assert _ALL_FIXTURES, f"No fixture files found in {FIXTURE_DIR}"


@pytest.mark.parametrize("fixture_path", _ALL_FIXTURES, ids=lambda p: p.stem)
def test_morning(fixture_path: Path) -> None:
    """Replay all events in a morning fixture and assert expectations."""
    data: dict[str, Any] = json.loads(fixture_path.read_text())
    rules: WakeRules = load_yaml(RULES_PATH)
    tz = ZoneInfo(data["tz"])
    window_cfg = rules.window.to_window_config()

    prev: PreviousTick | None = None
    ticks_today = 0
    confirmed_at_idx: int | None = None

    for i, event in enumerate(data["events"]):
        # Remove meta-keys so Signals.model_validate only sees the 17 fields.
        raw = {k: v for k, v in event.items() if not k.startswith("_") and k != "expect"}
        signals = Signals.model_validate(raw)
        device_ts = signals.device_ts
        local_date = local_date_for(device_ts, tz, window_cfg.day_boundary)
        window = window_for(local_date, tz, window_cfg)

        # State machine: once confirmed, mark as confirmed for all subsequent events.
        day_state = DayState.monitoring if confirmed_at_idx is None else DayState.confirmed

        ctx = EvalContext(
            window=window,
            day_state=day_state,
            rule_version=1,
            previous=prev,
            ticks_received_today=ticks_today,
        )

        # Use device_ts as "now" for fixture replay.
        evaluation = evaluate(signals, rules, device_ts, ctx)

        exp: dict[str, Any] = event["expect"]
        assert evaluation.gate.value == exp["gate"], (
            f"{fixture_path.stem} event {i}: gate expected={exp['gate']!r} "
            f"got={evaluation.gate.value!r}"
        )
        assert evaluation.score == exp["score"], (
            f"{fixture_path.stem} event {i}: score expected={exp['score']} got={evaluation.score}"
        )
        assert evaluation.decision.value == exp["decision"], (
            f"{fixture_path.stem} event {i}: decision expected={exp['decision']!r} "
            f"got={evaluation.decision.value!r}"
        )
        assert evaluation.consecutive_high == exp["consecutive_high"], (
            f"{fixture_path.stem} event {i}: consecutive_high expected={exp['consecutive_high']} "
            f"got={evaluation.consecutive_high}"
        )
        if "confirm_reason" in exp:
            assert evaluation.confirm_reason == exp["confirm_reason"], (
                f"{fixture_path.stem} event {i}: confirm_reason expected={exp['confirm_reason']!r} "
                f"got={evaluation.confirm_reason!r}"
            )

        # Track first confirm event.
        if evaluation.decision == EvaluationDecision.confirm and confirmed_at_idx is None:
            confirmed_at_idx = i

        # Thread context for next event (include outside-window ticks).
        prev = PreviousTick(
            device_ts=device_ts,
            local_date=local_date,
            battery_state=signals.battery_state,
            is_unlocked=signals.is_unlocked,
            score=evaluation.score,
            decision=evaluation.decision,
            consecutive_high=evaluation.consecutive_high,
        )
        ticks_today += 1

    # Verify day-level expectations.
    if "expect_day" in data:
        exp_day: dict[str, Any] = data["expect_day"]
        if "confirmed_at_event_index" in exp_day:
            assert confirmed_at_idx == exp_day["confirmed_at_event_index"], (
                f"{fixture_path.stem}: confirmed_at_event_index expected="
                f"{exp_day['confirmed_at_event_index']} got={confirmed_at_idx}"
            )
        elif exp_day.get("confirmed_at") is None:
            assert confirmed_at_idx is None, (
                f"{fixture_path.stem}: expected no confirm, but got "
                f"confirmed_at_event_index={confirmed_at_idx}"
            )
