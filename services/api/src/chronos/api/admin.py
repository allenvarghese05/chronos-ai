"""Explicit, single-attempt visible test push to the authenticated device only."""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text, update
from sqlmodel import Session

from chronos.auth import require_device
from chronos.db import get_session_dep
from chronos.delivery.push import PushClient, PushMessage
from chronos.enums import JobKind
from chronos.errors import ProblemException
from chronos.models.delivery import PushNotification
from chronos.models.devices import Device
from chronos.ratelimit import rate_limit
from chronos.time import utc_now_iso
from chronos.wake.rules import get_rules_cache
from chronos.wake.window import local_date_for

router = APIRouter(prefix="/admin", tags=["admin"])
_limit = rate_limit("push-test", per_minute=1)
TEST_TIMEOUT_SECONDS = 5.0


class PushTestResult(BaseModel):
    """Ticket outcome, never proof of APNs delivery or a provider error dump."""

    model_config = ConfigDict(extra="forbid")
    outcome: Literal["accepted", "failed", "unavailable", "unknown"]
    reason: Literal[
        "ticket_accepted", "provider_rejected", "not_registered", "not_configured", "uncertain"
    ]


def test_push_client(request: Request) -> PushClient:
    """Resolve the lifespan-owned client; tests may override only this dependency."""
    return request.app.state.push_client


@router.post("/push-test", response_model=PushTestResult)
async def push_test(
    request: Request,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
    client: PushClient = Depends(test_push_client),
    _rl: None = Depends(_limit),
) -> PushTestResult:
    """Send fixed visible content once, with no caller-selected token or payload."""
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > 1024:
        raise ProblemException(
            413, "payload_too_large", "Payload Too Large", "Test body is too large."
        )
    raw = await request.body()
    if len(raw) > 1024:
        raise ProblemException(
            413, "payload_too_large", "Payload Too Large", "Test body is too large."
        )
    if raw.strip() not in (b"", b"{}"):
        raise ProblemException(
            422, "validation", "Invalid Test Request", "Test push accepts no fields."
        )
    if not device.expo_push_token:
        return PushTestResult(outcome="unavailable", reason="not_registered")
    rules, _ = get_rules_cache().get_effective(session)
    now = datetime.datetime.now(datetime.UTC)
    local_date = local_date_for(
        now, ZoneInfo(device.tz), rules.window.to_window_config().day_boundary
    )
    message = PushMessage(
        to=device.expo_push_token,
        _contentAvailable=False,
        title="Chronos connection test",
        body="Your laptop sent this test notification.",
        sound="default",
        data={"type": "test"},
    )
    outcome: Literal["accepted", "failed", "unavailable", "unknown"] = "unknown"
    reason: Literal[
        "ticket_accepted", "provider_rejected", "not_registered", "not_configured", "uncertain"
    ] = "uncertain"
    ticket_id: str | None = None
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            tickets = await client.send_once([message])
        if len(tickets) == 1 and tickets[0].status == "ok" and tickets[0].id:
            outcome, reason, ticket_id = "accepted", "ticket_accepted", tickets[0].id
        elif len(tickets) == 1 and tickets[0].status == "error":
            outcome, reason = "failed", "provider_rejected"
            details = tickets[0].details or {}
            if (
                details.get("error") == "DeviceNotRegistered"
                or tickets[0].message == "DeviceNotRegistered"
            ):
                # A newer foreground registration may have replaced this token
                # while Expo was answering. Never erase that newer registration.
                session.exec(
                    update(Device)
                    .where(text("id=:device_id AND expo_push_token=:token"))
                    .values(expo_push_token=None),
                    params={"device_id": device.id, "token": message.to},
                )
    except (TimeoutError, Exception):
        # A request may have reached Expo before its response was lost. No retry.
        outcome, reason = "unknown", "uncertain"
    assert device.id is not None
    session.add(
        PushNotification(
            device_id=device.id,
            kind="test",
            local_date=local_date.isoformat(),
            payload_json=json.dumps(message.to_payload_dict_without_token()),
            ticket_id=ticket_id,
            ticket_status="ok" if outcome == "accepted" else "error",
            sent_at=utc_now_iso(),
            error=None if outcome == "accepted" else reason,
        )
    )
    session.commit()
    return PushTestResult(outcome=outcome, reason=reason)


# ── pipeline run (T3.5) ──────────────────────────────────────────────────────

_run_limit = rate_limit("admin-run", per_minute=6)


class RunRequest(BaseModel):
    """Run pipeline stages for the caller's device; defaults to today's content stages."""

    model_config = ConfigDict(extra="forbid")
    local_date: datetime.date | None = None
    stages: list[JobKind] = Field(
        default_factory=lambda: [JobKind.ingest, JobKind.script], min_length=1, max_length=6
    )
    force: bool = False


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_date: datetime.date
    stages: list[JobKind]
    job_id: int | None
    job_key: str | None
    job_status: str | None


@router.post("/run", response_model=RunResult, status_code=202)
def run_pipeline(
    body: RunRequest,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
    _rl: None = Depends(_run_limit),
) -> RunResult:
    """Enqueue the first stage; the worker runs the chain. Idempotent unless ``force``."""
    from chronos.jobs.queue import JobQueue
    from chronos.pipeline import enqueue_chain, ensure_day, implemented

    assert device.id is not None
    now = datetime.datetime.now(datetime.UTC)
    local_date = ensure_day(session, device, now, body.local_date)
    stages = implemented(body.stages)
    if not stages:
        raise ProblemException(
            422,
            "no_runnable_stages",
            "No Runnable Stages",
            "None of the stages is implemented yet.",
        )
    job = enqueue_chain(
        JobQueue(session.get_bind()),  # type: ignore[arg-type]
        local_date=local_date, device_id=device.id, stages=stages, now=now, force=body.force,
    )  # fmt: skip
    return RunResult(
        local_date=local_date,
        stages=stages,
        job_id=job.id if job else None,
        job_key=job.key if job else None,
        job_status=job.status if job else None,
    )


# ── manual delivery (T5.2) ───────────────────────────────────────────────────

_deliver_limit = rate_limit("admin-deliver", per_minute=3)


class DeliverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_date: datetime.date | None = None
    force: bool = False


class DeliverResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_date: datetime.date
    outcome: Literal[
        "sent", "already_delivered", "not_due", "pending_render", "no_token", "send_failed"
    ]


@router.post("/deliver", response_model=DeliverResult)
async def deliver_now(
    body: DeliverRequest,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
    client: PushClient = Depends(test_push_client),
    _rl: None = Depends(_deliver_limit),
) -> DeliverResult:
    """Deliver the day's push now. ``force`` moves an undelivered day to ``fallback`` first."""
    from chronos.delivery.deliver import deliver_day
    from chronos.enums import DayState, TransitionCause
    from chronos.models.wake import Day, DayTransition
    from chronos.pipeline import ensure_day

    assert device.id is not None
    now = datetime.datetime.now(datetime.UTC)
    local_date = ensure_day(session, device, now, body.local_date)
    day = session.get(Day, (local_date.isoformat(), device.id))
    assert day is not None
    if body.force and day.state in (DayState.idle.value, DayState.monitoring.value):
        stamp = utc_now_iso()
        session.add(
            DayTransition(local_date=day.local_date, device_id=device.id, from_state=day.state,
                          to_state=DayState.fallback.value, cause=TransitionCause.manual.value,
                          caused_by_evaluation_id=None, at=stamp)
        )  # fmt: skip
        day.state = DayState.fallback.value
        day.updated_at = stamp
        session.add(day)
        session.commit()
    outcome = await deliver_day(session, device, day, client, now)
    return DeliverResult(local_date=local_date, outcome=outcome)
