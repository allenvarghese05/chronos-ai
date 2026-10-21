"""HTTP integration tests for GET /v1/report/{local_date} (T1.8 step 4).

Covers: 200 text/markdown, file served if present, 404 no day, 422 bad date, 401 no token.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from pathlib import Path

import time_machine
from fastapi.testclient import TestClient
from sqlmodel import Session

from chronos.auth import hash_token, new_token
from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.ids import new_ulid
from chronos.models.devices import Device
from chronos.time import utc_now_iso
from chronos.wake.replay import load_fixture

_UTC = datetime.UTC
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "wake" / "mornings"
_TYPICAL_WEEKDAY = _FIXTURE_DIR / "typical_weekday.json"
_IN_WINDOW = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report_client(
    tmp_path: Path,
    tmp_db_url: str,
) -> Iterator[tuple[TestClient, Settings]]:
    """Create a TestClient + Settings with an isolated reports_dir."""
    from tests.conftest import _run_migrations, make_settings

    _run_migrations(tmp_db_url)
    settings = make_settings(
        database_url=tmp_db_url,
        reports_dir=tmp_path / "reports",
    )
    from chronos.main import create_app

    app = create_app(settings=settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, settings


def _insert_device(settings: Settings) -> tuple[str, int]:
    """Insert a device; return (plain_token, device_id)."""
    engine = create_db_engine(settings.database_url)
    plain = new_token()
    now = utc_now_iso()
    device = Device(
        public_id=new_ulid(),
        name="Test iPhone",
        token_hash=hash_token(plain),
        expo_push_token=None,
        tz="America/Los_Angeles",
        app_version="1.0.0",
        ios_version="18.1",
        model="iPhone 15 Pro",
        status="active",
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


def _ingest_fixture(client: TestClient, token: str) -> None:
    """Ingest typical_weekday fixture events via the API (clock frozen at fixture time)."""
    fd = load_fixture(_TYPICAL_WEEKDAY)
    with time_machine.travel(_IN_WINDOW, tick=False):
        for ev in fd.events:
            resp = client.post("/v1/telemetry", json=ev, headers=_auth(token))
            assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_report_200(tmp_path: Path, tmp_db_url: str) -> None:
    """GET /v1/report/2026-10-06 returns 200 with Markdown content."""
    for client, settings in _make_report_client(tmp_path, tmp_db_url):
        token, _ = _insert_device(settings)
        _ingest_fixture(client, token)

        resp = client.get("/v1/report/2026-10-06", headers=_auth(token))

        assert resp.status_code == 200
        assert "text/markdown" in resp.headers.get("content-type", "")
        assert "# Morning Report" in resp.text


def test_get_report_serves_file_if_present(tmp_path: Path, tmp_db_url: str) -> None:
    """If report file exists in reports_dir, it is served verbatim."""
    for client, settings in _make_report_client(tmp_path, tmp_db_url):
        token, _ = _insert_device(settings)
        _ingest_fixture(client, token)

        # Write mock report file to reports_dir.
        reports_dir = Path(settings.reports_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_file = reports_dir / "2026-10-06.md"
        report_file.write_text("# Prewritten Report Content", encoding="utf-8")

        resp = client.get("/v1/report/2026-10-06", headers=_auth(token))

        assert resp.status_code == 200
        assert "Prewritten Report Content" in resp.text


def test_get_report_404_no_day(tmp_path: Path, tmp_db_url: str) -> None:
    """GET /v1/report/{date} returns 404 when no day row exists."""
    for client, settings in _make_report_client(tmp_path, tmp_db_url):
        token, _ = _insert_device(settings)

        resp = client.get("/v1/report/2020-01-01", headers=_auth(token))

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"


def test_get_report_422_bad_date(tmp_path: Path, tmp_db_url: str) -> None:
    """GET /v1/report/{bad} returns 422 for an invalid date string."""
    for client, settings in _make_report_client(tmp_path, tmp_db_url):
        token, _ = _insert_device(settings)

        resp = client.get("/v1/report/not-a-date", headers=_auth(token))

        assert resp.status_code == 422
        assert resp.json()["code"] == "validation"


def test_get_report_401_no_token(tmp_path: Path, tmp_db_url: str) -> None:
    """GET /v1/report/{date} returns 401 without a bearer token."""
    for client, _settings in _make_report_client(tmp_path, tmp_db_url):
        resp = client.get("/v1/report/2026-10-06")

        assert resp.status_code == 401
