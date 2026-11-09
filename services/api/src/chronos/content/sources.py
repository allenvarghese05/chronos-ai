"""Source configuration loader and DB synchroniser (T3.2).

Public API
----------
- :class:`HttpConfig`
- :class:`SelectionConfig`
- :class:`SourceDef`
- :class:`SourcesConfig`
- :func:`load_sources_yaml`
- :func:`sync_sources`
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import Session, select

from chronos.enums import ContentCategory, SourceKind
from chronos.models.content import Source


class HttpConfig(BaseModel):
    """HTTP transport settings from ``sources.yaml``."""

    model_config = ConfigDict(extra="forbid")

    user_agent: str
    timeout_sec: float = Field(default=15.0, gt=0.0)
    max_bytes: int = Field(default=2_000_000, gt=0)
    max_redirects: int = Field(default=3, ge=0)


class SelectionConfig(BaseModel):
    """Story-selection parameters from ``sources.yaml``."""

    model_config = ConfigDict(extra="forbid")

    total_stories: int = Field(default=6, ge=1)
    category_min: dict[str, int]
    freshness_hours: int = Field(default=20, ge=1)
    cross_source_boost: float = Field(default=0.25, ge=0.0)


class SourceDef(BaseModel):
    """One source definition from ``sources.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    kind: SourceKind
    url: str
    category: ContentCategory
    quota: int = Field(default=1, ge=0)
    weight: float = Field(default=1.0, ge=0.0)
    min_points: int | None = Field(default=None, ge=0)


class SourcesConfig(BaseModel):
    """Root schema for ``sources.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: int
    http: HttpConfig
    sources: list[SourceDef]
    selection: SelectionConfig

    @field_validator("sources", mode="after")
    @classmethod
    def _no_duplicate_keys(cls, v: list[SourceDef]) -> list[SourceDef]:
        seen: set[str] = set()
        for s in v:
            if s.key in seen:
                raise ValueError(f"duplicate source key: {s.key}")
            seen.add(s.key)
        return v


def load_sources_yaml(path: Path) -> SourcesConfig:
    """Load and validate ``sources.yaml`` at *path*.

    Raises:
        FileNotFoundError: if *path* does not exist.
        yaml.YAMLError: on syntactically invalid YAML.
        pydantic.ValidationError: on schema violations.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SourcesConfig.model_validate(raw)


def sync_sources(session: Session, config: SourcesConfig, now: str) -> None:
    """Mirror *config* into the ``sources`` table by ``key``.

    Behaviour
    ---------
    - Existing rows are updated in-place (definition fields refreshed).
    - Removed keys are disabled (``enabled = 0``), never deleted.
    - Runtime/health fields (``etag``, ``last_modified``, ``consecutive_failures``,
      …) are preserved across re-syncs.
    - The caller is responsible for committing the session.
    """
    existing = {s.key: s for s in session.exec(select(Source)).all()}
    config_keys = {d.key for d in config.sources}

    for d in config.sources:
        row = existing.get(d.key)
        if row is None:
            row = Source(
                key=d.key,
                kind=d.kind.value,
                url=d.url,
                category=d.category.value,
                quota=d.quota,
                weight=d.weight,
                min_points=d.min_points,
                enabled=1,
            )
            session.add(row)
        else:
            row.kind = d.kind.value
            row.url = d.url
            row.category = d.category.value
            row.quota = d.quota
            row.weight = d.weight
            row.min_points = d.min_points
            row.enabled = 1

    for key, row in existing.items():
        if key not in config_keys and row.enabled == 1:
            row.enabled = 0
