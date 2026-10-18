"""Shared enum definitions for Chronos-AI.

Every enum here is a ``StrEnum`` (``str`` + ``enum.Enum``) so that:

- Pydantic v2 models accept enum members *and* their string values.
- SQLModel ``CheckConstraint`` builders can reference ``.value`` without
  extra conversion.
- Pyright strict mode is happy (``str`` mixin satisfies ``str`` annotations).

One definition per enum — used by both the SQLModel table classes (to build
``CheckConstraint`` lists) and the Pydantic request/response DTOs in T1.2+.
"""

from __future__ import annotations

import enum


class WakeSource(enum.StrEnum):
    """How the iOS app was woken to sample signals."""

    silent_push = "silent_push"
    bg_refresh = "bg_refresh"
    foreground = "foreground"
    manual = "manual"


class WalkingConf(enum.StrEnum):
    """CoreMotion walking-activity confidence level."""

    low = "low"
    medium = "medium"
    high = "high"


class BatteryState(enum.StrEnum):
    """iOS battery / charger state."""

    unplugged = "unplugged"
    charging = "charging"
    full = "full"
    unknown = "unknown"


class AppState(enum.StrEnum):
    """UIApplication application state."""

    active = "active"
    background = "background"
    inactive = "inactive"


class PedometerAuth(enum.StrEnum):
    """CMPedometer authorisation status."""

    authorized = "authorized"
    denied = "denied"
    restricted = "restricted"
    not_determined = "not_determined"


class DayKind(enum.StrEnum):
    """Calendar classification for a local date."""

    weekday = "weekday"
    weekend = "weekend"
    holiday = "holiday"


class DayState(enum.StrEnum):
    """State machine state for a ``days`` row."""

    idle = "idle"
    monitoring = "monitoring"
    confirmed = "confirmed"
    fallback = "fallback"
    delivered = "delivered"


class DeviceStatus(enum.StrEnum):
    """Lifecycle status of a paired device."""

    active = "active"
    revoked = "revoked"


class RuleSource(enum.StrEnum):
    """How a ``rule_versions`` row was created."""

    yaml = "yaml"
    api = "api"


class EvaluationGate(enum.StrEnum):
    """Whether the evaluation window was open."""

    open = "open"
    closed = "closed"


class EvaluationDecision(enum.StrEnum):
    """Outcome of a wake evaluation."""

    outside_window = "outside_window"
    low = "low"
    high = "high"
    confirm = "confirm"
    ignored_dedupe = "ignored_dedupe"
    already_confirmed = "already_confirmed"


class TransitionCause(enum.StrEnum):
    """What triggered a ``day_transitions`` row."""

    evaluation = "evaluation"
    scheduler_window_start = "scheduler_window_start"
    scheduler_window_end = "scheduler_window_end"
    deliver_ok = "deliver_ok"
    manual = "manual"


def _ck_values(e: type[enum.Enum]) -> str:
    """Return a SQL IN-list string for use in a ``CheckConstraint``.

    Example::

        _ck_values(WakeSource)
        # "('silent_push','bg_refresh','foreground','manual')"
    """
    values = ", ".join(f"'{m.value}'" for m in e)
    return f"({values})"
