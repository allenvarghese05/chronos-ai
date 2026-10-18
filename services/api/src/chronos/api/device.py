"""GET / PATCH / DELETE /v1/device/me — authenticated device management.

All endpoints require a valid, non-revoked bearer token via the
:func:`~chronos.auth.require_device` dependency.

- ``GET  /v1/device/me`` — Return the device record (no token field).
- ``PATCH /v1/device/me`` — Update push token, timezone, versions.
- ``DELETE /v1/device/me`` — Revoke the device (status → revoked).
"""

from __future__ import annotations

import zoneinfo
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from chronos.auth import require_device
from chronos.db import get_session_dep
from chronos.enums import DeviceStatus
from chronos.models.devices import Device
from chronos.ratelimit import rate_limit
from chronos.time import utc_now_iso

router = APIRouter(tags=["device"])

# Default rate limit: 120 req/min per IP (docs/05-api-contracts.md §0).
_default_rate_limit = rate_limit("default", per_minute=120)


# ── response model ────────────────────────────────────────────────────────────


class DeviceResponse(BaseModel):
    """Device record returned by GET and PATCH (no token field)."""

    model_config = ConfigDict(extra="forbid")

    device_id: str
    name: str
    model: str | None
    ios_version: str | None
    app_version: str | None
    tz: str
    expo_push_token: str | None
    status: str
    created_at: str
    last_seen_at: str
    revoked_at: str | None


def _to_response(device: Device) -> DeviceResponse:
    return DeviceResponse(
        device_id=device.public_id,
        name=device.name,
        model=device.model,
        ios_version=device.ios_version,
        app_version=device.app_version,
        tz=device.tz,
        expo_push_token=device.expo_push_token,
        status=device.status,
        created_at=device.created_at,
        last_seen_at=device.last_seen_at,
        revoked_at=device.revoked_at,
    )


# ── PATCH request model ───────────────────────────────────────────────────────


class DevicePatch(BaseModel):
    """Partial update body for ``PATCH /v1/device/me``.

    All fields are optional; only supplied fields are written.
    ``extra="forbid"`` causes a 422 if unknown fields are sent.
    """

    model_config = ConfigDict(extra="forbid")

    expo_push_token: Annotated[str | None, Field(default=None, max_length=256)]
    tz: Annotated[str | None, Field(default=None, min_length=1, max_length=64)]
    app_version: Annotated[str | None, Field(default=None, max_length=20)]
    ios_version: Annotated[str | None, Field(default=None, max_length=20)]

    @field_validator("tz")
    @classmethod
    def validate_tz(cls, v: str | None) -> str | None:
        """Reject timezone strings not in the IANA tz database."""
        if v is not None and v not in zoneinfo.available_timezones():
            raise ValueError(f"Unknown timezone: {v!r}")
        return v


# ── endpoints ─────────────────────────────────────────────────────────────────


@router.get(
    "/device/me",
    response_model=DeviceResponse,
    summary="Return the authenticated device record.",
)
def get_device(
    device: Device = Depends(require_device),
    _rl: None = Depends(_default_rate_limit),
) -> DeviceResponse:
    """Return the device record for the authenticated device.

    The bearer token is **not** included in the response.
    """
    return _to_response(device)


@router.patch(
    "/device/me",
    response_model=DeviceResponse,
    summary="Update push token, timezone, or version strings.",
)
def patch_device(
    body: DevicePatch,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
    _rl: None = Depends(_default_rate_limit),
) -> DeviceResponse:
    """Partially update the authenticated device.

    Only fields present in the request body are updated.  Sends an empty body
    ``{}`` to make a no-op update (useful to refresh ``last_seen_at``).
    """
    if body.expo_push_token is not None:
        device.expo_push_token = body.expo_push_token
    if body.tz is not None:
        device.tz = body.tz
    if body.app_version is not None:
        device.app_version = body.app_version
    if body.ios_version is not None:
        device.ios_version = body.ios_version

    session.add(device)
    session.commit()
    session.refresh(device)

    return _to_response(device)


@router.delete(
    "/device/me",
    status_code=204,
    summary="Revoke the authenticated device token.",
)
def delete_device(
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
    _rl: None = Depends(_default_rate_limit),
) -> Response:
    """Revoke the device token.

    Sets ``status = revoked`` and ``revoked_at = now``.  Any subsequent
    request with the same token returns 403 ``revoked``.
    """
    device.status = DeviceStatus.revoked
    device.revoked_at = utc_now_iso()
    session.add(device)
    session.commit()
    return Response(status_code=204)
