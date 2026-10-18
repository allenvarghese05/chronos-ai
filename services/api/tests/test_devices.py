"""Tests for GET/PATCH/DELETE /v1/device/me and auth dependency.

Coverage:
- Token works (bearer → 200).
- Revoked token → 403.
- Garbage token → 401.
- Missing Authorization header → 401.
- GET /v1/device/me returns correct fields.
- PATCH with extra field → 422.
- PATCH with bad timezone → 422.
- PATCH expo_push_token persists.
- DELETE revokes device; subsequent request → 403.
- last_seen_at throttle: bumped at most once per 60 s.
- 121st default request → 429.
"""

from __future__ import annotations

import time_machine
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from chronos.auth import hash_token, new_token
from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.enums import DeviceStatus
from chronos.ids import new_ulid
from chronos.main import create_app
from chronos.models.devices import Device
from chronos.ratelimit import TokenBucket, reset_all
from chronos.time import utc_now_iso


# ── helpers ───────────────────────────────────────────────────────────────────
def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "test",
        "secret_key": "test-secret-key-32-bytes-exactly!",
        "database_url": "sqlite:///data/test.db",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _insert_device(
    settings: Settings,
    *,
    status: DeviceStatus = DeviceStatus.active,
) -> tuple[str, Device]:
    """Insert a Device row and return (plaintext_token, row)."""
    engine = create_db_engine(settings.database_url)
    plain = new_token()
    now = utc_now_iso()
    device = Device(
        public_id=new_ulid(),
        name="Test iPhone",
        token_hash=hash_token(plain),
        expo_push_token=None,
        tz="America/New_York",
        app_version="1.0.0",
        ios_version="17.4",
        model="iPhone 15",
        status=status,
        created_at=now,
        last_seen_at=now,
        revoked_at=None,
    )
    with Session(engine) as session:
        session.add(device)
        session.commit()
        session.refresh(device)
    return plain, device


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── auth dependency ───────────────────────────────────────────────────────────
class TestAuthDependency:
    def test_valid_token_200(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        r = client.get("/v1/device/me", headers=_auth(plain))
        assert r.status_code == 200

    def test_missing_header_401(self, client: TestClient) -> None:
        r = client.get("/v1/device/me")
        assert r.status_code == 401
        assert r.json()["code"] == "unauthorized"

    def test_garbage_token_401(self, client: TestClient) -> None:
        r = client.get("/v1/device/me", headers=_auth("not-a-real-token"))
        assert r.status_code == 401
        assert r.json()["code"] == "unauthorized"

    def test_revoked_token_403(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings, status=DeviceStatus.revoked)
        r = client.get("/v1/device/me", headers=_auth(plain))
        assert r.status_code == 403
        assert r.json()["code"] == "revoked"

    def test_malformed_header_401(self, client: TestClient) -> None:
        r = client.get("/v1/device/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert r.status_code == 401

    def test_empty_bearer_401(self, client: TestClient) -> None:
        r = client.get("/v1/device/me", headers={"Authorization": "Bearer "})
        assert r.status_code == 401

    def test_token_hashed_not_stored_plain(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        plain, _ = _insert_device(test_settings)
        engine = create_db_engine(test_settings.database_url)
        with Session(engine) as session:
            device = session.exec(select(Device)).first()
        assert device is not None
        assert device.token_hash != plain
        assert device.token_hash == hash_token(plain)


# ── GET /v1/device/me ─────────────────────────────────────────────────────────
class TestGetDevice:
    def test_get_fields(self, client: TestClient, test_settings: Settings) -> None:
        plain, device = _insert_device(test_settings)
        body = client.get("/v1/device/me", headers=_auth(plain)).json()
        assert body["device_id"] == device.public_id
        assert body["name"] == device.name
        assert body["tz"] == device.tz
        assert body["status"] == DeviceStatus.active
        assert "token" not in body
        assert "token_hash" not in body

    def test_content_type(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        r = client.get("/v1/device/me", headers=_auth(plain))
        assert "application/json" in r.headers["content-type"]


# ── PATCH /v1/device/me ───────────────────────────────────────────────────────
class TestPatchDevice:
    def test_patch_push_token(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        r = client.patch(
            "/v1/device/me",
            headers=_auth(plain),
            json={"expo_push_token": "ExponentPushToken[abc123]"},
        )
        assert r.status_code == 200
        assert r.json()["expo_push_token"] == "ExponentPushToken[abc123]"

    def test_patch_push_token_persists(self, client: TestClient, test_settings: Settings) -> None:
        """Push token written by PATCH must survive a subsequent GET."""
        plain, _ = _insert_device(test_settings)
        client.patch(
            "/v1/device/me",
            headers=_auth(plain),
            json={"expo_push_token": "ExponentPushToken[xyz]"},
        )
        body = client.get("/v1/device/me", headers=_auth(plain)).json()
        assert body["expo_push_token"] == "ExponentPushToken[xyz]"

    def test_patch_timezone(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        r = client.patch("/v1/device/me", headers=_auth(plain), json={"tz": "Europe/London"})
        assert r.status_code == 200
        assert r.json()["tz"] == "Europe/London"

    def test_patch_extra_field_422(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        r = client.patch(
            "/v1/device/me",
            headers=_auth(plain),
            json={"unknown_field": "oops"},
        )
        assert r.status_code == 422

    def test_patch_bad_timezone_422(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        r = client.patch(
            "/v1/device/me",
            headers=_auth(plain),
            json={"tz": "Not/A/Valid/Timezone"},
        )
        assert r.status_code == 422

    def test_patch_empty_body_ok(self, client: TestClient, test_settings: Settings) -> None:
        """Empty PATCH body ({}) is a valid no-op."""
        plain, _ = _insert_device(test_settings)
        r = client.patch("/v1/device/me", headers=_auth(plain), json={})
        assert r.status_code == 200


# ── DELETE /v1/device/me ──────────────────────────────────────────────────────
class TestDeleteDevice:
    def test_delete_returns_204(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        r = client.delete("/v1/device/me", headers=_auth(plain))
        assert r.status_code == 204

    def test_delete_revokes_token(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        client.delete("/v1/device/me", headers=_auth(plain))
        r = client.get("/v1/device/me", headers=_auth(plain))
        assert r.status_code == 403
        assert r.json()["code"] == "revoked"

    def test_delete_sets_revoked_at(self, client: TestClient, test_settings: Settings) -> None:
        plain, _ = _insert_device(test_settings)
        client.delete("/v1/device/me", headers=_auth(plain))
        engine = create_db_engine(test_settings.database_url)
        with Session(engine) as session:
            device = session.exec(select(Device)).first()
        assert device is not None
        assert device.status == DeviceStatus.revoked
        assert device.revoked_at is not None


# ── last_seen_at throttle ─────────────────────────────────────────────────────
class TestLastSeenThrottle:
    def test_last_seen_at_not_bumped_within_60s(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        """Two requests within 60 s must not produce two different last_seen_at values."""
        plain, _ = _insert_device(test_settings)
        engine = create_db_engine(test_settings.database_url)

        # First request bumps last_seen_at (new device, always bumped).
        client.get("/v1/device/me", headers=_auth(plain))
        with Session(engine) as session:
            device = session.exec(select(Device)).first()
        assert device is not None
        first_seen = device.last_seen_at

        # Second request immediately after — should NOT bump.
        client.get("/v1/device/me", headers=_auth(plain))
        with Session(engine) as session:
            device = session.exec(select(Device)).first()
        assert device is not None
        assert device.last_seen_at == first_seen

    @time_machine.travel("2026-01-01T00:00:00Z", tick=False)
    def test_last_seen_at_bumped_after_60s(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        """After 60+ seconds, last_seen_at must be updated."""

        plain, _ = _insert_device(test_settings)
        engine = create_db_engine(test_settings.database_url)

        client.get("/v1/device/me", headers=_auth(plain))
        with Session(engine) as session:
            device = session.exec(select(Device)).first()
        assert device is not None
        first_seen = device.last_seen_at

        with time_machine.travel("2026-01-01T00:01:01Z", tick=False):
            client.get("/v1/device/me", headers=_auth(plain))
            with Session(engine) as session:
                device = session.exec(select(Device)).first()
            assert device is not None
            assert device.last_seen_at != first_seen


# ── rate limiting ─────────────────────────────────────────────────────────────
class TestDeviceRateLimit:
    def test_token_bucket_121st_blocked(self) -> None:
        """120 req/min bucket: 121st request is blocked (injectable clock, no sleeping)."""
        # Use a fixed clock so no refill occurs between checks.
        clock = lambda: 0.0  # noqa: E731
        bucket = TokenBucket(per_minute=120, clock=clock)
        for _ in range(120):
            allowed, _ = bucket.check("ip")
            assert allowed
        allowed, retry_after = bucket.check("ip")
        assert not allowed
        assert retry_after >= 1

    def test_121st_request_returns_429(self, test_settings: Settings) -> None:
        """121 rapid requests to GET /v1/device/me → 429 on the last."""
        reset_all()
        app = create_app(settings=test_settings)
        plain, _ = _insert_device(test_settings)
        with TestClient(app) as c:
            for i in range(120):
                r = c.get("/v1/device/me", headers=_auth(plain))
                assert r.status_code == 200, f"Expected 200 on request {i + 1}, got {r.status_code}"
            r = c.get("/v1/device/me", headers=_auth(plain))
            assert r.status_code == 429
            assert r.json()["code"] == "rate_limited"
            assert "Retry-After" in r.headers
