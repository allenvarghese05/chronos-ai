"""Content storage foundation (T3.1); semantic script validation belongs to T3.4."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from chronos.enums import BriefItemRole, BriefStatus, ContentCategory, SourceKind, ck_values


def encode_simhash(value: int) -> int:
    """Encode an unsigned 64-bit hash into SQLite's signed INTEGER without loss."""
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("simhash must be unsigned64")
    return value if value < 2**63 else value - 2**64


def decode_simhash(value: int) -> int:
    """Decode the two's-complement signed storage convention into unsigned64."""
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise ValueError("stored simhash must be signed64")
    return value % 2**64


class Source(SQLModel, table=True):
    """Versioned source definition mirrored by a future loader, plus fetch health."""

    __tablename__ = "sources"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("key", name="uq_sources_key"),
        CheckConstraint(f"kind IN {ck_values(SourceKind)}", name="ck_sources_kind"),
        CheckConstraint(f"category IN {ck_values(ContentCategory)}", name="ck_sources_category"),
        CheckConstraint("enabled IN (0, 1)", name="ck_sources_enabled"),
        CheckConstraint("quota >= 0", name="ck_sources_quota"),
        CheckConstraint("weight >= 0", name="ck_sources_weight"),
        CheckConstraint("consecutive_failures >= 0", name="ck_sources_failures"),
        CheckConstraint("min_points >= 0", name="ck_sources_min_points"),
    )

    id: int | None = Field(default=None, primary_key=True)
    key: str
    kind: str
    url: str
    category: str
    enabled: int = 1
    quota: int = 1
    weight: float = 1.0
    min_points: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    last_fetched_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0


class Item(SQLModel, table=True):
    """A normalized story, not yet fetched/selected by this infrastructure task."""

    __tablename__ = "items"  # type: ignore[assignment]
    __table_args__ = (
        ForeignKeyConstraint(["source_id"], ["sources.id"], name="fk_items_source_id"),
        UniqueConstraint("source_id", "external_id", name="uq_items_source_external"),
        Index("ix_items_url_hash", "url_hash"),
        Index("ix_items_published_at", "published_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    source_id: int
    external_id: str
    url: str
    url_hash: str
    title: str
    summary: str | None = None
    author: str | None = None
    published_at: str | None = None
    fetched_at: str
    image_url: str | None = None
    extra_json: str | None = None
    title_simhash: int = 0


class Brief(SQLModel, table=True):
    """Per-device local-day script draft/provenance; ready validation is future work."""

    __tablename__ = "briefs"  # type: ignore[assignment]
    __table_args__ = (
        ForeignKeyConstraint(
            ["local_date", "device_id"], ["days.local_date", "days.device_id"], name="fk_briefs_day"
        ),
        UniqueConstraint("local_date", "device_id", name="uq_briefs_day"),
        CheckConstraint(f"status IN {ck_values(BriefStatus)}", name="ck_briefs_status"),
        CheckConstraint("attempts >= 0", name="ck_briefs_attempts"),
        CheckConstraint("used_fallback_template IN (0, 1)", name="ck_briefs_fallback"),
        CheckConstraint("tokens_in IS NULL OR tokens_in >= 0", name="ck_briefs_tokens_in"),
        CheckConstraint("tokens_out IS NULL OR tokens_out >= 0", name="ck_briefs_tokens_out"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_briefs_latency"),
    )

    id: int | None = Field(default=None, primary_key=True)
    local_date: str
    device_id: int
    status: str = "drafting"
    script_json: str = "{}"
    llm_provider: str = ""
    llm_model: str = ""
    model_digest: str = ""
    prompt_version: str = ""
    attempts: int = 0
    used_fallback_template: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    created_at: str
    updated_at: str


class BriefItem(SQLModel, table=True):
    """Scene membership retains referenced stories through ordinary FK restrictions."""

    __tablename__ = "brief_items"  # type: ignore[assignment]
    __table_args__ = (
        ForeignKeyConstraint(["brief_id"], ["briefs.id"], name="fk_brief_items_brief"),
        ForeignKeyConstraint(["item_id"], ["items.id"], name="fk_brief_items_item"),
        CheckConstraint(f"role IN {ck_values(BriefItemRole)}", name="ck_brief_items_role"),
        CheckConstraint("scene_index >= 0", name="ck_brief_items_scene"),
    )

    brief_id: int = Field(primary_key=True)
    item_id: int = Field(primary_key=True)
    scene_index: int
    role: str = "primary"
