"""Read-only debug contract, isolation, rolling-time paging and honest tick correlation."""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from chronos.auth import hash_token, new_token
from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.enums import DeviceStatus
from chronos.ids import new_ulid
from chronos.models.delivery import PushNotification
from chronos.models.devices import Device
from chronos.models.wake import TelemetryEvent, WakeEvaluation

NOW = dt.datetime(2026, 11, 1, 8, 0, tzinfo=dt.UTC)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _insert_device(
    settings: Settings, *, status: DeviceStatus = DeviceStatus.active
) -> tuple[str, int]:
    token = new_token()
    with Session(create_db_engine(settings.database_url)) as session:
        device = Device(
            public_id=new_ulid(),
            name="Debug fixture",
            token_hash=hash_token(token),
            tz="America/Los_Angeles",
            app_version="0.0.1",
            ios_version="18.6",
            model="Simulator",
            status=status,
            created_at=NOW.isoformat(),
            last_seen_at=NOW.isoformat(),
        )
        session.add(device)
        session.commit()
        session.refresh(device)
        assert device.id is not None
        return token, device.id


def _base_payload(*, seq: int, device_ts: dt.datetime) -> dict[str, object]:
    return {
        "device_ts": device_ts.isoformat(),
        "tz": "America/Los_Angeles",
        "wake_source": "silent_push",
        "steps_10m": 35,
        "steps_since_window": 120,
        "walking_sec_10m": 95,
        "walking_conf": "high",
        "stationary_sec_10m": 30,
        "is_unlocked": True,
        "battery_state": "unplugged",
        "battery_level": 0.78,
        "app_state": "background",
        "low_power_mode": False,
        "pedometer_auth": "authorized",
        "seq": seq,
        "app_version": "0.0.1",
        "ios_version": "18.6",
    }


def _post_telemetry(client: TestClient, token: str) -> None:
    _event(client, token, 0, dt.datetime(2026, 10, 6, 13, 48, tzinfo=dt.UTC))


def _get(client: TestClient, token: str, path: str = "/v1/debug/wake"):
    with time_machine.travel(NOW, tick=False):
        return client.get(path, headers=_auth(token))


def _event(client: TestClient, token: str, seq: int, at: dt.datetime, **extra: object) -> None:
    payload = _base_payload(seq=seq, device_ts=at)
    payload.update(extra)
    with time_machine.travel(at + dt.timedelta(seconds=1), tick=False):
        result = client.post("/v1/telemetry", json=payload, headers=_auth(token))
    assert result.status_code == 200, result.text


def _push(settings: Settings, device: int, age: float, seq: int, **extra: object) -> int:
    values: dict[str, object] = {
        "device_id": device,
        "kind": "tick",
        "local_date": "2026-11-01",
        "tick_seq": seq,
        "payload_json": '{"private":"DO_NOT_EXPOSE"}',
        "ticket_status": "ok",
        "sent_at": (NOW - dt.timedelta(seconds=age)).isoformat(),
    }
    values.update(extra)
    with Session(create_db_engine(settings.database_url)) as session:
        row = PushNotification.model_validate(values)
        session.add(row)
        session.commit()
        session.refresh(row)
        assert row.id is not None
        return row.id


@pytest.mark.parametrize("path", ["wake", "scores", "ticks"])
def test_debug_requires_bearer_and_rejects_revoked(
    client: TestClient, test_settings: Settings, path: str
) -> None:
    assert client.get(f"/v1/debug/{path}").status_code == 401
    token, _ = _insert_device(test_settings, status=DeviceStatus.revoked)
    assert _get(client, token, f"/v1/debug/{path}").status_code == 403


def test_empty_debug_and_persisted_original_pair(
    client: TestClient, test_settings: Settings
) -> None:
    token, _ = _insert_device(test_settings)
    empty = _get(client, token)
    assert empty.status_code == 200
    assert empty.json()["latest"] is None and empty.json()["day"] is None
    at = NOW - dt.timedelta(minutes=5)
    _event(client, token, 7, at, wake_source="manual")
    result = _get(client, token)
    assert result.status_code == 200, result.text
    body = result.json()
    latest = body["latest"]
    assert latest["signals"]["seq"] == 7
    assert latest["signals"]["wake_source"] == "manual"
    assert "derived" in latest["evaluation"]
    assert body["server_time"] == NOW.isoformat().replace("+00:00", "Z")
    assert body["active_rule_version"] >= 1
    with Session(create_db_engine(test_settings.database_url)) as session:
        saved = session.exec(select(WakeEvaluation)).one()
        assert latest["evaluation"] == json.loads(saved.breakdown_json)
    assert "token" not in result.text and "payload_json" not in result.text


def test_debug_device_isolation(client: TestClient, test_settings: Settings) -> None:
    first, first_id = _insert_device(test_settings)
    second, _ = _insert_device(test_settings)
    _event(client, first, 1, NOW - dt.timedelta(minutes=1))
    _push(test_settings, first_id, 100, 1)
    assert _get(client, second).json()["latest"] is None
    for kind in ("scores", "ticks"):
        assert _get(client, second, f"/v1/debug/{kind}").json()["points"] == []


def test_explicit_annotation_day_and_unchanged_state(
    client: TestClient, test_settings: Settings
) -> None:
    token, _ = _insert_device(test_settings)
    _post_telemetry(client, token)
    path = "/v1/debug/wake?local_date=2026-10-06"
    before = _get(client, token, path).json()
    assert before["day"]["local_date"] == "2026-10-06"
    assert before["day"]["tz"] == "America/Los_Angeles"
    for _ in range(2):
        response = client.put(
            "/v1/day/2026-10-06/actual-wake",
            json={"actual_wake_local": "07:05"},
            headers=_auth(token),
        )
        assert response.status_code == 200
    after = _get(client, token, path).json()
    assert after["day"]["actual_wake_local"] == "07:05"
    assert after["day"]["state"] == before["day"]["state"]
    assert after["latest"] == before["latest"]
    assert _get(client, token, "/v1/debug/wake?local_date=20261006").status_code == 422


def test_rolling_24h_offsets_and_ties_paginate(client: TestClient, test_settings: Settings) -> None:
    token, _ = _insert_device(test_settings)
    # 01:15 at -05 occurs after 01:30 at -04. Stored string order would be wrong.
    times = [
        dt.datetime.fromisoformat(value)
        for value in (
            "2026-11-01T01:30:00-04:00",
            "2026-11-01T01:15:00-05:00",
        )
    ]
    for seq, at in enumerate(times):
        _event(client, token, seq, at)
    # Insert enough same-time rows directly from existing validated records to test stable IDs.
    with Session(create_db_engine(test_settings.database_url)) as session:
        original = session.exec(select(TelemetryEvent)).first()
        evaluation = session.exec(select(WakeEvaluation)).first()
        assert original and evaluation
        for seq in range(2, 45):
            row = TelemetryEvent.model_validate({**original.model_dump(), "id": None, "seq": seq})
            session.add(row)
            session.flush()
            clone = WakeEvaluation.model_validate(
                {
                    **evaluation.model_dump(),
                    "id": None,
                    "telemetry_event_id": row.id,
                }
            )
            session.add(clone)
        session.commit()
    first = _get(client, token, "/v1/debug/scores")
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["points"]) == 40 and page["next_cursor"]
    assert page["points"][0]["seq"] == 1
    assert dt.datetime.fromisoformat(page["end"]) - dt.datetime.fromisoformat(
        page["start"]
    ) == dt.timedelta(hours=24)
    # A concurrent old-capture insert must not enter an already frozen traversal.
    with Session(create_db_engine(test_settings.database_url)) as session:
        event = session.exec(select(TelemetryEvent)).first()
        evaluation = session.exec(select(WakeEvaluation)).first()
        assert event and evaluation
        arrival = TelemetryEvent.model_validate({**event.model_dump(), "id": None, "seq": 99})
        session.add(arrival)
        session.flush()
        session.add(
            WakeEvaluation.model_validate(
                {
                    **evaluation.model_dump(),
                    "id": None,
                    "telemetry_event_id": arrival.id,
                }
            )
        )
        session.commit()
    second = _get(client, token, f"/v1/debug/scores?cursor={page['next_cursor']}").json()
    points = page["points"] + second["points"]
    assert len(points) == 45 and len({p["event_id"] for p in points}) == 45
    assert second["end"] == page["end"] and second["next_cursor"] is None
    assert len(first.content) < 32768
    other_token, _ = _insert_device(test_settings)
    assert (
        _get(client, other_token, f"/v1/debug/scores?cursor={page['next_cursor']}").status_code
        == 422
    )


def test_range_cutoff_and_frozen_arrivals(client: TestClient, test_settings: Settings) -> None:
    token, _ = _insert_device(test_settings)
    _event(client, token, 0, NOW - dt.timedelta(days=1, seconds=1))
    _event(client, token, 1, NOW - dt.timedelta(days=1))
    _event(client, token, 2, NOW - dt.timedelta(seconds=1))
    page = _get(client, token, "/v1/debug/scores").json()
    assert {p["seq"] for p in page["points"]} == {1, 2}


@pytest.mark.parametrize("cursor", ["!", "a" * 513, base64.urlsafe_b64encode(b"{}").decode()])
def test_rejects_bad_cursor(client: TestClient, test_settings: Settings, cursor: str) -> None:
    token, _ = _insert_device(test_settings)
    assert _get(client, token, f"/v1/debug/scores?cursor={cursor}").status_code == 422


def test_tick_grace_failure_manual_and_daily_identity(
    client: TestClient, test_settings: Settings
) -> None:
    token, device = _insert_device(test_settings)
    _push(test_settings, device, 59, 1)
    _push(test_settings, device, 60, 2)
    _push(test_settings, device, 120, 3, ticket_status="error", error="DO_NOT_EXPOSE")
    _push(test_settings, device, 120, 4)
    _push(test_settings, device, 120, 5, local_date="2026-10-31")
    _event(client, token, 7, NOW - dt.timedelta(seconds=10), wake_source="manual", tick_seq=2)
    # LA local day for these UTC times is 2026-11-01 only after 03:00; insert correlation
    # via existing stored row projection to isolate tick identity rather than scoring gates.
    with Session(create_db_engine(test_settings.database_url)) as session:
        original = session.exec(select(TelemetryEvent)).one()
        for seq, tick, local_date in ((8, 4, "2026-11-01"), (9, 5, "2026-11-01")):
            session.add(
                TelemetryEvent.model_validate(
                    {
                        **original.model_dump(),
                        "id": None,
                        "seq": seq,
                        "tick_seq": tick,
                        "wake_source": "silent_push",
                        "local_date": local_date,
                    }
                )
            )
        session.commit()
    result = _get(client, token, "/v1/debug/ticks")
    assert result.status_code == 200, result.text
    statuses = {p["tick_seq"]: p["status"] for p in result.json()["points"]}
    assert statuses == {1: "awaiting", 2: "overdue", 3: "send_failed", 4: "received", 5: "overdue"}
    assert "DO_NOT_EXPOSE" not in result.text and "receipt" not in result.text


def test_debug_response_rejects_corrupt_and_oversize_records(
    client: TestClient, test_settings: Settings
) -> None:
    token, _ = _insert_device(test_settings)
    _event(client, token, 1, NOW - dt.timedelta(seconds=10))
    with Session(create_db_engine(test_settings.database_url)) as session:
        row = session.exec(select(WakeEvaluation)).one()
        stored = json.loads(row.breakdown_json)
        stored["notes"] = ["x" * 32768]
        row.breakdown_json = json.dumps(stored)
        session.add(row)
        session.commit()
    assert _get(client, token).status_code == 503
    with Session(create_db_engine(test_settings.database_url)) as session:
        row = session.exec(select(WakeEvaluation)).one()
        row.breakdown_json = '{"secret":"DO_NOT_EXPOSE"}'
        session.add(row)
        session.commit()
    response = _get(client, token)
    assert response.status_code == 503 and "DO_NOT_EXPOSE" not in response.text


def test_debug_rate_limit(
    client: TestClient, test_settings: Settings, frozen_rate_limit_clocks: None
) -> None:
    token, _ = _insert_device(test_settings)
    for _ in range(120):
        assert _get(client, token).status_code == 200
    response = _get(client, token)
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0


def test_tick_paging_and_late_correlation(client: TestClient, test_settings: Settings) -> None:
    token, device = _insert_device(test_settings)
    for seq in range(45):
        _push(test_settings, device, 120 + seq, seq)
    first = _get(client, token, "/v1/debug/ticks").json()
    second = _get(client, token, f"/v1/debug/ticks?cursor={first['next_cursor']}").json()
    assert len(first["points"]) == 40 and len(second["points"]) == 5
    assert len({p["attempt_id"] for p in first["points"] + second["points"]}) == 45
    _event(client, token, 99, NOW - dt.timedelta(seconds=10), tick_seq=0)
    with Session(create_db_engine(test_settings.database_url)) as session:
        event = session.exec(select(TelemetryEvent)).one()
        event.local_date = "2026-11-01"
        session.add(event)
        session.commit()
    refreshed = _get(client, token, "/v1/debug/ticks").json()
    assert refreshed["points"][0]["status"] == "received"
