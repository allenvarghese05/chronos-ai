"""Health endpoints: /healthz and /readyz.

Both endpoints are mounted at the root (not under /v1) and require no
authentication — they are consumed by launchd, monitoring scripts, and the
/readyz check in ``chronos serve``.

``/healthz`` — liveness: the process is alive and the version is readable.
``/readyz``   — readiness: liveness + DB connectivity.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from chronos import __version__
from chronos.db import check_db

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, Any]:
    """Return ``{"ok": true, "version": "<version>"}`` with HTTP 200.

    This endpoint never checks external dependencies; a 200 means the process
    is running and imports are intact.
    """
    return {"ok": True, "version": __version__}


@router.get("/readyz", summary="Readiness probe")
async def readyz(request: Request) -> JSONResponse:
    """Return readiness of the service and its dependencies.

    HTTP 200 when all checks pass; HTTP 503 when any check fails.
    Response body always contains ``{"ok": bool, "db": "<status>"}``.
    """
    db_status = check_db(request.app.state.db_engine)
    ok = db_status == "ok"
    body: dict[str, Any] = {"ok": ok, "db": db_status}
    return JSONResponse(content=body, status_code=200 if ok else 503)
