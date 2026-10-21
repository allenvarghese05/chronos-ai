"""GET /v1/report/{local_date} - serve the nightly Markdown report (T1.8).

Returns the Markdown report for a local date as ``text/markdown``.
The file is served from ``settings.reports_dir`` if it exists; otherwise
the report is built in memory from the DB and returned (no disk write).
"""

from __future__ import annotations

import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlmodel import Session, select

from chronos.auth import require_device
from chronos.config import get_settings
from chronos.db import get_session_dep
from chronos.errors import ProblemException
from chronos.models.devices import Device
from chronos.models.wake import Day
from chronos.report.builder import build_day_report

router = APIRouter(prefix="/report", tags=["report"])


@router.get("/{local_date_str}")
def get_report(
    local_date_str: str,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> Response:
    """Return the Markdown report for a local date as ``text/markdown``.

    Serves the pre-written file from ``settings.reports_dir`` if it exists;
    otherwise builds the report on-the-fly from the DB (no caching).

    Args:
        local_date_str: Local date in ``YYYY-MM-DD`` format.

    Returns:
        200 with Markdown content.
        401 if no valid bearer token provided.
        404 ``not_found`` if no day row exists for this device + date.
        422 ``validation`` if date format is invalid.
    """
    # Validate date format.
    try:
        local_date = datetime.date.fromisoformat(local_date_str)
    except ValueError as exc:
        raise ProblemException(
            status_code=422,
            code="validation",
            title="Unprocessable Entity",
            detail=f"Invalid local_date {local_date_str!r}: must be YYYY-MM-DD.",
        ) from exc

    # 404 if no day row.
    assert device.id is not None
    day = session.exec(  # type: ignore[call-overload]
        select(Day).where(Day.local_date == local_date_str, Day.device_id == device.id)
    ).first()
    if day is None:
        raise ProblemException(
            status_code=404,
            code="not_found",
            title="Not Found",
            detail=f"No day row for local_date={local_date_str!r}.",
        )

    # Serve pre-written file if it exists.
    settings = get_settings()
    report_file = Path(settings.reports_dir) / f"{local_date_str}.md"
    if report_file.exists():
        content = report_file.read_text(encoding="utf-8")
    else:
        # Build on-the-fly.
        now = datetime.datetime.now(tz=datetime.UTC)
        content = build_day_report(session, device.id, local_date, now)

    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
    )
