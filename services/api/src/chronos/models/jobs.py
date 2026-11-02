"""Durable queue state; execution ownership is a fresh token, never an authoritative PID."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from chronos.enums import JobKind, JobStatus, ck_values


class Job(SQLModel, table=True):
    """One immutable kind/key request and its fenced attempt state."""

    __tablename__ = "jobs"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("kind", "key", name="uq_jobs_kind_key"),
        CheckConstraint(f"kind IN {ck_values(JobKind)}", name="ck_jobs_kind"),
        CheckConstraint(f"status IN {ck_values(JobStatus)}", name="ck_jobs_status"),
        CheckConstraint("length(key) BETWEEN 1 AND 200", name="ck_jobs_key"),
        CheckConstraint("length(CAST(payload_json AS BLOB)) <= 16384", name="ck_jobs_payload"),
        CheckConstraint("priority BETWEEN -100 AND 100", name="ck_jobs_priority"),
        CheckConstraint("attempts BETWEEN 0 AND 10", name="ck_jobs_attempts"),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="ck_jobs_max_attempts"),
        CheckConstraint("timeout_sec BETWEEN 1 AND 3600", name="ck_jobs_timeout"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_jobs_duration"),
        Index("ix_jobs_due", "status", "run_after", "priority", "id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    kind: str
    key: str
    payload_json: str
    status: str = "queued"
    priority: int = 10
    attempts: int = 0
    max_attempts: int = 3
    run_after: str
    locked_at: str | None = None
    locked_by: str | None = None
    timeout_sec: int = 120
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    last_error: str | None = None
    created_at: str
