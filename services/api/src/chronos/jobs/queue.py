"""Short SQLite transactions and per-attempt fencing, separate from OS execution ownership."""

from __future__ import annotations

import datetime as dt
import json
import secrets
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from sqlalchemy import Engine, bindparam, text
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, col, select

from chronos.enums import JobKind
from chronos.models.jobs import Job
from chronos.time import format_utc_iso, parse_utc_iso

ErrorCode = Literal["handler_failed", "timeout", "interrupted", "worker_lost", "spawn_failed"]
_ERRORS = {"handler_failed", "timeout", "interrupted", "worker_lost", "spawn_failed"}
_TIMEOUTS: dict[JobKind, int] = {
    JobKind.ingest: 120,
    JobKind.script: 300,
    JobKind.assets: 300,
    JobKind.tts: 300,
    JobKind.captions: 300,
    JobKind.render: 900,
    JobKind.deliver: 60,
    JobKind.receipts: 60,
    JobKind.retention: 300,
    JobKind.report: 120,
}


def timestamp(now: dt.datetime) -> str:
    """Reject naive clocks and normalize aware instants into sortable canonical UTC."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("queue clocks must be timezone aware")
    return format_utc_iso(now)


def payload_json(value: dict[str, JsonValue]) -> str:
    """Canonical bounded JSON object; never log a rejected body or a handler payload."""

    def depth(item: JsonValue, level: int) -> None:
        if level > 8:
            raise ValueError("job payload nesting exceeds limit")
        if isinstance(item, dict):
            for child in item.values():
                depth(child, level + 1)
        elif isinstance(item, list):
            for child in item:
                depth(child, level + 1)

    depth(value, 0)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 16384:
        raise ValueError("job payload exceeds limit")
    return encoded


class JobInput(BaseModel):
    """Trusted internal enqueue input, never an HTTP argv/module execution contract."""

    model_config = ConfigDict(extra="forbid")
    kind: JobKind
    key: str = Field(min_length=1, max_length=200, strict=True)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    priority: Annotated[int, Field(strict=True, ge=-100, le=100)] = 10
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=10)] = 3
    timeout_sec: Annotated[int, Field(strict=True, ge=1, le=3600)] | None = None

    @field_validator("payload")
    @classmethod
    def _payload(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        payload_json(value)
        return value


@dataclass(frozen=True)
class Claim:
    """Immutable identity of exactly one attempt; payload is an immutable JSON string."""

    id: int
    kind: JobKind
    payload_json: str
    attempts: int
    timeout_sec: int
    owner: str


class JobQueue:
    """Own an Engine, not a Session; heavy execution never holds a writer transaction."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def enqueue(
        self, request: JobInput, now: dt.datetime, run_after: dt.datetime | None = None
    ) -> Job:
        """Insert once by kind/key; a concurrent duplicate returns the original unmodified row."""
        stamp = timestamp(now)
        due = timestamp(run_after if run_after is not None else now)
        body = payload_json(request.payload)
        with Session(self.engine, expire_on_commit=False) as session:
            statement = (
                insert(Job)
                .values(
                    kind=request.kind.value,
                    key=request.key,
                    payload_json=body,
                    status="queued",
                    priority=request.priority,
                    attempts=0,
                    max_attempts=request.max_attempts,
                    run_after=due,
                    timeout_sec=request.timeout_sec or _TIMEOUTS[request.kind],
                    created_at=stamp,
                )
                .on_conflict_do_nothing(index_elements=["kind", "key"])
            )
            session.exec(statement)
            row = session.exec(
                select(Job).where(Job.kind == request.kind, Job.key == request.key)
            ).one()
            session.commit()
            return row

    def claim(self, kinds: set[JobKind], now: dt.datetime) -> Claim | None:
        """Atomically claim a due supported row; caller also owns the OS execution lock."""
        stamp = timestamp(now)
        if not kinds:
            return None
        owner = secrets.token_hex(24)
        statement = text(
            "UPDATE jobs SET status='running', locked_at=:now, locked_by=:owner,"
            " attempts=attempts+1, started_at=:now, finished_at=NULL, duration_ms=NULL"
            " WHERE id=(SELECT id FROM jobs WHERE status='queued' AND run_after<=:now"
            " AND attempts<max_attempts AND kind IN :kinds ORDER BY priority,id LIMIT 1)"
            " AND status='queued' RETURNING *"
        ).bindparams(bindparam("kinds", expanding=True))
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    statement, {"now": stamp, "owner": owner, "kinds": sorted(kinds)}
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            job = Job.model_validate(dict(row))
        assert job.id is not None
        return Claim(
            job.id, JobKind(job.kind), job.payload_json, job.attempts, job.timeout_sec, owner
        )

    def complete(self, claim: Claim, now: dt.datetime, duration_ms: int) -> bool:
        """Mark success only while this exact attempt still owns the running row."""
        return self._finish(claim, now, duration_ms, None, False)

    def fail(
        self,
        claim: Claim,
        now: dt.datetime,
        duration_ms: int,
        reason: ErrorCode,
        *,
        cancelled: bool = False,
    ) -> bool:
        """Retry with bounded exponential backoff, or finish at the attempt limit."""
        if reason not in _ERRORS:
            raise ValueError("invalid safe job outcome")
        return self._finish(claim, now, duration_ms, reason, cancelled)

    def _finish(
        self,
        claim: Claim,
        now: dt.datetime,
        duration_ms: int,
        reason: ErrorCode | None,
        cancelled: bool,
    ) -> bool:
        if type(duration_ms) is not int or duration_ms < 0:
            raise ValueError("invalid duration")
        stamp = timestamp(now)
        with Session(self.engine) as session:
            # Read is immutable planning only: the eventual UPDATE repeats the entire owner fence.
            row = session.get(Job, claim.id)
            if row is None or row.status != "running" or row.locked_by != claim.owner:
                return False
            status = "done"
            due = row.run_after
            if cancelled:
                status = "cancelled"
            elif reason is not None:
                status = "failed" if row.attempts >= row.max_attempts else "queued"
                due = timestamp(now + dt.timedelta(seconds=min(300, 5 * 2 ** (row.attempts - 1))))
            result = (
                session.connection()
                .execute(
                    text(
                        "UPDATE jobs SET status=:status, run_after=:due, finished_at=:finished,"
                        " duration_ms=:duration, last_error=:reason, locked_at=NULL, locked_by=NULL"
                        " WHERE id=:id AND status='running' AND locked_by=:owner RETURNING id"
                    ),
                    {
                        "status": status,
                        "due": due,
                        "finished": None if status == "queued" else stamp,
                        "duration": duration_ms,
                        "reason": reason,
                        "id": claim.id,
                        "owner": claim.owner,
                    },
                )
                .first()
            )
            session.commit()
            return result is not None

    def reclaim(self, now: dt.datetime) -> int:
        """Reclaim expired attempts ONLY after caller acquires the actual host execution lock.

        Timestamp expiry is not proof that a process stopped. Worker.run_once owns the OS lock
        before this method; it never calls this while a supervisor/descendant retains ownership.
        """
        timestamp(now)
        with Session(self.engine) as session:
            rows = session.exec(select(Job).where(Job.status == "running")).all()
            claims = [
                (
                    Claim(
                        row.id,
                        JobKind(row.kind),
                        row.payload_json,
                        row.attempts,
                        row.timeout_sec,
                        row.locked_by,
                    ),
                    row.locked_at,
                )
                for row in rows
                if row.id is not None and row.locked_by and row.locked_at
            ]
        reclaimed = 0
        for claim, locked_at in claims:
            if now >= parse_utc_iso(locked_at) + dt.timedelta(seconds=claim.timeout_sec):
                reclaimed += self.fail(claim, now, 0, "worker_lost")
        return reclaimed

    def get(self, job_id: int) -> Job | None:
        """Read isolated queue state for internal callers without exposing raw payload over HTTP."""
        with Session(self.engine) as session:
            return session.exec(select(Job).where(col(Job.id) == job_id)).first()
