"""Tests for wake/replay.py (T1.8 step 2).

Covers: fixture replay, DB-range determinism, invalid raw_json skipping,
compare() maths (latency, early flag, no-confirm, no-actual-wake).
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event as sa_event
from sqlmodel import Session, create_engine, select

from chronos.enums import EvaluationDecision
from chronos.models.devices import Device
from chronos.models.wake import RuleVersion, TelemetryEvent, WakeEvaluation
from chronos.wake.replay import (
    ReplayDay,
    ReplayEval,
    compare,
    load_fixture,
    replay_db_range,
    replay_events,
    replay_fixture,
)
from chronos.wake.rules import ActiveRuleVersion, load_yaml
from chronos.wake.service import ingest_telemetry
from chronos.wake.signals import Signals

_UTC = datetime.UTC
_LA = ZoneInfo("America/Los_Angeles")
_RULES_PATH = Path(__file__).parents[2] / "config" / "wake_rules.yaml"
_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "wake" / "mornings"
_TYPICAL_WEEKDAY = _FIXTURE_DIR / "typical_weekday.json"

# 2026-10-06 Tuesday; window 06:00-11:00 LA = 13:00-18:00 UTC.
_IN_WINDOW = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)  # 06:48 LA
_IN_WINDOW2 = datetime.datetime(2026, 10, 6, 13, 53, 0, tzinfo=_UTC)  # 06:53 LA


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(tmp_path: Path) -> Iterator[Session]:
    db_url = f"sqlite:///{tmp_path / 'test_replay.db'}"
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


def _make_device(session: Session) -> Device:
    from tests.factories import make_device

    d = make_device(session)
    session.commit()
    return d


def _make_rv(session: Session) -> ActiveRuleVersion:
    from tests.factories import make_rule_version

    rv_orm = make_rule_version(session, active=1)
    session.commit()
    assert rv_orm.id is not None
    return ActiveRuleVersion(
        id=rv_orm.id,
        version=rv_orm.version,
        source=rv_orm.source,
        sha256=rv_orm.sha256,
        created_at=rv_orm.created_at,
        created_by=rv_orm.created_by,
    )


def _signals(
    *,
    device_ts: datetime.datetime = _IN_WINDOW,
    seq: int = 0,
    steps_10m: int = 35,
    is_unlocked: bool = True,
) -> Signals:
    return Signals.model_validate(
        {
            "device_ts": device_ts,
            "tz": "America/Los_Angeles",
            "wake_source": "silent_push",
            "steps_10m": steps_10m,
            "steps_since_window": 120,
            "walking_sec_10m": 95,
            "walking_conf": "high",
            "stationary_sec_10m": 30,
            "is_unlocked": is_unlocked,
            "battery_state": "unplugged",
            "battery_level": 0.78,
            "app_state": "background",
            "low_power_mode": False,
            "pedometer_auth": "authorized",
            "seq": seq,
            "app_version": "1.0.0",
            "ios_version": "18.1",
        }
    )


def _raw(sig: Signals) -> bytes:
    return sig.model_dump_json().encode()


# ---------------------------------------------------------------------------
# load_fixture tests
# ---------------------------------------------------------------------------


def test_load_fixture_returns_fixture_data() -> None:
    """load_fixture returns FixtureData with stripped events."""
    fd = load_fixture(_TYPICAL_WEEKDAY)
    assert fd.name == "typical_weekday"
    assert fd.tz == "America/Los_Angeles"
    assert fd.day_kind == "weekday"
    assert len(fd.events) > 0
    # Meta-keys stripped.
    for ev in fd.events:
        assert not any(k.startswith("_") for k in ev)
        assert "expect" not in ev


def test_load_fixture_events_parseable() -> None:
    """Fixture events can be parsed into Signals after stripping."""
    fd = load_fixture(_TYPICAL_WEEKDAY)
    for ev in fd.events:
        # Should not raise.
        Signals.model_validate(ev)


# ---------------------------------------------------------------------------
# replay_events tests
# ---------------------------------------------------------------------------


def test_replay_events_typical_weekday_matches_fixture_scores() -> None:
    """replay_events scores match the 'expect' fields in the fixture JSON."""
    rules = load_yaml(_RULES_PATH)
    fd = load_fixture(_TYPICAL_WEEKDAY)
    raw_data = json.loads(_TYPICAL_WEEKDAY.read_text())

    results = replay_events(fd.events, rules, rule_version_number=1)

    assert len(results) == len(fd.events)
    for i, (result, raw_ev) in enumerate(zip(results, raw_data["events"], strict=True)):
        exp = raw_ev["expect"]
        assert result.score == exp["score"], f"event {i}: score mismatch"
        assert result.decision.value == exp["decision"], f"event {i}: decision mismatch"


def test_replay_events_threads_previous_tick() -> None:
    """replay_events produces confirm on event 1 for typical_weekday."""
    rules = load_yaml(_RULES_PATH)
    fd = load_fixture(_TYPICAL_WEEKDAY)

    results = replay_events(fd.events, rules, rule_version_number=1)

    # typical_weekday has confirmed_at_event_index = 1.
    assert results[1].decision == EvaluationDecision.confirm


def test_replay_events_skips_invalid_event() -> None:
    """replay_events skips events that fail Signals validation."""
    rules = load_yaml(_RULES_PATH)
    bad_event = {"device_ts": "not-a-timestamp", "tz": "UTC"}  # missing required fields

    results = replay_events([bad_event], rules, rule_version_number=1)
    assert results == []


def test_replay_events_skips_one_invalid_continues() -> None:
    """replay_events skips a bad event and continues with valid ones."""
    rules = load_yaml(_RULES_PATH)
    fd = load_fixture(_TYPICAL_WEEKDAY)

    # Insert a bad event at position 0.
    bad_event = {"bad": "data"}
    events = [bad_event, *fd.events]
    results = replay_events(events, rules, rule_version_number=1)

    # Only the valid events produce results.
    assert len(results) == len(fd.events)


# ---------------------------------------------------------------------------
# compare() tests
# ---------------------------------------------------------------------------


def _make_eval(
    device_ts: datetime.datetime,
    score: int,
    decision: EvaluationDecision,
) -> ReplayEval:
    return ReplayEval(
        device_ts=device_ts,
        score=score,
        decision=decision,
        confirm_reason=None,
        consecutive_high=1 if decision == EvaluationDecision.confirm else 0,
        rule_version_id=None,
        wake_source="silent_push",
    )


def test_compare_latency_maths() -> None:
    """compare() computes latency correctly."""
    # candidate confirms at 07:45 LA; actual_wake=07:05 → latency = +40 min.
    confirm_ts = datetime.datetime(2026, 10, 6, 14, 45, 0, tzinfo=_UTC)  # 07:45 LA
    candidate_evals = [_make_eval(confirm_ts, 85, EvaluationDecision.confirm)]
    baseline_evals: list[ReplayEval] = []

    cmp = compare(
        baseline_evals=baseline_evals,
        candidate_evals=candidate_evals,
        actual_wake_local="07:05",
        early_min=30,
        confirm_threshold=70,
        tz="America/Los_Angeles",
    )

    assert cmp.latency_min == 40
    assert not cmp.is_early


def test_compare_early_flag() -> None:
    """is_early=True when confirm > early_min before actual_wake."""
    # Confirm at 06:30 LA; actual_wake=07:05; gap=35 min > early_min=30.
    confirm_ts = datetime.datetime(2026, 10, 6, 13, 30, 0, tzinfo=_UTC)  # 06:30 LA
    candidate_evals = [_make_eval(confirm_ts, 85, EvaluationDecision.confirm)]

    cmp = compare(
        baseline_evals=[],
        candidate_evals=candidate_evals,
        actual_wake_local="07:05",
        early_min=30,
        confirm_threshold=70,
        tz="America/Los_Angeles",
    )

    assert cmp.is_early is True
    assert cmp.latency_min == -35


def test_compare_not_early_within_threshold() -> None:
    """is_early=False when confirm is within early_min."""
    # Confirm at 06:45 LA; actual_wake=07:05; gap=20 min < early_min=30.
    confirm_ts = datetime.datetime(2026, 10, 6, 13, 45, 0, tzinfo=_UTC)  # 06:45 LA
    candidate_evals = [_make_eval(confirm_ts, 85, EvaluationDecision.confirm)]

    cmp = compare(
        baseline_evals=[],
        candidate_evals=candidate_evals,
        actual_wake_local="07:05",
        early_min=30,
        confirm_threshold=70,
        tz="America/Los_Angeles",
    )

    assert not cmp.is_early
    assert cmp.latency_min == -20


def test_compare_no_actual_wake() -> None:
    """latency_min=None and is_early=False when actual_wake_local is None."""
    confirm_ts = datetime.datetime(2026, 10, 6, 13, 30, 0, tzinfo=_UTC)
    candidate_evals = [_make_eval(confirm_ts, 85, EvaluationDecision.confirm)]

    cmp = compare(
        baseline_evals=[],
        candidate_evals=candidate_evals,
        actual_wake_local=None,
        early_min=30,
        confirm_threshold=70,
        tz="America/Los_Angeles",
    )

    assert cmp.latency_min is None
    assert not cmp.is_early


def test_compare_no_confirm() -> None:
    """Both confirm_times=None when no confirm evals."""
    low_ts = datetime.datetime(2026, 10, 6, 13, 5, 0, tzinfo=_UTC)
    evals = [_make_eval(low_ts, 40, EvaluationDecision.low)]

    cmp = compare(
        baseline_evals=evals,
        candidate_evals=evals,
        actual_wake_local="07:05",
        early_min=30,
        confirm_threshold=70,
        tz="America/Los_Angeles",
    )

    assert cmp.baseline_confirm_time is None
    assert cmp.candidate_confirm_time is None
    assert cmp.latency_min is None
    assert cmp.ticks_ge_confirm == 0


def test_compare_ticks_ge_confirm() -> None:
    """ticks_ge_confirm counts candidate ticks with score >= threshold."""
    ts1 = datetime.datetime(2026, 10, 6, 13, 5, 0, tzinfo=_UTC)
    ts2 = datetime.datetime(2026, 10, 6, 13, 10, 0, tzinfo=_UTC)
    ts3 = datetime.datetime(2026, 10, 6, 13, 15, 0, tzinfo=_UTC)
    evals = [
        _make_eval(ts1, 75, EvaluationDecision.high),
        _make_eval(ts2, 60, EvaluationDecision.low),  # below 70
        _make_eval(ts3, 85, EvaluationDecision.confirm),
    ]

    cmp = compare(
        baseline_evals=[],
        candidate_evals=evals,
        actual_wake_local=None,
        early_min=30,
        confirm_threshold=70,
        tz="America/Los_Angeles",
    )

    assert cmp.ticks_ge_confirm == 2  # 75 and 85


# ---------------------------------------------------------------------------
# replay_db_range determinism test
# ---------------------------------------------------------------------------


def test_replay_db_range_determinism(db_session: Session) -> None:
    """Replaying stored events under the original rules reproduces stored scores."""
    from chronos.time import utc_now_iso
    from chronos.wake.rules import canonical_json, rules_sha256

    rules = load_yaml(_RULES_PATH)
    device = _make_device(db_session)

    # Use a rule version that stores the REAL rules JSON (not the stub from factory).
    rules_json_str = canonical_json(rules)
    rv_orm = RuleVersion(
        version=1,
        source="yaml",
        rules_json=rules_json_str,
        sha256=rules_sha256(rules),
        created_at=utc_now_iso(),
        created_by="test",
        active=1,
    )
    db_session.add(rv_orm)
    db_session.commit()
    assert rv_orm.id is not None
    rv = ActiveRuleVersion(
        id=rv_orm.id,
        version=rv_orm.version,
        source=rv_orm.source,
        sha256=rv_orm.sha256,
        created_at=rv_orm.created_at,
        created_by=rv_orm.created_by,
    )

    fd = load_fixture(_TYPICAL_WEEKDAY)
    now = _IN_WINDOW

    # Ingest each fixture event into the DB.
    for ev_dict in fd.events:
        sig = Signals.model_validate(ev_dict)
        raw = sig.model_dump_json().encode()
        ingest_telemetry(db_session, device, sig, raw, now, rules, rv)
        # Advance "now" slightly for subsequent ticks.
        now = now + datetime.timedelta(minutes=5)

    # Query stored scores.
    stored = list(
        db_session.exec(  # type: ignore[call-overload]
            select(WakeEvaluation)
            .join(TelemetryEvent, WakeEvaluation.telemetry_event_id == TelemetryEvent.id)  # type: ignore[arg-type]
            .where(TelemetryEvent.device_id == device.id)
            .order_by(TelemetryEvent.device_ts.asc())  # type: ignore[attr-defined]
        ).all()
    )

    assert stored, "No evaluations stored — ingest_telemetry failed"

    # Replay from DB range.
    from_date = datetime.date(2026, 10, 6)
    to_date = datetime.date(2026, 10, 6)
    assert device.id is not None
    replay_days = replay_db_range(
        session=db_session,
        device_id=device.id,
        from_date=from_date,
        to_date=to_date,
        candidate_rules=rules,
        candidate_rule_version_number=rv.version,
    )

    assert len(replay_days) == 1
    replay_day = replay_days[0]

    # Baseline evals should reproduce stored scores.
    assert len(replay_day.baseline_evals) == len(stored)
    for i, (replayed, stored_we) in enumerate(zip(replay_day.baseline_evals, stored, strict=True)):
        assert replayed.score == stored_we.score, (
            f"event {i}: replayed score {replayed.score} != stored {stored_we.score}"
        )
        assert replayed.decision.value == stored_we.decision, (
            f"event {i}: replayed decision {replayed.decision.value!r} "
            f"!= stored {stored_we.decision!r}"
        )


def test_replay_db_range_skips_invalid_raw_json(db_session: Session) -> None:
    """replay_db_range skips rows with invalid raw_json and does not crash."""
    from chronos.time import utc_now_iso
    from chronos.wake.rules import canonical_json, rules_sha256

    device = _make_device(db_session)
    rules = load_yaml(_RULES_PATH)

    # Create a rule version with the real rules JSON so _get_baseline_rules works.
    rv_orm = RuleVersion(
        version=1,
        source="yaml",
        rules_json=canonical_json(rules),
        sha256=rules_sha256(rules),
        created_at=utc_now_iso(),
        created_by="test",
        active=1,
    )
    db_session.add(rv_orm)
    db_session.commit()
    assert rv_orm.id is not None
    assert device.id is not None

    # Create a day row so replay has context.
    from chronos.wake.rules import load_yaml as _ly
    from chronos.wake.state import get_or_create_day

    rules = _ly(_RULES_PATH)
    get_or_create_day(
        db_session,
        device,
        datetime.date(2026, 10, 6),
        "America/Los_Angeles",
        rules,
        _IN_WINDOW,
    )

    # Manually insert a TelemetryEvent + WakeEvaluation with invalid raw_json.
    bad_te = TelemetryEvent(
        device_id=device.id,
        received_at=utc_now_iso(),
        device_ts=_IN_WINDOW.isoformat(),
        tz="America/Los_Angeles",
        local_date="2026-10-06",
        wake_source="silent_push",
        seq=999,
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
        pedometer_auth="authorized",
        tick_seq=None,
        app_version="1.0.0",
        ios_version="18.1",
        raw_json="INVALID_JSON",  # intentionally bad
    )
    db_session.add(bad_te)
    db_session.flush()

    bad_we = WakeEvaluation(
        telemetry_event_id=bad_te.id,
        rule_version_id=rv_orm.id,
        local_date="2026-10-06",
        gate="open",
        score=0,
        breakdown_json="{}",
        decision="low",
        consecutive_high=0,
        evaluated_at=utc_now_iso(),
    )
    db_session.add(bad_we)
    db_session.commit()

    # Should not raise; the bad row is skipped.
    replay_days = replay_db_range(
        session=db_session,
        device_id=device.id,
        from_date=datetime.date(2026, 10, 6),
        to_date=datetime.date(2026, 10, 6),
        candidate_rules=rules,
        candidate_rule_version_number=1,
    )

    # The day exists but has 0 valid evals (all skipped).
    assert len(replay_days) == 1
    assert replay_days[0].baseline_evals == []
    assert replay_days[0].candidate_evals == []


# ---------------------------------------------------------------------------
# replay_fixture tests
# ---------------------------------------------------------------------------


def test_replay_fixture_both_rules() -> None:
    """replay_fixture works under two rule sets and returns a ReplayDay."""
    rules = load_yaml(_RULES_PATH)
    fd = load_fixture(_TYPICAL_WEEKDAY)

    replay_day = replay_fixture(fd, baseline_rules=rules, candidate_rules=rules)

    assert isinstance(replay_day, ReplayDay)
    assert replay_day.kind == "weekday"
    # Both baseline and candidate should produce same scores under same rules.
    assert len(replay_day.baseline_evals) == len(fd.events)
    assert len(replay_day.candidate_evals) == len(fd.events)
    for b, c in zip(replay_day.baseline_evals, replay_day.candidate_evals, strict=True):
        assert b.score == c.score


def test_replay_fixture_candidate_lower_threshold_confirms_earlier() -> None:
    """A candidate with lower confirm threshold may confirm at same or earlier tick."""
    import yaml

    rules = load_yaml(_RULES_PATH)
    fd = load_fixture(_TYPICAL_WEEKDAY)

    # Build a candidate with a much lower confirm threshold (50 vs 70).
    candidate_dict = yaml.safe_load(
        (_RULES_PATH.parent.parent / "config" / "wake_rules.yaml").read_text()
    )
    candidate_dict["thresholds"]["confirm"] = 50
    candidate_dict["thresholds"]["strong"] = 90
    from chronos.wake.rules import WakeRules as _WR

    candidate_rules = _WR.model_validate(candidate_dict)

    baseline_rd = replay_fixture(fd, baseline_rules=rules, candidate_rules=rules)
    candidate_rd = replay_fixture(fd, baseline_rules=rules, candidate_rules=candidate_rules)

    # baseline confirms at event 1 (consecutive).
    baseline_confirm_idx = next(
        (
            i
            for i, ev in enumerate(baseline_rd.baseline_evals)
            if ev.decision == EvaluationDecision.confirm
        ),
        None,
    )
    candidate_confirm_idx = next(
        (
            i
            for i, ev in enumerate(candidate_rd.candidate_evals)
            if ev.decision == EvaluationDecision.confirm
        ),
        None,
    )

    # With a lower threshold, candidate confirms at same or earlier index.
    assert candidate_confirm_idx is not None
    if baseline_confirm_idx is not None:
        assert candidate_confirm_idx <= baseline_confirm_idx
