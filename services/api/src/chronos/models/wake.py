"""SQLModel table classes for the wake engine aggregate.

Tables: RuleVersion, TelemetryEvent, WakeEvaluation, Day, DayTransition.
Source: docs/04-data-model.md §2.2 + docs/03-wake-engine.md §3.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy import Column as SAColumn
from sqlmodel import Field, SQLModel

from chronos.enums import (
    AppState,
    BatteryState,
    DayKind,
    DayState,
    EvaluationDecision,
    EvaluationGate,
    PedometerAuth,
    RuleSource,
    TransitionCause,
    WakeSource,
    WalkingConf,
    ck_values,
)


class RuleVersion(SQLModel, table=True):
    """Every wake-rule set ever in effect.  Exactly one row has ``active = 1``."""

    __tablename__ = "rule_versions"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("version", name="uq_rule_versions_version"),
        CheckConstraint(
            f"source IN {ck_values(RuleSource)}",
            name="ck_rule_versions_source",
        ),
        # Partial unique index: at most one row may have active = 1.
        Index(
            "ix_rule_versions_active_one",
            "active",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    version: int
    source: str  # RuleSource
    rules_json: str
    sha256: str
    created_at: str
    created_by: str
    active: int = 0  # INTEGER; 1 = current, 0 = historical


class TelemetryEvent(SQLModel, table=True):
    """One sensor snapshot from the phone."""

    __tablename__ = "telemetry_events"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("device_id", "seq", name="uq_telemetry_events_device_seq"),
        CheckConstraint(
            f"wake_source IN {ck_values(WakeSource)}",
            name="ck_telemetry_events_wake_source",
        ),
        CheckConstraint(
            f"walking_conf IN {ck_values(WalkingConf)}",
            name="ck_telemetry_events_walking_conf",
        ),
        CheckConstraint(
            f"battery_state IN {ck_values(BatteryState)}",
            name="ck_telemetry_events_battery_state",
        ),
        CheckConstraint(
            f"app_state IN {ck_values(AppState)}",
            name="ck_telemetry_events_app_state",
        ),
        CheckConstraint(
            f"pedometer_auth IN {ck_values(PedometerAuth)}",
            name="ck_telemetry_events_pedometer_auth",
        ),
        Index("ix_telemetry_events_device_received", "device_id", "received_at"),
        Index("ix_telemetry_events_local_date", "local_date"),
    )

    id: int | None = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="devices.id")
    received_at: str
    device_ts: str
    tz: str
    local_date: str
    wake_source: str  # WakeSource
    seq: int
    steps_10m: int
    steps_since_window: int
    walking_sec_10m: int
    stationary_sec_10m: int
    walking_conf: str  # WalkingConf
    is_unlocked: int  # bool (0/1)
    battery_state: str  # BatteryState
    battery_level: float | None = Field(default=None)
    app_state: str  # AppState
    low_power_mode: int  # bool (0/1)
    pedometer_auth: str  # PedometerAuth
    tick_seq: int | None = Field(default=None)
    app_version: str
    ios_version: str
    raw_json: str


class WakeEvaluation(SQLModel, table=True):
    """Score and decision for one telemetry event (1:1)."""

    __tablename__ = "wake_evaluations"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "telemetry_event_id",
            name="uq_wake_evaluations_telemetry_event_id",
        ),
        CheckConstraint(
            f"gate IN {ck_values(EvaluationGate)}",
            name="ck_wake_evaluations_gate",
        ),
        CheckConstraint(
            f"decision IN {ck_values(EvaluationDecision)}",
            name="ck_wake_evaluations_decision",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    telemetry_event_id: int = Field(foreign_key="telemetry_events.id")
    rule_version_id: int = Field(foreign_key="rule_versions.id")
    local_date: str
    gate: str  # EvaluationGate
    score: int
    breakdown_json: str
    decision: str  # EvaluationDecision
    consecutive_high: int
    evaluated_at: str


class Day(SQLModel, table=True):
    """One row per local date per device — the state-machine anchor."""

    __tablename__ = "days"  # type: ignore[assignment]
    __table_args__ = (
        PrimaryKeyConstraint("local_date", "device_id", name="pk_days"),
        # FK from device_id to devices(id); declared here (not inline) so that
        # the composite PK definition above takes precedence.
        ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name="fk_days_device_id",
        ),
        CheckConstraint(
            f"kind IN {ck_values(DayKind)}",
            name="ck_days_kind",
        ),
        CheckConstraint(
            f"state IN {ck_values(DayState)}",
            name="ck_days_state",
        ),
    )

    # Composite PK — both columns must be declared without individual primary_key=True.
    local_date: str = Field(sa_column=SAColumn("local_date", String, nullable=False))
    device_id: int = Field(
        sa_column=SAColumn(
            "device_id",
            Integer,
            # ForeignKeyConstraint is declared in __table_args__ for composite FK compatibility.
            nullable=False,
        )
    )
    tz: str
    kind: str  # DayKind
    window_start_utc: str
    window_end_utc: str
    state: str = "idle"  # DayState
    confirmed_at: str | None = Field(default=None)
    confirm_evaluation_id: int | None = Field(
        default=None,
        foreign_key="wake_evaluations.id",
    )
    delivered_at: str | None = Field(default=None)
    fallback_used: int = 0  # bool (0/1)
    actual_wake_local: str | None = Field(default=None)
    ticks_sent: int = 0
    ticks_received: int = 0
    brief_id: int | None = Field(default=None)  # future M3
    reel_id: int | None = Field(default=None)  # future M4
    created_at: str
    updated_at: str


class DayTransition(SQLModel, table=True):
    """Audit log of every state-machine transition on a ``days`` row."""

    __tablename__ = "day_transitions"  # type: ignore[assignment]
    __table_args__ = (
        # Composite FK to days(local_date, device_id) — required because days has a composite PK.
        ForeignKeyConstraint(
            ["local_date", "device_id"],
            ["days.local_date", "days.device_id"],
            name="fk_day_transitions_days",
        ),
        CheckConstraint(
            f"cause IN {ck_values(TransitionCause)}",
            name="ck_day_transitions_cause",
        ),
        CheckConstraint(
            f"from_state IN {ck_values(DayState)}",
            name="ck_day_transitions_from_state",
        ),
        CheckConstraint(
            f"to_state IN {ck_values(DayState)}",
            name="ck_day_transitions_to_state",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    local_date: str
    device_id: int
    from_state: str
    to_state: str
    cause: str  # TransitionCause
    caused_by_evaluation_id: int | None = Field(
        default=None,
        foreign_key="wake_evaluations.id",
    )
    at: str
