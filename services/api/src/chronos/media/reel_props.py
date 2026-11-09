"""``ReelProps``: the renderer's input contract, owned by the API (D14, T3.4).

``chronos schema export-reel-props`` turns these models into ``packages/renderer/src/
schema.json`` and ``schema.generated.ts`` (zod). ``packages/renderer/src/schema.ts`` adds the
cross-field refinements (``e >= s``, ``endSec >= startSec``) and the cover/story/outro union.

Field names are camelCase because they are the renderer's JSON keys. Optional fields are
omitted (never ``null``) when serialised: use :meth:`ReelProps.to_json`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptionWord(_Model):
    """One spoken word with its timing in seconds."""

    w: str = Field(min_length=1)
    s: float = Field(ge=0)
    e: float = Field(ge=0)


class Media(_Model):
    """A scene's visual layer."""

    type: Literal["image", "video", "gradient"]
    url: str | None = None
    query: str | None = None
    index: int | None = Field(default=None, ge=0)


class SceneSource(_Model):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1, json_schema_extra={"format": "uri"})


class SceneBase(_Model):
    """Fields shared by every scene type (the renderer adds ``type``)."""

    index: int = Field(ge=0)
    title: str | None = None
    kicker: str | None = None
    body: list[str] | None = None
    media: Media | None = None
    source: SceneSource | None = None
    startSec: float = Field(ge=0)
    endSec: float = Field(ge=0)


class Scene(SceneBase):
    type: Literal["cover", "story", "outro"]


class Audio(_Model):
    src: str = Field(min_length=1)
    durationSec: float = Field(gt=0)


class ReelProps(_Model):
    """Everything Remotion needs to render one Morning Reel."""

    localDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    greeting: str = Field(min_length=1)
    tone: Literal["calm", "energetic"]
    audio: Audio
    scenes: list[Scene] = Field(min_length=1)
    captions: list[list[CaptionWord]]
    theme: Literal["dawn", "mono", "ink"]
    attribution: list[str]

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)
