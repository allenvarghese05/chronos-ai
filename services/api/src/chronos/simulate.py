"""``chronos simulate-morning``: a full morning on a simulated clock (T5.5).

Runs the *real* scheduler, wake engine, pipeline jobs and delivery against an **isolated**
database and data directory (never the live ones), with a fake push client and a fake phone:

1. minute by minute from window start - 35 min, the scheduler runs as it does in production;
2. prewarm (window start - 30 min) enqueues the pipeline, which the local worker executes for
   real (ingest -> script -> assets -> tts -> captions -> render) unless ``pipeline=False``;
3. every silent tick the scheduler "sends" is answered by a synthetic telemetry snapshot: in
   bed (charging, locked, still) before ``wake_at``, up and walking afterwards;
4. the run stops once the visible push is delivered (or the window ends).

The result lists what a real morning should show (docs/11 T5.5): prewarm before the window,
>= 1 tick per 5 minutes, confirmation within 10 minutes of the wake time, the push, the reel.
"""

from __future__ import annotations

import datetime
import itertools
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import structlog
from sqlmodel import Session, select

from chronos.auth import hash_token, new_token
from chronos.config import Settings, settings_snapshot
from chronos.db import create_db_engine
from chronos.delivery.push import FakePushClient, PushMessage
from chronos.enums import JobKind
from chronos.ids import new_ulid
from chronos.jobs.queue import JobQueue
from chronos.jobs.worker import Handler, Worker
from chronos.models.devices import Device
from chronos.models.jobs import Job
from chronos.models.media import Reel
from chronos.models.wake import Day
from chronos.time import utc_now_iso
from chronos.wake.rules import ensure_initial_version, get_rules_cache
from chronos.wake.service import ingest_telemetry
from chronos.wake.signals import Signals
from chronos.wake.window import window_for

log = structlog.get_logger(__name__)

FAKE_PUSH_TOKEN = "ExponentPushToken[simulated-morning]"  # noqa: S105 - not a secret


@dataclass
class SimulationReport:
    local_date: str
    wake_at_utc: str
    window_start_utc: str
    prewarm_at: str | None = None
    pipeline: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    ticks: list[str] = field(default_factory=list[str])
    confirmed_at: str | None = None
    delivered_at: str | None = None
    delivery: dict[str, object] | None = None
    reel_file: str | None = None
    reel_seconds: float | None = None

    def checks(self) -> dict[str, bool]:
        start = datetime.datetime.fromisoformat(self.window_start_utc)
        wake = datetime.datetime.fromisoformat(self.wake_at_utc)
        done = [s for s, status in self.pipeline if status == "done"]
        ticks = [datetime.datetime.fromisoformat(t) for t in self.ticks]
        gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(ticks)]
        confirm = datetime.datetime.fromisoformat(self.confirmed_at) if self.confirmed_at else None
        return {
            "prewarm before window": self.prewarm_at is not None
            and datetime.datetime.fromisoformat(self.prewarm_at) < start,
            "pipeline completed": bool(self.pipeline) and len(done) == len(self.pipeline),
            ">= 1 tick per 5 min": bool(ticks) and all(g <= 300 for g in gaps),
            "confirmed within 10 min of wake": confirm is not None
            and datetime.timedelta(0) <= confirm - wake <= datetime.timedelta(minutes=10),
            "push delivered": self.delivery is not None,
            "reel ready": self.reel_file is not None,
        }


def _phone(now: datetime.datetime, wake: datetime.datetime, tz: str, seq: int,
           tick_seq: int | None) -> Signals:  # fmt: skip
    """A synthetic snapshot: asleep before *wake*, up and moving after."""
    awake_min = (now - wake).total_seconds() / 60
    up = awake_min >= 0
    steps = min(400, int(40 + 12 * awake_min)) if up else 0
    return Signals.model_validate(
        {
            "device_ts": now,
            "tz": tz,
            "wake_source": "silent_push",
            "steps_10m": steps,
            "steps_since_window": min(5000, steps * 2),
            "walking_sec_10m": min(600, int(60 + 30 * awake_min)) if up else 0,
            "walking_conf": "high" if up else "low",
            "stationary_sec_10m": 0 if up else 600,
            "is_unlocked": up,
            "battery_state": "unplugged" if up else "charging",
            "battery_level": 0.8,
            "app_state": "background",
            "low_power_mode": False,
            "pedometer_auth": "authorized",
            "seq": seq,
            "tick_seq": tick_seq,
            "app_version": "simulated",
            "ios_version": "18.0",
        }
    )


def _drain(engine_url: str, settings: Settings, handlers: Mapping[JobKind, Handler],
           timeout_sec: float) -> None:  # fmt: skip
    """Run queued jobs with a local worker until the queue is empty (or timeout)."""
    import time

    engine = create_db_engine(engine_url)
    worker = Worker(JobQueue(engine), settings.data_root.resolve(), dict(handlers),
                    settings_json=settings_snapshot(settings))  # fmt: skip
    stop = threading.Event()
    deadline = time.monotonic() + timeout_sec
    try:
        while time.monotonic() < deadline:
            with Session(engine) as session:
                pending = session.exec(
                    select(Job).where(Job.status.in_(["queued", "running"]))  # type: ignore[attr-defined]
                ).first()
            if pending is None:
                return
            if not worker.run_once(stop):
                time.sleep(0.2)
    finally:
        engine.dispose()


async def simulate_morning(
    settings: Settings,
    *,
    local_date: datetime.date,
    wake_local: datetime.time,
    tz: str = "Asia/Kolkata",
    handlers: Mapping[JobKind, Handler] | None = None,
    pipeline_timeout_sec: float = 900.0,
) -> SimulationReport:
    """Simulate one morning against ``settings`` (which must point at an isolated DB/data dir)."""
    from chronos.pipeline import HANDLERS
    from chronos.scheduler import Scheduler

    engine = create_db_engine(settings.database_url)
    with Session(engine) as session:
        ensure_initial_version(session, settings.wake_rules_path)
        session.commit()
        rules, _ = get_rules_cache().get_effective(session)
        now_str = utc_now_iso()
        device = Device(public_id=new_ulid(), name="Simulated iPhone",
                        token_hash=hash_token(new_token()), expo_push_token=FAKE_PUSH_TOKEN,
                        tz=tz, app_version="simulated", ios_version="18.0", model="Simulator",
                        status="active", created_at=now_str, last_seen_at=now_str)  # fmt: skip
        session.add(device)
        session.commit()
        session.refresh(device)
        device_id = device.id or 0
    window = window_for(local_date, ZoneInfo(tz), rules.window.to_window_config())
    wake = datetime.datetime.combine(local_date, wake_local, ZoneInfo(tz)).astimezone(datetime.UTC)
    report = SimulationReport(
        local_date.isoformat(), wake.isoformat(), window.start_utc.isoformat()
    )

    push = FakePushClient()
    scheduler = Scheduler(session_factory=lambda: Session(engine), push_client=push,
                          settings=settings)  # fmt: skip
    now = window.start_utc - datetime.timedelta(minutes=35)
    end = min(window.end_utc, wake + datetime.timedelta(minutes=45))
    answered = 0
    seq = 0
    while now <= end and report.delivery is None:
        await scheduler.run_due(now)
        with Session(engine) as session:
            if report.prewarm_at is None and session.exec(select(Job)).first() is not None:
                report.prewarm_at = now.isoformat()
                _drain(settings.database_url, settings, handlers or HANDLERS,
                       pipeline_timeout_sec)  # fmt: skip
                jobs = session.exec(select(Job).order_by(Job.id)).all()  # type: ignore[arg-type]
                report.pipeline = [(j.kind, j.status) for j in jobs]
        new: list[PushMessage] = push.sent[answered:]
        answered = len(push.sent)
        for message in new:
            kind = message.data.get("type")
            if kind == "tick":
                report.ticks.append(now.isoformat())
                with Session(engine) as session:
                    dev = session.get(Device, device_id)
                    assert dev is not None
                    rules, version = get_rules_cache().get_effective(session)
                    signals = _phone(now, wake, tz, seq, message.data.get("tick_seq"))  # type: ignore[arg-type]
                    ingest_telemetry(session, dev, signals, signals.model_dump_json().encode(),
                                     now, rules, version)  # fmt: skip
                    seq += 1
            elif kind in ("reel_ready", "text_fallback"):
                report.delivery = dict(message.data)
                report.delivered_at = now.isoformat()
        with Session(engine) as session:
            day = session.get(Day, (local_date.isoformat(), device_id))
            if day is not None and day.confirmed_at and report.confirmed_at is None:
                report.confirmed_at = datetime.datetime.fromisoformat(
                    day.confirmed_at.replace("Z", "+00:00")
                ).isoformat()
        now += datetime.timedelta(minutes=1)

    with Session(engine) as session:
        reel = session.exec(select(Reel).where(Reel.status == "ready")).first()
        if reel is not None:
            report.reel_file = str(settings.data_root / "media" / (reel.file_path or ""))
            report.reel_seconds = reel.duration_sec
    engine.dispose()
    return report


def render_report(report: SimulationReport) -> str:
    lines = [f"Simulated morning {report.local_date}", f"  window opens  {report.window_start_utc}",
             f"  owner wakes   {report.wake_at_utc}", f"  prewarm       {report.prewarm_at}",
             f"  pipeline      {', '.join(f'{k}:{s}' for k, s in report.pipeline) or '-'}",
             f"  ticks         {len(report.ticks)}", f"  confirmed     {report.confirmed_at}",
             f"  delivered     {report.delivered_at} {json.dumps(report.delivery)}",
             f"  reel          {report.reel_file} ({report.reel_seconds} s)",
             "", "Checks:"]  # fmt: skip
    lines += [f"  {'✓' if ok else '✗'} {name}" for name, ok in report.checks().items()]
    return "\n".join(lines)
