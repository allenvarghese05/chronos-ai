"""GET / PUT /v1/config/wake — wake-engine rule management (T1.4).

Endpoints
---------
``GET  /v1/config/wake`` — Return the currently active rule version plus
    metadata (version, source, sha256, yaml_stale, …).
``PUT  /v1/config/wake`` — Validate and activate a new WakeRules document
    (JSON body); returns 200 if SHA matches active, 201 if a new version was
    created.

Both endpoints require the device bearer token (via
:func:`~chronos.auth.require_device`) and the default rate limit.

The PUT handler accepts the raw JSON body as ``Any`` and validates it
manually against :class:`~chronos.wake.rules.WakeRules` so that validation
errors surface as a ``422 rules_invalid`` problem+json with per-field paths
in the ``errors`` list (RFC 9457 extension).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlmodel import Session

from chronos.auth import require_device
from chronos.db import get_session_dep
from chronos.errors import PROBLEM_CONTENT_TYPE, ProblemException
from chronos.models.devices import Device
from chronos.ratelimit import rate_limit
from chronos.wake.rules import (
    WakeRules,
    activate_new_version,
    get_rules_cache,
)
from chronos.wake.rules import (
    yaml_stale as _yaml_stale,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/config", tags=["config"])

_default_rate_limit = rate_limit("default", per_minute=120)

# Path to the canonical YAML file.
# parents[3] of src/chronos/api/config_wake.py is services/api/
_YAML_PATH: Path = Path(__file__).parents[3] / "config" / "wake_rules.yaml"


# ── response model ────────────────────────────────────────────────────────────


class ConfigWakeResponse(BaseModel):
    """Response body for GET /v1/config/wake."""

    model_config = ConfigDict(extra="forbid")

    version: int
    source: str
    rules: WakeRules
    sha256: str
    yaml_sha256: str
    yaml_stale: bool
    created_at: str
    created_by: str


# ── GET ───────────────────────────────────────────────────────────────────────


@router.get("/wake", response_model=ConfigWakeResponse)
def get_config_wake(
    request: Request,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
    _rl: None = Depends(_default_rate_limit),
) -> ConfigWakeResponse:
    """Return the currently active wake-engine rule version.

    Raises:
        :class:`~chronos.errors.ProblemException` 500 if no active version
            exists (startup guard should prevent this in prod).
    """
    from chronos.wake.rules import rules_sha256

    try:
        rules, rv = get_rules_cache().get_effective(session)
    except RuntimeError as exc:
        raise ProblemException(
            status_code=500,
            code="no_active_rules",
            title="No Active Rules",
            detail=str(exc),
        ) from exc

    # Compute the SHA of the YAML file on disk for the yaml_sha256 / yaml_stale fields.
    stale = _yaml_stale(session, _YAML_PATH)
    # For yaml_sha256, load and hash the current YAML file content.
    try:
        from chronos.wake.rules import load_yaml

        yaml_rules = load_yaml(_YAML_PATH)
        y_sha = rules_sha256(yaml_rules)
    except Exception:
        y_sha = ""

    return ConfigWakeResponse(
        version=rv.version,
        source=rv.source,
        rules=rules,
        sha256=rv.sha256,
        yaml_sha256=y_sha,
        yaml_stale=stale,
        created_at=rv.created_at,
        created_by=rv.created_by,
    )


# ── PUT ───────────────────────────────────────────────────────────────────────

# Maximum accepted request body size for PUT /v1/config/wake.
_MAX_BODY_BYTES: int = 65_536  # 64 KiB

# openapi_extra declares the WakeRules request-body schema so that the generated
# OpenAPI spec is accurate, while the handler still receives body as Any and
# performs manual validation to emit rules_invalid problem+json on failure.
_PUT_OPENAPI_EXTRA: dict[str, object] = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/WakeRules"},
            },
        },
    },
}


@router.put("/wake", status_code=201, openapi_extra=_PUT_OPENAPI_EXTRA)
def put_config_wake(
    request: Request,
    body: Annotated[Any, Body()],
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
    _rl: None = Depends(_default_rate_limit),
) -> JSONResponse:
    """Validate and activate a new WakeRules document.

    The body is accepted as ``Any`` so that validation errors are caught here
    and re-emitted as ``422 rules_invalid`` problem+json with per-field paths.
    Requests exceeding ``_MAX_BODY_BYTES`` are rejected with **413**.

    Returns:
        ``200`` — SHA matches active version (no-op).
        ``201`` — New version created.

    Raises:
        :class:`~chronos.errors.ProblemException` 422 ``rules_invalid`` with
            ``errors: [{loc, msg, type}]`` on validation failure.
    """
    # Content-Length guard: reject oversized payloads before expensive validation.
    content_length_raw = request.headers.get("content-length")
    if content_length_raw is not None:
        try:
            if int(content_length_raw) > _MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "type": "urn:chronos:error:payload_too_large",
                        "title": "Payload Too Large",
                        "status": 413,
                        "detail": f"Request body must not exceed {_MAX_BODY_BYTES} bytes.",
                        "instance": str(request.url.path),
                    },
                    headers={"Content-Type": PROBLEM_CONTENT_TYPE},
                )
        except ValueError:
            pass  # non-integer Content-Length header; let body parsing handle it

    # Manual validation so we can re-emit field-level errors in problem+json.
    try:
        rules = WakeRules.model_validate(body)
    except ValidationError as exc:
        errors = [
            {
                "loc": list(e["loc"]),
                "msg": e["msg"],
                "type": e["type"],
            }
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "type": "urn:chronos:error:rules_invalid",
                "title": "Wake Rules Validation Failed",
                "status": 422,
                "detail": f"{exc.error_count()} validation error(s) in wake rules",
                "instance": str(request.url.path),
                "errors": errors,
            },
            headers={"Content-Type": PROBLEM_CONTENT_TYPE},
        )

    created_by = f"api:{device.public_id}"
    rv, created = activate_new_version(session, rules, source="api", created_by=created_by)
    session.commit()

    status_code = 201 if created else 200
    log.info(
        "config_wake_put",
        version=rv.version,
        created=created,
        device=device.public_id,
    )
    return JSONResponse(
        status_code=status_code,
        content={"version": rv.version, "created": created},
    )
