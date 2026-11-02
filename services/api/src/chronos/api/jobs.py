"""GET /v1/jobs — authenticated read-only job metadata listing (T3.1).

Returns bounded safe metadata pages; never exposes key, payload, lock tokens,
raw error output, process paths or tracebacks. Handler output is never logged.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, col, select

from chronos.auth import require_device
from chronos.db import get_session_dep
from chronos.enums import JobKind, JobStatus
from chronos.errors import ProblemException
from chronos.models.devices import Device
from chronos.models.jobs import Job

router = APIRouter(prefix="/jobs", tags=["jobs"])

_MAX_LIMIT = 40
_MAX_PAGE_BYTES = 30 * 1024

_ALLOWED_ERRORS = frozenset(
    {"handler_failed", "timeout", "interrupted", "worker_lost", "spawn_failed"}
)


class JobSummary(BaseModel):
    """Safe projection of a job row; no key, payload, lock token, path or raw output."""

    model_config = ConfigDict(extra="forbid")

    id: int
    kind: JobKind
    status: JobStatus
    priority: int
    attempts: int
    max_attempts: int
    timeout_sec: int
    run_after: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    #: Fixed allowlisted outcome code; never a raw exception message.
    last_error: str | None = None


class JobPage(BaseModel):
    """Bounded response page; next_cursor=None means no more results."""

    model_config = ConfigDict(extra="forbid")

    jobs: list[JobSummary]
    next_cursor: int | None = None


def _project(row: Job) -> JobSummary:
    """Project a DB row to a safe metadata summary, stripping sensitive fields."""
    assert row.id is not None
    error: str | None = row.last_error if row.last_error in _ALLOWED_ERRORS else None
    if row.last_error is not None and row.last_error not in _ALLOWED_ERRORS:
        error = "handler_failed"
    return JobSummary(
        id=row.id,
        kind=JobKind(row.kind),
        status=JobStatus(row.status),
        priority=row.priority,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        timeout_sec=row.timeout_sec,
        run_after=row.run_after,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        last_error=error,
    )


@router.get("", response_model=JobPage)
def list_jobs(
    status: JobStatus | None = Query(default=None, description="Filter by job status."),
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _MAX_LIMIT,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> JobPage:
    """List job metadata in reverse ID order (newest first).

    - ``status``: optional filter; omit to return all statuses.
    - ``limit``: 1-40 rows (default 40).
    - ``cursor``: return rows with id < cursor (keyset pagination, newest-first).

    Returns:
        200 ``JobPage`` with ``jobs`` and optional ``next_cursor``.
        401 if no valid bearer token.
        422 ``validation`` if query parameters are invalid.
    """
    query = select(Job).order_by(col(Job.id).desc())
    if status is not None:
        query = query.where(col(Job.status) == status.value)
    if cursor is not None:
        query = query.where(col(Job.id) < cursor)
    query = query.limit(limit + 1)
    rows = session.exec(query).all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    summaries = [_project(row) for row in page_rows]

    next_cursor: int | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        assert last.id is not None
        next_cursor = last.id

    page = JobPage(jobs=summaries, next_cursor=next_cursor)
    encoded = page.model_dump_json()
    if len(encoded.encode()) > _MAX_PAGE_BYTES:
        raise ProblemException(
            status_code=422,
            code="validation",
            title="Response too large",
            detail="Reduce limit or use cursor pagination.",
        )
    return page
