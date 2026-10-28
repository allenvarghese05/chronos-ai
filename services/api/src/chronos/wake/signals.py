"""Signal registry and validated telemetry snapshot for the Chronos-AI wake engine.

Every field that may appear in a ``points`` rule or a ``penalty.when`` condition
must be registered here.  The registry is the single source of truth for:

- Field names (validated at rule-load time so typos fail fast).
- Field *kind* (``numeric`` | ``bool`` | ``enum``): drives threshold validation.
- Whether the field comes from the phone telemetry payload or is computed
  server-side from two consecutive snapshots.
- Allowed enum values (for ``enum`` kind).
- Whether the field requires pedometer authorisation (T1.5 scoring reweighting).

The scoring engine (T1.5) imports ``SIGNALS`` to look up the kind of each signal
before evaluating a rule's threshold rows.  The :class:`Signals` pydantic model
is imported by the T1.6 telemetry endpoint as the request body type.

Public API
----------
- :class:`SignalKind` — ``numeric`` / ``bool`` / ``enum``.
- :class:`SignalSpec` — immutable descriptor for one signal.
- :data:`SIGNALS` — ``dict[str, SignalSpec]`` registry.
- :class:`Signals` — validated telemetry snapshot from the iOS phone.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chronos.enums import AppState, BatteryState, PedometerAuth, WakeSource, WalkingConf


class SignalKind(enum.StrEnum):
    """Semantic kind of a wake-engine signal."""

    numeric = "numeric"
    bool_ = "bool"
    enum_ = "enum"


@dataclass(frozen=True)
class SignalSpec:
    """Immutable descriptor for one wake-engine signal.

    Attributes:
        name:             Canonical field name (matches telemetry payload key or derived name).
        kind:             ``numeric``, ``bool``, or ``enum``.
        source:           ``"phone"`` — sent in the telemetry payload;
                          ``"derived"`` — computed server-side from consecutive snapshots.
        enum_values:      Non-empty only when ``kind == SignalKind.enum_``; the
                          exhaustive set of allowed string values.
        needs_pedometer:  ``True`` when CoreMotion pedometer authorisation is required
                          to produce meaningful values.  Signals marked ``True`` are
                          skipped by the scoring engine when
                          ``pedometer_auth ∈ {denied, restricted, not_determined}``
                          and the ``reweight_without_pedometer`` redistribution applies.
    """

    name: str
    kind: SignalKind
    source: Literal["phone", "derived"]
    enum_values: frozenset[str] = field(default_factory=lambda: frozenset[str]())
    needs_pedometer: bool = False


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
        needs_pedometer=True,
    ),
    "steps_since_window": SignalSpec(
        name="steps_since_window",
        kind=SignalKind.numeric,
        source="phone",
        needs_pedometer=True,
    ),
    "walking_sec_10m": SignalSpec(
        name="walking_sec_10m",
        kind=SignalKind.numeric,
        source="phone",
        needs_pedometer=True,
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
        needs_pedometer=True,
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


# ---------------------------------------------------------------------------
# Validated telemetry snapshot (T1.5)
# ---------------------------------------------------------------------------


class Signals(BaseModel):
    """Validated telemetry snapshot from the iOS app.

    All 17 phone fields are required; extra fields are forbidden.
    ``device_ts`` must carry timezone information (naive datetimes are rejected
    by the ``_reject_naive_device_ts`` validator so that the scoring engine
    never silently misclassifies a tick).

    Reused by the T1.6 telemetry endpoint as the request body type.

    Bounds (enforced by Pydantic):
    - ``steps_10m``, ``steps_since_window`` \u2208 [0, 5000]
    - ``walking_sec_10m``, ``stationary_sec_10m`` \u2208 [0, 600]
    - ``battery_level`` \u2208 [0.0, 1.0] or ``None``
    - ``seq`` \u2265 0
    """

    model_config = ConfigDict(extra="forbid")

    device_ts: datetime.datetime
    """When the snapshot was taken.  Must be timezone-aware (offset or UTC)."""

    tz: str
    """IANA timezone identifier, e.g. ``"America/Los_Angeles"``."""

    wake_source: WakeSource
    steps_10m: int = Field(ge=0, le=5000)
    steps_since_window: int = Field(ge=0, le=5000)
    walking_sec_10m: int = Field(ge=0, le=600)
    walking_conf: WalkingConf
    stationary_sec_10m: int = Field(ge=0, le=600)
    is_unlocked: bool
    battery_state: BatteryState
    battery_level: float | None = Field(default=None, ge=0.0, le=1.0)
    app_state: AppState
    low_power_mode: bool
    pedometer_auth: PedometerAuth
    seq: int = Field(ge=0)
    tick_seq: int | None = Field(default=None, ge=0)
    """Optional server tick number; stored only, never used as a scoring signal."""
    app_version: str
    ios_version: str

    @model_validator(mode="after")
    def _reject_naive_device_ts(self) -> Signals:
        if self.device_ts.tzinfo is None:
            raise ValueError(
                "device_ts must be timezone-aware (ISO-8601 with UTC offset); "
                "naive datetimes are rejected to prevent silent timezone errors."
            )
        return self
