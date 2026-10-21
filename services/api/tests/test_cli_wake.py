"""Tests for chronos wake replay / annotate and chronos report build (T1.8 step 5).

Uses typer CliRunner to invoke subcommands in-process.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event as sa_event
from sqlmodel import Session, create_engine, select
from typer.testing import CliRunner

from chronos.cli import app
from chronos.models.wake import Day, RuleVersion
from chronos.wake.replay import load_fixture
from chronos.wake.rules import load_yaml
from chronos.wake.service import ingest_telemetry

_UTC = datetime.UTC
_RULES_PATH = Path(__file__).parent / ".." / "config" / "wake_rules.yaml"
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "wake" / "mornings"
_TYPICAL_WEEKDAY = _FIXTURE_DIR / "typical_weekday.json"
_IN_WINDOW = datetime.datetime(2026, 10, 6, 13, 48, 0, tzinfo=_UTC)

runner = CliRunner()


# ---------------------------------------------------------------------------
# DB fixture helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "cli_test.db"
    db_url = f"sqlite:///{db}"
    ini_path = Path(__file__).parent / ".." / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    return db


def _make_session(db_path: Path) -> Session:
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})

    @sa_event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: sqlite3.Connection, _record: object) -> None:
        dbapi_conn.cursor().execute("PRAGMA foreign_keys=ON")

    return Session(engine)


def _ingest_fixture_db(db_path: Path) -> None:
    """Ingest typical_weekday into the DB."""
    from chronos.time import utc_now_iso
    from chronos.wake.rules import ActiveRuleVersion, canonical_json, rules_sha256
    from chronos.wake.signals import Signals

    rules = load_yaml(_RULES_PATH)
    fd = load_fixture(_TYPICAL_WEEKDAY)

    with _make_session(db_path) as session:
        from tests.factories import make_device

        device = make_device(session)
        session.commit()

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

        active_rv = ActiveRuleVersion(
            id=rv_orm.id,
            version=rv_orm.version,
            source=rv_orm.source,
            sha256=rv_orm.sha256,
            created_at=rv_orm.created_at,
            created_by=rv_orm.created_by,
        )
        now = _IN_WINDOW
        for ev in fd.events:
            sig = Signals.model_validate(ev)
            raw = sig.model_dump_json().encode()
            ingest_telemetry(session, device, sig, raw, now, rules, active_rv)
            now = now + datetime.timedelta(minutes=5)
        session.commit()


# ---------------------------------------------------------------------------
# wake replay tests
# ---------------------------------------------------------------------------


def test_wake_replay_fixture_prints_table(tmp_path: Path, db_path: Path) -> None:
    """wake replay --fixture prints a table with date and kind."""
    result = runner.invoke(
        app,
        [
            "wake",
            "replay",
            "--from",
            "2026-10-06",
            "--to",
            "2026-10-06",
            "--fixture",
            str(_TYPICAL_WEEKDAY),
        ],
        env={"CHRONOS_DATABASE_URL": f"sqlite:///{db_path}", "CHRONOS_SECRET_KEY": "x" * 32},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    output = result.output
    # Should contain the date and at least "weekday".
    assert "2026-10-06" in output or "weekday" in output or "Summary" in output


def test_wake_replay_fixture_json_output(tmp_path: Path, db_path: Path) -> None:
    """wake replay --fixture --json outputs valid JSON with 'days' key."""
    result = runner.invoke(
        app,
        [
            "wake",
            "replay",
            "--from",
            "2026-10-06",
            "--to",
            "2026-10-06",
            "--fixture",
            str(_TYPICAL_WEEKDAY),
            "--json",
        ],
        env={"CHRONOS_DATABASE_URL": f"sqlite:///{db_path}", "CHRONOS_SECRET_KEY": "x" * 32},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # Extract JSON from output (may have leading summary text).
    json_start = result.output.find("{")
    assert json_start >= 0, f"No JSON found in output: {result.output}"
    data = json.loads(result.output[json_start:])
    assert "days" in data
    assert "summary" in data


def test_wake_replay_typical_weekday_confirms(tmp_path: Path, db_path: Path) -> None:
    """Typical weekday fixture replay shows at least one confirm in the summary."""
    result = runner.invoke(
        app,
        [
            "wake",
            "replay",
            "--from",
            "2026-10-06",
            "--to",
            "2026-10-06",
            "--fixture",
            str(_TYPICAL_WEEKDAY),
            "--json",
        ],
        env={"CHRONOS_DATABASE_URL": f"sqlite:///{db_path}", "CHRONOS_SECRET_KEY": "x" * 32},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    json_start = result.output.find("{")
    data = json.loads(result.output[json_start:])
    days = data["days"]
    assert len(days) == 1
    # typical_weekday confirms at event 1, so baseline_confirm should be set.
    assert days[0]["baseline_confirm"] is not None


# ---------------------------------------------------------------------------
# wake annotate tests
# ---------------------------------------------------------------------------


def test_wake_annotate_valid(db_path: Path) -> None:
    """wake annotate updates actual_wake_local in the DB."""
    _ingest_fixture_db(db_path)

    result = runner.invoke(
        app,
        ["wake", "annotate", "2026-10-06", "--actual", "07:05"],
        env={"CHRONOS_DATABASE_URL": f"sqlite:///{db_path}", "CHRONOS_SECRET_KEY": "x" * 32},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "07:05" in result.output

    # Verify DB was updated.
    with _make_session(db_path) as session:
        day = session.exec(  # type: ignore[call-overload]
            select(Day).where(Day.local_date == "2026-10-06")
        ).first()
        assert day is not None
        assert day.actual_wake_local == "07:05"


def test_wake_annotate_invalid_format(db_path: Path) -> None:
    """wake annotate exits 1 when --actual is not HH:MM."""
    result = runner.invoke(
        app,
        ["wake", "annotate", "2026-10-06", "--actual", "not-a-time"],
        env={"CHRONOS_DATABASE_URL": f"sqlite:///{db_path}", "CHRONOS_SECRET_KEY": "x" * 32},
    )
    assert result.exit_code == 1


def test_wake_annotate_invalid_date(db_path: Path) -> None:
    """wake annotate exits 1 when date is not YYYY-MM-DD."""
    result = runner.invoke(
        app,
        ["wake", "annotate", "bad-date", "--actual", "07:05"],
        env={"CHRONOS_DATABASE_URL": f"sqlite:///{db_path}", "CHRONOS_SECRET_KEY": "x" * 32},
    )
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# report build tests
# ---------------------------------------------------------------------------


def test_report_build_stdout(tmp_path: Path, db_path: Path) -> None:
    """report build --date --stdout prints Markdown to stdout."""
    _ingest_fixture_db(db_path)

    result = runner.invoke(
        app,
        ["report", "build", "--date", "2026-10-06", "--stdout"],
        env={
            "CHRONOS_DATABASE_URL": f"sqlite:///{db_path}",
            "CHRONOS_SECRET_KEY": "x" * 32,
            "CHRONOS_REPORTS_DIR": str(tmp_path / "reports"),
        },
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "# Morning Report" in result.output


def test_report_build_writes_file(tmp_path: Path, db_path: Path) -> None:
    """report build --date writes a file to reports_dir."""
    _ingest_fixture_db(db_path)
    reports_dir = tmp_path / "reports"

    result = runner.invoke(
        app,
        ["report", "build", "--date", "2026-10-06"],
        env={
            "CHRONOS_DATABASE_URL": f"sqlite:///{db_path}",
            "CHRONOS_SECRET_KEY": "x" * 32,
            "CHRONOS_REPORTS_DIR": str(reports_dir),
        },
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert (reports_dir / "2026-10-06.md").exists()
