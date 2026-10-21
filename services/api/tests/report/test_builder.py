"""Unit tests for report/builder.py (T1.8 step 3).

Covers: report sections, sparkline presence, no-evaluation case,
anomaly notes (force-quit, ticks lost), actual_wake latency,
write_day_report file creation and idempotency.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event as sa_event
from sqlmodel import Session, create_engine, select

from chronos.models.wake import Day
from chronos.report.builder import build_day_report, write_day_report
from chronos.wake.replay import load_fixture
from chronos.wake.rules import ActiveRuleVersion, WakeRules, canonical_json, load_yaml, rules_sha256
from chronos.wake.service import ingest_telemetry
from chronos.wake.signals import Signals

_UTC = datetime.UTC
_LA = ZoneInfo("America/Los_Angeles")
_RULES_PATH = Path(__file__).parents[2] / "config" / "wake_rules.yaml"
_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "wake" / "mornings"
_TYPICAL_WEEKDAY = _FIXTURE_DIR / "typical_weekday.json"

# 2026-10-06 Tuesday; window 06:00-11:00 LA = 13:00-18:00 UTC.
_IN_WINDOW = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(tmp_path: Path) -> Iterator[Session]:
    db_url = f"sqlite:///{tmp_path / 'test_builder.db'}"
    ini_path = Path(__file__).parents[2] / "alembic.ini"
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


def _make_device_rv(session: Session) -> tuple[object, ActiveRuleVersion]:
    """Create a device + proper rule version; return (device, ActiveRuleVersion)."""
    from chronos.models.wake import RuleVersion
    from chronos.time import utc_now_iso
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


def _ingest_fixture(
    session: object, device: object, rules: WakeRules, rv: ActiveRuleVersion
) -> None:
    """Ingest typical_weekday fixture events into the DB."""
    from chronos.models.devices import Device as _Device

    assert isinstance(session, Session)
    assert isinstance(device, _Device)

    fd = load_fixture(_TYPICAL_WEEKDAY)
    now = _IN_WINDOW
    for ev_dict in fd.events:
        sig = Signals.model_validate(ev_dict)
        raw = sig.model_dump_json().encode()
        ingest_telemetry(session, device, sig, raw, now, rules, rv)
        now = now + datetime.timedelta(minutes=5)


# ---------------------------------------------------------------------------
# build_day_report tests
# ---------------------------------------------------------------------------


def test_build_day_report_sections_present(db_session: Session) -> None:
    """Report contains all required section headers."""
    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    _ingest_fixture(db_session, device, rules, rv)

    from chronos.models.devices import Device as _Device

    assert isinstance(device, _Device)
    assert device.id is not None

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)

    assert "## State" in report
    assert "## Tick counts" in report
    assert "## Score timeline" in report
    assert "## State transitions" in report
    assert "## Push notifications" in report
    assert "## Scoring metadata" in report
    assert "## Notes" in report


def test_build_day_report_header(db_session: Session) -> None:
    """Report header contains date, kind, timezone and window."""
    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    _ingest_fixture(db_session, device, rules, rv)

    from chronos.models.devices import Device as _Device

    assert isinstance(device, _Device)
    assert device.id is not None

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)

    assert "2026-10-06" in report
    assert "weekday" in report
    assert "America/Los_Angeles" in report
    assert "06:00" in report  # window start local


def test_build_day_report_sparkline_in_report(db_session: Session) -> None:
    """Report contains at least one sparkline block character."""
    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    _ingest_fixture(db_session, device, rules, rv)

    from chronos.models.devices import Device as _Device

    assert isinstance(device, _Device)
    assert device.id is not None

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)

    sparkline_chars = set("\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588")
    assert any(c in sparkline_chars for c in report), "No sparkline chars found in report"


def test_build_day_report_no_evaluations(db_session: Session) -> None:
    """Report for a day with no evaluations shows zero counts."""
    from chronos.wake.state import get_or_create_day

    rules = load_yaml(_RULES_PATH)
    device, _rv = _make_device_rv(db_session)

    from chronos.models.devices import Device as _Device

    assert isinstance(device, _Device)
    assert device.id is not None

    # Create the day row manually (no evaluations).
    get_or_create_day(
        db_session, device, datetime.date(2026, 10, 6), "America/Los_Angeles", rules, _IN_WINDOW
    )
    db_session.commit()

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)

    assert "Evaluations: 0" in report
    assert "Ticks sent: 0" in report


def test_build_day_report_raises_no_day_row(db_session: Session) -> None:
    """build_day_report raises ValueError if no day row exists."""
    with pytest.raises(ValueError, match="No day row"):
        build_day_report(db_session, 9999, datetime.date(2026, 10, 6), _IN_WINDOW)


def test_build_day_report_anomaly_force_quit(db_session: Session) -> None:
    """Report notes section contains force-quit warning when no silent_push events."""
    from chronos.models.wake import RuleVersion, TelemetryEvent, WakeEvaluation
    from chronos.time import utc_now_iso
    from chronos.wake.state import get_or_create_day

    rules = load_yaml(_RULES_PATH)
    device, rv_force_quit = _make_device_rv(db_session)

    from chronos.models.devices import Device as _Device

    assert isinstance(device, _Device)
    assert device.id is not None

    # Create day in "fallback" state (so force-quit check triggers).
    day = get_or_create_day(
        db_session, device, datetime.date(2026, 10, 6), "America/Los_Angeles", rules, _IN_WINDOW
    )
    day.state = "fallback"
    day.fallback_used = 1
    db_session.add(day)

    # Add a single foreground (non-silent_push) evaluation to trigger the check.
    te = TelemetryEvent(
        device_id=device.id,
        received_at=utc_now_iso(),
        device_ts=_IN_WINDOW.isoformat(),
        tz="America/Los_Angeles",
        local_date="2026-10-06",
        wake_source="foreground",  # NOT silent_push
        seq=0,
        steps_10m=0,
        steps_since_window=0,
        walking_sec_10m=0,
        stationary_sec_10m=0,
        walking_conf="low",
        is_unlocked=1,
        battery_state="unplugged",
        battery_level=None,
        app_state="active",
        low_power_mode=0,
        pedometer_auth="authorized",
        tick_seq=None,
        app_version="1.0.0",
        ios_version="18.1",
        raw_json="{}",
    )
    db_session.add(te)
    db_session.flush()
    assert te.id is not None

    rv_row = db_session.exec(  # type: ignore[call-overload]
        select(RuleVersion).where(RuleVersion.id == rv_force_quit.id)
    ).first()
    assert rv_row is not None

    from chronos.wake.scoring import evaluate

    sig = Signals.model_validate(
        {
            "device_ts": _IN_WINDOW.isoformat(),
            "tz": "America/Los_Angeles",
            "wake_source": "foreground",
            "steps_10m": 0,
            "steps_since_window": 0,
            "walking_sec_10m": 0,
            "walking_conf": "low",
            "stationary_sec_10m": 0,
            "is_unlocked": True,
            "battery_state": "unplugged",
            "battery_level": None,
            "app_state": "active",
            "low_power_mode": False,
            "pedometer_auth": "authorized",
            "seq": 0,
            "app_version": "1.0.0",
            "ios_version": "18.1",
        }
    )
    from chronos.enums import DayState
    from chronos.wake.context import build_eval_context

    ctx = build_eval_context(sig, rules, 1, None, 0, DayState.monitoring)
    eval_result = evaluate(sig, rules, _IN_WINDOW, ctx)

    we = WakeEvaluation(
        telemetry_event_id=te.id,
        rule_version_id=rv_force_quit.id,
        local_date="2026-10-06",
        gate=eval_result.gate.value,
        score=eval_result.score,
        breakdown_json=eval_result.model_dump_json(),
        decision=eval_result.decision.value,
        consecutive_high=eval_result.consecutive_high,
        evaluated_at=utc_now_iso(),
    )
    db_session.add(we)
    db_session.commit()

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)
    assert "Force-quit suspected" in report


def test_build_day_report_anomaly_ticks_lost(db_session: Session) -> None:
    """Report notes contain ticks-lost warning when ticks_sent - ticks_received >= 3."""
    from chronos.wake.state import get_or_create_day

    rules = load_yaml(_RULES_PATH)
    device, _rv3 = _make_device_rv(db_session)

    from chronos.models.devices import Device as _Device

    assert isinstance(device, _Device)
    assert device.id is not None

    day = get_or_create_day(
        db_session, device, datetime.date(2026, 10, 6), "America/Los_Angeles", rules, _IN_WINDOW
    )
    day.ticks_sent = 10
    day.ticks_received = 6  # 4 lost >= 3 threshold
    db_session.add(day)
    db_session.commit()

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)
    assert "ticks lost" in report


def test_build_day_report_with_actual_wake(db_session: Session) -> None:
    """Report shows latency when actual_wake_local is set."""
    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    _ingest_fixture(db_session, device, rules, rv)

    from chronos.models.devices import Device as _Device

    assert isinstance(device, _Device)
    assert device.id is not None

    # Set actual wake time.
    day = db_session.exec(  # type: ignore[call-overload]
        select(Day).where(Day.local_date == "2026-10-06", Day.device_id == device.id)
    ).first()
    assert day is not None
    day.actual_wake_local = "07:05"
    db_session.add(day)
    db_session.commit()

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)

    assert "07:05" in report
    assert "Latency:" in report


# ---------------------------------------------------------------------------
# write_day_report tests
# ---------------------------------------------------------------------------


def test_write_day_report_creates_file(tmp_path: Path) -> None:
    """write_day_report creates the file at the expected path."""
    content = "# Hello"
    result = write_day_report(content, datetime.date(2026, 10, 6), tmp_path)

    assert result == tmp_path / "2026-10-06.md"
    assert result.exists()
    assert result.read_text(encoding="utf-8") == content


def test_write_day_report_creates_directory(tmp_path: Path) -> None:
    """write_day_report creates the reports directory if it does not exist."""
    reports_dir = tmp_path / "deep" / "nested" / "reports"
    assert not reports_dir.exists()

    write_day_report("# Test", datetime.date(2026, 10, 6), reports_dir)

    assert reports_dir.exists()
    assert (reports_dir / "2026-10-06.md").exists()


def test_write_day_report_idempotent(tmp_path: Path) -> None:
    """write_day_report overwrites on second call (idempotent)."""
    write_day_report("# First", datetime.date(2026, 10, 6), tmp_path)
    write_day_report("# Second", datetime.date(2026, 10, 6), tmp_path)

    content = (tmp_path / "2026-10-06.md").read_text(encoding="utf-8")
    assert content == "# Second"


# ---------------------------------------------------------------------------
# MINOR 3: anomaly flag tests (pedometer denied, > 20% deduped, push errors,
# fallback used) — each asserts the flag appears AND is absent when the
# condition is not met.
# ---------------------------------------------------------------------------


_EVAL_SEQ_COUNTER: list[int] = [1000]  # global counter for unique seq values


def _make_minimal_eval_row(
    db_session: Session,
    device_id: int,
    rv_id: int,
    local_date: str,
    decision: str = "low",
    gate: str = "open",
    pedometer_auth: str = "authorized",
) -> None:
    """Insert a TelemetryEvent + WakeEvaluation with controlled fields."""
    from chronos.enums import DayState
    from chronos.models.wake import TelemetryEvent, WakeEvaluation
    from chronos.time import utc_now_iso
    from chronos.wake.context import build_eval_context
    from chronos.wake.rules import load_yaml as _load_yaml
    from chronos.wake.scoring import evaluate as _evaluate
    from chronos.wake.signals import Signals as _Signals

    seq = _EVAL_SEQ_COUNTER[0]
    _EVAL_SEQ_COUNTER[0] += 1

    rules = _load_yaml(_RULES_PATH)
    sig = _Signals.model_validate(
        {
            "device_ts": _IN_WINDOW.isoformat(),
            "tz": "America/Los_Angeles",
            "wake_source": "silent_push",
            "steps_10m": 0,
            "steps_since_window": 0,
            "walking_sec_10m": 0,
            "walking_conf": "low",
            "stationary_sec_10m": 0,
            "is_unlocked": False,
            "battery_state": "unplugged",
            "battery_level": None,
            "app_state": "background",
            "low_power_mode": False,
            "pedometer_auth": pedometer_auth,
            "seq": seq,
            "app_version": "1.0.0",
            "ios_version": "18.1",
        }
    )
    ctx = build_eval_context(sig, rules, rv_id, None, 0, DayState.monitoring)
    eval_result = _evaluate(sig, rules, _IN_WINDOW, ctx)

    te = TelemetryEvent(
        device_id=device_id,
        received_at=utc_now_iso(),
        device_ts=_IN_WINDOW.isoformat(),
        tz="America/Los_Angeles",
        local_date=local_date,
        wake_source="silent_push",
        seq=seq,
        steps_10m=0,
        steps_since_window=0,
        walking_sec_10m=0,
        stationary_sec_10m=0,
        walking_conf="low",
        is_unlocked=0,
        battery_state="unplugged",
        battery_level=None,
        app_state="background",
        low_power_mode=0,
        pedometer_auth=pedometer_auth,
        tick_seq=None,
        app_version="1.0.0",
        ios_version="18.1",
        raw_json="{}",
    )
    db_session.add(te)
    db_session.flush()
    assert te.id is not None

    we = WakeEvaluation(
        telemetry_event_id=te.id,
        rule_version_id=rv_id,
        local_date=local_date,
        gate=gate,
        score=eval_result.score,
        breakdown_json=eval_result.model_dump_json(),
        decision=decision,
        consecutive_high=0,
        evaluated_at=utc_now_iso(),
    )
    db_session.add(we)
    db_session.commit()


def test_anomaly_pedometer_denied_flag_present(db_session: Session) -> None:
    """'Pedometer denied' warning appears when any evaluation has pedometer_auth='denied'."""
    from chronos.models.devices import Device as _Device
    from chronos.wake.state import get_or_create_day

    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    assert isinstance(device, _Device)
    assert device.id is not None

    get_or_create_day(
        db_session, device, datetime.date(2026, 10, 6), "America/Los_Angeles", rules, _IN_WINDOW
    )
    db_session.commit()
    _make_minimal_eval_row(db_session, device.id, rv.id, "2026-10-06", pedometer_auth="denied")

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)
    assert "Pedometer denied" in report


def test_anomaly_pedometer_denied_flag_absent(db_session: Session) -> None:
    """'Pedometer denied' warning absent when all evaluations have pedometer_auth='authorized'."""
    from chronos.models.devices import Device as _Device

    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    _ingest_fixture(db_session, device, rules, rv)

    assert isinstance(device, _Device)
    assert device.id is not None

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)
    assert "Pedometer denied" not in report


def test_anomaly_deduped_flag_present(db_session: Session) -> None:
    """'> 20% ticks deduped' warning appears when deduped fraction exceeds 20%."""
    from chronos.models.devices import Device as _Device
    from chronos.wake.state import get_or_create_day

    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    assert isinstance(device, _Device)
    assert device.id is not None

    get_or_create_day(
        db_session, device, datetime.date(2026, 10, 6), "America/Los_Angeles", rules, _IN_WINDOW
    )
    db_session.commit()

    # Insert 1 normal eval + 4 deduped evals → 80% deduped.
    _make_minimal_eval_row(db_session, device.id, rv.id, "2026-10-06", decision="low")
    for _ in range(4):
        _make_minimal_eval_row(
            db_session, device.id, rv.id, "2026-10-06", decision="ignored_dedupe"
        )

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)
    assert "> 20% ticks deduped" in report


def test_anomaly_deduped_flag_absent(db_session: Session) -> None:
    """'> 20% ticks deduped' absent when deduped fraction is within limits."""
    from chronos.models.devices import Device as _Device

    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    _ingest_fixture(db_session, device, rules, rv)

    assert isinstance(device, _Device)
    assert device.id is not None

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)
    assert "> 20% ticks deduped" not in report


def test_anomaly_push_errors_flag_present(db_session: Session) -> None:
    """'Push errors' warning appears when push notifications have error entries."""
    from chronos.models.delivery import PushNotification
    from chronos.models.devices import Device as _Device
    from chronos.time import utc_now_iso

    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    _ingest_fixture(db_session, device, rules, rv)

    assert isinstance(device, _Device)
    assert device.id is not None

    # Insert a push notification with an error.
    pn = PushNotification(
        device_id=device.id,
        kind="tick",
        local_date="2026-10-06",
        reel_id=None,
        tick_seq=0,
        payload_json="{}",
        ticket_id=None,
        ticket_status="error",
        receipt_status=None,
        sent_at=utc_now_iso(),
        receipt_checked_at=None,
        error="DeviceNotRegistered",
    )
    db_session.add(pn)
    db_session.commit()

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)
    assert "Push errors" in report


def test_anomaly_push_errors_flag_absent(db_session: Session) -> None:
    """'Push errors' absent when no push notifications have errors."""
    from chronos.models.devices import Device as _Device

    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    _ingest_fixture(db_session, device, rules, rv)

    assert isinstance(device, _Device)
    assert device.id is not None

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)
    assert "Push errors" not in report


def test_anomaly_fallback_used_flag_present(db_session: Session) -> None:
    """'Fallback used' warning appears when day.fallback_used is set."""
    from chronos.models.devices import Device as _Device
    from chronos.models.wake import Day

    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    _ingest_fixture(db_session, device, rules, rv)

    assert isinstance(device, _Device)
    assert device.id is not None

    day = db_session.exec(  # type: ignore[call-overload]
        select(Day).where(Day.local_date == "2026-10-06", Day.device_id == device.id)
    ).first()
    assert day is not None
    day.fallback_used = 1
    db_session.add(day)
    db_session.commit()

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)
    # The anomaly note text (in the ## Notes section); distinct from the
    # "**Fallback used:** yes/no" line in the state section which always appears.
    assert "Fallback used: confirm window expired" in report


def test_anomaly_fallback_used_flag_absent(db_session: Session) -> None:
    """'Fallback used' anomaly note absent when day.fallback_used is 0."""
    from chronos.models.devices import Device as _Device

    rules = load_yaml(_RULES_PATH)
    device, rv = _make_device_rv(db_session)
    _ingest_fixture(db_session, device, rules, rv)

    assert isinstance(device, _Device)
    assert device.id is not None

    report = build_day_report(db_session, device.id, datetime.date(2026, 10, 6), _IN_WINDOW)
    # The anomaly note must be absent; "**Fallback used:** no" still appears in state section.
    assert "Fallback used: confirm window expired" not in report
