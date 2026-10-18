"""Tests for POST /v1/pair — device pairing flow.

Coverage:
- Happy path: valid code → 201 with device_id/token/api_base/server_time.
- Wrong code x5 burns; 6th correct -> 400.
- Expired code → 410.
- Consumed code → 400.
- Conflict (active device exists) → 409.
- Token is hashed at rest (plaintext never stored).
- Rate limit: 6th pairing attempt in a minute → 429 with Retry-After.
- Config test: 0.0.0.0 bind_host without public_base_url raises.
"""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from chronos.auth import (
    generate_pairing_code,
    hash_token,
    normalise_pairing_code,
)
from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.enums import DeviceStatus
from chronos.main import create_app
from chronos.models.devices import Device, PairingCode
from chronos.ratelimit import TokenBucket, reset_all
from chronos.time import parse_utc_iso, utc_now_iso

# ── helpers ───────────────────────────────────────────────────────────────────


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "test",
        "secret_key": "test-secret-key-32-bytes-exactly!",
        "database_url": "sqlite:///data/test.db",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _insert_code(
    settings: Settings,
    *,
    minutes_until_expiry: int = 10,
    consumed: bool = False,
    attempts: int = 0,
) -> tuple[str, PairingCode]:
    """Insert a PairingCode row and return (raw_code, row)."""
    engine = create_db_engine(settings.database_url)
    raw_code = generate_pairing_code()
    code_hash = hash_token(normalise_pairing_code(raw_code))
    now_iso = utc_now_iso()
    expires_dt = parse_utc_iso(now_iso) + timedelta(minutes=minutes_until_expiry)
    expires_iso = expires_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    consumed_at = utc_now_iso() if consumed else None
    pc = PairingCode(
        code_hash=code_hash,
        expires_at=expires_iso,
        consumed_at=consumed_at,
        attempts=attempts,
    )
    with Session(engine) as session:
        session.add(pc)
        session.commit()
        session.refresh(pc)
    return raw_code, pc


def _pair_body(code: str, tz: str = "America/New_York") -> dict[str, object]:
    return {
        "code": f"{code[:4]}-{code[4:]}",
        "device": {
            "name": "Allen's iPhone",
            "model": "iPhone 15 Pro",
            "ios_version": "17.4",
            "app_version": "1.0.0",
            "tz": tz,
        },
    }


# ── config validation ─────────────────────────────────────────────────────────


class TestConfigValidation:
    def test_zero_host_without_public_base_url_raises(self, tmp_db_url: str) -> None:
        with pytest.raises(ValueError, match="CHRONOS_PUBLIC_BASE_URL must be set"):
            make_settings(
                database_url=tmp_db_url,
                api_bind_host="0.0.0.0",  # nosec B104  # noqa: S104
            )

    def test_public_base_url_derived_from_host_port(self, tmp_db_url: str) -> None:
        s = make_settings(database_url=tmp_db_url, api_bind_host="127.0.0.1", api_bind_port=8443)
        assert s.public_base_url == "http://127.0.0.1:8443/v1"

    def test_public_base_url_explicit(self, tmp_db_url: str) -> None:
        s = make_settings(
            database_url=tmp_db_url,
            public_base_url="http://192.168.1.5:8000/v1",
        )
        assert s.public_base_url == "http://192.168.1.5:8000/v1"

    def test_public_base_url_zero_rejected(self, tmp_db_url: str) -> None:
        with pytest.raises(ValueError, match=r"must not use 0\.0\.0\.0"):
            make_settings(
                database_url=tmp_db_url,
                public_base_url="http://0.0.0.0:8000/v1",
            )


# ── happy path ────────────────────────────────────────────────────────────────


class TestPairHappyPath:
    def test_pair_returns_201(self, client: TestClient, test_settings: Settings) -> None:
        raw_code, _ = _insert_code(test_settings)
        r = client.post("/v1/pair", json=_pair_body(raw_code))
        assert r.status_code == 201

    def test_pair_response_fields(self, client: TestClient, test_settings: Settings) -> None:
        raw_code, _ = _insert_code(test_settings)
        body = client.post("/v1/pair", json=_pair_body(raw_code)).json()
        assert "device_id" in body
        assert "token" in body
        assert "api_base" in body
        assert "server_time" in body

    def test_pair_token_hashed_at_rest(self, client: TestClient, test_settings: Settings) -> None:
        """The plaintext token must NOT be stored in the DB."""
        raw_code, _ = _insert_code(test_settings)
        body = client.post("/v1/pair", json=_pair_body(raw_code)).json()
        plaintext = body["token"]
        engine = create_db_engine(test_settings.database_url)
        with Session(engine) as session:
            device = session.exec(select(Device)).first()
        assert device is not None
        assert device.token_hash != plaintext
        assert device.token_hash == hash_token(plaintext)

    def test_pair_code_consumed_after_success(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        raw_code, _ = _insert_code(test_settings)
        client.post("/v1/pair", json=_pair_body(raw_code))
        engine = create_db_engine(test_settings.database_url)
        with Session(engine) as session:
            pc = session.exec(select(PairingCode)).first()
        assert pc is not None
        assert pc.consumed_at is not None

    def test_pair_code_with_dash(self, client: TestClient, test_settings: Settings) -> None:
        """Code submitted with XXXX-XXXX dash format should be accepted."""
        raw_code, _ = _insert_code(test_settings)
        body = _pair_body(raw_code)
        body["code"] = f"{raw_code[:4]}-{raw_code[4:]}"
        r = client.post("/v1/pair", json=body)
        assert r.status_code == 201

    def test_pair_code_without_dash(self, client: TestClient, test_settings: Settings) -> None:
        """Code submitted without dash should also be accepted."""
        raw_code, _ = _insert_code(test_settings)
        body = _pair_body(raw_code)
        body["code"] = raw_code
        r = client.post("/v1/pair", json=body)
        assert r.status_code == 201

    def test_pair_api_base_in_response(self, client: TestClient, test_settings: Settings) -> None:
        raw_code, _ = _insert_code(test_settings)
        body = client.post("/v1/pair", json=_pair_body(raw_code)).json()
        assert body["api_base"] == test_settings.public_base_url

    def test_device_row_created(self, client: TestClient, test_settings: Settings) -> None:
        raw_code, _ = _insert_code(test_settings)
        body = client.post("/v1/pair", json=_pair_body(raw_code)).json()
        engine = create_db_engine(test_settings.database_url)
        with Session(engine) as session:
            device = session.exec(select(Device)).first()
        assert device is not None
        assert device.public_id == body["device_id"]
        assert device.status == DeviceStatus.active
        assert device.tz == "America/New_York"


# ── error cases ───────────────────────────────────────────────────────────────


class TestPairErrors:
    def test_wrong_code_400(self, client: TestClient, test_settings: Settings) -> None:
        _insert_code(test_settings)
        # generate a different code so it won't match
        wrong = generate_pairing_code()
        r = client.post("/v1/pair", json=_pair_body(wrong))
        assert r.status_code == 400
        assert r.json()["code"] == "pairing_invalid"

    def test_wrong_x5_burns_code(self, client: TestClient, test_settings: Settings) -> None:
        """5 wrong attempts burn the code; correct code after burn → 400.

        The pairing rate limit is 5/min per IP.  After 5 wrong attempts that
        burn the code, we reset the rate limiter before the 6th request so we
        can verify the *burned-code* 400 rather than the rate-limit 429.
        """
        raw_code, _ = _insert_code(test_settings)
        wrong = generate_pairing_code()
        # ensure wrong code doesn't accidentally match
        while wrong == raw_code:  # pragma: no cover
            wrong = generate_pairing_code()

        for _ in range(5):
            r = client.post("/v1/pair", json=_pair_body(wrong))
            assert r.status_code == 400

        # Reset the rate limiter so the 6th request tests burned-code behaviour,
        # not rate-limit 429.  (Rate-limit 429 is tested separately.)
        reset_all()

        # The correct code is now burned: should return 400 pairing_invalid.
        r = client.post("/v1/pair", json=_pair_body(raw_code))
        assert r.status_code == 400
        assert r.json()["code"] == "pairing_invalid"

    def test_attempts_incremented_on_wrong(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        raw_code, _ = _insert_code(test_settings)
        wrong = generate_pairing_code()
        while wrong == raw_code:  # pragma: no cover
            wrong = generate_pairing_code()

        for expected_attempts in range(1, 4):
            client.post("/v1/pair", json=_pair_body(wrong))
            engine = create_db_engine(test_settings.database_url)
            with Session(engine) as session:
                pc = session.exec(select(PairingCode)).first()
            assert pc is not None
            assert pc.attempts == expected_attempts

    def test_expired_code_410(self, client: TestClient, test_settings: Settings) -> None:
        raw_code, _ = _insert_code(test_settings, minutes_until_expiry=-1)
        r = client.post("/v1/pair", json=_pair_body(raw_code))
        assert r.status_code == 410
        assert r.json()["code"] == "pairing_expired"

    def test_consumed_code_400(self, client: TestClient, test_settings: Settings) -> None:
        """A previously consumed code must return 400 (not 410)."""
        raw_code, _ = _insert_code(test_settings, consumed=True)
        r = client.post("/v1/pair", json=_pair_body(raw_code))
        assert r.status_code == 400
        assert r.json()["code"] == "pairing_invalid"

    def test_conflict_when_active_device_exists(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        # First pair succeeds.
        raw_code1, _ = _insert_code(test_settings)
        r = client.post("/v1/pair", json=_pair_body(raw_code1))
        assert r.status_code == 201

        # Second pair attempt while active → 409.
        raw_code2, _ = _insert_code(test_settings)
        r2 = client.post("/v1/pair", json=_pair_body(raw_code2))
        assert r2.status_code == 409
        assert r2.json()["code"] == "conflict"

    def test_no_active_code_400(self, client: TestClient) -> None:
        """POST /v1/pair with no pairing code in DB → 400."""
        r = client.post(
            "/v1/pair",
            json=_pair_body("AAAA" + "BBBB"),  # doesn't matter — no row to find
        )
        assert r.status_code == 400
        assert r.json()["code"] == "pairing_invalid"

    def test_bad_timezone_422(self, client: TestClient, test_settings: Settings) -> None:
        raw_code, _ = _insert_code(test_settings)
        body = _pair_body(raw_code, tz="Invalid/Zone")
        r = client.post("/v1/pair", json=body)
        assert r.status_code == 422

    def test_extra_field_422(self, client: TestClient, test_settings: Settings) -> None:
        raw_code, _ = _insert_code(test_settings)
        body = _pair_body(raw_code)
        body["unknown_field"] = "oops"
        r = client.post("/v1/pair", json=body)
        assert r.status_code == 422


# ── rate limiting ─────────────────────────────────────────────────────────────


class TestPairingRateLimit:
    def test_sixth_request_429(self, test_settings: Settings) -> None:
        """6th pairing attempt in a minute → 429 with Retry-After."""
        reset_all()
        app = create_app(settings=test_settings)
        with TestClient(app) as c:
            # insert a fresh code each iteration (we only care about the 429, not the pair result)
            for i in range(5):
                _insert_code(test_settings)
                r = c.post("/v1/pair", json=_pair_body(generate_pairing_code()))
                assert r.status_code in (201, 400, 409, 410), f"Unexpected {r.status_code} on {i}"
            # 6th request — rate limiter should fire regardless of body validity
            r = c.post("/v1/pair", json=_pair_body(generate_pairing_code()))
            assert r.status_code == 429
            assert r.json()["code"] == "rate_limited"
            assert "Retry-After" in r.headers

    def test_token_bucket_injectable_clock(self) -> None:
        """TokenBucket with injectable clock: 6th check (over limit of 5) is blocked."""
        # Use a fixed clock so no refill occurs between checks.
        clock = lambda: 0.0  # noqa: E731
        bucket = TokenBucket(per_minute=5, clock=clock)
        for _ in range(5):
            allowed, _ = bucket.check("ip")
            assert allowed
        allowed, retry_after = bucket.check("ip")
        assert not allowed
        assert retry_after > 0

    def test_retry_after_header_present(self, test_settings: Settings) -> None:
        reset_all()
        app = create_app(settings=test_settings)
        with TestClient(app) as c:
            r = c.post("/v1/pair", json=_pair_body(generate_pairing_code()))
            for _ in range(5):
                r = c.post("/v1/pair", json=_pair_body(generate_pairing_code()))
            assert r.headers.get("retry-after") is not None
