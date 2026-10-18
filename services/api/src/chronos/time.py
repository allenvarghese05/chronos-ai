"""UTC timestamp helpers for Chronos-AI (D25).

All ``*_at`` columns store UTC ISO-8601 strings with microsecond precision
and a ``Z`` suffix, e.g. ``"2026-10-06T07:12:04.000000Z"``.  Use
:func:`utc_now_iso` everywhere instead of calling ``datetime.utcnow()``
directly so the format stays consistent.
"""

from __future__ import annotations

import datetime


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with microseconds and a ``Z`` suffix.

    Example::

        >>> utc_now_iso()
        '2026-10-06T07:12:04.000000Z'

    Returns:
        A string of the form ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (microsecond precision).
        Strings returned by consecutive calls are monotonic non-decreasing and
        sort lexicographically in time order.
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc_iso(s: str) -> datetime.datetime:
    """Parse a ``Z``-suffix ISO-8601 UTC string into a tz-aware :class:`datetime`.

    Accepts both second-precision strings (``YYYY-MM-DDTHH:MM:SSZ``) and
    microsecond-precision strings produced by :func:`utc_now_iso`
    (``YYYY-MM-DDTHH:MM:SS.ffffffZ``).

    Args:
        s: A UTC ISO-8601 string ending in ``Z``, e.g.
           ``"2026-10-06T07:12:04.000000Z"`` or ``"2026-10-06T07:12:04Z"``.

    Returns:
        A :class:`datetime.datetime` with ``tzinfo=UTC``.

    Raises:
        ValueError: If ``s`` is not a valid ISO-8601 UTC string with ``Z`` suffix.
    """
    try:
        if not s.endswith("Z"):
            raise ValueError(f"Expected UTC ISO-8601 string ending in 'Z', got: {s!r}")
        # Replace trailing Z with +00:00 so fromisoformat can parse it
        # (handles both second- and microsecond-precision variants).
        return datetime.datetime.fromisoformat(s[:-1] + "+00:00")
    except AttributeError as exc:
        raise ValueError(f"Expected a string, got: {s!r}") from exc
    except ValueError:
        raise
