"""Tests for chronos.wake.window — window and local-day computation (T1.3).

Covers:
* 17 concrete parametrised cases from the brief (boundary, DST, holidays, ticks).
* 3 Hypothesis property tests (monotonicity, start<end, tick spacing).

All instants are UTC-aware; naive inputs trigger ValueError.
Clocks are not frozen here — all cases use explicit fixed UTC values so
time-machine is not needed (pure arithmetic).
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st
from pydantic import ValidationError

from chronos.enums import DayKind
from chronos.wake.window import (
    Window,
    WindowConfig,
    day_kind,
    is_in_window,
    local_date_for,
    next_window_start,
    tick_instants,
    window_for,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")  # UTC+5:30, no DST
NY = ZoneInfo("America/New_York")  # EST/EDT, DST transitions

UTC = datetime.UTC


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime.datetime:
    """Build a UTC-aware datetime."""
    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _cfg(
    *,
    weekday_start: datetime.time = datetime.time(6, 0),
    weekday_end: datetime.time = datetime.time(11, 0),
    weekend_start: datetime.time = datetime.time(8, 0),
    weekend_end: datetime.time = datetime.time(14, 0),
    day_boundary: datetime.time = datetime.time(3, 0),
    holidays: frozenset[datetime.date] = frozenset(),
    tick_interval_min: int = 5,
) -> WindowConfig:
    return WindowConfig(
        weekday_start=weekday_start,
        weekday_end=weekday_end,
        weekend_start=weekend_start,
        weekend_end=weekend_end,
        day_boundary=day_boundary,
        holidays=holidays,
        tick_interval_min=tick_interval_min,
    )


# Default config used in most tests
DEFAULT_CFG = _cfg()


# ---------------------------------------------------------------------------
# Step 1 — WindowConfig and Window model tests
# ---------------------------------------------------------------------------


class TestWindowConfig:
    def test_defaults(self) -> None:
        cfg = WindowConfig(
            weekday_start=datetime.time(6, 0),
            weekday_end=datetime.time(11, 0),
            weekend_start=datetime.time(8, 0),
            weekend_end=datetime.time(14, 0),
        )
        assert cfg.day_boundary == datetime.time(3, 0)
        assert cfg.tick_interval_min == 5
        assert cfg.holidays == frozenset()

    def test_invalid_weekday_range(self) -> None:
        with pytest.raises(ValidationError):
            WindowConfig(
                weekday_start=datetime.time(11, 0),
                weekday_end=datetime.time(6, 0),
                weekend_start=datetime.time(8, 0),
                weekend_end=datetime.time(14, 0),
            )

    def test_weekday_equal_raises(self) -> None:
        with pytest.raises(ValidationError):
            WindowConfig(
                weekday_start=datetime.time(6, 0),
                weekday_end=datetime.time(6, 0),
                weekend_start=datetime.time(8, 0),
                weekend_end=datetime.time(14, 0),
            )

    def test_invalid_weekend_range(self) -> None:
        with pytest.raises(ValidationError):
            WindowConfig(
                weekday_start=datetime.time(6, 0),
                weekday_end=datetime.time(11, 0),
                weekend_start=datetime.time(14, 0),
                weekend_end=datetime.time(8, 0),
            )

    def test_tick_interval_min_too_low(self) -> None:
        with pytest.raises(ValidationError):
            _cfg(tick_interval_min=1)

    def test_tick_interval_min_too_high(self) -> None:
        with pytest.raises(ValidationError):
            _cfg(tick_interval_min=31)

    def test_frozen(self) -> None:
        # Frozen Pydantic model: direct attribute assignment raises ValidationError.
        cfg = DEFAULT_CFG
        with pytest.raises(ValidationError):
            cfg.tick_interval_min = 10  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            WindowConfig.model_validate(  # type: ignore[call-arg]
                {
                    "weekday_start": "06:00",
                    "weekday_end": "11:00",
                    "weekend_start": "08:00",
                    "weekend_end": "14:00",
                    "unknown_field": "bad",
                }
            )


# ---------------------------------------------------------------------------
# Step 2 — local_date_for
# ---------------------------------------------------------------------------


class TestLocalDateFor:
    """All edge cases from the brief, parametrised across IST and NY."""

    # --- boundary: 02:59:59 → previous date ---
    def test_02_59_59_ist(self) -> None:
        # 2026-10-06 02:59:59 IST = 2026-10-05 21:29:59 UTC
        instant = _utc(2026, 10, 5, 21, 29, 59)
        assert local_date_for(instant, IST, datetime.time(3, 0)) == datetime.date(2026, 10, 5)

    def test_02_59_59_ny(self) -> None:
        # 2026-10-06 02:59:59 EDT (UTC-4) = 2026-10-06 06:59:59 UTC
        instant = _utc(2026, 10, 6, 6, 59, 59)
        assert local_date_for(instant, NY, datetime.time(3, 0)) == datetime.date(2026, 10, 5)

    # --- boundary: 03:00:00 → same date ---
    def test_03_00_00_ist(self) -> None:
        # 2026-10-06 03:00:00 IST = 2026-10-05 21:30:00 UTC
        instant = _utc(2026, 10, 5, 21, 30, 0)
        assert local_date_for(instant, IST, datetime.time(3, 0)) == datetime.date(2026, 10, 6)

    def test_03_00_00_ny(self) -> None:
        # 2026-10-06 03:00:00 EDT = 2026-10-06 07:00:00 UTC
        instant = _utc(2026, 10, 6, 7, 0, 0)
        assert local_date_for(instant, NY, datetime.time(3, 0)) == datetime.date(2026, 10, 6)

    # --- 23:59 → same date ---
    def test_late_evening_ist(self) -> None:
        # 2026-10-06 23:59:00 IST = 2026-10-06 18:29:00 UTC
        instant = _utc(2026, 10, 6, 18, 29, 0)
        assert local_date_for(instant, IST, datetime.time(3, 0)) == datetime.date(2026, 10, 6)

    def test_late_evening_ny(self) -> None:
        # 2026-10-06 23:59:00 EDT = 2026-10-07 03:59:00 UTC
        instant = _utc(2026, 10, 7, 3, 59, 0)
        assert local_date_for(instant, NY, datetime.time(3, 0)) == datetime.date(2026, 10, 6)

    # --- 00:30 local → previous date ---
    def test_after_midnight_before_boundary_ist(self) -> None:
        # 2026-10-07 00:30:00 IST = 2026-10-06 19:00:00 UTC
        instant = _utc(2026, 10, 6, 19, 0, 0)
        assert local_date_for(instant, IST, datetime.time(3, 0)) == datetime.date(2026, 10, 6)

    def test_after_midnight_before_boundary_ny(self) -> None:
        # 2026-10-07 00:30:00 EDT = 2026-10-07 04:30:00 UTC
        instant = _utc(2026, 10, 7, 4, 30, 0)
        assert local_date_for(instant, NY, datetime.time(3, 0)) == datetime.date(2026, 10, 6)

    # --- Brief case #1: 05:59:50 UTC, Kolkata, boundary 03:00 ---
    def test_tick_at_boundary_kolkata(self) -> None:
        # 2026-10-06 05:59:50 UTC = 11:29:50 IST — well after 03:00 boundary
        instant = _utc(2026, 10, 6, 5, 59, 50)
        assert local_date_for(instant, IST, datetime.time(3, 0)) == datetime.date(2026, 10, 6)

    # --- Brief case #2: 00:30 IST after midnight ---
    def test_after_midnight_before_boundary_brief(self) -> None:
        # 2026-10-06 19:00:00 UTC = 2026-10-07 00:30:00 IST → belongs to 2026-10-06
        instant = _utc(2026, 10, 6, 19, 0, 0)
        assert local_date_for(instant, IST, datetime.time(3, 0)) == datetime.date(2026, 10, 6)

    # --- Brief case #3: 23:59 IST ---
    def test_late_evening_brief(self) -> None:
        # 2026-10-06 18:29:00 UTC = 23:59 IST
        instant = _utc(2026, 10, 6, 18, 29, 0)
        assert local_date_for(instant, IST, datetime.time(3, 0)) == datetime.date(2026, 10, 6)

    # --- Brief case #12: Kolkata 02:00 IST before boundary ---
    def test_kolkata_before_boundary(self) -> None:
        # 2026-10-06 20:30:00 UTC = 2026-10-07 02:00:00 IST → belongs to 2026-10-06
        instant = _utc(2026, 10, 6, 20, 30, 0)
        assert local_date_for(instant, IST, datetime.time(3, 0)) == datetime.date(2026, 10, 6)

    # --- Naive datetime raises ValueError ---
    def test_naive_raises(self) -> None:
        naive = datetime.datetime(2026, 10, 6, 6, 0, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            local_date_for(naive, IST, datetime.time(3, 0))

    # --- Brief case #8: device_ts ISO offset string ---
    def test_device_ts_iso_offset(self) -> None:
        # device_ts = "2026-10-06T06:00:10+05:30" → parse to UTC-aware and call local_date_for
        device_ts = datetime.datetime.fromisoformat("2026-10-06T06:00:10+05:30")
        # Convert to UTC
        instant = device_ts.astimezone(UTC)
        assert local_date_for(instant, IST, datetime.time(3, 0)) == datetime.date(2026, 10, 6)


# ---------------------------------------------------------------------------
# Step 2 — day_kind
# ---------------------------------------------------------------------------


class TestDayKind:
    def test_weekday_monday(self) -> None:
        # 2026-10-05 is a Monday
        assert day_kind(datetime.date(2026, 10, 5), DEFAULT_CFG) == DayKind.weekday

    def test_weekday_friday(self) -> None:
        # 2026-10-09 is a Friday
        assert day_kind(datetime.date(2026, 10, 9), DEFAULT_CFG) == DayKind.weekday

    def test_weekend_saturday(self) -> None:
        # 2026-10-10 is a Saturday
        assert day_kind(datetime.date(2026, 10, 10), DEFAULT_CFG) == DayKind.weekend

    def test_weekend_sunday(self) -> None:
        # 2026-10-11 is a Sunday
        assert day_kind(datetime.date(2026, 10, 11), DEFAULT_CFG) == DayKind.weekend

    def test_holiday_on_weekday(self) -> None:
        # Brief case #9: Friday 2026-10-02 in holidays → holiday
        cfg = _cfg(holidays=frozenset({datetime.date(2026, 10, 2)}))
        assert day_kind(datetime.date(2026, 10, 2), cfg) == DayKind.holiday

    def test_holiday_on_weekend_is_weekend(self) -> None:
        # Brief case #10: Saturday 2026-10-10 in holidays → weekend (weekend beats holiday)
        cfg = _cfg(holidays=frozenset({datetime.date(2026, 10, 10)}))
        assert day_kind(datetime.date(2026, 10, 10), cfg) == DayKind.weekend

    def test_holiday_on_sunday_is_weekend(self) -> None:
        cfg = _cfg(holidays=frozenset({datetime.date(2026, 10, 11)}))
        assert day_kind(datetime.date(2026, 10, 11), cfg) == DayKind.weekend


# ---------------------------------------------------------------------------
# Step 3 — window_for
# ---------------------------------------------------------------------------


class TestWindowFor:
    # --- Brief case #4: Saturday NY weekend window ---
    def test_weekend_ny(self) -> None:
        # 2026-10-10 Saturday, NY (EDT = UTC-4)
        # 08:00 EDT = 12:00 UTC, 14:00 EDT = 18:00 UTC
        w = window_for(datetime.date(2026, 10, 10), NY, DEFAULT_CFG)
        assert w.kind == DayKind.weekend
        assert w.start_utc == _utc(2026, 10, 10, 12, 0, 0)
        assert w.end_utc == _utc(2026, 10, 10, 18, 0, 0)
        assert w.start_local == datetime.time(8, 0)
        assert w.end_local == datetime.time(14, 0)

    def test_weekday_ny(self) -> None:
        # 2026-10-06 Tuesday, NY (EDT = UTC-4)
        # 06:00 EDT = 10:00 UTC, 11:00 EDT = 15:00 UTC
        w = window_for(datetime.date(2026, 10, 6), NY, DEFAULT_CFG)
        assert w.kind == DayKind.weekday
        assert w.start_utc == _utc(2026, 10, 6, 10, 0, 0)
        assert w.end_utc == _utc(2026, 10, 6, 15, 0, 0)
        assert w.start_local == datetime.time(6, 0)
        assert w.end_local == datetime.time(11, 0)

    def test_weekday_kolkata(self) -> None:
        # 2026-10-06 Tuesday, Kolkata (IST = UTC+5:30, no DST)
        # 06:00 IST = 00:30 UTC, 11:00 IST = 05:30 UTC
        w = window_for(datetime.date(2026, 10, 6), IST, DEFAULT_CFG)
        assert w.kind == DayKind.weekday
        assert w.start_utc == _utc(2026, 10, 6, 0, 30, 0)
        assert w.end_utc == _utc(2026, 10, 6, 5, 30, 0)
        assert w.start_local == datetime.time(6, 0)
        assert w.end_local == datetime.time(11, 0)

    # --- Brief case #5: NY spring-forward 2026-03-08, 08:00 weekend start (no gap) ---
    def test_spring_forward_no_gap(self) -> None:
        # 2026-03-08 is a SUNDAY (US spring-forward is always the 2nd Sunday in March).
        # Spring-forward: 2:00 AM EST → 3:00 AM EDT.  08:00 is AFTER the gap.
        # 08:00 EDT (UTC-4) = 12:00 UTC, 14:00 EDT = 18:00 UTC
        w = window_for(datetime.date(2026, 3, 8), NY, DEFAULT_CFG)
        assert w.kind == DayKind.weekend
        assert w.start_utc == _utc(2026, 3, 8, 12, 0, 0)
        assert w.end_utc == _utc(2026, 3, 8, 18, 0, 0)
        assert w.start_local == datetime.time(8, 0)
        assert w.end_local == datetime.time(14, 0)

    # --- Brief case #6: NY spring-forward 2026-03-08, gap at 02:30 → shifts to 03:00 ---
    def test_spring_forward_gap_shifts(self) -> None:
        # 2026-03-08 is a Sunday → uses weekend_start/weekend_end.
        # weekend_start=02:30 is in the spring-forward gap (2:00-3:00 AM EST).
        # Should shift to 03:00 EDT = 07:00 UTC.
        cfg = _cfg(
            weekend_start=datetime.time(2, 30),
            weekend_end=datetime.time(11, 0),
        )
        w = window_for(datetime.date(2026, 3, 8), NY, cfg)
        assert w.start_utc == _utc(2026, 3, 8, 7, 0, 0)
        assert w.start_local == datetime.time(3, 0)
        # end (11:00 EDT = 15:00 UTC) is not in the gap
        assert w.end_utc == _utc(2026, 3, 8, 15, 0, 0)
        assert w.end_local == datetime.time(11, 0)

    # --- Brief case #7: NY fall-back 2026-11-01, 01:30 ambiguous → fold=0 (EDT = UTC-4) ---
    def test_fall_back_ambiguous_fold0(self) -> None:
        # 2026-11-01 is a SUNDAY (US fall-back is always the 1st Sunday in November).
        # Fall-back: 2:00 AM EDT → 1:00 AM EST.
        # 01:30 occurs twice: fold=0 = EDT (UTC-4) → 05:30 UTC;
        #                     fold=1 = EST (UTC-5) → 06:30 UTC
        # Owner answer: use fold=0 (earlier UTC = 05:30)
        cfg = _cfg(
            weekend_start=datetime.time(1, 30),
            weekend_end=datetime.time(11, 0),
        )
        w = window_for(datetime.date(2026, 11, 1), NY, cfg)
        assert w.start_utc == _utc(2026, 11, 1, 5, 30, 0)
        assert w.start_local == datetime.time(1, 30)
        # 11:00 AM EST (UTC-5) = 16:00 UTC (after fall-back, 11:00 is EST)
        assert w.end_utc == _utc(2026, 11, 1, 16, 0, 0)

    def test_holiday_uses_weekend_times(self) -> None:
        # Friday 2026-10-02 in holidays → DayKind.holiday, uses weekend window
        # 08:00 IST = 02:30 UTC, 14:00 IST = 08:30 UTC
        cfg = _cfg(holidays=frozenset({datetime.date(2026, 10, 2)}))
        w = window_for(datetime.date(2026, 10, 2), IST, cfg)
        assert w.kind == DayKind.holiday
        assert w.start_local == datetime.time(8, 0)
        assert w.end_local == datetime.time(14, 0)

    def test_window_local_date_and_kind_set(self) -> None:
        w = window_for(datetime.date(2026, 10, 6), NY, DEFAULT_CFG)
        assert w.local_date == datetime.date(2026, 10, 6)
        assert w.kind == DayKind.weekday


# ---------------------------------------------------------------------------
# Step 3 — is_in_window
# ---------------------------------------------------------------------------


class TestIsInWindow:
    @pytest.fixture()
    def sample_window(self) -> Window:
        # NY weekday 2026-10-06: 10:00-15:00 UTC
        return window_for(datetime.date(2026, 10, 6), NY, DEFAULT_CFG)

    def test_at_start_inclusive(self, sample_window: Window) -> None:
        # Brief case #13: at start → True
        assert is_in_window(sample_window.start_utc, sample_window) is True

    def test_at_end_exclusive(self, sample_window: Window) -> None:
        # Brief case #14: at end → False
        assert is_in_window(sample_window.end_utc, sample_window) is False

    def test_inside(self, sample_window: Window) -> None:
        mid = sample_window.start_utc + datetime.timedelta(hours=1)
        assert is_in_window(mid, sample_window) is True

    def test_before_start(self, sample_window: Window) -> None:
        before = sample_window.start_utc - datetime.timedelta(seconds=1)
        assert is_in_window(before, sample_window) is False

    def test_after_end(self, sample_window: Window) -> None:
        after = sample_window.end_utc + datetime.timedelta(seconds=1)
        assert is_in_window(after, sample_window) is False

    def test_naive_raises(self, sample_window: Window) -> None:
        naive = datetime.datetime(2026, 10, 6, 10, 0, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            is_in_window(naive, sample_window)

    # --- Brief: 05:59:50 vs 06:00:10 on 2026-10-06 in IST window ---
    def test_kolkata_boundary_05_59_50_before_window(self) -> None:
        # Kolkata weekday window: 06:00 IST = 00:30 UTC
        w = window_for(datetime.date(2026, 10, 6), IST, DEFAULT_CFG)
        # 05:59:50 UTC is 11:29:50 IST — well inside window (window closes at 05:30 UTC)
        # Let's use the correct UTC for 05:59:50 IST:
        # 05:59:50 IST = 00:29:50 UTC (just before window open at 00:30:00 UTC)
        instant_before = _utc(2026, 10, 6, 0, 29, 50)
        assert is_in_window(instant_before, w) is False

    def test_kolkata_boundary_06_00_10_inside_window(self) -> None:
        w = window_for(datetime.date(2026, 10, 6), IST, DEFAULT_CFG)
        # 06:00:10 IST = 00:30:10 UTC (just after window open at 00:30:00 UTC)
        instant_inside = _utc(2026, 10, 6, 0, 30, 10)
        assert is_in_window(instant_inside, w) is True


# ---------------------------------------------------------------------------
# Step 4 — next_window_start
# ---------------------------------------------------------------------------


class TestNextWindowStart:
    def test_before_today_window_returns_today(self) -> None:
        # 2026-10-06 05:00 UTC; NY weekday window starts at 10:00 UTC
        after = _utc(2026, 10, 6, 5, 0, 0)
        result = next_window_start(after, NY, DEFAULT_CFG)
        # 05:00 UTC = 01:00 EDT; local_date_for with boundary 03:00 → 2026-10-05
        # window_for(2026-10-05) start = 2026-10-05 10:00 UTC < after(2026-10-06 05:00) → advance
        # window_for(2026-10-06) start = 2026-10-06 10:00 UTC > after(2026-10-06 05:00) → return
        assert result == _utc(2026, 10, 6, 10, 0, 0)

    # --- Brief case #15: after window end → next day ---
    def test_after_today_window_returns_next_day(self) -> None:
        # 2026-10-06 18:01 UTC; NY weekday window ends at 15:00 UTC
        after = _utc(2026, 10, 6, 18, 1, 0)
        result = next_window_start(after, NY, DEFAULT_CFG)
        # Next day 2026-10-07 is a Wednesday; window starts at 10:00 UTC
        assert result == _utc(2026, 10, 7, 10, 0, 0)

    # --- Brief case #16: during window → NEXT day (strictly after; in-progress not returned) ---
    def test_during_window_returns_next_day(self) -> None:
        # 2026-10-06 14:59 UTC; NY weekday window is 10:00-15:00 UTC
        # Today's start (10:00) is NOT strictly after 14:59 → skip to 2026-10-07
        after = _utc(2026, 10, 6, 14, 59, 0)
        result = next_window_start(after, NY, DEFAULT_CFG)
        assert result == _utc(2026, 10, 7, 10, 0, 0)

    def test_naive_raises(self) -> None:
        naive = datetime.datetime(2026, 10, 6, 10, 0, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            next_window_start(naive, NY, DEFAULT_CFG)

    def test_crosses_weekend(self) -> None:
        # After Friday window end → Saturday weekend window
        # 2026-10-09 (Friday) after 15:00 UTC → next is 2026-10-10 (Saturday) at 12:00 UTC
        after = _utc(2026, 10, 9, 15, 1, 0)
        result = next_window_start(after, NY, DEFAULT_CFG)
        assert result == _utc(2026, 10, 10, 12, 0, 0)

    def test_kolkata_before_window(self) -> None:
        # after = 2026-10-05 22:00 UTC = 2026-10-06 03:30 IST (≥ 03:00 boundary → date = 2026-10-06)
        # Kolkata weekday window on 2026-10-06 starts at 00:30 UTC (06:00 IST - 5:30)
        # 2026-10-06 00:30 UTC > 2026-10-05 22:00 UTC → next window is 2026-10-06 00:30 UTC
        after = _utc(2026, 10, 5, 22, 0, 0)
        result = next_window_start(after, IST, DEFAULT_CFG)
        assert result == _utc(2026, 10, 6, 0, 30, 0)

    # --- NEW TEST 1: after == exact window start → returns NEXT window (strictly after) ---

    def test_at_exact_window_start_ist(self) -> None:
        """after == window.start_utc is NOT strictly after → must return NEXT day's start.

        IST (UTC+5:30, no DST).
        2026-10-06 (Tuesday) window starts at 00:30 UTC (06:00 IST).
        Passing that exact instant as *after* must skip it and return 2026-10-07 00:30 UTC.
        """
        after = _utc(2026, 10, 6, 0, 30, 0)  # exact start of Tue 2026-10-06 window
        result = next_window_start(after, IST, DEFAULT_CFG)
        # 2026-10-07 is a Wednesday (weekday) → start = 06:00 IST = 00:30 UTC
        assert result == _utc(2026, 10, 7, 0, 30, 0)

    def test_at_exact_window_start_ny(self) -> None:
        """after == window.start_utc is NOT strictly after → must return NEXT day's start.

        America/New_York in EDT (UTC-4).
        2026-10-06 (Tuesday) window starts at 10:00 UTC (06:00 EDT).
        Passing that exact instant as *after* must skip it and return 2026-10-07 10:00 UTC.
        """
        after = _utc(2026, 10, 6, 10, 0, 0)  # exact start of Tue 2026-10-06 window
        result = next_window_start(after, NY, DEFAULT_CFG)
        # 2026-10-07 is a Wednesday (weekday) → start = 06:00 EDT = 10:00 UTC
        assert result == _utc(2026, 10, 7, 10, 0, 0)

    # --- NEW TEST 2: next_window_start across a holiday ---

    def test_saturday_holiday_treated_as_weekend(self) -> None:
        """Saturday in holidays → DayKind.weekend (weekend beats holiday) → uses weekend times.

        Scenario (America/New_York, EDT = UTC-4):
        - Saturday 2026-10-10 is listed in holidays.
        - Monday  2026-10-12 is listed in holidays.
        - *after* = Friday 2026-10-09 15:01 UTC (just after the weekday window end at 15:00 UTC).
        - Expected: Saturday 2026-10-10 is classified as weekend despite being in holidays,
          so it uses weekend_start = 08:00 EDT = 12:00 UTC → result = 2026-10-10 12:00 UTC.
        """
        cfg = _cfg(holidays=frozenset({datetime.date(2026, 10, 10), datetime.date(2026, 10, 12)}))
        after = _utc(2026, 10, 9, 15, 1, 0)  # Friday 2026-10-09, just past window end
        result = next_window_start(after, NY, cfg)
        # Saturday is weekend regardless of holiday membership → weekend_start 08:00 EDT = 12:00 UTC
        assert result == _utc(2026, 10, 10, 12, 0, 0)

    def test_monday_holiday_uses_weekend_times(self) -> None:
        """Monday in holidays → DayKind.holiday → uses weekend_start/weekend_end.

        Scenario (America/New_York, EDT = UTC-4):
        - Saturday 2026-10-10 is listed in holidays (still weekend, irrelevant here).
        - Monday  2026-10-12 is listed in holidays → DayKind.holiday → weekend window.
        - *after* = Sunday 2026-10-11 22:00 UTC (well after the weekend window closes at 18:00 UTC).
        - Expected: Monday 2026-10-12 uses weekend times → start = 08:00 EDT = 12:00 UTC.
        """
        cfg = _cfg(holidays=frozenset({datetime.date(2026, 10, 10), datetime.date(2026, 10, 12)}))
        after = _utc(2026, 10, 11, 22, 0, 0)  # Sunday 2026-10-11, well after window close
        result = next_window_start(after, NY, cfg)
        # Monday 2026-10-12 is a holiday → uses weekend_start 08:00 EDT = 12:00 UTC
        assert result == _utc(2026, 10, 12, 12, 0, 0)


# ---------------------------------------------------------------------------
# Step 4 — tick_instants
# ---------------------------------------------------------------------------


class TestTickInstants:
    def test_5h_window_5min_gives_60_ticks(self) -> None:
        # Brief case #17: 5-h window at 5 min = 60 ticks
        start = _utc(2026, 10, 6, 10, 0, 0)
        end = _utc(2026, 10, 6, 15, 0, 0)
        w = Window(
            local_date=datetime.date(2026, 10, 6),
            kind=DayKind.weekday,
            start_utc=start,
            end_utc=end,
            start_local=datetime.time(6, 0),
            end_local=datetime.time(11, 0),
        )
        ticks = tick_instants(w, 5)
        assert len(ticks) == 60
        assert ticks[0] == start
        assert ticks[-1] < end
        assert ticks[-1] == _utc(2026, 10, 6, 14, 55, 0)

    def test_start_inclusive(self) -> None:
        w = window_for(datetime.date(2026, 10, 6), NY, DEFAULT_CFG)
        ticks = tick_instants(w, 5)
        assert ticks[0] == w.start_utc

    def test_end_exclusive(self) -> None:
        w = window_for(datetime.date(2026, 10, 6), NY, DEFAULT_CFG)
        ticks = tick_instants(w, 5)
        for tick in ticks:
            assert tick < w.end_utc
        assert w.end_utc not in ticks

    def test_all_in_window(self) -> None:
        w = window_for(datetime.date(2026, 10, 6), NY, DEFAULT_CFG)
        ticks = tick_instants(w, 5)
        for tick in ticks:
            assert is_in_window(tick, w)

    def test_strictly_increasing(self) -> None:
        w = window_for(datetime.date(2026, 10, 6), NY, DEFAULT_CFG)
        ticks = tick_instants(w, 5)
        for i in range(len(ticks) - 1):
            assert ticks[i] < ticks[i + 1]

    def test_equally_spaced(self) -> None:
        w = window_for(datetime.date(2026, 10, 6), NY, DEFAULT_CFG)
        ticks = tick_instants(w, 5)
        delta = datetime.timedelta(minutes=5)
        for i in range(len(ticks) - 1):
            assert ticks[i + 1] - ticks[i] == delta

    def test_small_window_returns_at_least_start(self) -> None:
        # 1-h window, 5-min interval → 12 ticks
        start = _utc(2026, 10, 6, 10, 0, 0)
        end = _utc(2026, 10, 6, 11, 0, 0)
        w = Window(
            local_date=datetime.date(2026, 10, 6),
            kind=DayKind.weekday,
            start_utc=start,
            end_utc=end,
            start_local=datetime.time(6, 0),
            end_local=datetime.time(7, 0),
        )
        ticks = tick_instants(w, 5)
        assert len(ticks) == 12

    def test_interval_larger_than_window_returns_one_tick(self) -> None:
        # 5-min window, 30-min interval → only the start tick
        start = _utc(2026, 10, 6, 10, 0, 0)
        end = _utc(2026, 10, 6, 10, 5, 0)
        w = Window(
            local_date=datetime.date(2026, 10, 6),
            kind=DayKind.weekday,
            start_utc=start,
            end_utc=end,
            start_local=datetime.time(6, 0),
            end_local=datetime.time(6, 5),
        )
        ticks = tick_instants(w, 30)
        assert ticks == [start]

    # --- NEW TEST 3: 7-min interval over 5-hour weekday window → exactly 43 ticks ---

    def test_7min_interval_5h_window_43_ticks(self) -> None:
        """7-minute ticks over a 300-minute window produce exactly 43 ticks.

        300 min / 7 min = 42.857...  -> ticks at offsets 0, 7, 14, ..., 42*7 = 294 min.
        Count: 43 (indices 0 through 42).

        Window: NY weekday 2026-10-06, start = 10:00 UTC, end = 15:00 UTC.
        - first tick = 10:00 UTC (== start)
        - last  tick = 10:00 UTC + 294 min = 14:54 UTC  (< 15:00 UTC end)
        """
        start = _utc(2026, 10, 6, 10, 0, 0)  # 06:00 EDT
        end = _utc(2026, 10, 6, 15, 0, 0)  # 11:00 EDT  — 5-hour window
        w = Window(
            local_date=datetime.date(2026, 10, 6),
            kind=DayKind.weekday,
            start_utc=start,
            end_utc=end,
            start_local=datetime.time(6, 0),
            end_local=datetime.time(11, 0),
        )
        ticks = tick_instants(w, 7)
        assert len(ticks) == 43
        assert ticks[0] == start
        assert ticks[-1] == start + datetime.timedelta(minutes=42 * 7)  # 14:54 UTC
        assert ticks[-1] < end


# ---------------------------------------------------------------------------
# Step 5 — Hypothesis property tests
# ---------------------------------------------------------------------------

_ZONES = [IST, NY]
_tz_st = st.sampled_from(_ZONES)
_date_st = st.dates(
    min_value=datetime.date(2020, 1, 1),
    max_value=datetime.date(2030, 12, 31),
)
_instant_st = st.datetimes(
    min_value=datetime.datetime(2020, 1, 1),
    max_value=datetime.datetime(2030, 12, 31, 23, 59, 59),
    timezones=st.just(UTC),
)

_FIXED_CFG = _cfg()  # standard 06:00-11:00 / 08:00-14:00, no holidays


@given(t1=_instant_st, t2=_instant_st, tz=_tz_st)
@h_settings(deadline=None)
def test_local_date_for_monotone(
    t1: datetime.datetime,
    t2: datetime.datetime,
    tz: ZoneInfo,
) -> None:
    """local_date_for is monotone non-decreasing: t1 ≤ t2 ⟹ d1 ≤ d2."""
    if t1 > t2:
        t1, t2 = t2, t1
    boundary = datetime.time(3, 0)
    d1 = local_date_for(t1, tz, boundary)
    d2 = local_date_for(t2, tz, boundary)
    assert d1 <= d2


@given(local_date=_date_st, tz=_tz_st)
@h_settings(deadline=None)
def test_window_start_before_end_property(
    local_date: datetime.date,
    tz: ZoneInfo,
) -> None:
    """window.start_utc < window.end_utc always holds."""
    window = window_for(local_date, tz, _FIXED_CFG)
    assert window.start_utc < window.end_utc


@given(
    local_date=_date_st,
    tz=_tz_st,
    interval_min=st.integers(min_value=2, max_value=30),
)
@h_settings(deadline=None)
def test_tick_instants_properties(
    local_date: datetime.date,
    tz: ZoneInfo,
    interval_min: int,
) -> None:
    """tick_instants: all ticks in [start, end), strictly increasing, equally spaced."""
    cfg = _cfg(tick_interval_min=interval_min)
    window = window_for(local_date, tz, cfg)
    ticks = tick_instants(window, interval_min)
    delta = datetime.timedelta(minutes=interval_min)

    for tick in ticks:
        assert window.start_utc <= tick < window.end_utc

    for i in range(len(ticks) - 1):
        assert ticks[i] < ticks[i + 1]
        assert ticks[i + 1] - ticks[i] == delta
