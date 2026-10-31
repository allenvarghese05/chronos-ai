"""GET /v1/day/today, GET /v1/day/{local_date}, PUT /v1/day/{local_date}/actual-wake.

All endpoints require device bearer token (require_device).
"""

from __future__ import annotations

import datetime
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, field_validator
from sqlmodel import Session, select

from chronos.auth import require_device
from chronos.db import get_session_dep
from chronos.errors import ProblemException
from chronos.models.devices import Device
from chronos.models.wake import Day, TelemetryEvent, WakeEvaluation
from chronos.ratelimit import rate_limit
from chronos.wake.rules import get_rules_cache
from chronos.wake.window import local_date_for

router = APIRouter(prefix="/day", tags=["day"])

_HH_MM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


# ---------------------------------------------------------------------------
# Response / request models
# ---------------------------------------------------------------------------


class ScoreTimelinePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: str  # device_ts as ISO-8601 UTC "YYYY-MM-DDTHH:MM:SSZ"
    score: int


class DayResponse(BaseModel):
    """Response schema for all day endpoints."""

    model_config = ConfigDict(extra="forbid")

    local_date: str  # "YYYY-MM-DD"
    kind: str  # DayKind value
    tz: str
    window: dict[str, str]  # {"start": "HH:MM", "end": "HH:MM"} wall-clock local
    state: str  # DayState value
    confirmed_at: str | None
    delivered_at: str | None
    fallback_used: bool
    ticks_sent: int
    ticks_received: int
    reel: dict[str, str] | None  # null for M1-M4
    score_timeline: list[ScoreTimelinePoint]
    actual_wake_local: str | None


class ActualWakeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_wake_local: str

    @field_validator("actual_wake_local")
    @classmethod
    def validate_hhmm(cls, v: str) -> str:
        if not _HH_MM_RE.match(v):
            raise ValueError("actual_wake_local must be in HH:MM (24-hour) format, e.g. '07:05'")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_window_dict(day: Day) -> dict[str, str]:
    """Convert UTC window bounds to local HH:MM strings."""
    tz = ZoneInfo(day.tz)
    start_utc = datetime.datetime.fromisoformat(day.window_start_utc.rstrip("Z")).replace(
        tzinfo=datetime.UTC
    )
    end_utc = datetime.datetime.fromisoformat(day.window_end_utc.rstrip("Z")).replace(
        tzinfo=datetime.UTC
    )
    return {
        "start": start_utc.astimezone(tz).strftime("%H:%M"),
        "end": end_utc.astimezone(tz).strftime("%H:%M"),
    }


def _build_score_timeline(
    session: Session, device_id: int, local_date: str
) -> list[ScoreTimelinePoint]:
    """Fetch evaluations for the day, ordered by device_ts, excluding dedupe/confirmed."""
    from chronos.enums import EvaluationDecision

    excluded = [
        EvaluationDecision.ignored_dedupe.value,
        EvaluationDecision.already_confirmed.value,
    ]
    stmt = (
        select(WakeEvaluation, TelemetryEvent)
        .join(  # type: ignore[call-overload]
            TelemetryEvent,
            WakeEvaluation.telemetry_event_id == TelemetryEvent.id,  # type: ignore[arg-type]
        )
        .where(
            TelemetryEvent.device_id == device_id,
            TelemetryEvent.local_date == local_date,
            WakeEvaluation.decision.notin_(excluded),  # type: ignore[attr-defined]
        )
        .order_by(TelemetryEvent.device_ts.asc())  # type: ignore[attr-defined]
    )
    rows = list(session.exec(stmt))  # type: ignore[call-overload]
    timeline: list[ScoreTimelinePoint] = []
    for we, te in rows:
        # Normalise device_ts to UTC Z format.
        try:
            dt = datetime.datetime.fromisoformat(te.device_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            at_str = dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            at_str = te.device_ts
        timeline.append(ScoreTimelinePoint(at=at_str, score=we.score))
    return timeline


def _day_to_response(session: Session, device: Device, day: Day) -> DayResponse:
    assert device.id is not None
    timeline = _build_score_timeline(session, device.id, day.local_date)
    return DayResponse(
        local_date=day.local_date,
        kind=day.kind,
        tz=day.tz,
        window=_build_window_dict(day),
        state=day.state,
        confirmed_at=day.confirmed_at,
        delivered_at=day.delivered_at,
        fallback_used=bool(day.fallback_used),
        ticks_sent=day.ticks_sent,
        ticks_received=day.ticks_received,
        reel=None,  # M3/M4 future
        score_timeline=timeline,
        actual_wake_local=day.actual_wake_local,
    )


def _fetch_day_or_404(session: Session, device: Device, local_date_str: str) -> Day:
    """Fetch Day row or raise 404."""
    assert device.id is not None
    stmt = select(Day).where(
        Day.local_date == local_date_str,
        Day.device_id == device.id,
    )
    day = session.exec(stmt).first()  # type: ignore[call-overload]
    if day is None:
        raise ProblemException(
            status_code=404,
            code="not_found",
            title="Not Found",
            detail=f"No day row for local_date={local_date_str!r}.",
        )
    return day


def _parse_local_date_str(s: str) -> str:
    """Validate and return a local_date string, raising 422 on bad format."""
    try:
        datetime.date.fromisoformat(s)
    except ValueError as exc:
        raise ProblemException(
            status_code=422,
            code="validation",
            title="Unprocessable Entity",
            detail=f"Invalid local_date {s!r}: must be YYYY-MM-DD.",
        ) from exc
    return s


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/today")
def get_day_today(
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> DayResponse:
    """Return the day row for the device's current local date.

    Uses ``device.tz`` (from the DB row) + server UTC now to derive local_date.

    Returns:
        200 with day shape, or 404 if no telemetry received yet for today.
    """
    rules, _ = get_rules_cache().get_effective(session)
    window_cfg = rules.window.to_window_config()

    now_utc = datetime.datetime.now(tz=datetime.UTC)
    tz_obj = ZoneInfo(device.tz)
    local_date = local_date_for(now_utc, tz_obj, window_cfg.day_boundary)
    local_date_str = local_date.isoformat()

    return _day_to_response(session, device, _fetch_day_or_404(session, device, local_date_str))


@router.get("/{local_date_str}")
def get_day(
    local_date_str: str,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> DayResponse:
    """Return the day row for a specific local date.

    Returns:
        200 with day shape, or 404 if not found, or 422 if date format invalid.
    """
    _parse_local_date_str(local_date_str)
    return _day_to_response(session, device, _fetch_day_or_404(session, device, local_date_str))


@router.put("/{local_date_str}/actual-wake", dependencies=[Depends(rate_limit("default", 120))])
def put_actual_wake(
    local_date_str: str,
    body: ActualWakeBody,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> DayResponse:
    """Set the owner's actual wake time for a local date (idempotent overwrite).

    Args:
        local_date_str: YYYY-MM-DD local date.
        body: JSON body with ``actual_wake_local`` in HH:MM format.

    Returns:
        200 with the updated day shape.
    """
    _parse_local_date_str(local_date_str)
    day = _fetch_day_or_404(session, device, local_date_str)
    day.actual_wake_local = body.actual_wake_local
    day.updated_at = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    session.add(day)
    session.commit()
    session.refresh(day)
    return _day_to_response(session, device, day)
