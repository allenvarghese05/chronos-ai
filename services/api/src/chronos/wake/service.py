"""Telemetry ingestion service for Chronos-AI (T1.6).

Orchestrates the full POST /v1/telemetry lifecycle as a single DB transaction:
clock-skew check → idempotency check → local_date → day get-or-create →
previous tick lookup → evaluate → insert TelemetryEvent + WakeEvaluation →
ticks_received increment → state machine apply.

Public API
----------
- :class:`TelemetryResult`   — typed result returned by ingest_telemetry.
- :func:`ingest_telemetry`   — single entry point; called by the HTTP layer.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field

import structlog
from sqlmodel import Session, select

from chronos.enums import DayState
from chronos.errors import ProblemException
from chronos.models.devices import Device
from chronos.models.wake import Day, RuleVersion, TelemetryEvent, WakeEvaluation
from chronos.time import format_utc_iso
from chronos.wake.rules import WakeRules
from chronos.wake.scoring import (
    EvalContext,
    Evaluation,
    PreviousTick,
    evaluate,
)
from chronos.wake.signals import Signals
from chronos.wake.state import apply, get_or_create_day
from chronos.wake.window import local_date_for, window_for

log = structlog.get_logger(__name__)

#: Maximum allowed absolute delta between device_ts and server now (seconds).
CLOCK_SKEW_MAX_SEC: int = 300

#: Maximum raw body size stored in raw_json (bytes). Bodies are capped by the HTTP layer.
RAW_BODY_MAX_BYTES: int = 65_536  # 64 KiB


@dataclass
class TelemetryResult:
    """Typed result of :func:`ingest_telemetry`."""

    evaluation_id: int
    """PK of the WakeEvaluation row (new or pre-existing on duplicate)."""

    local_date: datetime.date
    """Local calendar date this evaluation belongs to."""

    evaluation: Evaluation
    """Complete scoring result."""

    day_state: DayState
    """Day state after any transitions."""

    rule_version_id: int
    """PK of the RuleVersion used."""

    rule_version_number: int
    """Human-readable rule version number echoed in the response."""

    is_duplicate: bool = field(default=False)
    """True when the same (device_id, seq) was already stored."""


def ingest_telemetry(
    session: Session,
    device: Device,
    signals: Signals,
    raw_body: bytes,
    now: datetime.datetime,
    rules: WakeRules,
    rule_version: RuleVersion,
) -> TelemetryResult:
    """Validate, score and persist one telemetry snapshot.

    Steps (all inside one transaction; this function commits):
    1. Clock skew: |device_ts_utc - now| > CLOCK_SKEW_MAX_SEC → ProblemException 422.
    2. Idempotency: SELECT telemetry_events WHERE device_id=? AND seq=?.
       If found → reconstruct Evaluation from stored WakeEvaluation.breakdown_json
       and return early (200, is_duplicate=True). Log warning if raw_json differs.
    3. local_date_for(device_ts, tz, boundary).
    4. get_or_create_day(session, device, local_date, tz, rules, now).
    5. Previous tick: latest WakeEvaluation WHERE device_id=device.id AND
       local_date=? by device_ts DESC LIMIT 1 → build PreviousTick | None.
    6. EvalContext(window, day_state, rule_version, previous, ticks_received_today).
    7. evaluate(signals, rules, now, ctx) → Evaluation.
    8. INSERT TelemetryEvent (raw_json=raw_body[:RAW_BODY_MAX_BYTES]).
    9. INSERT WakeEvaluation.
    10. day.ticks_received += 1; day.updated_at = now.
    11. apply(session, day, evaluation, now, evaluation_id=wake_eval.id).
    12. session.commit().
    13. Return TelemetryResult.

    Args:
        session:      Open Session; this function commits it.
        device:       Authenticated Device row.
        signals:      Validated Signals payload.
        raw_body:     Exact request bytes (capped at RAW_BODY_MAX_BYTES).
        now:          UTC now (injected; frozen in tests).
        rules:        Active WakeRules.
        rule_version: Active RuleVersion (for FK and version number).

    Returns:
        :class:`TelemetryResult` with full scoring detail.

    Raises:
        ProblemException 422 clock_skew: if |device_ts - now| > 300 s.
        Exception:       Any unexpected DB error propagates to the caller.
    """
    assert device.id is not None, "device must be persisted before ingest_telemetry"
    assert rule_version.id is not None

    # ── 1. Clock skew check (must happen BEFORE any DB write) ─────────────
    device_ts_utc = signals.device_ts.astimezone(datetime.UTC).replace(tzinfo=datetime.UTC)
    skew_sec = abs((device_ts_utc - now).total_seconds())
    if skew_sec > CLOCK_SKEW_MAX_SEC:
        server_time = format_utc_iso(now)
        raise ProblemException(
            status_code=422,
            code="clock_skew",
            title="Clock Skew Too Large",
            detail=f"Device clock is {int(skew_sec)} seconds from server time.",
            extensions={"server_time": server_time},
        )

    # ── 2. Idempotency: check (device_id, seq) uniqueness ─────────────────
    dup_te_stmt = select(TelemetryEvent).where(
        TelemetryEvent.device_id == device.id,
        TelemetryEvent.seq == signals.seq,
    )
    dup_te = session.exec(dup_te_stmt).first()  # type: ignore[call-overload]
    if dup_te is not None:
        # Check if the raw body differs (warn but still return first).
        stored_raw = dup_te.raw_json
        incoming_raw = raw_body[:RAW_BODY_MAX_BYTES].decode("utf-8", errors="replace")
        if stored_raw != incoming_raw:
            log.warning(
                "telemetry_seq_resubmitted_different_payload",
                device_id=device.id,
                seq=signals.seq,
                stored_len=len(stored_raw),
                incoming_len=len(incoming_raw),
            )
        # Reconstruct Evaluation from stored WakeEvaluation.
        we_stmt = select(WakeEvaluation).where(
            WakeEvaluation.telemetry_event_id == dup_te.id,
        )
        dup_we = session.exec(we_stmt).first()  # type: ignore[call-overload]
        if dup_we is None:  # should not happen; guard anyway
            raise RuntimeError(f"WakeEvaluation missing for telemetry_event {dup_te.id}")
        stored_eval = Evaluation.model_validate(json.loads(dup_we.breakdown_json))
        # Fetch the day for its current state.
        day_stmt = select(Day).where(
            Day.local_date == dup_te.local_date,
            Day.device_id == device.id,
        )
        dup_day = session.exec(day_stmt).first()  # type: ignore[call-overload]
        day_state = DayState(dup_day.state) if dup_day else DayState.idle
        local_date = datetime.date.fromisoformat(dup_te.local_date)
        log.info(
            "telemetry_duplicate_seq",
            device_id=device.id,
            seq=signals.seq,
            evaluation_id=dup_we.id,
        )
        return TelemetryResult(
            evaluation_id=dup_we.id,  # type: ignore[arg-type]
            local_date=local_date,
            evaluation=stored_eval,
            day_state=day_state,
            rule_version_id=dup_we.rule_version_id,
            rule_version_number=rule_version.version,
            is_duplicate=True,
        )

    # ── 3. Derive local_date ───────────────────────────────────────────────
    from zoneinfo import ZoneInfo

    tz_obj = ZoneInfo(signals.tz)
    window_cfg = rules.window.to_window_config()
    local_date = local_date_for(device_ts_utc, tz_obj, window_cfg.day_boundary)

    # ── 4. Get or create the Day row ───────────────────────────────────────
    day = get_or_create_day(session, device, local_date, signals.tz, rules, now)

    # ── 5. Previous tick lookup ───────────────────────────────────────────
    local_date_str = local_date.isoformat()
    prev_we_stmt = (
        select(WakeEvaluation, TelemetryEvent)
        .join(  # type: ignore[call-overload]
            TelemetryEvent,
            WakeEvaluation.telemetry_event_id == TelemetryEvent.id,  # type: ignore[arg-type]
        )
        .where(
            TelemetryEvent.device_id == device.id,
            TelemetryEvent.local_date == local_date_str,
        )
        .order_by(TelemetryEvent.device_ts.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    prev_row = session.exec(prev_we_stmt).first()  # type: ignore[call-overload]
    previous: PreviousTick | None = None
    if prev_row is not None:
        prev_we, prev_te = prev_row
        try:
            prev_device_ts = datetime.datetime.fromisoformat(prev_te.device_ts)
            if prev_device_ts.tzinfo is None:
                prev_device_ts = prev_device_ts.replace(tzinfo=datetime.UTC)
        except ValueError:
            prev_device_ts = now  # fallback; won't crash
        prev_local_date = datetime.date.fromisoformat(prev_te.local_date)
        previous = PreviousTick(
            device_ts=prev_device_ts,
            local_date=prev_local_date,
            battery_state=prev_te.battery_state,  # type: ignore[arg-type]
            is_unlocked=bool(prev_te.is_unlocked),
            score=prev_we.score,
            decision=prev_we.decision,  # type: ignore[arg-type]
            consecutive_high=prev_we.consecutive_high,
        )

    # ── 6. Build EvalContext ──────────────────────────────────────────────
    window = window_for(local_date, tz_obj, window_cfg)
    ctx = EvalContext(
        window=window,
        day_state=DayState(day.state),
        rule_version=rule_version.version,
        previous=previous,
        ticks_received_today=day.ticks_received,
    )

    # ── 7. Score ──────────────────────────────────────────────────────────
    evaluation = evaluate(signals, rules, now, ctx)

    # ── 8. INSERT TelemetryEvent ──────────────────────────────────────────
    raw_str = raw_body[:RAW_BODY_MAX_BYTES].decode("utf-8", errors="replace")
    device_ts_str = signals.device_ts.isoformat()
    te = TelemetryEvent(
        device_id=device.id,
        received_at=format_utc_iso(now),
        device_ts=device_ts_str,
        tz=signals.tz,
        local_date=local_date_str,
        wake_source=signals.wake_source.value,
        seq=signals.seq,
        steps_10m=signals.steps_10m,
        steps_since_window=signals.steps_since_window,
        walking_sec_10m=signals.walking_sec_10m,
        stationary_sec_10m=signals.stationary_sec_10m,
        walking_conf=signals.walking_conf.value,
        is_unlocked=int(signals.is_unlocked),
        battery_state=signals.battery_state.value,
        battery_level=signals.battery_level,
        app_state=signals.app_state.value,
        low_power_mode=int(signals.low_power_mode),
        pedometer_auth=signals.pedometer_auth.value,
        tick_seq=None,  # scheduler-assigned in T1.7
        app_version=signals.app_version,
        ios_version=signals.ios_version,
        raw_json=raw_str,
    )
    session.add(te)
    session.flush()  # get te.id

    # ── 9. INSERT WakeEvaluation ──────────────────────────────────────────
    breakdown_json = json.dumps(evaluation.model_dump(mode="json"))
    assert te.id is not None, "TelemetryEvent must have an id after flush"
    wake_eval = WakeEvaluation(
        telemetry_event_id=te.id,
        rule_version_id=rule_version.id,
        local_date=local_date_str,
        gate=evaluation.gate.value,
        score=evaluation.score,
        breakdown_json=breakdown_json,
        decision=evaluation.decision.value,
        consecutive_high=evaluation.consecutive_high,
        evaluated_at=format_utc_iso(now),
    )
    session.add(wake_eval)
    session.flush()  # get wake_eval.id

    # ── 10. Increment ticks_received ──────────────────────────────────────
    day.ticks_received += 1
    day.updated_at = format_utc_iso(now)

    # ── 11. State machine ─────────────────────────────────────────────────
    assert wake_eval.id is not None
    apply(session, day, evaluation, now, evaluation_id=wake_eval.id)

    # ── 12. Commit ────────────────────────────────────────────────────────
    session.commit()

    log.info(
        "telemetry_ingested",
        device_id=device.id,
        seq=signals.seq,
        local_date=local_date_str,
        score=evaluation.score,
        decision=evaluation.decision.value,
        day_state=day.state,
    )

    return TelemetryResult(
        evaluation_id=wake_eval.id,
        local_date=local_date,
        evaluation=evaluation,
        day_state=DayState(day.state),
        rule_version_id=rule_version.id,
        rule_version_number=rule_version.version,
        is_duplicate=False,
    )
