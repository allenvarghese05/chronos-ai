"""HTTP integration tests for POST /v1/telemetry (T1.6 step 6).

Coverage:
- 401 missing/invalid bearer
- 403 revoked device
- 429 31st request in a minute
- 422 clock_skew with server_time in detail
- 200 valid in-window payload (correct shape)
- 200 out-of-window event -> decision=outside_window
- Duplicate seq -> same evaluation_id (idempotency)
- Duplicate seq, different payload -> first evaluation returned
- Integration: typical_weekday fixture -> two ticks -> confirmed
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from chronos.auth import hash_token, new_token
from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.enums import DeviceStatus
from chronos.ids import new_ulid
from chronos.models.devices import Device
from chronos.models.wake import DayTransition
from chronos.time import utc_now_iso

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "wake" / "mornings"
_WEEKDAY_FIXTURE = _FIXTURE_DIR / "typical_weekday.json"

# 2026-10-06 Tuesday 13:48 UTC = 06:48 LA — in window.
_IN_WINDOW_UTC = "2026-10-06T13:48:00+00:00"
_BEFORE_WINDOW_UTC = "2026-10-06T12:30:00+00:00"  # 05:30 LA, before 06:00 window


def _insert_device(
    settings: Settings,
    *,
    status: DeviceStatus = DeviceStatus.active,
    tz: str = "America/Los_Angeles",
) -> tuple[str, Device]:
    engine = create_db_engine(settings.database_url)
    plain = new_token()
    now = utc_now_iso()
    public_id = new_ulid()
    device = Device(
        public_id=public_id,
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
        # Detach a copy with id set.
        device_id = device.id

    # Return a minimal stand-in; callers only need the plaintext token.
    device.id = device_id
    return plain, device


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _base_payload(
    *,
    device_ts: str = _IN_WINDOW_UTC,
    tz: str = "America/Los_Angeles",
    seq: int = 0,
    steps_10m: int = 35,
) -> dict[str, object]:
    return {
        "device_ts": device_ts,
        "tz": tz,
        "wake_source": "silent_push",
        "steps_10m": steps_10m,
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


# ---------------------------------------------------------------------------
# Auth / rate-limit tests
# ---------------------------------------------------------------------------


class TestTelemetryAuth:
    def test_requires_bearer_401(self, client: TestClient) -> None:
        r = client.post("/v1/telemetry", json=_base_payload())
        assert r.status_code == 401
        assert r.json()["code"] == "unauthorized"

    def test_revoked_device_403(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings, status=DeviceStatus.revoked)
        r = client.post("/v1/telemetry", json=_base_payload(), headers=_auth(plain))
        assert r.status_code == 403
        assert r.json()["code"] == "revoked"

    def test_rate_limit_31st_request_429(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)

        # Exhaust the bucket: 30 requests (all hit 422 clock_skew — doesn't matter).
        for i in range(30):
            client.post(
                "/v1/telemetry",
                json=_base_payload(seq=i),
                headers=_auth(plain),
            )
        # 31st should be rate-limited before the clock-skew check.
        r = client.post(
            "/v1/telemetry",
            json=_base_payload(seq=30),
            headers=_auth(plain),
        )
        assert r.status_code == 429
        assert r.json()["code"] == "rate_limited"
        assert "Retry-After" in r.headers


# ---------------------------------------------------------------------------
# Clock-skew test
# ---------------------------------------------------------------------------


class TestTelemetryClockSkew:
    def test_clock_skew_422_with_server_time(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        """device_ts 600 s stale → 422 clock_skew; server_time is a structured extension member."""
        import time_machine

        plain, _ = _insert_device(test_settings)
        # Freeze server at a known UTC instant; send device_ts 600 s earlier.
        frozen_now = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=datetime.UTC)
        stale_ts = frozen_now - datetime.timedelta(seconds=600)
        payload = _base_payload(device_ts=stale_ts.isoformat())
        with time_machine.travel(frozen_now, tick=False):
            r = client.post("/v1/telemetry", json=payload, headers=_auth(plain))
        assert r.status_code == 422
        body = r.json()
        assert body["code"] == "clock_skew"
        # detail still describes the skew in human-readable form.
        assert "seconds" in body["detail"]
        # server_time is a structured RFC 9457 extension member (not buried in detail text).
        assert "server_time" in body, f"server_time missing from body: {body}"
        server_time_str: str = body["server_time"]
        parsed_st = datetime.datetime.fromisoformat(server_time_str.replace("Z", "+00:00"))
        assert abs((parsed_st - frozen_now).total_seconds()) < 2, (
            f"server_time {server_time_str!r} not close to frozen clock"
        )


# ---------------------------------------------------------------------------
# Valid payload shape test
# ---------------------------------------------------------------------------


class TestTelemetryValidPayload:
    def test_valid_in_window_200_shape(self, client: TestClient, test_settings: Settings) -> None:
        """Valid in-window payload returns 200 with correct response shape."""
        import time_machine

        plain, _ = _insert_device(test_settings)
        device_ts = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=datetime.UTC)
        # Freeze server time close to device_ts so skew check passes.
        with time_machine.travel(device_ts + datetime.timedelta(seconds=5), tick=False):
            r = client.post(
                "/v1/telemetry",
                json=_base_payload(device_ts=device_ts.isoformat(), seq=0),
                headers=_auth(plain),
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "evaluation_id" in body
        assert "local_date" in body
        assert "gate" in body
        assert "score" in body
        assert "decision" in body
        assert "confirm_reason" in body
        assert "threshold" in body
        assert "consecutive_high" in body
        assert "rule_version" in body
        assert "breakdown" in body
        assert "penalties" in body
        assert "day_state" in body
        assert body["next_tick_in_sec"] is None

    def test_out_of_window_decision_outside_window(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        """Before-window event → gate=closed, decision=outside_window."""
        import time_machine

        plain, _ = _insert_device(test_settings)
        device_ts = datetime.datetime(2026, 10, 6, 12, 30, 0, tzinfo=datetime.UTC)  # 05:30 LA
        with time_machine.travel(device_ts + datetime.timedelta(seconds=5), tick=False):
            r = client.post(
                "/v1/telemetry",
                json=_base_payload(device_ts=device_ts.isoformat(), seq=0),
                headers=_auth(plain),
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["gate"] == "closed"
        assert body["decision"] == "outside_window"
        assert body["day_state"] == "idle"


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


class TestTelemetryIdempotency:
    def test_duplicate_seq_returns_same_evaluation_id(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        import time_machine

        plain, _ = _insert_device(test_settings)
        device_ts = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=datetime.UTC)
        payload = _base_payload(device_ts=device_ts.isoformat(), seq=0)

        with time_machine.travel(device_ts + datetime.timedelta(seconds=5), tick=False):
            r1 = client.post("/v1/telemetry", json=payload, headers=_auth(plain))
            r2 = client.post("/v1/telemetry", json=payload, headers=_auth(plain))

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["evaluation_id"] == r2.json()["evaluation_id"]
        # Full response bodies must be identical for the same frozen clock.
        assert r1.json() == r2.json(), (
            f"Duplicate seq responses differ:\nr1={r1.json()}\nr2={r2.json()}"
        )

    def test_duplicate_seq_different_payload_first_evaluation(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        import time_machine

        plain, _ = _insert_device(test_settings)
        device_ts = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=datetime.UTC)
        p1 = _base_payload(device_ts=device_ts.isoformat(), seq=0, steps_10m=35)
        p2 = _base_payload(device_ts=device_ts.isoformat(), seq=0, steps_10m=60)

        with time_machine.travel(device_ts + datetime.timedelta(seconds=5), tick=False):
            r1 = client.post("/v1/telemetry", json=p1, headers=_auth(plain))
            r2 = client.post("/v1/telemetry", json=p2, headers=_auth(plain))

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["evaluation_id"] == r2.json()["evaluation_id"]


# ---------------------------------------------------------------------------
# Integration: typical_weekday fixture -> confirmed
# ---------------------------------------------------------------------------


class TestTelemetryTypicalWeekdayIntegration:
    """Replay the typical_weekday fixture and assert monitoring→confirmed."""

    def test_typical_weekday_confirms(self, client: TestClient, test_settings: Settings) -> None:
        import time_machine

        plain, _ = _insert_device(test_settings)
        fixture = json.loads(_WEEKDAY_FIXTURE.read_text())
        events = fixture["events"]

        responses: list[dict[str, object]] = []
        for i, event in enumerate(events):
            event.pop("_comment", None)
            expected = event.pop("expect")
            payload = dict(event.items())

            device_ts = datetime.datetime.fromisoformat(payload["device_ts"]).astimezone(
                datetime.UTC
            )
            server_now = device_ts + datetime.timedelta(seconds=5)

            with time_machine.travel(server_now, tick=False):
                r = client.post("/v1/telemetry", json=payload, headers=_auth(plain))

            assert r.status_code == 200, f"Event {i} failed: {r.text}"
            body = r.json()
            assert body["gate"] == expected["gate"], f"Event {i}: gate mismatch"
            assert body["score"] == expected["score"], f"Event {i}: score mismatch"
            assert body["decision"] == expected["decision"], f"Event {i}: decision mismatch"
            assert body["consecutive_high"] == expected["consecutive_high"], (
                f"Event {i}: consecutive_high mismatch"
            )
            assert body["confirm_reason"] == expected["confirm_reason"], (
                f"Event {i}: confirm_reason mismatch"
            )
            responses.append(body)

        # After all events, final state should be confirmed.
        last: dict[str, object] = responses[-1]
        assert last["day_state"] == "confirmed"

        # Verify transition rows in DB.
        engine = create_db_engine(test_settings.database_url)
        with Session(engine) as session:
            # Need device id — look up by token hash.
            from chronos.auth import hash_token as ht

            dev = session.exec(  # type: ignore[call-overload]
                select(Device).where(Device.token_hash == ht(plain))
            ).first()
            assert dev is not None
            transitions = list(  # type: ignore[call-overload]
                session.exec(
                    select(DayTransition)
                    .where(DayTransition.device_id == dev.id)
                    .order_by(DayTransition.id)  # type: ignore[arg-type]
                )
            )

        causes = [t.cause for t in transitions]
        assert "scheduler_window_start" in causes, f"Missing idle->monitoring: {causes}"
        assert "evaluation" in causes, f"Missing monitoring->confirmed: {causes}"
