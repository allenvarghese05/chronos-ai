"""Wake-engine window and local-day computation (T1.3).

Pure functions — no I/O, no DB access, no Settings import.  Every function
that accepts a ``datetime.datetime`` raises ``ValueError`` for naive inputs.

Public API
----------
- :class:`WindowConfig` — frozen Pydantic model with weekday/weekend/holiday times.
- :class:`Window` — frozen Pydantic model for a resolved day window.
- :func:`local_date_for` — derive local calendar date from a UTC instant.
- :func:`day_kind` — classify a local date (weekday / weekend / holiday).
- :func:`window_for` — compute the Window for a local date.
- :func:`is_in_window` — test membership in ``[start, end)``.
- :func:`next_window_start` — UTC start of the next window (strictly after *after*).
- :func:`tick_instants` — list of UTC tick timestamps inside a Window.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chronos.enums import DayKind

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class WindowConfig(BaseModel):
    """Immutable window configuration produced by the wake-rules YAML loader (T1.4).

    All ``*_start`` / ``*_end`` fields are naive ``datetime.time`` values (no tzinfo).
    Validators enforce that start < end for each day kind.

    Attributes:
        weekday_start: Window open time on weekdays (Mon-Fri, not in holidays).
        weekday_end: Window close time on weekdays.
        weekend_start: Window open time on weekends (Sat-Sun) and holidays.
        weekend_end: Window close time on weekends and holidays.
        day_boundary: Local time at which a new "day" begins (default 03:00).
            Instants before this time are attributed to the previous calendar date.
        holidays: Dates treated as holidays (weekday dates only; weekend dates remain
            classified as ``DayKind.weekend``).
        tick_interval_min: Interval in minutes between scheduler ticks (2-30).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    weekday_start: datetime.time
    weekday_end: datetime.time
    weekend_start: datetime.time
    weekend_end: datetime.time
    day_boundary: datetime.time = datetime.time(3, 0)
    holidays: frozenset[datetime.date] = frozenset()
    tick_interval_min: int = Field(default=5, ge=2, le=30)

    @model_validator(mode="after")
    def _check_windows(self) -> WindowConfig:
        if self.weekday_start >= self.weekday_end:
            raise ValueError(
                f"weekday_start ({self.weekday_start}) must be before "
                f"weekday_end ({self.weekday_end})"
            )
        if self.weekend_start >= self.weekend_end:
            raise ValueError(
                f"weekend_start ({self.weekend_start}) must be before "
                f"weekend_end ({self.weekend_end})"
            )
        return self


class Window(BaseModel):
    """A resolved time window for one local date.

    ``start_utc`` and ``end_utc`` are UTC-aware.
    ``start_local`` and ``end_local`` are naive wall-clock times (they reflect the
    actual resolved local time, which may differ from the configured time when a
    spring-forward DST gap shifts the boundary forward).

    The window is half-open: ``[start_utc, end_utc)``.

    Attributes:
        local_date: The local calendar date this window covers.
        kind: Classification of the date (weekday / weekend / holiday).
        start_utc: UTC-aware window open instant.
        end_utc: UTC-aware window close instant (exclusive).
        start_local: Naive local wall-clock time for the open instant.
        end_local: Naive local wall-clock time for the close instant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    local_date: datetime.date
    kind: DayKind
    start_utc: datetime.datetime
    end_utc: datetime.datetime
    start_local: datetime.time
    end_local: datetime.time


# ---------------------------------------------------------------------------
# DST helpers (private)
# ---------------------------------------------------------------------------


def _find_transition_utc(
    lo: datetime.datetime,
    hi: datetime.datetime,
    tz: ZoneInfo,
) -> datetime.datetime:
    """Binary-search for the first UTC *second* in the post-transition period.

    Invariants: *lo* is pre-transition, *hi* is post-transition.
    Returns a UTC datetime with microsecond=0 that is the exact transition second.
    Runs at most ceil(log2((hi-lo)/1 s)) ≈ 14 iterations for a 3 600-second window.
    """
    post_offset: datetime.timedelta | None = hi.astimezone(tz).utcoffset()
    while (hi - lo).total_seconds() > 1.0:
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == post_offset:
            hi = mid
        else:
            lo = mid
    # Snap to second boundary.  hi is in [T, T+1) where T is the transition second.
    result = hi.replace(microsecond=0)
    if result.astimezone(tz).utcoffset() != post_offset:  # pragma: no cover
        result += datetime.timedelta(seconds=1)  # pragma: no cover
    return result


def _resolve_local_to_utc(
    date: datetime.date,
    t: datetime.time,
    tz: ZoneInfo,
) -> datetime.datetime:
    """Convert a local (date, wall-clock time) pair to a UTC-aware datetime.

    Handles three cases:

    * **Normal time** — standard UTC conversion via fold=0.
    * **Spring-forward gap** — the configured local time does not exist; the boundary
      is shifted forward to the first valid UTC second after the gap (e.g., 02:30 on
      the NY spring-forward day shifts to 03:00 EDT = 07:00 UTC).
    * **Fall-back ambiguity** — the local time occurs twice; fold=0 selects the first
      occurrence (earlier UTC, e.g., 01:30 EDT = 05:30 UTC rather than 06:30 UTC).

    Returns:
        A UTC-aware ``datetime`` with microsecond=0.
    """
    # Build with fold=0 (first occurrence / pre-transition offset).
    local_fold0 = datetime.datetime(
        date.year,
        date.month,
        date.day,
        t.hour,
        t.minute,
        t.second,
        0,
        fold=0,
        tzinfo=tz,
    )
    utc0 = local_fold0.astimezone(datetime.UTC)

    # Detect gap: round-trip back to local; mismatched naive components → gap.
    roundtrip = utc0.astimezone(tz)
    if roundtrip.replace(tzinfo=None) != local_fold0.replace(tzinfo=None):
        # Spring-forward gap.  Use fold=1 as the lower bound for binary search:
        # utc1 < transition_utc ≤ utc0.
        local_fold1 = datetime.datetime(
            date.year,
            date.month,
            date.day,
            t.hour,
            t.minute,
            t.second,
            0,
            fold=1,
            tzinfo=tz,
        )
        utc1 = local_fold1.astimezone(datetime.UTC)
        utc0 = _find_transition_utc(utc1, utc0, tz)

    return utc0.replace(microsecond=0)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def local_date_for(
    instant: datetime.datetime,
    tz: ZoneInfo,
    boundary: datetime.time,
) -> datetime.date:
    """Return the local calendar date for a UTC instant with a custom day boundary.

    The "local date" rolls over at *boundary* local time (not midnight).  An instant
    whose local time is before *boundary* is attributed to the **previous** calendar
    date (e.g., with boundary 03:00, an instant at 02:30 local on 2026-10-07 belongs
    to the local date 2026-10-06).

    Args:
        instant: A UTC-aware ``datetime`` (``tzinfo`` must not be ``None``).
        tz: IANA timezone (``ZoneInfo`` object).
        boundary: Local wall-clock time at which a new day begins (naive).

    Returns:
        A ``datetime.date`` in the local calendar.

    Raises:
        ValueError: If *instant* is naive (``tzinfo is None``).

    Examples::

        >>> from zoneinfo import ZoneInfo
        >>> import datetime
        >>> tz = ZoneInfo("Asia/Kolkata")
        >>> # 2026-10-06 at 02:59:59 IST → attributed to 2026-10-05
        >>> instant = datetime.datetime(2026, 10, 5, 21, 29, 59, tzinfo=datetime.UTC)
        >>> local_date_for(instant, tz, datetime.time(3, 0))
        datetime.date(2026, 10, 5)
    """
    if instant.tzinfo is None:
        raise ValueError(
            "instant must be timezone-aware; got a naive datetime. "
            "Pass a UTC-aware datetime (e.g. datetime.now(tz=datetime.UTC))."
        )
    local_dt = instant.astimezone(tz)
    local_time = local_dt.time().replace(tzinfo=None)
    if local_time < boundary:
        return local_dt.date() - datetime.timedelta(days=1)
    return local_dt.date()


def day_kind(local_date: datetime.date, cfg: WindowConfig) -> DayKind:
    """Classify a local date as weekday, weekend, or holiday.

    Priority order:

    1. Saturday (``weekday() == 5``) or Sunday (``weekday() == 6``) →
       ``DayKind.weekend`` (a holiday on a weekend is still weekend).
    2. Date in ``cfg.holidays`` → ``DayKind.holiday`` (uses weekend window times).
    3. All others → ``DayKind.weekday``.

    Args:
        local_date: The local calendar date to classify.
        cfg: Window configuration with the ``holidays`` set.

    Returns:
        A ``DayKind`` enum member.

    Examples::

        >>> cfg = WindowConfig(weekday_start=..., weekday_end=...,
        ...                    weekend_start=..., weekend_end=...,
        ...                    holidays=frozenset({datetime.date(2026, 10, 2)}))
        >>> day_kind(datetime.date(2026, 10, 2), cfg)   # Friday in holidays
        <DayKind.holiday: 'holiday'>
        >>> day_kind(datetime.date(2026, 10, 10), cfg)  # Saturday
        <DayKind.weekend: 'weekend'>
    """
    if local_date.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return DayKind.weekend
    if local_date in cfg.holidays:
        return DayKind.holiday
    return DayKind.weekday


def window_for(
    local_date: datetime.date,
    tz: ZoneInfo,
    cfg: WindowConfig,
) -> Window:
    """Compute the UTC window for *local_date* in *tz* according to *cfg*.

    Boundaries are resolved with DST awareness:

    * **Spring-forward gap**: a configured boundary that does not exist is shifted
      forward to the first valid UTC instant (e.g., 02:30 → 03:00 EDT on the NY
      spring-forward day).
    * **Fall-back ambiguity**: fold=0 selects the first occurrence (earlier UTC).

    The returned ``Window.start_local`` / ``end_local`` reflect the *actual*
    resolved wall-clock times, which may differ from the configured times when
    a gap shift occurs.

    Args:
        local_date: The local calendar date.
        tz: IANA timezone.
        cfg: Window configuration.

    Returns:
        A ``Window`` with UTC-aware start/end and naive local wall-clock times.
    """
    kind = day_kind(local_date, cfg)
    if kind in (DayKind.weekend, DayKind.holiday):
        start_t, end_t = cfg.weekend_start, cfg.weekend_end
    else:
        start_t, end_t = cfg.weekday_start, cfg.weekday_end

    start_utc = _resolve_local_to_utc(local_date, start_t, tz)
    end_utc = _resolve_local_to_utc(local_date, end_t, tz)

    return Window(
        local_date=local_date,
        kind=kind,
        start_utc=start_utc,
        end_utc=end_utc,
        start_local=start_utc.astimezone(tz).time().replace(tzinfo=None),
        end_local=end_utc.astimezone(tz).time().replace(tzinfo=None),
    )


def is_in_window(instant: datetime.datetime, window: Window) -> bool:
    """Return ``True`` iff *instant* is in the half-open interval ``[start_utc, end_utc)``.

    Args:
        instant: A UTC-aware ``datetime``.
        window: A computed :class:`Window`.

    Returns:
        ``True`` if ``window.start_utc <= instant < window.end_utc``.

    Raises:
        ValueError: If *instant* is naive.
    """
    if instant.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    return window.start_utc <= instant < window.end_utc


def next_window_start(
    after: datetime.datetime,
    tz: ZoneInfo,
    cfg: WindowConfig,
) -> datetime.datetime:
    """Return the UTC start of the next window whose start is strictly after *after*.

    An in-progress window (whose start is ≤ *after*) is **not** returned; the search
    advances to the following day until it finds a window start > *after*.

    Args:
        after: A UTC-aware reference instant.
        tz: IANA timezone.
        cfg: Window configuration.

    Returns:
        UTC ``datetime`` of the next window start, strictly > *after*.

    Raises:
        ValueError: If *after* is naive.
    """
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    check_date = local_date_for(after, tz, cfg.day_boundary)
    for _ in range(400):  # safety limit; searches at most ~1 year forward
        window = window_for(check_date, tz, cfg)
        if window.start_utc > after:
            return window.start_utc
        check_date += datetime.timedelta(days=1)
    # Unreachable with any finite WindowConfig, but keeps the type-checker happy.
    raise RuntimeError(  # pragma: no cover
        "next_window_start: no window found within 400 days"
    )


def tick_instants(window: Window, interval_min: int) -> list[datetime.datetime]:
    """Generate UTC tick timestamps at *interval_min*-minute intervals within *window*.

    Ticks start at ``window.start_utc`` (inclusive) and stop before
    ``window.end_utc`` (exclusive).  All returned datetimes are UTC-aware and
    equally spaced by exactly ``timedelta(minutes=interval_min)``.

    Args:
        window: The time window (``start_utc < end_utc`` must hold).
        interval_min: Interval in minutes (positive integer).

    Returns:
        Ordered list of UTC-aware ``datetime`` objects; may contain only
        ``start_utc`` when *interval_min* ≥ window duration.
    """
    ticks: list[datetime.datetime] = []
    t = window.start_utc
    delta = datetime.timedelta(minutes=interval_min)
    while t < window.end_utc:
        ticks.append(t)
        t += delta
    return ticks
