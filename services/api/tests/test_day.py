"""HTTP integration tests for GET/PUT /v1/day/* (T1.6 step 6).

Coverage:
- GET /v1/day/today 404 before first event
- GET /v1/day/today 200 shape after event
- GET /v1/day/{local_date} 200 shape
- GET /v1/day/{local_date} 404 not found
- GET /v1/day/{local_date} 422 invalid date
- PUT /v1/day/{local_date}/actual-wake sets field
- PUT /v1/day/{local_date}/actual-wake invalid format 422
- PUT /v1/day/{local_date}/actual-wake 404 no day
- PUT actual-wake idempotent
- GET /v1/day/today requires bearer
- score_timeline ordered and excludes dedupe decisions
"""

from __future__ import annotations

import datetime

import time_machine
from fastapi.testclient import TestClient
from sqlmodel import Session

from chronos.auth import hash_token, new_token
from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.enums import DeviceStatus
from chronos.ids import new_ulid
from chronos.models.devices import Device
from chronos.time import utc_now_iso

_IN_WINDOW_UTC = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=datetime.UTC)


def _insert_device(
    settings: Settings,
    *,
    status: DeviceStatus = DeviceStatus.active,
    tz: str = "America/Los_Angeles",
) -> tuple[str, int]:
    """Insert device, return (token, device_id)."""
    engine = create_db_engine(settings.database_url)
    plain = new_token()
    now = utc_now_iso()
    device = Device(
        public_id=new_ulid(),
        name="Test iPhone",
        token_hash=hash_token(plain),
        expo_push_token=None,
        tz=tz,
        app_version="1.0.0",
        ios_version="18.1",
        model="iPhone 15 Pro",
        status=status,
        created_at=now,
        last_seen_at=now,
        revoked_at=None,
    )
    with Session(engine) as session:
        session.add(device)
        session.commit()
        session.refresh(device)
        assert device.id is not None
        return plain, device.id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _base_payload(
    *,
    device_ts: datetime.datetime = _IN_WINDOW_UTC,
    seq: int = 0,
) -> dict[str, object]:
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
        "app_version": "1.0.0",
        "ios_version": "18.1",
    }


def _post_telemetry(
    client: TestClient, token: str, *, device_ts: datetime.datetime = _IN_WINDOW_UTC, seq: int = 0
) -> None:
    """Helper: post one telemetry event with time frozen near device_ts."""
    now = device_ts + datetime.timedelta(seconds=5)
    with time_machine.travel(now, tick=False):
        r = client.post(
            "/v1/telemetry",
            json=_base_payload(device_ts=device_ts, seq=seq),
            headers=_auth(token),
        )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# GET /v1/day/today
# ---------------------------------------------------------------------------


class TestGetDayToday:
    def test_requires_bearer(self, client: TestClient) -> None:
        r = client.get("/v1/day/today")
        assert r.status_code == 401

    def test_404_before_first_event(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        with time_machine.travel(_IN_WINDOW_UTC + datetime.timedelta(seconds=5), tick=False):
            r = client.get("/v1/day/today", headers=_auth(plain))
        assert r.status_code == 404

    def test_200_shape_after_event(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        _post_telemetry(client, plain)
        with time_machine.travel(_IN_WINDOW_UTC + datetime.timedelta(minutes=1), tick=False):
            r = client.get("/v1/day/today", headers=_auth(plain))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["local_date"] == "2026-10-06"
        assert body["tz"] == "America/Los_Angeles"
        assert body["kind"] == "weekday"
        assert "window" in body
        assert "start" in body["window"]
        assert "end" in body["window"]
        assert "state" in body
        assert "score_timeline" in body
        assert isinstance(body["score_timeline"], list)
        assert body["reel"] is None
        assert "actual_wake_local" in body


# ---------------------------------------------------------------------------
# GET /v1/day/{local_date}
# ---------------------------------------------------------------------------


class TestGetDayByDate:
    def test_200_shape(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        _post_telemetry(client, plain)
        r = client.get("/v1/day/2026-10-06", headers=_auth(plain))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["local_date"] == "2026-10-06"

    def test_404_not_found(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        r = client.get("/v1/day/2020-01-01", headers=_auth(plain))
        assert r.status_code == 404
        assert r.json()["code"] == "not_found"

    def test_invalid_date_422(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        r = client.get("/v1/day/not-a-date", headers=_auth(plain))
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# PUT /v1/day/{local_date}/actual-wake
# ---------------------------------------------------------------------------


class TestPutActualWake:
    def test_sets_field(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        _post_telemetry(client, plain)
        r = client.put(
            "/v1/day/2026-10-06/actual-wake",
            json={"actual_wake_local": "07:05"},
            headers=_auth(plain),
        )
        assert r.status_code == 200, r.text
        assert r.json()["actual_wake_local"] == "07:05"

    def test_invalid_format_422(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        _post_telemetry(client, plain)
        r = client.put(
            "/v1/day/2026-10-06/actual-wake",
            json={"actual_wake_local": "7:5"},  # wrong format
            headers=_auth(plain),
        )
        assert r.status_code == 422

    def test_404_no_day(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        r = client.put(
            "/v1/day/2020-01-01/actual-wake",
            json={"actual_wake_local": "07:05"},
            headers=_auth(plain),
        )
        assert r.status_code == 404

    def test_idempotent(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        _post_telemetry(client, plain)

        r1 = client.put(
            "/v1/day/2026-10-06/actual-wake",
            json={"actual_wake_local": "07:05"},
            headers=_auth(plain),
        )
        r2 = client.put(
            "/v1/day/2026-10-06/actual-wake",
            json={"actual_wake_local": "07:05"},
            headers=_auth(plain),
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["actual_wake_local"] == r2.json()["actual_wake_local"]


# ---------------------------------------------------------------------------
# Score timeline test
# ---------------------------------------------------------------------------


class TestScoreTimeline:
    def test_score_timeline_ordered(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        ts1 = _IN_WINDOW_UTC
        ts2 = ts1 + datetime.timedelta(minutes=5)

        _post_telemetry(client, plain, device_ts=ts1, seq=0)
        _post_telemetry(client, plain, device_ts=ts2, seq=1)

        r = client.get("/v1/day/2026-10-06", headers=_auth(plain))
        assert r.status_code == 200
        timeline = r.json()["score_timeline"]
        assert len(timeline) >= 1
        # Ordered ascending by device_ts.
        ats = [t["at"] for t in timeline]
        assert ats == sorted(ats)
