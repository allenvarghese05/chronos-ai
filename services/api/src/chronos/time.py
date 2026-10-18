"""UTC timestamp helpers for Chronos-AI (D25).

All ``*_at`` columns store UTC ISO-8601 strings with a ``Z`` suffix,
e.g. ``"2026-10-06T07:12:04Z"``.  Use :func:`utc_now_iso` everywhere instead
of calling ``datetime.utcnow()`` directly so the format stays consistent.
"""

from __future__ import annotations

import datetime


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix.

    Example::

        >>> utc_now_iso()
        '2026-10-06T07:12:04Z'

    Returns:
        A string of the form ``YYYY-MM-DDTHH:MM:SSZ`` (seconds precision).
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(s: str) -> datetime.datetime:
    """Parse a ``Z``-suffix ISO-8601 UTC string into a tz-aware :class:`datetime`.

    Args:
        s: A string produced by :func:`utc_now_iso`, e.g. ``"2026-10-06T07:12:04Z"``.

    Returns:
        A :class:`datetime.datetime` with ``tzinfo=UTC``.

    Raises:
        ValueError: If ``s`` is not a valid ``YYYY-MM-DDTHH:MM:SSZ`` string.
    """
    try:
        if not s.endswith("Z"):
            raise ValueError(f"Expected UTC ISO-8601 string ending in 'Z', got: {s!r}")
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    except AttributeError as exc:
        raise ValueError(f"Expected a string, got: {s!r}") from exc
    except ValueError:
        raise
