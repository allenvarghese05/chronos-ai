"""Reels and media endpoints (T5.1, docs/05 §3).

Reel endpoints need the device bearer token and only ever see that device's reels.
``GET /v1/media/...`` is authorised by its signature alone (see :mod:`chronos.media.serving`).
"""

from __future__ import annotations

import datetime
import json
import shutil
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, col, delete, or_, select

from chronos.auth import require_device
from chronos.config import Settings
from chronos.db import get_session_dep
from chronos.errors import ProblemException
from chronos.media.reels import media_root, reel_dir
from chronos.media.serving import (
    SERVABLE,
    RangeNotSatisfiable,
    iter_file,
    parse_range,
    signature_ok,
    signed_url,
    valid_name,
)
from chronos.models.devices import Device
from chronos.models.media import Reel, ReelAsset
from chronos.time import parse_utc_iso

router = APIRouter(tags=["reels"])


def _settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


class SceneSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    type: Literal["cover", "story", "outro"]
    title: str | None = None
    source: str | None = None
    url: str | None = None
    start_sec: float
    end_sec: float


class ReelSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_id: str
    local_date: str
    status: str
    duration_sec: float | None
    saved: bool
    expires_at: str | None
    thumbnail_url: str | None


class ReelDetail(ReelSummary):
    media_url: str | None
    captions_url: str | None
    scenes: list[SceneSummary]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _live(reel: Reel, now: datetime.datetime) -> bool:
    """Saved reels never expire; others live until ``expires_at``."""
    return bool(reel.saved) or reel.expires_at is None or parse_utc_iso(reel.expires_at) > now


def _url(settings: Settings, reel: Reel, file: str, now: datetime.datetime) -> str:
    return signed_url(settings.public_base_url, settings.secret_key.get_secret_value(),
                      reel.public_id, file, int(now.timestamp()))  # fmt: skip


def _summary(settings: Settings, reel: Reel, now: datetime.datetime) -> dict[str, Any]:
    ready = reel.status == "ready"
    return {
        "public_id": reel.public_id,
        "local_date": reel.local_date,
        "status": reel.status,
        "duration_sec": reel.duration_sec,
        "saved": bool(reel.saved),
        "expires_at": None if reel.saved else reel.expires_at,
        "thumbnail_url": _url(settings, reel, "thumb.jpg", now) if ready else None,
    }


def _own_reel(session: Session, device: Device, public_id: str) -> Reel:
    reel = session.exec(
        select(Reel).where(Reel.public_id == public_id, Reel.device_id == device.id)
    ).first()
    if reel is None:
        raise ProblemException(404, "not_found", "Not Found", "No such reel.")
    return reel


def _scenes(settings: Settings, reel: Reel) -> list[SceneSummary]:
    props_path = reel_dir(settings.data_root.resolve(), reel) / "reel.json"
    try:
        props = cast("dict[str, Any]", json.loads(props_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
    out: list[SceneSummary] = []
    for scene in cast("list[dict[str, Any]]", props.get("scenes", [])):
        source = cast("dict[str, Any] | None", scene.get("source"))
        out.append(
            SceneSummary(
                index=int(scene["index"]),
                type=scene["type"],
                title=scene.get("title"),
                source=source.get("name") if source else None,
                url=source.get("url") if source else None,
                start_sec=float(scene["startSec"]),
                end_sec=float(scene["endSec"]),
            )
        )
    return out


@router.get("/reels", response_model=list[ReelSummary])
def list_reels(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> list[dict[str, Any]]:
    """Newest first: reels still live (today's) plus saved ones."""
    settings = _settings_dep(request)
    now = _now()
    rows = session.exec(
        select(Reel)
        .where(Reel.device_id == device.id)
        .where(or_(Reel.saved == 1, col(Reel.expires_at) > now.strftime("%Y-%m-%dT%H:%M:%S")))
        .order_by(col(Reel.local_date).desc(), col(Reel.id).desc())
        .limit(limit)
    ).all()
    return [_summary(settings, r, now) for r in rows if _live(r, now)]


@router.get("/reels/{public_id}", response_model=ReelDetail)
def get_reel(
    public_id: str,
    request: Request,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> dict[str, Any]:
    settings = _settings_dep(request)
    now = _now()
    reel = _own_reel(session, device, public_id)
    if not _live(reel, now):
        raise ProblemException(410, "media_expired", "Gone", "This reel has expired.")
    ready = reel.status == "ready"
    return {
        **_summary(settings, reel, now),
        "media_url": _url(settings, reel, "reel.mp4", now) if ready else None,
        "captions_url": _url(settings, reel, "captions.json", now) if ready else None,
        "scenes": [s.model_dump() for s in _scenes(settings, reel)],
    }


@router.post("/reels/{public_id}/save", response_model=ReelSummary)
def save_reel(
    public_id: str,
    request: Request,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> dict[str, Any]:
    reel = _own_reel(session, device, public_id)
    reel.saved = 1
    session.add(reel)
    session.commit()
    return _summary(_settings_dep(request), reel, _now())


@router.delete("/reels/{public_id}/save", response_model=ReelSummary)
def unsave_reel(
    public_id: str,
    request: Request,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> dict[str, Any]:
    reel = _own_reel(session, device, public_id)
    reel.saved = 0
    session.add(reel)
    session.commit()
    return _summary(_settings_dep(request), reel, _now())


def delete_reel_files(session: Session, settings: Settings, reel: Reel) -> None:
    """Remove the reel's folder, asset rows and the reel row (caller commits)."""
    shutil.rmtree(reel_dir(settings.data_root.resolve(), reel), ignore_errors=True)
    session.exec(delete(ReelAsset).where(col(ReelAsset.reel_id) == reel.id))
    session.delete(reel)


@router.delete("/reels/{public_id}", status_code=204)
def delete_reel(
    public_id: str,
    request: Request,
    device: Device = Depends(require_device),
    session: Session = Depends(get_session_dep),
) -> Response:
    reel = _own_reel(session, device, public_id)
    delete_reel_files(session, _settings_dep(request), reel)
    session.commit()
    return Response(status_code=204)


@router.get("/media/{public_id}/{file}", response_model=None, include_in_schema=True)
def get_media(
    public_id: str,
    file: str,
    request: Request,
    exp: int = Query(...),
    sig: str = Query(..., min_length=64, max_length=64),
    session: Session = Depends(get_session_dep),
) -> Response:
    """Signed, expiring media with single-range support (no bearer token)."""
    settings = _settings_dep(request)
    not_found = ProblemException(404, "not_found", "Not Found", "No such media.")
    if not valid_name(public_id, file):
        raise not_found
    if not signature_ok(settings.secret_key.get_secret_value(), public_id, file, exp, sig):
        raise not_found
    now = _now()
    if exp < int(now.timestamp()):
        raise ProblemException(410, "media_expired", "Gone", "This link has expired.")
    reel = session.exec(select(Reel).where(Reel.public_id == public_id)).first()
    if reel is None or not _live(reel, now):
        raise ProblemException(410, "media_expired", "Gone", "This reel is no longer available.")
    path = reel_dir(settings.data_root.resolve(), reel) / file
    root = media_root(settings.data_root.resolve()).resolve()
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ProblemException(410, "media_expired", "Gone", "This file is no longer available.")
    size = path.stat().st_size
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=0"}
    try:
        byte_range = parse_range(request.headers.get("range"), size)
    except RangeNotSatisfiable:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    if byte_range is None:
        return StreamingResponse(iter_file(path, 0, size), media_type=SERVABLE[file],
                                 headers={**headers, "Content-Length": str(size)})  # fmt: skip
    return StreamingResponse(
        iter_file(path, byte_range.start, byte_range.length),
        status_code=206,
        media_type=SERVABLE[file],
        headers={
            **headers,
            "Content-Length": str(byte_range.length),
            "Content-Range": f"bytes {byte_range.start}-{byte_range.end}/{size}",
        },
    )
