"""POST /v1/telemetry — receive, validate, score and store one sensor snapshot.

Endpoint contract (docs/05-api-contracts.md §2):
- Requires device bearer token (require_device).
- Rate limit: 30 req/min per IP (rate_limit("telemetry", 30)).
- Body: application/json, Signals schema (extra=forbid).
- Raw body capped at 64 KiB (reads request.body(), checks len).
- 422 clock_skew if |device_ts - server_now| > 300 s (with server_time in detail).
- Idempotent on (device_id, seq): returns stored evaluation with 200.
- Response: TelemetryResponse (see below).
- next_tick_in_sec: seconds until next scheduled tick (null if not monitoring or no scheduler).
"""

from __future__ import annotations

import datetime
from typing import Any

import pydantic
import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session

from chronos.auth import require_device
from chronos.db import get_session_dep
from chronos.errors import ProblemException
from chronos.models.devices import Device
from chronos.ratelimit import rate_limit
from chronos.wake.rules import get_rules_cache
from chronos.wake.service import RAW_BODY_MAX_BYTES, TelemetryResult, ingest_telemetry
from chronos.wake.signals import Signals

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

_telemetry_rate_limit = rate_limit("telemetry", per_minute=30)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TelemetryResponse(BaseModel):
    """Response body for POST /v1/telemetry (docs/05-api-contracts.md §2)."""

    model_config = ConfigDict(extra="forbid")

    evaluation_id: int
    local_date: str  # "YYYY-MM-DD"
    gate: str  # EvaluationGate value
    score: int
    decision: str  # EvaluationDecision value
    confirm_reason: str | None  # "strong" | "consecutive" | "both" | null
    threshold: dict[str, int]  # {"confirm": int, "strong": int, "confirm_max_gap_min": int}
    consecutive_high: int
    rule_version: int
    breakdown: list[dict[str, Any]]
    penalties: list[dict[str, Any]]
    day_state: str  # DayState value
    next_tick_in_sec: int | None  # always null for T1.6


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("", status_code=200)
async def post_telemetry(
    request: Request,
    device: Device = Depends(require_device),
    _rl: None = Depends(_telemetry_rate_limit),
    session: Session = Depends(get_session_dep),
) -> TelemetryResponse:
    """Receive, validate, score and store one telemetry snapshot.

    Returns:
        200 with full scoring breakdown and updated day state.

    Raises:
        422 clock_skew: |device_ts - server_now| > 300 s.
        422 validation: Body exceeds 64 KiB or fails Signals schema.
        429 rate_limited: > 30 req/min per IP.
        401 unauthorized: Missing/invalid bearer token.
        403 revoked: Device revoked.
    """
    # ── 1. Read raw body (must be done before Pydantic parse) ─────────────
    raw = await request.body()
    if len(raw) > RAW_BODY_MAX_BYTES:
        raise ProblemException(
            status_code=422,
            code="validation",
            title="Payload Too Large",
            detail=f"Request body exceeds {RAW_BODY_MAX_BYTES // 1024} KiB limit.",
        )

    # ── 2. Parse and validate Signals ─────────────────────────────────────
    try:
        signals = Signals.model_validate_json(raw)
    except pydantic.ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        detail = "; ".join(
            f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ProblemException(
            status_code=422,
            code="validation",
            title="Unprocessable Entity",
            detail=detail or "Validation error",
            extensions={"errors": errors},
        ) from exc

    # ── 3. Load rules ─────────────────────────────────────────────────────
    rules, rule_version = get_rules_cache().get_effective(session)

    # ── 4. Ingest (clock skew + idempotency + score + persist) ───────────
    now = datetime.datetime.now(tz=datetime.UTC)
    result: TelemetryResult = ingest_telemetry(
        session=session,
        device=device,
        signals=signals,
        raw_body=raw,
        now=now,
        rules=rules,
        rule_version=rule_version,
    )

    # ── 5. Build response ─────────────────────────────────────────────────
    evaluation = result.evaluation
    threshold_dict: dict[str, int] = {
        "confirm": evaluation.thresholds.confirm,
        "strong": evaluation.thresholds.strong,
        "confirm_max_gap_min": evaluation.thresholds.confirm_max_gap_min,
    }
    breakdown: list[dict[str, Any]] = [b.model_dump(mode="json") for b in evaluation.breakdown]
    penalties: list[dict[str, Any]] = [p.model_dump(mode="json") for p in evaluation.penalties]

    # Fill next_tick_in_sec from the scheduler's in-memory state.
    next_tick_in_sec: int | None = None
    if hasattr(request.app.state, "scheduler"):
        scheduler = request.app.state.scheduler
        assert device.id is not None
        nxt = scheduler.get_next_tick_for(device.id)
        if nxt is not None:
            delta = (nxt - now).total_seconds()
            next_tick_in_sec = max(0, int(delta))

    return TelemetryResponse(
        evaluation_id=result.evaluation_id,
        local_date=result.local_date.isoformat(),
        gate=evaluation.gate.value,
        score=evaluation.score,
        decision=evaluation.decision.value,
        confirm_reason=evaluation.confirm_reason,
        threshold=threshold_dict,
        consecutive_high=evaluation.consecutive_high,
        rule_version=result.rule_version_number,
        breakdown=breakdown,
        penalties=penalties,
        day_state=result.day_state.value,
        next_tick_in_sec=next_tick_in_sec,
    )
