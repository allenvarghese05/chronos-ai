"""T5.2 delivery: exactly-once reel_ready, text fallback, pending renders, scheduler, admin."""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlmodel import Session, select

from chronos.config import Settings
from chronos.content.script.fallback import template_script
from chronos.db import create_db_engine
from chronos.delivery.deliver import deliver_day
from chronos.delivery.push import FakePushClient, PushClientError, PushMessage, Ticket
from chronos.ids import new_ulid
from chronos.models.content import Brief
from chronos.models.delivery import PushNotification
from chronos.models.devices import Device
from chronos.models.media import Reel
from chronos.models.wake import Day, DayTransition
from chronos.time import format_utc_iso
from tests.content.script.helpers import DAY, candidates
from tests.factories import make_day, make_device

NOW = datetime.datetime(2026, 10, 6, 14, 0, tzinfo=datetime.UTC)


def seed(
    session: Session,
    *,
    state: str = "confirmed",
    reel_status: str | None = "ready",
    reel_age: datetime.timedelta = datetime.timedelta(minutes=5),
    token: str | None = "ExponentPushToken[deliver-test]",  # noqa: S107 - fake push token
) -> tuple[Device, Day, Reel | None]:
    device = make_device(session, expo_push_token=token)
    day = make_day(session, device, local_date="2026-10-06", state=state)
    stamp = format_utc_iso(NOW - reel_age)
    brief = Brief(local_date="2026-10-06", device_id=device.id or 0, status="ready",
                  script_json=template_script(candidates(6), DAY).model_dump_json(),
                  created_at=stamp, updated_at=stamp)  # fmt: skip
    session.add(brief)
    session.flush()
    reel = None
    if reel_status is not None:
        reel = Reel(public_id=new_ulid(), brief_id=brief.id or 0, local_date="2026-10-06",
                    device_id=device.id or 0, status=reel_status, created_at=stamp,
                    updated_at=stamp)  # fmt: skip
        session.add(reel)
    session.commit()
    return device, day, reel


def test_confirm_sends_exactly_one_reel_ready(test_settings: Settings, tmp_db_url: str) -> None:
    engine = create_db_engine(tmp_db_url)
    push = FakePushClient()
    with Session(engine) as session:
        device, day, reel = seed(session)
        assert reel is not None
        assert asyncio.run(deliver_day(session, device, day, push, NOW)) == "sent"
        assert asyncio.run(deliver_day(session, device, day, push, NOW)) == "already_delivered"
        (msg,) = push.sent
        assert msg.data == {"type": "reel_ready", "reel_public_id": reel.public_id,
                            "local_date": "2026-10-06", "fallback": False}  # fmt: skip
        assert msg.title == "Good morning ☀️" and msg.sound == "default"
        assert msg.to_payload_dict_without_token()["_contentAvailable"] is False
        session.refresh(day)
        assert day.state == "delivered" and day.delivered_at == format_utc_iso(NOW)
        note = session.exec(select(PushNotification)).one()
        assert note.kind == "reel_ready" and note.reel_id == reel.id and note.ticket_status == "ok"
        assert "deliver-test" not in note.payload_json  # token never stored
        t = session.exec(select(DayTransition)).one()
        assert (t.from_state, t.to_state, t.cause) == ("confirmed", "delivered", "deliver_ok")


def test_confirm_racing_window_end_still_one_push(test_settings: Settings, tmp_db_url: str) -> None:
    """AC: confirm and window-end race -> exactly one reel_ready push."""
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        seed(session)
    push = FakePushClient()

    async def race() -> list[str]:
        with Session(engine) as a, Session(engine) as b:
            dev_a, day_a = a.exec(select(Device)).one(), a.exec(select(Day)).one()
            dev_b, day_b = b.exec(select(Device)).one(), b.exec(select(Day)).one()
            return list(await asyncio.gather(deliver_day(a, dev_a, day_a, push, NOW),
                                             deliver_day(b, dev_b, day_b, push, NOW)))  # fmt: skip

    outcomes = asyncio.run(race())
    assert sorted(outcomes) == ["already_delivered", "sent"]
    assert len(push.sent) == 1
    with Session(engine) as session:
        assert len(session.exec(select(PushNotification)).all()) == 1


def test_failed_render_sends_text_fallback_with_headlines(
    test_settings: Settings, tmp_db_url: str
) -> None:
    """AC: failed render -> text_fallback push with headlines."""
    with Session(create_db_engine(tmp_db_url)) as session:
        device, day, _ = seed(session, state="fallback", reel_status="failed")
        push = FakePushClient()
        assert asyncio.run(deliver_day(session, device, day, push, NOW)) == "sent"
        (msg,) = push.sent
        assert msg.data["type"] == "text_fallback" and msg.data["fallback"] is True
        assert msg.body is not None and msg.body.startswith("Today's headlines: Story number 0")
        assert len(msg.data["headlines"]) == 3  # type: ignore[arg-type]
        session.refresh(day)
        assert day.state == "delivered" and day.fallback_used == 1


def test_render_in_progress_waits_then_stalls_to_fallback(
    test_settings: Settings, tmp_db_url: str
) -> None:
    with Session(create_db_engine(tmp_db_url)) as session:
        device, day, _ = seed(session, reel_status="rendering")
        push = FakePushClient()
        assert asyncio.run(deliver_day(session, device, day, push, NOW)) == "pending_render"
        assert push.sent == []
        later = NOW + datetime.timedelta(minutes=40)
        assert asyncio.run(deliver_day(session, device, day, push, later)) == "sent"
        assert push.sent[0].data["type"] == "text_fallback"


def test_no_reel_yet_waits(test_settings: Settings, tmp_db_url: str) -> None:
    with Session(create_db_engine(tmp_db_url)) as session:
        device, day, _ = seed(session, reel_status=None)
        assert asyncio.run(deliver_day(session, device, day, FakePushClient(), NOW)) == (
            "pending_render"
        )


@pytest.mark.parametrize(
    ("state", "token", "expected"),
    [("monitoring", "ExponentPushToken[x]", "not_due"), ("confirmed", None, "no_token")],
)
def test_not_due_and_no_token(
    test_settings: Settings, tmp_db_url: str, state: str, token: str | None, expected: str
) -> None:
    with Session(create_db_engine(tmp_db_url)) as session:
        device, day, _ = seed(session, state=state, token=token)
        assert asyncio.run(deliver_day(session, device, day, FakePushClient(), NOW)) == expected


def test_send_failure_releases_claim_for_retry(test_settings: Settings, tmp_db_url: str) -> None:
    class Flaky(FakePushClient):
        calls = 0

        async def send(self, messages: Sequence[PushMessage]) -> list[Ticket]:
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise PushClientError("network down")
            return await super().send(messages)

    with Session(create_db_engine(tmp_db_url)) as session:
        device, day, _ = seed(session)
        push = Flaky()
        assert asyncio.run(deliver_day(session, device, day, push, NOW)) == "send_failed"
        session.refresh(day)
        assert day.delivered_at is None and day.state == "confirmed"
        assert asyncio.run(deliver_day(session, device, day, push, NOW)) == "sent"


@pytest.mark.anyio
async def test_scheduler_delivers_confirmed_days(test_settings: Settings, tmp_db_url: str) -> None:
    from chronos.scheduler import Scheduler

    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        seed(session)
    push = FakePushClient()
    sched = Scheduler(session_factory=lambda: Session(engine), push_client=push,
                      settings=test_settings)  # fmt: skip
    await sched.run_deliveries(NOW)
    await sched.run_deliveries(NOW + datetime.timedelta(minutes=1))
    assert len(push.sent) == 1 and push.sent[0].data["type"] == "reel_ready"


def test_admin_deliver_force(test_settings: Settings, tmp_db_url: str, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from chronos.api.admin import test_push_client as push_dep
    from chronos.main import create_app
    from tests.test_config_wake import auth_headers, insert_device_for_test

    token, device = insert_device_for_test(test_settings)
    with Session(create_db_engine(tmp_db_url)) as session:
        row = session.get(Device, device.id)
        assert row is not None
        row.expo_push_token = "ExponentPushToken[admin]"
        session.add(row)
        session.commit()
    fake = FakePushClient()
    app = create_app(test_settings)
    app.dependency_overrides[push_dep] = lambda: fake
    with TestClient(app) as client:
        h = auth_headers(token)
        today = client.post("/v1/admin/deliver", headers=h, json={"local_date": "2026-10-06"})
        assert today.json()["outcome"] == "not_due"
        forced = client.post("/v1/admin/deliver", headers=h,
                             json={"local_date": "2026-10-06", "force": True})  # fmt: skip
        assert forced.json()["outcome"] == "sent"
        assert fake.sent[0].data["type"] == "text_fallback"  # no brief -> text fallback
