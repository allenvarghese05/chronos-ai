"""Allowlisted reel preference storage; no secrets or future pipeline side effects."""

from __future__ import annotations

import json
from typing import Literal, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError, model_validator
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from chronos.auth import require_device
from chronos.config import Settings
from chronos.db import get_session_dep
from chronos.errors import ProblemException
from chronos.models.devices import Device
from chronos.models.settings import Setting
from chronos.ratelimit import rate_limit
from chronos.time import utc_now_iso

Theme = Literal["auto", "dawn", "mono", "ink"]
Mode = Literal["eager", "on_trigger"]
Origin = Literal["default", "env", "db"]
router = APIRouter(prefix="/config", tags=["config"])
_limit = rate_limit("default", per_minute=120)
_KEYS = ("theme", "render_mode", "pexels_enabled")


class ReelPreferences(BaseModel):
    """Safe effective preferences; availability is distinct from requested Pexels use."""

    model_config = ConfigDict(extra="forbid")
    theme: Theme
    render_mode: Mode
    pexels_enabled: StrictBool
    pexels_effective: bool
    pexels_key_configured: bool
    origins: dict[Literal["theme", "render_mode", "pexels_enabled"], Origin]
    rendering_available: Literal[False] = False


class PreferencesPatch(BaseModel):
    """Only these explicit keys can be changed; null is never a reset operation."""

    model_config = ConfigDict(extra="forbid")
    theme: Theme | None = None
    render_mode: Mode | None = None
    pexels_enabled: StrictBool | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_null(cls, value: object) -> object:
        if isinstance(value, dict) and any(
            item is None for item in cast(dict[str, object], value).values()
        ):
            raise ValueError("Preference values cannot be null")
        return cast(object, value)


def preferences(session: Session, settings: Settings) -> ReelPreferences:
    """Resolve known DB keys over environment/default values, rejecting corrupt storage."""
    values: dict[str, object] = {key: getattr(settings, key) for key in _KEYS}
    origins: dict[str, str] = {
        key: "env" if key in settings.model_fields_set else "default" for key in _KEYS
    }
    rows = session.exec(select(Setting).where(Setting.key.in_(_KEYS))).all()  # type: ignore[attr-defined]
    try:
        for row in rows:
            values[row.key] = json.loads(row.value)
            origins[row.key] = "db"
        key_available = bool(
            settings.pexels_api_key and settings.pexels_api_key.get_secret_value().strip()
        )
        return ReelPreferences.model_validate(
            {
                **values,
                "origins": origins,
                "pexels_key_configured": key_available,
                "pexels_effective": values["pexels_enabled"] is True and key_available,
            }
        )
    except (ValueError, ValidationError) as exc:
        raise ProblemException(
            500, "settings_invalid", "Settings Unavailable", "Stored reel preferences are invalid."
        ) from exc


@router.get("", response_model=ReelPreferences)
def get_config(
    request: Request,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
    _rl: None = Depends(_limit),
) -> ReelPreferences:
    """Return only the settings projection, never the runtime configuration object."""
    return preferences(session, request.app.state.settings)


@router.patch(
    "",
    response_model=ReelPreferences,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/PreferencesPatch"}}
            },
        }
    },
)
async def patch_config(
    request: Request,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
    _rl: None = Depends(_limit),
) -> ReelPreferences:
    """Validate a small patch before atomically writing only its allowlisted keys."""
    try:
        declared = int(request.headers.get("content-length", "0") or "0")
    except ValueError:
        declared = 0
    if declared > 65536:
        raise ProblemException(
            413, "payload_too_large", "Payload Too Large", "Settings body is too large."
        )
    raw = await request.body()
    if len(raw) > 65536:
        raise ProblemException(
            413, "payload_too_large", "Payload Too Large", "Settings body is too large."
        )
    try:
        patch = PreferencesPatch.model_validate_json(raw)
    except ValidationError as exc:
        raise ProblemException(
            422, "settings_invalid", "Invalid Settings", "Use only valid reel preference fields."
        ) from exc
    for key, value in patch.model_dump(exclude_unset=True).items():
        statement = insert(Setting).values(
            key=key,
            value=json.dumps(value),
            updated_at=utc_now_iso(),
            updated_by=f"api:{device.public_id}",
        )
        statement = statement.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "value": statement.excluded.value,
                "updated_at": statement.excluded.updated_at,
                "updated_by": statement.excluded.updated_by,
            },
        )
        session.exec(statement)
    result = preferences(session, request.app.state.settings)
    session.commit()
    return result
