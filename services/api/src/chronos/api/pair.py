"""POST /v1/pair — device pairing endpoint (docs/05-api-contracts.md §1).

Exchange a one-time pairing code (displayed by ``chronos pair-code``) for a
256-bit bearer token.  The token is returned once in the response body and
must be stored securely on the device (Keychain / expo-secure-store).

Error codes returned by this router:
- 400 ``pairing_invalid`` — code malformed, consumed, or burned (≥ 5 attempts).
- 409 ``conflict``        — an active device already exists; revoke it first.
- 410 ``pairing_expired`` — code past its 10-minute expiry.
- 429 ``rate_limited``    — 5 requests/min per IP exceeded (Retry-After header set).
"""

from __future__ import annotations

import zoneinfo
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session
from sqlmodel import select

from chronos.auth import (
    hash_token,
    new_token,
    normalise_pairing_code,
)
from chronos.db import get_session_dep
from chronos.enums import DeviceStatus
from chronos.errors import ProblemException
from chronos.ids import new_ulid
from chronos.models.devices import Device, PairingCode
from chronos.ratelimit import rate_limit
from chronos.time import parse_utc_iso, utc_now_iso

router = APIRouter(tags=["pairing"])

# Rate limit: 5 pairing attempts per minute per IP (docs/05-api-contracts.md §0).
_pairing_rate_limit = rate_limit("pairing", per_minute=5)


# ── request / response models ─────────────────────────────────────────────────


class DeviceInfo(BaseModel):
    """Device metadata sent at pairing time."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=120)]
    model: Annotated[str, Field(min_length=1, max_length=80)]
    ios_version: Annotated[str, Field(min_length=1, max_length=20)]
    app_version: Annotated[str, Field(min_length=1, max_length=20)]
    tz: Annotated[str, Field(min_length=1, max_length=64)]

    @field_validator("tz")
    @classmethod
    def validate_tz(cls, v: str) -> str:
        """Reject timezone strings not in the IANA tz database."""
        if v not in zoneinfo.available_timezones():
            raise ValueError(f"Unknown timezone: {v!r}")
        return v


class PairRequest(BaseModel):
    """Body of ``POST /v1/pair``."""

    model_config = ConfigDict(extra="forbid")

    #: The pairing code exactly as displayed (``XXXX-XXXX``) or without dash.
    code: Annotated[str, Field(min_length=8, max_length=9)]
    device: DeviceInfo


class PairResponse(BaseModel):
    """201 response body of ``POST /v1/pair``."""

    device_id: str
    token: str
    api_base: str
    server_time: str


# ── endpoint ──────────────────────────────────────────────────────────────────


@router.post(
    "/pair",
    status_code=201,
    response_model=PairResponse,
    summary="Exchange a one-time pairing code for a device bearer token.",
)
def create_pair(
    body: PairRequest,
    request: Request,
    session: Session = Depends(get_session_dep),
    _rl: None = Depends(_pairing_rate_limit),
) -> PairResponse:
    """Pair a new device.

    The pairing code is validated against the most recently generated active
    code (non-consumed, tracked by attempt count).  A correct code issues a
    fresh bearer token; an incorrect code increments the attempt counter and
    burns the code after 5 failures.

    Returns:
        201 with :class:`PairResponse`.

    Raises:
        :class:`~chronos.errors.ProblemException` 409 if an active device exists.
        :class:`~chronos.errors.ProblemException` 400 for invalid/burned codes.
        :class:`~chronos.errors.ProblemException` 410 for expired codes.
        :class:`~chronos.errors.ProblemException` 429 when rate-limited.
    """
    # 1. Enforce one-active-device constraint.
    existing = session.exec(select(Device).where(Device.status == DeviceStatus.active)).first()
    if existing is not None:
        raise ProblemException(
            status_code=409,
            code="conflict",
            title="Conflict",
            detail=(
                "An active device already exists. "
                "Run `chronos device revoke` or DELETE /v1/device/me first."
            ),
        )

    # 2. Find the most recently generated active pairing code (non-consumed).
    pairing_code = session.exec(
        select(PairingCode)
        .where(PairingCode.consumed_at.is_(None))  # type: ignore[union-attr]
        .order_by(PairingCode.id.desc())  # type: ignore[union-attr]
    ).first()

    if pairing_code is None:
        raise ProblemException(
            status_code=400,
            code="pairing_invalid",
            title="Bad Request",
            detail="No active pairing code found. Run `chronos pair-code` on the laptop.",
        )

    # 3. Increment attempts *before* comparing (count every submission).
    pairing_code.attempts += 1
    session.add(pairing_code)
    session.flush()  # write attempts without committing yet

    now = utc_now_iso()

    # 4. Check if already burned (>= 5 attempts reached by a *previous* request).
    #    After the increment above, attempts == 5 means this is the 5th wrong attempt;
    #    attempts > 5 means it was already burned.
    #    We check >= 5 here because we burned on the 5th wrong; correct code on 6th → 400.
    submitted_hash = hash_token(normalise_pairing_code(body.code))
    is_correct = submitted_hash == pairing_code.code_hash

    if not is_correct:
        # Burn the code on the 5th wrong attempt.
        if pairing_code.attempts >= 5:
            pairing_code.consumed_at = now  # mark burned
            session.add(pairing_code)
        session.commit()
        raise ProblemException(
            status_code=400,
            code="pairing_invalid",
            title="Bad Request",
            detail="Invalid pairing code.",
        )

    # Code is correct — now check expiry and burned status.
    if pairing_code.attempts > 1:
        # This was a retry with the correct code; check if previously burned.
        # (attempts was incremented above, so attempts > 5 means it was burned before.)
        pass  # handled below

    # Re-check consumed_at: could have been set by an earlier run (burn).
    # Note: we flushed but didn't commit yet; if consumed_at was set in this request
    # (wrong code path), we would have returned above.  But a previously burned code
    # would have consumed_at set before this request, so pairing_code would not have
    # been found by the "consumed_at IS NULL" query.  So if we're here, it's non-consumed.

    # Check expiry.
    try:
        expires_dt = parse_utc_iso(pairing_code.expires_at)
    except ValueError as exc:
        raise ProblemException(
            status_code=400,
            code="pairing_invalid",
            title="Bad Request",
            detail="Pairing code has an invalid expiry timestamp.",
        ) from exc

    now_dt = parse_utc_iso(now)
    if now_dt > expires_dt:
        session.commit()  # save the incremented attempts
        raise ProblemException(
            status_code=410,
            code="pairing_expired",
            title="Gone",
            detail=(
                f"Pairing code expired at {pairing_code.expires_at}. "
                f"Server time is {now}. Run `chronos pair-code` to generate a new code."
            ),
        )

    # 5. All checks pass — consume the code and create the device.
    pairing_code.consumed_at = now
    session.add(pairing_code)

    plaintext_token = new_token()
    token_hash = hash_token(plaintext_token)
    device_public_id = new_ulid()

    device = Device(
        public_id=device_public_id,
        name=body.device.name,
        token_hash=token_hash,
        expo_push_token=None,
        tz=body.device.tz,
        app_version=body.device.app_version,
        ios_version=body.device.ios_version,
        model=body.device.model,
        status=DeviceStatus.active,
        created_at=now,
        last_seen_at=now,
        revoked_at=None,
    )
    session.add(device)
    session.commit()
    session.refresh(device)

    # 6. Retrieve api_base from settings (stored on app.state).
    api_base: str = request.app.state.settings.public_base_url

    return PairResponse(
        device_id=device.public_id,
        token=plaintext_token,  # only time plaintext is sent
        api_base=api_base,
        server_time=now,
    )
