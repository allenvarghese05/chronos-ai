"""Media storage (T4.1, docs/04 §2.4): one reel per brief and the files it is built from."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from chronos.enums import AssetLicence, ReelAssetKind, ReelStatus, ck_values


class Reel(SQLModel, table=True):
    """The day's video; ``public_id`` (ULID) is what URLs and pushes carry."""

    __tablename__ = "reels"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_reels_public_id"),
        UniqueConstraint("brief_id", name="uq_reels_brief_id"),
        ForeignKeyConstraint(["brief_id"], ["briefs.id"], name="fk_reels_brief"),
        ForeignKeyConstraint(
            ["local_date", "device_id"], ["days.local_date", "days.device_id"], name="fk_reels_day"
        ),
        CheckConstraint(f"status IN {ck_values(ReelStatus)}", name="ck_reels_status"),
        CheckConstraint("saved IN (0, 1)", name="ck_reels_saved"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_reels_size"),
        Index("ix_reels_expires_at", "expires_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    public_id: str
    brief_id: int
    local_date: str
    device_id: int
    status: str = ReelStatus.assets.value
    file_path: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    duration_sec: float | None = None
    width: int = 1080
    height: int = 1920
    fps: int = 30
    render_ms: int | None = None
    tts_ms: int | None = None
    captions_ms: int | None = None
    renderer_version: str = ""
    saved: int = 0
    expires_at: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class ReelAsset(SQLModel, table=True):
    """One file used to build a reel (image, b-roll, voice, captions, props, thumbnail)."""

    __tablename__ = "reel_assets"  # type: ignore[assignment]
    __table_args__ = (
        ForeignKeyConstraint(["reel_id"], ["reels.id"], name="fk_reel_assets_reel"),
        CheckConstraint(f"kind IN {ck_values(ReelAssetKind)}", name="ck_reel_assets_kind"),
        CheckConstraint(
            f"licence IS NULL OR licence IN {ck_values(AssetLicence)}",
            name="ck_reel_assets_licence",
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_reel_assets_size"),
        Index("ix_reel_assets_reel", "reel_id", "kind", "scene_index"),
    )

    id: int | None = Field(default=None, primary_key=True)
    reel_id: int
    kind: str
    path: str
    source_url: str | None = None
    licence: str | None = None
    width: int | None = None
    height: int | None = None
    duration_sec: float | None = None
    size_bytes: int | None = None
    sha256: str
    scene_index: int | None = None
