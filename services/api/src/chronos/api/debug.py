"""Bounded, device-scoped read-only wake diagnostics; no scoring is performed here."""

from __future__ import annotations

import base64
import datetime as dt
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, func, or_
from sqlmodel import Session, col, select

from chronos import __version__
from chronos.auth import require_device
from chronos.db import get_session_dep
from chronos.enums import EvaluationDecision, WakeSource
from chronos.errors import ProblemException
from chronos.models.delivery import PushNotification
from chronos.models.devices import Device
from chronos.models.wake import Day, TelemetryEvent, WakeEvaluation
from chronos.ratelimit import rate_limit
from chronos.wake.rules import get_rules_cache
from chronos.wake.scoring import Evaluation
from chronos.wake.signals import Signals
from chronos.wake.window import local_date_for

router = APIRouter(
    prefix="/debug", tags=["debug"], dependencies=[Depends(rate_limit("default", 120))]
)
PAGE_SIZE = 40


class DebugModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DebugDay(DebugModel):
    """Annotation target is explicit; HH:MM is a wall time, not an inferred instant."""

    local_date: str
    tz: str
    state: str
    actual_wake_local: str | None


class StoredSample(DebugModel):
    event_id: int
    evaluation_id: int
    received_at: str
    evaluated_at: str
    signals: Signals
    evaluation: Evaluation


class DebugSnapshot(DebugModel):
    server_time: str
    server_version: str
    active_rule_version: int
    day: DebugDay | None
    latest: StoredSample | None


class ScorePoint(DebugModel):
    event_id: int
    evaluation_id: int
    at: str
    seq: int
    score: int = Field(ge=0, le=100)
    decision: EvaluationDecision
    wake_source: WakeSource
    local_date: str


class TickPoint(DebugModel):
    attempt_id: int
    at: str
    local_date: str
    tick_seq: int | None
    status: Literal["received", "awaiting", "overdue", "send_failed"]


class HistoryPage(DebugModel):
    start: str
    end: str
    next_cursor: str | None


class ScorePage(HistoryPage):
    points: list[ScorePoint] = Field(max_length=PAGE_SIZE)


class TickPage(HistoryPage):
    points: list[TickPoint] = Field(max_length=PAGE_SIZE)


class Cursor(DebugModel):
    version: Literal[1] = 1
    kind: Literal["scores", "ticks"]
    anchor: dt.datetime
    last_id: int = Field(ge=1, le=2**63 - 1)
    max_id: int = Field(ge=1, le=2**63 - 1)


def _utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed.astimezone(dt.UTC)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _invalid_cursor() -> ProblemException:
    return ProblemException(
        status_code=422,
        code="validation",
        title="Invalid diagnostics cursor",
        detail="Refresh diagnostics to start a new bounded history interval.",
    )


def _page_cursor(
    raw: str | None, kind: Literal["scores", "ticks"], now: dt.datetime
) -> tuple[dt.datetime, int | None, int | None]:
    if raw is None:
        return now, None, None
    try:
        if len(raw) > 512:
            raise ValueError("long cursor")
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        cursor = Cursor.model_validate_json(data)
        if (
            cursor.kind != kind
            or cursor.last_id > cursor.max_id
            or cursor.anchor.tzinfo is None
            or cursor.anchor > now
            or cursor.anchor < now - dt.timedelta(hours=24)
        ):
            raise ValueError("invalid anchor")
        return cursor.anchor.astimezone(dt.UTC), cursor.last_id, cursor.max_id
    except (ValueError, ValidationError) as exc:
        raise _invalid_cursor() from exc


def _encode(
    kind: Literal["scores", "ticks"], anchor: dt.datetime, last_id: int, max_id: int
) -> str:
    return (
        base64.urlsafe_b64encode(
            Cursor(kind=kind, anchor=anchor, last_id=last_id, max_id=max_id)
            .model_dump_json()
            .encode()
        )
        .decode()
        .rstrip("=")
    )


def _bounded[Model: BaseModel](model: Model) -> Model:
    """Retain complete stored breakdowns or fail explicitly, never silently truncate them."""
    if len(model.model_dump_json().encode()) > 30 * 1024:
        raise ProblemException(
            status_code=503,
            code="diagnostics_unavailable",
            title="Diagnostics unavailable",
            detail="Stored diagnostics exceed the bounded response limit.",
        )
    return model


@router.get("/wake")
def get_wake_debug(
    local_date: str | None = Query(default=None, max_length=10),
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> DebugSnapshot:
    """Return the original latest sample/evaluation and explicit annotation day, without scoring."""
    now = dt.datetime.now(dt.UTC)
    rules, version = get_rules_cache().get_effective(session)
    if local_date is not None:
        try:
            if dt.date.fromisoformat(local_date).isoformat() != local_date:
                raise ValueError("noncanonical date")
        except ValueError as exc:
            raise _invalid_cursor() from exc
    else:
        local_date = local_date_for(
            now, ZoneInfo(device.tz), rules.window.to_window_config().day_boundary
        ).isoformat()
    day = session.exec(
        select(Day).where(Day.device_id == device.id, Day.local_date == local_date)
    ).first()
    latest = session.exec(
        select(TelemetryEvent)
        .where(TelemetryEvent.device_id == device.id)
        .order_by(col(TelemetryEvent.id).desc())
        .limit(1)
    ).first()
    stored: StoredSample | None = None
    if latest is not None:
        evaluation = session.exec(
            select(WakeEvaluation).where(WakeEvaluation.telemetry_event_id == latest.id)
        ).first()
        if evaluation is not None:
            try:
                signals = Signals.model_validate_json(latest.raw_json)
                parsed = Evaluation.model_validate_json(evaluation.breakdown_json)
                assert latest.id is not None and evaluation.id is not None
                if signals.seq != latest.seq:
                    raise ValueError("stored sequence mismatch")
                stored = StoredSample(
                    event_id=latest.id,
                    evaluation_id=evaluation.id,
                    received_at=latest.received_at,
                    evaluated_at=evaluation.evaluated_at,
                    signals=signals,
                    evaluation=parsed,
                )
            except (ValueError, ValidationError) as exc:
                raise ProblemException(
                    status_code=503,
                    code="diagnostics_unavailable",
                    title="Diagnostics unavailable",
                    detail="Stored diagnostics could not be validated.",
                ) from exc
    return _bounded(
        DebugSnapshot(
            server_time=_iso(now),
            server_version=__version__,
            active_rule_version=version.version,
            day=DebugDay(
                local_date=day.local_date,
                tz=day.tz,
                state=day.state,
                actual_wake_local=day.actual_wake_local,
            )
            if day
            else None,
            latest=stored,
        )
    )


@router.get("/scores")
def get_score_history(
    cursor: str | None = Query(default=None, max_length=512),
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> ScorePage:
    """Return offset-normalized score history over a frozen elapsed 24-hour interval."""
    anchor, last_id, max_id = _page_cursor(cursor, "scores", dt.datetime.now(dt.UTC))
    if max_id is None:
        max_id = (
            session.exec(
                select(func.max(col(TelemetryEvent.id))).where(
                    TelemetryEvent.device_id == device.id
                )
            ).one()
            or 0
        )
    start = anchor - dt.timedelta(hours=24)
    instant = func.julianday(col(TelemetryEvent.device_ts))
    stmt = (
        select(TelemetryEvent, WakeEvaluation)
        .join(WakeEvaluation, col(WakeEvaluation.telemetry_event_id) == col(TelemetryEvent.id))
        .where(
            TelemetryEvent.device_id == device.id,
            col(TelemetryEvent.id) <= max_id,
            func.julianday(col(TelemetryEvent.received_at)) <= func.julianday(_iso(anchor)),
            instant >= func.julianday(_iso(start)),
            instant <= func.julianday(_iso(anchor)),
        )
    )
    if last_id is not None:
        last = session.exec(
            select(TelemetryEvent).where(
                TelemetryEvent.id == last_id, TelemetryEvent.device_id == device.id
            )
        ).first()
        if (
            last is None
            or not start <= _utc(last.device_ts) <= anchor
            or _utc(last.received_at) > anchor
        ):
            raise _invalid_cursor()
        last_instant = func.julianday(last.device_ts)
        stmt = stmt.where(
            or_(
                instant < last_instant,
                and_(instant == last_instant, col(TelemetryEvent.id) < last_id),
            )
        )
    rows = list(
        session.exec(
            stmt.order_by(instant.desc(), col(TelemetryEvent.id).desc()).limit(PAGE_SIZE + 1)
        )
    )
    points = [
        ScorePoint(
            event_id=event.id,
            evaluation_id=evaluation.id,
            at=_iso(_utc(event.device_ts)),
            seq=event.seq,
            score=evaluation.score,
            decision=EvaluationDecision(evaluation.decision),
            wake_source=WakeSource(event.wake_source),
            local_date=event.local_date,
        )
        for event, evaluation in rows[:PAGE_SIZE]
        if event.id is not None and evaluation.id is not None
    ]
    return _bounded(
        ScorePage(
            start=_iso(start),
            end=_iso(anchor),
            points=points,
            next_cursor=_encode("scores", anchor, points[-1].event_id, max_id)
            if len(rows) > PAGE_SIZE
            else None,
        )
    )


@router.get("/ticks")
def get_tick_history(
    cursor: str | None = Query(default=None, max_length=512),
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> TickPage:
    """Return actual send attempts; receipt status never implies execution."""
    anchor, last_id, max_id = _page_cursor(cursor, "ticks", dt.datetime.now(dt.UTC))
    if max_id is None:
        max_id = (
            session.exec(
                select(func.max(col(PushNotification.id))).where(
                    PushNotification.device_id == device.id
                )
            ).one()
            or 0
        )
    start = anchor - dt.timedelta(hours=24)
    instant = func.julianday(col(PushNotification.sent_at))
    stmt = select(PushNotification).where(
        PushNotification.device_id == device.id,
        col(PushNotification.id) <= max_id,
        PushNotification.kind == "tick",
        instant >= func.julianday(_iso(start)),
        instant <= func.julianday(_iso(anchor)),
    )
    if last_id is not None:
        last = session.exec(
            select(PushNotification).where(
                PushNotification.id == last_id,
                PushNotification.device_id == device.id,
                PushNotification.kind == "tick",
            )
        ).first()
        if last is None or not start <= _utc(last.sent_at) <= anchor:
            raise _invalid_cursor()
        stamp = func.julianday(last.sent_at)
        stmt = stmt.where(
            or_(instant < stamp, and_(instant == stamp, col(PushNotification.id) < last_id))
        )
    rows = list(
        session.exec(
            stmt.order_by(instant.desc(), col(PushNotification.id).desc()).limit(PAGE_SIZE + 1)
        )
    )
    points: list[TickPoint] = []
    for push in rows[:PAGE_SIZE]:
        assert push.id is not None
        matched = None
        if push.tick_seq is not None:
            matched = session.exec(
                select(TelemetryEvent.id)
                .where(
                    TelemetryEvent.device_id == device.id,
                    TelemetryEvent.local_date == push.local_date,
                    TelemetryEvent.tick_seq == push.tick_seq,
                    TelemetryEvent.wake_source == "silent_push",
                    func.julianday(col(TelemetryEvent.received_at)) <= func.julianday(_iso(anchor)),
                )
                .limit(1)
            ).first()
        status: Literal["received", "awaiting", "overdue", "send_failed"] = (
            "send_failed"
            if push.ticket_status != "ok"
            else "received"
            if matched is not None
            else "overdue"
            if (anchor - _utc(push.sent_at)).total_seconds() >= 60
            else "awaiting"
        )
        points.append(
            TickPoint(
                attempt_id=push.id,
                at=_iso(_utc(push.sent_at)),
                local_date=push.local_date,
                tick_seq=push.tick_seq,
                status=status,
            )
        )
    return _bounded(
        TickPage(
            start=_iso(start),
            end=_iso(anchor),
            points=points,
            next_cursor=_encode("ticks", anchor, points[-1].attempt_id, max_id)
            if len(rows) > PAGE_SIZE
            else None,
        )
    )
