"""SQLModel table class: Setting.

A simple key/value store for runtime toggles (docs/04-data-model.md §2.6).
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class Setting(SQLModel, table=True):
    """Runtime key/value settings (e.g. ``render_mode``, ``pexels_enabled``)."""

    __tablename__ = "settings"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("key", name="uq_settings_key"),)

    id: int | None = Field(default=None, primary_key=True)
    key: str
    value: str
    updated_at: str
    updated_by: str
