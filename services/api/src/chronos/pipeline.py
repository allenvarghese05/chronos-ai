"""Pipeline orchestration: one job per stage, chained, idempotent per day (T3.5).

Stages run in a fixed order (``STAGE_ORDER``). Each stage is a durable ``jobs`` row keyed
``<stage>:<chain>`` where ``chain`` is ``<local_date>:<device_id>`` (plus ``:force:<token>`` for
a forced re-run). Because ``(kind, key)`` is unique, enqueuing an already-run chain is a no-op:
running the pipeline twice does not re-fetch or re-generate unless forced. A stage handler
enqueues the next stage only after its own work succeeded, so a failing stage leaves every
earlier artefact (items, brief) in place and stops the chain.

Stages implemented so far are listed in :data:`HANDLERS`; later milestones (assets, tts,
captions, render) register theirs here. ``render_mode`` (D09):

- ``eager``: prewarm at window start - 30 min runs every implemented stage;
- ``on_trigger``: prewarm runs only ``ingest``; the rest is enqueued when the day confirms.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from chronos.config import Settings, get_settings, settings_from_snapshot, settings_snapshot
from chronos.content.ingest import IngestReport, fetch_all
from chronos.content.script.job import ScriptJobPayload, run_script_job
from chronos.content.script.service import build_plans
from chronos.content.selection import persist_outcomes
from chronos.content.sources import SourcesConfig, load_sources_yaml, sync_sources
from chronos.db import create_db_engine
from chronos.enums import JobKind
from chronos.http import GuardedHttpClient
from chronos.jobs.queue import JobInput, JobQueue
from chronos.jobs.worker import SETTINGS_FILE, Handler, JobContext, Worker
from chronos.models.devices import Device
from chronos.models.jobs import Job
from chronos.time import utc_now_iso

log = structlog.get_logger(__name__)

STAGE_ORDER: tuple[JobKind, ...] = (
    JobKind.ingest,
    JobKind.script,
    JobKind.assets,
    JobKind.tts,
    JobKind.captions,
    JobKind.render,
)
PREWARM_LEAD = datetime.timedelta(minutes=30)
RenderMode = Literal["eager", "on_trigger"]


class StagePayload(BaseModel):
    """Payload of every pipeline stage job."""

    model_config = ConfigDict(extra="forbid")

    local_date: datetime.date
    device_id: int
    chain: str = Field(min_length=1, max_length=120)
    stages: list[JobKind] = Field(min_length=1)
    force: bool = False
    max_attempts: int = Field(default=3, ge=1, le=10)


def chain_key(local_date: datetime.date, device_id: int, force: bool) -> str:
    base = f"{local_date.isoformat()}:{device_id}"
    return f"{base}:force:{secrets.token_hex(4)}" if force else base


def job_key(stage: JobKind, chain: str) -> str:
    return f"{stage.value}:{chain}"


def implemented(stages: Sequence[JobKind]) -> list[JobKind]:
    """*stages* in pipeline order, restricted to stages with a registered handler."""
    wanted = set(stages)
    return [s for s in STAGE_ORDER if s in wanted and s in HANDLERS]


def stages_for(render_mode: RenderMode, trigger: Literal["prewarm", "confirm"]) -> list[JobKind]:
    """Which stages a trigger runs under *render_mode* (D09)."""
    if render_mode == "eager":
        return implemented(STAGE_ORDER) if trigger == "prewarm" else []
    rest = [s for s in STAGE_ORDER if s != JobKind.ingest]
    return implemented([JobKind.ingest] if trigger == "prewarm" else rest)


def prewarm_due(now: datetime.datetime, window_start: datetime.datetime,
                window_end: datetime.datetime) -> bool:  # fmt: skip
    """Pure: prewarm is due from window start - 30 min until the window ends."""
    return window_start - PREWARM_LEAD <= now < window_end


def enqueue_chain(
    queue: JobQueue,
    *,
    local_date: datetime.date,
    device_id: int,
    stages: Sequence[JobKind],
    now: datetime.datetime,
    force: bool = False,
    max_attempts: int = 3,
) -> Job | None:
    """Enqueue the first stage of a chain; returns the job (existing one when not forced)."""
    ordered = implemented(stages)
    if not ordered:
        return None
    payload = StagePayload(
        local_date=local_date,
        device_id=device_id,
        chain=chain_key(local_date, device_id, force),
        stages=ordered,
        force=force,
        max_attempts=max_attempts,
    )
    return _enqueue_stage(queue, ordered[0], payload, now)


def _enqueue_stage(
    queue: JobQueue, stage: JobKind, payload: StagePayload, now: datetime.datetime
) -> Job:
    return queue.enqueue(
        JobInput(
            kind=stage,
            key=job_key(stage, payload.chain),
            payload=json.loads(payload.model_dump_json()),
            max_attempts=payload.max_attempts,
        ),
        now,
    )


def enqueue_next(queue: JobQueue, payload: StagePayload, done: JobKind,
                 now: datetime.datetime) -> Job | None:  # fmt: skip
    """After *done* succeeded, enqueue the following stage of the chain (if any)."""
    remaining = payload.stages[payload.stages.index(done) + 1 :]
    return _enqueue_stage(queue, remaining[0], payload, now) if remaining else None


def ensure_day(
    session: Session,
    device: Device,
    now: datetime.datetime,
    local_date: datetime.date | None = None,
) -> datetime.date:
    """Resolve the device-local day (default: today in the device timezone) and make sure its
    ``days`` row exists, so stage jobs (the script job needs it) can run. Commits."""
    from zoneinfo import ZoneInfo

    from chronos.wake.rules import get_rules_cache
    from chronos.wake.state import get_or_create_day
    from chronos.wake.window import local_date_for

    rules, _ = get_rules_cache().get_effective(session)
    if local_date is None:
        boundary = rules.window.to_window_config().day_boundary
        local_date = local_date_for(now, ZoneInfo(device.tz), boundary)
    get_or_create_day(session, device, local_date, device.tz, rules, now)
    session.commit()
    return local_date


# --- stage implementations (injectable, tested in-process) --------------------------------------


def ingest_stage(
    session: Session, sources: SourcesConfig, client: GuardedHttpClient, now: str
) -> IngestReport:
    """Sync ``sources.yaml`` into the DB, fetch every enabled source, persist items."""
    sync_sources(session, sources, now)
    session.commit()

    async def fetch() -> IngestReport:
        try:
            return await fetch_all(client, session, sources, now)
        finally:
            await client.aclose()

    report = asyncio.run(fetch())
    written = persist_outcomes(session, report.outcomes, now)
    log.info("ingest_done", items=report.total_items, persisted=sum(written.values()),
             errors=[o.source_key for o in report.outcomes if o.status == "error"])  # fmt: skip
    return report


# --- worker entry points (subprocess; build real dependencies from settings) -------------------


@dataclass(frozen=True)
class _Deps:
    settings: Settings
    engine: Engine


def _deps(ctx: JobContext | None = None) -> _Deps:
    """Settings for a job process: the worker's 0600 snapshot (deleted once read), else env."""
    settings: Settings | None = None
    if ctx is not None:
        snapshot = ctx.work_dir / SETTINGS_FILE
        if snapshot.exists():
            settings = settings_from_snapshot(snapshot.read_text(encoding="utf-8"))
            snapshot.unlink()
    settings = settings or get_settings()
    return _Deps(settings, create_db_engine(settings.database_url))


def _chain_next(deps: _Deps, payload: StagePayload, done: JobKind) -> None:
    nxt = enqueue_next(JobQueue(deps.engine), payload, done, datetime.datetime.now(datetime.UTC))
    if nxt is not None:
        log.info("pipeline_next_enqueued", stage=nxt.kind, key=nxt.key)


def ingest_job(ctx: JobContext) -> None:
    payload = StagePayload.model_validate_json(ctx.payload_json)
    deps = _deps(ctx)
    try:
        sources = load_sources_yaml(deps.settings.sources_path)
        client = GuardedHttpClient(
            sources.http.user_agent,
            timeout_sec=sources.http.timeout_sec,
            max_bytes=sources.http.max_bytes,
            max_redirects=sources.http.max_redirects,
        )
        with Session(deps.engine) as session:
            ingest_stage(session, sources, client, utc_now_iso())
        _chain_next(deps, payload, JobKind.ingest)
    finally:
        deps.engine.dispose()


def script_job(ctx: JobContext) -> None:
    payload = StagePayload.model_validate_json(ctx.payload_json)
    deps = _deps(ctx)
    try:
        sources = load_sources_yaml(deps.settings.sources_path)
        with Session(deps.engine) as session:
            run_script_job(
                session,
                ScriptJobPayload(
                    local_date=payload.local_date, device_id=payload.device_id,
                    force=payload.force,
                ),
                sources,
                utc_now_iso(),
                build_plans(deps.settings),
                expected_digest=deps.settings.ollama_digest,
            )  # fmt: skip
        _chain_next(deps, payload, JobKind.script)
    finally:
        deps.engine.dispose()


HANDLERS: dict[JobKind, Handler] = {
    JobKind.ingest: Handler("chronos.pipeline", "ingest_job"),
    JobKind.script: Handler("chronos.pipeline", "script_job"),
}


# --- synchronous driver for `chronos run` -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageResult:
    stage: str
    status: str
    attempts: int
    duration_ms: int | None
    error: str | None


def chain_status(engine: Engine, chain: str, stages: Sequence[JobKind]) -> list[StageResult]:
    keys = [job_key(s, chain) for s in stages]
    with Session(engine) as session:
        rows = {j.key: j for j in session.exec(select(Job).where(col(Job.key).in_(keys))).all()}
    out: list[StageResult] = []
    for stage, key in zip(stages, keys, strict=True):
        job = rows.get(key)
        out.append(
            StageResult(stage.value, job.status, job.attempts, job.duration_ms, job.last_error)
            if job is not None
            else StageResult(stage.value, "pending", 0, None, None)
        )
    return out


def run_chain(
    engine: Engine,
    data_root: Path,
    settings: Settings,
    *,
    local_date: datetime.date,
    device_id: int,
    stages: Sequence[JobKind],
    force: bool = False,
    handlers: Mapping[JobKind, Handler] | None = None,
    timeout_sec: float = 1800.0,
    poll_sec: float = 0.5,
    clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC),
) -> list[StageResult]:
    """Enqueue a chain and drive a local worker until it finishes, fails or times out.

    If the API's own worker is running it may execute the jobs instead; the shared execution
    lock guarantees only one runs at a time, and this loop just observes the jobs table.
    """
    ordered = implemented(stages) if handlers is None else [
        s for s in STAGE_ORDER if s in set(stages) and s in handlers
    ]  # fmt: skip
    if not ordered:
        return []
    queue = JobQueue(engine)
    payload = StagePayload(
        local_date=local_date, device_id=device_id,
        chain=chain_key(local_date, device_id, force), stages=ordered, force=force,
        max_attempts=1,
    )  # fmt: skip
    _enqueue_stage(queue, ordered[0], payload, clock())
    worker = Worker(
        queue, data_root, dict(handlers or HANDLERS), clock=clock,
        settings_json=settings_snapshot(settings),
    )  # fmt: skip
    stop = threading.Event()
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        results = chain_status(engine, payload.chain, ordered)
        if any(r.status in ("failed", "cancelled") for r in results) or all(
            r.status == "done" for r in results
        ):
            return results
        if not worker.run_once(stop):
            time.sleep(poll_sec)
    return chain_status(engine, payload.chain, ordered)
