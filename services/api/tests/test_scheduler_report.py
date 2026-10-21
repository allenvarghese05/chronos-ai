"""Tests for Scheduler.run_reports (T1.8 step 6).

Covers: file written after boundary + grace, idempotency, skips before boundary,
skips when no day row.
"""

from __future__ import annotations

import asyncio
import datetime
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event as sa_event
from sqlmodel import Session, create_engine

from chronos.models.devices import Device
from chronos.models.wake import RuleVersion
from chronos.scheduler import Scheduler
from chronos.time import utc_now_iso
from chronos.wake.replay import load_fixture
from chronos.wake.rules import canonical_json, load_yaml, rules_sha256
from chronos.wake.service import ingest_telemetry
from chronos.wake.signals import Signals

_UTC = datetime.UTC
_RULES_PATH = Path(__file__).parent / ".." / "config" / "wake_rules.yaml"
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "wake" / "mornings"
_TYPICAL_WEEKDAY = _FIXTURE_DIR / "typical_weekday.json"

# 2026-10-06 Tuesday window 06:00-11:00 LA = 13:00-18:00 UTC.
# Day boundary = 03:00 LA = 10:00 UTC.
# 2026-10-07 03:00 LA = 10:00 UTC.  03:15 LA = 10:15 UTC (past boundary + 15 min grace).
_PREV_LOCAL_DATE = datetime.date(2026, 10, 6)
_TODAY_LOCAL_DATE = datetime.date(2026, 10, 7)
# 03:00 LA on 2026-10-07 = 10:00 UTC
_BOUNDARY_UTC = datetime.datetime(2026, 10, 7, 10, 0, 0, tzinfo=_UTC)
# 03:16 LA on 2026-10-07 = 10:16 UTC (past boundary + 15 min)
_AFTER_GRACE_UTC = datetime.datetime(2026, 10, 7, 10, 16, 0, tzinfo=_UTC)
# 03:10 LA on 2026-10-07 = 10:10 UTC (before boundary + 15 min)
_BEFORE_GRACE_UTC = datetime.datetime(2026, 10, 7, 10, 10, 0, tzinfo=_UTC)
# Fixture ingestion time
_IN_WINDOW = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(tmp_path: Path) -> Iterator[Session]:
    db_url = f"sqlite:///{tmp_path / 'test_sched_report.db'}"
    ini_path = Path(__file__).parent / ".." / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")

    engine = create_engine(db_url, connect_args={"check_same_thread": False})

    @sa_event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: sqlite3.Connection, _record: object) -> None:
        dbapi_conn.cursor().execute("PRAGMA foreign_keys=ON")

    with Session(engine) as session:
        yield session

    engine.dispose()


def _make_scheduler(db_session: Session, reports_dir: Path) -> tuple[Scheduler, Session]:
    """Create a Scheduler with a fake push client and the given reports_dir."""
    from tests.conftest import make_settings

    settings = make_settings(
        database_url=str(db_session.get_bind().url),
        reports_dir=reports_dir,
    )

    push_client = AsyncMock()
    push_client.send = AsyncMock(return_value=[])

    def _factory() -> Session:
        return db_session

    scheduler = Scheduler(
        session_factory=_factory,
        push_client=push_client,
        settings=settings,
        clock=lambda: _AFTER_GRACE_UTC,
    )
    return scheduler, db_session


def _insert_device_and_rules(session: Session) -> tuple[Device, object]:
    """Insert device + rule version; return (device, ActiveRuleVersion)."""
    from chronos.wake.rules import ActiveRuleVersion
    from tests.factories import make_device

    device = make_device(session)
    session.commit()

    rules = load_yaml(_RULES_PATH)
    rv_orm = RuleVersion(
        version=1,
        source="yaml",
        rules_json=canonical_json(rules),
        sha256=rules_sha256(rules),
        created_at=utc_now_iso(),
        created_by="test",
        active=1,
    )
    session.add(rv_orm)
    session.commit()
    assert rv_orm.id is not None

    rv = ActiveRuleVersion(
        id=rv_orm.id,
        version=rv_orm.version,
        source=rv_orm.source,
        sha256=rv_orm.sha256,
        created_at=rv_orm.created_at,
        created_by=rv_orm.created_by,
    )
    return device, rv


def _ingest_fixture(session: Session, device: Device, rv: object) -> None:
    """Ingest typical_weekday fixture."""
    from chronos.wake.rules import ActiveRuleVersion

    assert isinstance(rv, ActiveRuleVersion)
    rules = load_yaml(_RULES_PATH)
    fd = load_fixture(_TYPICAL_WEEKDAY)
    now = _IN_WINDOW
    for ev in fd.events:
        sig = Signals.model_validate(ev)
        raw = sig.model_dump_json().encode()
        ingest_telemetry(session, device, sig, raw, now, rules, rv)
        now = now + datetime.timedelta(minutes=5)
    session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scheduler_run_reports_writes_file_after_boundary(
    db_session: Session, tmp_path: Path
) -> None:
    """run_reports writes the previous day's report after boundary + 15 min grace."""
    reports_dir = tmp_path / "reports"
    scheduler, session = _make_scheduler(db_session, reports_dir)

    device, rv = _insert_device_and_rules(session)
    _ingest_fixture(session, device, rv)

    asyncio.run(scheduler.run_reports(_AFTER_GRACE_UTC))

    report_file = reports_dir / "2026-10-06.md"
    assert report_file.exists(), f"Expected report file at {report_file}"
    content = report_file.read_text(encoding="utf-8")
    assert "# Morning Report" in content


def test_scheduler_run_reports_idempotent(db_session: Session, tmp_path: Path) -> None:
    """run_reports skips writing if the file already exists."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    scheduler, session = _make_scheduler(db_session, reports_dir)

    device, rv = _insert_device_and_rules(session)
    _ingest_fixture(session, device, rv)

    # Pre-create the report file with sentinel content.
    report_file = reports_dir / "2026-10-06.md"
    report_file.write_text("# Existing Report", encoding="utf-8")

    asyncio.run(scheduler.run_reports(_AFTER_GRACE_UTC))

    # File should still contain the sentinel (not overwritten).
    assert report_file.read_text(encoding="utf-8") == "# Existing Report"


def test_scheduler_run_reports_not_before_boundary(db_session: Session, tmp_path: Path) -> None:
    """run_reports does NOT write before boundary + 15 min grace."""
    reports_dir = tmp_path / "reports"
    scheduler, session = _make_scheduler(db_session, reports_dir)

    device, rv = _insert_device_and_rules(session)
    _ingest_fixture(session, device, rv)

    # Clock is before the grace period.
    asyncio.run(scheduler.run_reports(_BEFORE_GRACE_UTC))

    report_file = reports_dir / "2026-10-06.md"
    assert not report_file.exists(), "Should not write before boundary + grace"


def test_scheduler_run_reports_no_day_row(db_session: Session, tmp_path: Path) -> None:
    """run_reports skips gracefully when no day row exists for the previous date."""
    reports_dir = tmp_path / "reports"
    scheduler, session = _make_scheduler(db_session, reports_dir)

    # Insert device + rules but no day row.
    _insert_device_and_rules(session)

    # Should not raise.
    asyncio.run(scheduler.run_reports(_AFTER_GRACE_UTC))

    # No file written.
    assert not (reports_dir / "2026-10-06.md").exists()
