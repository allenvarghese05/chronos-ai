"""Nightly retention and backup (T5.1, docs/04 §3, D24).

Deletion order respects every foreign key:

1. unsaved reels older than 24 h (files, ``reel_assets``, row);
2. days older than 90 days that no remaining reel uses: their ``brief_items``, ``briefs``,
   ``day_transitions`` and the ``days`` rows;
3. wake evaluations older than 30 days that no remaining day/transition references;
4. telemetry older than 30 days that no remaining evaluation references;
5. items older than 14 days that no brief references;
6. push notifications older than 30 days; jobs ``done`` > 7 days, ``failed`` > 30 days.

Then ``VACUUM INTO data/backups/chronos-<date>.sqlite`` (last 7 kept).
"""

from __future__ import annotations

import datetime
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from sqlalchemy import Engine, text
from sqlmodel import Session, col, select

from chronos.media.reels import reel_dir
from chronos.models.media import Reel
from chronos.time import format_utc_iso

log = structlog.get_logger(__name__)

REEL_TTL = datetime.timedelta(hours=24)
DAYS_KEEP = datetime.timedelta(days=90)
WAKE_KEEP = datetime.timedelta(days=30)
ITEMS_KEEP = datetime.timedelta(days=14)
PUSH_KEEP = datetime.timedelta(days=30)
JOBS_DONE_KEEP = datetime.timedelta(days=7)
JOBS_FAILED_KEEP = datetime.timedelta(days=30)
BACKUPS_KEEP = 7

# Days older than 90 days that no remaining reel uses (literal SQL, bound :c only).
_DELETE_OLD_BRIEF_ITEMS = (
    "DELETE FROM brief_items WHERE brief_id IN (SELECT id FROM briefs WHERE "
    "(local_date, device_id) IN (SELECT d.local_date, d.device_id FROM days d "
    "WHERE d.created_at < :c AND NOT EXISTS (SELECT 1 FROM reels r "
    "WHERE r.local_date = d.local_date AND r.device_id = d.device_id))"
    ")"
)
_DELETE_OLD_BRIEFS = (
    "DELETE FROM briefs WHERE "
    "(local_date, device_id) IN (SELECT d.local_date, d.device_id FROM days d "
    "WHERE d.created_at < :c AND NOT EXISTS (SELECT 1 FROM reels r "
    "WHERE r.local_date = d.local_date AND r.device_id = d.device_id))"
)
_DELETE_OLD_TRANSITIONS = (
    "DELETE FROM day_transitions WHERE "
    "(local_date, device_id) IN (SELECT d.local_date, d.device_id FROM days d "
    "WHERE d.created_at < :c AND NOT EXISTS (SELECT 1 FROM reels r "
    "WHERE r.local_date = d.local_date AND r.device_id = d.device_id))"
)
_DELETE_OLD_DAYS = (
    "DELETE FROM days WHERE "
    "(local_date, device_id) IN (SELECT d.local_date, d.device_id FROM days d "
    "WHERE d.created_at < :c AND NOT EXISTS (SELECT 1 FROM reels r "
    "WHERE r.local_date = d.local_date AND r.device_id = d.device_id))"
)


@dataclass
class RetentionReport:
    deleted: dict[str, int] = field(default_factory=dict[str, int])
    backup: str | None = None


def run_retention(session: Session, data_root: Path, now: datetime.datetime) -> RetentionReport:
    """Purge expired data (commits). See the module docstring for the order."""
    report = RetentionReport()

    def cut(delta: datetime.timedelta) -> str:
        return format_utc_iso(now - delta)

    def execute(name: str, sql: str, **params: str) -> None:
        result = session.connection().execute(text(sql), params)
        report.deleted[name] = report.deleted.get(name, 0) + (result.rowcount or 0)

    # 1. unsaved reels older than 24 h
    expired = session.exec(
        select(Reel).where(Reel.saved == 0, col(Reel.created_at) < cut(REEL_TTL))
    ).all()
    for reel in expired:
        shutil.rmtree(reel_dir(data_root, reel), ignore_errors=True)
        execute("reel_assets", "DELETE FROM reel_assets WHERE reel_id = :r", r=str(reel.id))
        session.delete(reel)
        report.deleted["reels"] = report.deleted.get("reels", 0) + 1
    session.flush()

    # 2. days older than 90 days that no remaining reel uses (constant SQL, bound params only)
    c90 = cut(DAYS_KEEP)
    execute("brief_items", _DELETE_OLD_BRIEF_ITEMS, c=c90)
    execute("briefs", _DELETE_OLD_BRIEFS, c=c90)
    execute("day_transitions", _DELETE_OLD_TRANSITIONS, c=c90)
    execute("days", _DELETE_OLD_DAYS, c=c90)

    # 3-4. wake evaluations and telemetry (only rows nothing references any more)
    execute(
        "wake_evaluations",
        "DELETE FROM wake_evaluations WHERE evaluated_at < :c "
        "AND id NOT IN (SELECT confirm_evaluation_id FROM days WHERE confirm_evaluation_id "
        "IS NOT NULL) AND id NOT IN (SELECT caused_by_evaluation_id FROM day_transitions "
        "WHERE caused_by_evaluation_id IS NOT NULL)",
        c=cut(WAKE_KEEP),
    )
    execute(
        "telemetry_events",
        "DELETE FROM telemetry_events WHERE received_at < :c "
        "AND id NOT IN (SELECT telemetry_event_id FROM wake_evaluations)",
        c=cut(WAKE_KEEP),
    )

    # 5. items no brief references
    execute("items", "DELETE FROM items WHERE fetched_at < :c "
            "AND id NOT IN (SELECT item_id FROM brief_items)", c=cut(ITEMS_KEEP))  # fmt: skip

    # 6. push notifications, jobs
    execute("push_notifications", "DELETE FROM push_notifications WHERE sent_at < :c",
            c=cut(PUSH_KEEP))  # fmt: skip
    execute("jobs_done", "DELETE FROM jobs WHERE status = 'done' AND finished_at < :c",
            c=cut(JOBS_DONE_KEEP))  # fmt: skip
    execute("jobs_failed", "DELETE FROM jobs WHERE status IN ('failed', 'cancelled') "
            "AND finished_at < :c", c=cut(JOBS_FAILED_KEEP))  # fmt: skip
    session.commit()
    log.info("retention_done", **report.deleted)
    return report


def backup_database(engine: Engine, data_root: Path, now: datetime.datetime) -> Path:
    """``VACUUM INTO`` a dated copy under ``data/backups``; keeps the newest seven."""
    backups = data_root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"chronos-{now.strftime('%Y%m%d')}.sqlite"
    if target.exists():
        target.unlink()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("VACUUM INTO ?", (str(target),))
    for old in sorted(backups.glob("chronos-*.sqlite"))[:-BACKUPS_KEEP]:
        old.unlink()
    return target
