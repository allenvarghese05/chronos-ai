"""Visible delivery push (T5.2, docs/05 push payloads).

Once a day is ``confirmed`` (wake detected) or ``fallback`` (window ended), the owner gets one
visible push:

- ``reel_ready`` with the reel's public id when the reel is ready;
- ``text_fallback`` with the day's headlines when there is no usable reel (render failed, no
  brief, or a render stalled for more than :data:`RENDER_STALL` minutes);
- nothing yet while a render is still in progress (the scheduler retries every minute).

Exactly-once: a single ``UPDATE days SET delivered_at = :now WHERE ... AND delivered_at IS NULL``
claims the day before anything is sent, so a confirm racing the window end still sends one
push. If the send itself fails the claim is released so the next loop retries.
"""

from __future__ import annotations

import datetime
import json
from typing import Literal

import structlog
from sqlalchemy import text
from sqlmodel import Session, select

from chronos.content.script.schema import ReelScript
from chronos.delivery.push import PushClient, PushClientError, PushMessage
from chronos.enums import DayState, PushKind, ReelStatus, TicketStatus, TransitionCause
from chronos.models.content import Brief
from chronos.models.delivery import PushNotification
from chronos.models.devices import Device
from chronos.models.media import Reel
from chronos.models.wake import Day, DayTransition
from chronos.time import format_utc_iso, parse_utc_iso

log = structlog.get_logger(__name__)

RENDER_STALL = datetime.timedelta(minutes=30)
READY_TITLE = "Good morning ☀️"
READY_BODY = "Your 60-second morning reel is ready."
FALLBACK_TITLE = "Good morning"

Outcome = Literal[
    "sent", "already_delivered", "not_due", "pending_render", "no_token", "send_failed"
]


def _brief_and_reel(session: Session, day: Day) -> tuple[Brief | None, Reel | None]:
    brief = session.exec(
        select(Brief).where(Brief.local_date == day.local_date, Brief.device_id == day.device_id)
    ).first()
    reel = None
    if brief is not None:
        reel = session.exec(select(Reel).where(Reel.brief_id == brief.id)).first()
    return brief, reel


def _headlines(brief: Brief | None, limit: int = 3) -> list[str]:
    if brief is None or brief.status != "ready":
        return []
    try:
        script = ReelScript.model_validate_json(brief.script_json)
    except ValueError:
        return []
    return [s.title for s in script.stories][:limit]


def plan_message(
    brief: Brief | None, reel: Reel | None, day: Day, now: datetime.datetime
) -> tuple[PushKind, dict[str, object], str, str] | None:
    """What to send for *day*; ``None`` while a render is still running."""
    fallback = day.state == DayState.fallback.value
    if reel is not None and reel.status == ReelStatus.ready.value:
        data: dict[str, object] = {"type": "reel_ready", "reel_public_id": reel.public_id,
                                   "local_date": day.local_date, "fallback": fallback}  # fmt: skip
        return PushKind.reel_ready, data, READY_TITLE, READY_BODY
    in_progress = reel is not None and reel.status != ReelStatus.failed.value
    if in_progress and reel is not None and now - parse_utc_iso(reel.updated_at) < RENDER_STALL:
        return None
    if reel is None and brief is not None and brief.status == "ready":
        if now - parse_utc_iso(brief.updated_at) < RENDER_STALL:
            return None  # media stages have not created the reel yet
    titles = _headlines(brief)
    body = (
        ("Today's headlines: " + " · ".join(titles))
        if titles
        else ("Your morning reel isn't ready today.")
    )
    data = {"type": "text_fallback", "local_date": day.local_date, "headlines": titles,
            "fallback": fallback}  # fmt: skip
    return PushKind.text_fallback, data, FALLBACK_TITLE, body[:500]


def _claim(session: Session, day: Day, now_str: str) -> bool:
    result = session.connection().execute(
        text("UPDATE days SET delivered_at = :now WHERE local_date = :d AND device_id = :dev "
             "AND delivered_at IS NULL"),
        {"now": now_str, "d": day.local_date, "dev": day.device_id},
    )  # fmt: skip
    session.commit()
    return (result.rowcount or 0) == 1


def _release(session: Session, day: Day) -> None:
    session.connection().execute(
        text("UPDATE days SET delivered_at = NULL WHERE local_date = :d AND device_id = :dev"),
        {"d": day.local_date, "dev": day.device_id},
    )
    session.commit()


async def deliver_day(
    session: Session, device: Device, day: Day, push_client: PushClient, now: datetime.datetime
) -> Outcome:
    """Send the day's visible push at most once. See the module docstring."""
    session.refresh(day)
    if day.delivered_at is not None or day.state == DayState.delivered.value:
        return "already_delivered"
    if day.state not in (DayState.confirmed.value, DayState.fallback.value):
        return "not_due"
    if device.expo_push_token is None:
        return "no_token"
    brief, reel = _brief_and_reel(session, day)
    planned = plan_message(brief, reel, day, now)
    if planned is None:
        return "pending_render"
    kind, data, title, body = planned
    now_str = format_utc_iso(now)
    if not _claim(session, day, now_str):
        return "already_delivered"

    message = PushMessage(to=device.expo_push_token, _contentAvailable=False, data=data,
                          title=title, body=body, sound="default", priority="high")  # fmt: skip
    try:
        tickets = await push_client.send([message])
    except PushClientError as exc:
        _release(session, day)
        log.warning("deliver_send_failed", device_id=device.id, local_date=day.local_date,
                    error=str(exc)[:200])  # fmt: skip
        return "send_failed"
    ticket = tickets[0] if tickets else None
    session.add(
        PushNotification(
            device_id=day.device_id,
            kind=kind.value,
            local_date=day.local_date,
            reel_id=reel.id if reel is not None and kind == PushKind.reel_ready else None,
            payload_json=json.dumps(message.to_payload_dict_without_token()),
            ticket_id=ticket.id if ticket else None,
            ticket_status=(ticket.status if ticket else TicketStatus.error.value),
            sent_at=now_str,
            error=(ticket.message if ticket and ticket.status != "ok" else None),
        )
    )
    session.refresh(day)
    previous = day.state
    session.add(
        DayTransition(
            local_date=day.local_date,
            device_id=day.device_id,
            from_state=previous,
            to_state=DayState.delivered.value,
            cause=TransitionCause.deliver_ok.value,
            caused_by_evaluation_id=None,
            at=now_str,
        )
    )
    day.state = DayState.delivered.value
    if previous == DayState.fallback.value:
        day.fallback_used = 1
    day.updated_at = now_str
    session.add(day)
    session.commit()
    log.info("delivered", device_id=device.id, local_date=day.local_date, kind=kind.value,
             fallback=previous == DayState.fallback.value)  # fmt: skip
    return "sent"
