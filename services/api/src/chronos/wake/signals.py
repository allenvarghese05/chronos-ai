"""Signal registry for the Chronos-AI wake engine (T1.4).

Every field that may appear in a ``points`` rule or a ``penalty.when`` condition
must be registered here.  The registry is the single source of truth for:

- Field names (validated at rule-load time so typos fail fast).
- Field *kind* (``numeric`` | ``bool`` | ``enum``): drives threshold validation.
- Whether the field comes from the phone telemetry payload or is computed
  server-side from two consecutive snapshots.
- Allowed enum values (for ``enum`` kind).

The scoring engine (T1.5) imports ``SIGNALS`` to look up the kind of each signal
before evaluating a rule's threshold rows.

Public API
----------
- :class:`SignalKind` — ``numeric`` / ``bool`` / ``enum``.
- :class:`SignalSpec` — immutable descriptor for one signal.
- :data:`SIGNALS` — ``dict[str, SignalSpec]`` registry.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal


class SignalKind(enum.StrEnum):
    """Semantic kind of a wake-engine signal."""

    numeric = "numeric"
    bool_ = "bool"
    enum_ = "enum"


@dataclass(frozen=True)
class SignalSpec:
    """Immutable descriptor for one wake-engine signal.

    Attributes:
        name:         Canonical field name (matches telemetry payload key or derived name).
        kind:         ``numeric``, ``bool``, or ``enum``.
        source:       ``"phone"`` — sent in the telemetry payload;
                      ``"derived"`` — computed server-side from consecutive snapshots.
        enum_values:  Non-empty only when ``kind == SignalKind.enum_``; the
                      exhaustive set of allowed string values.
    """

    name: str
    kind: SignalKind
    source: Literal["phone", "derived"]
    enum_values: frozenset[str] = field(default_factory=lambda: frozenset[str]())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SIGNALS: dict[str, SignalSpec] = {
    # ── phone fields ──────────────────────────────────────────────────────────
    "device_ts": SignalSpec(
        name="device_ts",
        kind=SignalKind.enum_,  # ISO-8601 string; not used in rules
        source="phone",
        enum_values=frozenset(),  # free-form; not validated as enum in rules
    ),
    "tz": SignalSpec(
        name="tz",
        kind=SignalKind.enum_,
        source="phone",
        enum_values=frozenset(),  # IANA string; not rule-scored
    ),
    "wake_source": SignalSpec(
        name="wake_source",
        kind=SignalKind.enum_,
        source="phone",
        enum_values=frozenset({"silent_push", "bg_refresh", "foreground", "manual"}),
    ),
    "steps_10m": SignalSpec(
        name="steps_10m",
        kind=SignalKind.numeric,
        source="phone",
    ),
    "steps_since_window": SignalSpec(
        name="steps_since_window",
        kind=SignalKind.numeric,
        source="phone",
    ),
    "walking_sec_10m": SignalSpec(
        name="walking_sec_10m",
        kind=SignalKind.numeric,
        source="phone",
    ),
    "walking_conf": SignalSpec(
        name="walking_conf",
        kind=SignalKind.enum_,
        source="phone",
        enum_values=frozenset({"low", "medium", "high"}),
    ),
    "stationary_sec_10m": SignalSpec(
        name="stationary_sec_10m",
        kind=SignalKind.numeric,
        source="phone",
    ),
    "is_unlocked": SignalSpec(
        name="is_unlocked",
        kind=SignalKind.bool_,
        source="phone",
    ),
    "battery_state": SignalSpec(
        name="battery_state",
        kind=SignalKind.enum_,
        source="phone",
        enum_values=frozenset({"unplugged", "charging", "full", "unknown"}),
    ),
    "battery_level": SignalSpec(
        name="battery_level",
        kind=SignalKind.numeric,
        source="phone",
    ),
    "app_state": SignalSpec(
        name="app_state",
        kind=SignalKind.enum_,
        source="phone",
        enum_values=frozenset({"active", "background", "inactive"}),
    ),
    "low_power_mode": SignalSpec(
        name="low_power_mode",
        kind=SignalKind.bool_,
        source="phone",
    ),
    "pedometer_auth": SignalSpec(
        name="pedometer_auth",
        kind=SignalKind.enum_,
        source="phone",
        enum_values=frozenset({"authorized", "denied", "restricted", "not_determined"}),
    ),
    "seq": SignalSpec(
        name="seq",
        kind=SignalKind.numeric,
        source="phone",
    ),
    "app_version": SignalSpec(
        name="app_version",
        kind=SignalKind.enum_,
        source="phone",
        enum_values=frozenset(),  # free-form semver string
    ),
    "ios_version": SignalSpec(
        name="ios_version",
        kind=SignalKind.enum_,
        source="phone",
        enum_values=frozenset(),  # free-form version string
    ),
    # ── server-derived fields ─────────────────────────────────────────────────
    "charger_disconnected": SignalSpec(
        name="charger_disconnected",
        kind=SignalKind.bool_,
        source="derived",
    ),
    "consecutive_unlocked_ticks": SignalSpec(
        name="consecutive_unlocked_ticks",
        kind=SignalKind.numeric,
        source="derived",
    ),
    "minutes_since_window_start": SignalSpec(
        name="minutes_since_window_start",
        kind=SignalKind.numeric,
        source="derived",
    ),
    "ticks_received_today": SignalSpec(
        name="ticks_received_today",
        kind=SignalKind.numeric,
        source="derived",
    ),
}
