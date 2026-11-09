"""Reel bookkeeping and the assets stage (T4.1).

A reel is created for the day's ready brief at the assets stage. Files live under
``<data_root>/media/<local_date>/<public_id>/``; ``reels.file_path`` and ``reel_assets.path`` are
relative to ``<data_root>/media`` (docs/04 §2.4).
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlmodel import Session, col, select

from chronos.content.script.schema import ReelScript
from chronos.enums import AssetLicence, BriefStatus, ReelAssetKind
from chronos.http import GuardedHttpClient
from chronos.ids import new_ulid
from chronos.media.assets import (
    HTML_MAX_BYTES,
    IMAGE_MAX_BYTES,
    SceneImageRequest,
    SceneImageResult,
    fetch_scene_images,
)
from chronos.models.content import Brief, Item
from chronos.models.media import Reel, ReelAsset
from chronos.time import format_utc_iso, parse_utc_iso

log = structlog.get_logger(__name__)

REEL_TTL = datetime.timedelta(hours=24)


class BriefNotReady(LookupError):
    """There is no ``ready`` brief for the day, so there is nothing to build media from."""


def media_root(data_root: Path) -> Path:
    return data_root / "media"


def reel_dir(data_root: Path, reel: Reel) -> Path:
    return media_root(data_root) / reel.local_date / reel.public_id


def relative(data_root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(media_root(data_root).resolve()))


def ready_brief(session: Session, local_date: str, device_id: int) -> Brief:
    brief = session.exec(
        select(Brief).where(Brief.local_date == local_date, Brief.device_id == device_id)
    ).first()
    if brief is None or brief.status != BriefStatus.ready.value:
        raise BriefNotReady(f"no ready brief for {local_date} / device {device_id}")
    return brief


def get_or_create_reel(session: Session, brief: Brief, now: str) -> Reel:
    """The brief's reel (one per brief); new reels expire 24 h after creation unless saved."""
    assert brief.id is not None
    reel = session.exec(select(Reel).where(Reel.brief_id == brief.id)).first()
    if reel is None:
        reel = Reel(
            public_id=new_ulid(),
            brief_id=brief.id,
            local_date=brief.local_date,
            device_id=brief.device_id,
            expires_at=format_utc_iso(parse_utc_iso(now) + REEL_TTL),
            created_at=now,
            updated_at=now,
        )
        session.add(reel)
        session.commit()
        session.refresh(reel)
    return reel


def existing_asset(
    session: Session, reel_id: int, kind: ReelAssetKind, scene_index: int | None
) -> ReelAsset | None:
    query = select(ReelAsset).where(ReelAsset.reel_id == reel_id, ReelAsset.kind == kind.value)
    query = query.where(
        col(ReelAsset.scene_index).is_(None)
        if scene_index is None
        else ReelAsset.scene_index == scene_index
    )
    return session.exec(query).first()


@dataclass(frozen=True, slots=True)
class AssetsReport:
    reel_public_id: str
    images: int
    gradients: int
    skipped_existing: int


def assets_stage(
    session: Session,
    *,
    local_date: str,
    device_id: int,
    data_root: Path,
    user_agent: str,
    now: str,
    pexels_available: bool = False,
    html_client: GuardedHttpClient | None = None,
    image_client: GuardedHttpClient | None = None,
) -> AssetsReport:
    """Create the reel if needed and fetch one image per story scene (idempotent).

    A scene whose image asset is already recorded and still on disk is not fetched again.
    Scenes asking for b-roll fall back to the image path while Pexels is unavailable
    (``CHRONOS_PEXELS_API_KEY`` unset or disabled); since D37 code assigns every story
    ``visual="image"``, so b-roll is never requested yet.
    """
    brief = ready_brief(session, local_date, device_id)
    reel = get_or_create_reel(session, brief, now)
    assert reel.id is not None
    script = ReelScript.model_validate_json(brief.script_json)
    out_dir = reel_dir(data_root, reel) / "assets"

    requests: list[SceneImageRequest] = []
    skipped = 0
    for scene in script.stories:
        if scene.visual == "broll" and not pexels_available:
            log.info("broll_skipped_no_pexels", scene=scene.index)
        asset = existing_asset(session, reel.id, ReelAssetKind.image, scene.index)
        if asset is not None and (media_root(data_root) / asset.path).exists():
            skipped += 1
            continue
        item = session.get(Item, scene.item_id) if scene.item_id is not None else None
        requests.append(
            SceneImageRequest(
                scene_index=scene.index,
                feed_image_url=item.image_url if item is not None else None,
                article_url=scene.source_url or "",
            )
        )

    html = html_client or GuardedHttpClient(user_agent, max_bytes=HTML_MAX_BYTES, timeout_sec=10)
    images = image_client or GuardedHttpClient(user_agent, max_bytes=IMAGE_MAX_BYTES)

    async def run() -> list[SceneImageResult]:
        try:
            return await fetch_scene_images(html, images, requests, out_dir)
        finally:
            await html.aclose()
            await images.aclose()

    results = asyncio.run(run()) if requests else []
    made = 0
    for result in results:
        image = result.image
        if image is None:
            continue
        scene_index = result.scene_index
        old = existing_asset(session, reel.id, ReelAssetKind.image, scene_index)
        if old is not None:
            session.delete(old)
        session.add(
            ReelAsset(
                reel_id=reel.id,
                kind=ReelAssetKind.image.value,
                path=relative(data_root, image.path),
                source_url=result.source_url,
                licence=AssetLicence.og_image.value,
                width=image.width,
                height=image.height,
                size_bytes=image.size_bytes,
                sha256=image.sha256,
                scene_index=scene_index,
            )
        )
        made += 1
    reel.updated_at = now
    session.add(reel)
    session.commit()
    report = AssetsReport(reel.public_id, made + skipped, len(script.stories) - made - skipped,
                          skipped)  # fmt: skip
    log.info("assets_done", reel=reel.public_id, images=report.images, gradients=report.gradients)
    return report
