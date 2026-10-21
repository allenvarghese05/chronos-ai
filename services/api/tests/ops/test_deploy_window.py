"""Tests for chronos.ops.deploy_window.in_deploy_window (T0.6).

Window config from config/wake_rules.yaml:
  weekday: 06:00 - 11:00, exclusion: 05:15 - 11:00 (LEAD_MIN = 45)
  weekend: 08:00 - 14:00, exclusion: 07:15 - 14:00

All datetimes are in America/Los_Angeles unless noted.
Fixed date: 2024-01-15 (Monday = weekday), 2024-01-13 (Saturday = weekend).
"""

from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chronos.ops.deploy_window import LEAD_MIN, in_deploy_window
from chronos.wake.rules import WakeRules, load_yaml

LA = ZoneInfo("America/Los_Angeles")
RULES_PATH = Path(__file__).parents[2] / "config" / "wake_rules.yaml"


@pytest.fixture(scope="module")
def rules() -> WakeRules:
    """Load the real wake_rules.yaml (read-only, idempotent)."""
    return load_yaml(RULES_PATH)


def _la(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime.datetime:
    """Return a tz-aware LA datetime for use in in_deploy_window."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=LA)


# ---- weekday (2024-01-15, Monday) -------------------------------------------
# weekday window: 06:00-11:00; exclusion: 05:15-11:00


class TestWeekday:
    def test_at_exclusion_start(self, rules: WakeRules) -> None:
        """05:15 LA = exactly at exclusion boundary -> inside."""
        now = _la(2024, 1, 15, 5, 15)
        assert in_deploy_window(now, rules, LA) is True

    def test_inside_lead(self, rules: WakeRules) -> None:
        """05:30 LA = 30 min before window start (within 45-min lead) -> inside."""
        now = _la(2024, 1, 15, 5, 30)
        assert in_deploy_window(now, rules, LA) is True

    def test_before_exclusion(self, rules: WakeRules) -> None:
        """05:00 LA = 15 min before exclusion start -> outside."""
        now = _la(2024, 1, 15, 5, 0)
        assert in_deploy_window(now, rules, LA) is False

    def test_mid_window(self, rules: WakeRules) -> None:
        """08:30 LA = mid-window -> inside exclusion."""
        now = _la(2024, 1, 15, 8, 30)
        assert in_deploy_window(now, rules, LA) is True

    def test_at_window_end(self, rules: WakeRules) -> None:
        """11:00 LA = exactly at window end -> outside (half-open interval)."""
        now = _la(2024, 1, 15, 11, 0)
        assert in_deploy_window(now, rules, LA) is False

    def test_after_window_end(self, rules: WakeRules) -> None:
        """11:01 LA = 1 min after window end -> outside."""
        now = _la(2024, 1, 15, 11, 1)
        assert in_deploy_window(now, rules, LA) is False


# ---- weekend (2024-01-13, Saturday) -----------------------------------------
# weekend window: 08:00-14:00; exclusion: 07:15-14:00


class TestWeekend:
    def test_inside_lead(self, rules: WakeRules) -> None:
        """07:30 LA = 30 min before weekend window start -> inside exclusion."""
        now = _la(2024, 1, 13, 7, 30)
        assert in_deploy_window(now, rules, LA) is True

    def test_before_exclusion(self, rules: WakeRules) -> None:
        """07:00 LA = 15 min before weekend exclusion start -> outside."""
        now = _la(2024, 1, 13, 7, 0)
        assert in_deploy_window(now, rules, LA) is False

    def test_after_window_end(self, rules: WakeRules) -> None:
        """14:01 LA = after weekend window end -> outside."""
        now = _la(2024, 1, 13, 14, 1)
        assert in_deploy_window(now, rules, LA) is False


# ---- error handling ----------------------------------------------------------


def test_naive_now_raises(rules: WakeRules) -> None:
    """Naive datetime must raise ValueError."""
    naive = datetime.datetime(2024, 1, 15, 5, 30)
    with pytest.raises(ValueError, match="tz-aware"):
        in_deploy_window(naive, rules, LA)


# ---- constant sanity ---------------------------------------------------------


def test_lead_min_constant() -> None:
    """LEAD_MIN must be 45 (per T0.6 spec)."""
    assert LEAD_MIN == 45
