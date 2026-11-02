"""Tests for GET /v1/jobs — authenticated safe job metadata listing (T3.1 step 4)."""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlmodel import Session

from chronos.auth import hash_token, new_token
from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.enums import JobKind
from chronos.jobs.queue import JobInput, JobQueue
from chronos.models.devices import Device
from chronos.time import utc_now_iso

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _insert_device(settings: Settings) -> str:
    """Insert an active device and return plaintext token."""
    engine = create_db_engine(settings.database_url)
    plain = new_token()
    now = utc_now_iso()
    device = Device(
        public_id="01DEVICE000000000000000001",
        name="Test Phone",
        token_hash=hash_token(plain),
        expo_push_token=None,
        tz="UTC",
        app_version="1.0.0",
        ios_version="18.1",
        model="iPhone 16",
        status="active",
        created_at=now,
        last_seen_at=now,
        revoked_at=None,
    )
    with Session(engine) as session:
        session.add(device)
        session.commit()
    engine.dispose()
    return plain


# --------------------------------------------------------------------------
# auth enforcement
# --------------------------------------------------------------------------


class TestJobsAuth:
    def test_no_token_returns_401(self, client: TestClient) -> None:
        resp = client.get("/v1/jobs")
        assert resp.status_code == 401

    def test_bad_token_returns_401(self, client: TestClient) -> None:
        resp = client.get("/v1/jobs", headers={"Authorization": "Bearer bad-token"})
        assert resp.status_code == 401

    def test_valid_token_empty_list(self, client: TestClient, test_settings: Settings) -> None:
        plain = _insert_device(test_settings)
        resp = client.get("/v1/jobs", headers=_auth(plain))
        assert resp.status_code == 200
        body = resp.json()
        assert body["jobs"] == []
        assert body["next_cursor"] is None


# --------------------------------------------------------------------------
# filtering and pagination
# --------------------------------------------------------------------------


class TestJobsFiltering:
    def test_status_filter_queued_only(self, client: TestClient, test_settings: Settings) -> None:
        plain = _insert_device(test_settings)
        engine = create_db_engine(test_settings.database_url)
        queue = JobQueue(engine)
        now = dt.datetime.now(dt.UTC)
        queue.enqueue(JobInput(kind=JobKind.ingest, key="j1"), now)
        queue.enqueue(JobInput(kind=JobKind.report, key="j2"), now)
        engine.dispose()

        resp = client.get("/v1/jobs?status=queued", headers=_auth(plain))
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["jobs"]) == 2
        assert all(j["status"] == "queued" for j in body["jobs"])

    def test_status_filter_done_empty(self, client: TestClient, test_settings: Settings) -> None:
        plain = _insert_device(test_settings)
        engine = create_db_engine(test_settings.database_url)
        queue = JobQueue(engine)
        queue.enqueue(JobInput(kind=JobKind.ingest, key="x"), dt.datetime.now(dt.UTC))
        engine.dispose()

        resp = client.get("/v1/jobs?status=done", headers=_auth(plain))
        assert resp.status_code == 200
        assert resp.json()["jobs"] == []

    def test_limit_and_cursor_pagination(self, client: TestClient, test_settings: Settings) -> None:
        plain = _insert_device(test_settings)
        engine = create_db_engine(test_settings.database_url)
        queue = JobQueue(engine)
        now = dt.datetime.now(dt.UTC)
        for i in range(5):
            queue.enqueue(JobInput(kind=JobKind.ingest, key=f"p{i}"), now)
        engine.dispose()

        resp = client.get("/v1/jobs?limit=3", headers=_auth(plain))
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["jobs"]) == 3
        cursor = body["next_cursor"]
        assert cursor is not None

        resp2 = client.get(f"/v1/jobs?limit=3&cursor={cursor}", headers=_auth(plain))
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert len(body2["jobs"]) == 2
        assert body2["next_cursor"] is None

    def test_limit_exceeds_max_is_rejected(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        plain = _insert_device(test_settings)
        resp = client.get("/v1/jobs?limit=99", headers=_auth(plain))
        assert resp.status_code == 422

    def test_invalid_status_returns_422(self, client: TestClient, test_settings: Settings) -> None:
        plain = _insert_device(test_settings)
        resp = client.get("/v1/jobs?status=notastatus", headers=_auth(plain))
        assert resp.status_code == 422


# --------------------------------------------------------------------------
# safe projection — no key, payload, lock token, path or raw error
# --------------------------------------------------------------------------


class TestJobsSafeProjection:
    def test_response_contains_no_sensitive_fields(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        plain = _insert_device(test_settings)
        engine = create_db_engine(test_settings.database_url)
        queue = JobQueue(engine)
        queue.enqueue(
            JobInput(
                kind=JobKind.ingest,
                key="PRIVATE_KEY_CANARY",
                payload={"secret": "PRIVATE_PAYLOAD_CANARY"},
            ),
            dt.datetime.now(dt.UTC),
        )
        engine.dispose()

        resp = client.get("/v1/jobs", headers=_auth(plain))
        assert resp.status_code == 200
        text = resp.text
        assert "PRIVATE_KEY_CANARY" not in text
        assert "PRIVATE_PAYLOAD_CANARY" not in text

    def test_unknown_error_code_sanitised(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        plain = _insert_device(test_settings)
        engine = create_db_engine(test_settings.database_url)
        # Inject a job row with an unallowlisted last_error directly.
        from sqlmodel import Session as _Session

        from chronos.models.jobs import Job

        with _Session(engine) as session:
            job = Job(
                kind="ingest",
                key="sanitise-test",
                payload_json="{}",
                status="failed",
                priority=10,
                attempts=1,
                max_attempts=1,
                run_after=utc_now_iso(),
                timeout_sec=120,
                created_at=utc_now_iso(),
                last_error="PRIVATE_TRACEBACK_CANARY",
            )
            session.add(job)
            session.commit()
        engine.dispose()

        resp = client.get("/v1/jobs", headers=_auth(plain))
        assert resp.status_code == 200
        text = resp.text
        assert "PRIVATE_TRACEBACK_CANARY" not in text
        # Sanitised to an allowlisted code
        body = resp.json()
        assert body["jobs"][0]["last_error"] == "handler_failed"

    def test_response_fields_present(self, client: TestClient, test_settings: Settings) -> None:
        plain = _insert_device(test_settings)
        engine = create_db_engine(test_settings.database_url)
        queue = JobQueue(engine)
        queue.enqueue(JobInput(kind=JobKind.report, key="fields-test"), dt.datetime.now(dt.UTC))
        engine.dispose()

        resp = client.get("/v1/jobs", headers=_auth(plain))
        assert resp.status_code == 200
        job = resp.json()["jobs"][0]
        for field in (
            "id",
            "kind",
            "status",
            "priority",
            "attempts",
            "max_attempts",
            "timeout_sec",
            "run_after",
            "created_at",
        ):
            assert field in job, f"missing field: {field}"
        # Sensitive fields must be absent
        for field in ("key", "payload", "payload_json", "locked_by"):
            assert field not in job, f"sensitive field exposed: {field}"

    def test_ordering_newest_first(self, client: TestClient, test_settings: Settings) -> None:
        plain = _insert_device(test_settings)
        engine = create_db_engine(test_settings.database_url)
        queue = JobQueue(engine)
        now = dt.datetime.now(dt.UTC)
        for i in range(3):
            queue.enqueue(JobInput(kind=JobKind.ingest, key=f"ord{i}"), now)
        engine.dispose()

        resp = client.get("/v1/jobs", headers=_auth(plain))
        assert resp.status_code == 200
        ids = [j["id"] for j in resp.json()["jobs"]]
        assert ids == sorted(ids, reverse=True)
