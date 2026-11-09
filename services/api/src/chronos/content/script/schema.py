"""The ``ReelScript`` schema and its context validator (T3.4, docs/06 §4).

Two layers:

1. :class:`ReelScript` / :class:`Scene` - shape rules any script must satisfy (lengths, scene
   order, cover/story/outro structure). Its JSON schema is what Ollama is constrained to.
2. :func:`validate_script` - rules that need the day's candidate list: every candidate exactly
   once and in order, exact ``item_id``/``source_url`` match (no hallucinated stories or URLs),
   the word budget and the deny list. ``total_words`` is always recomputed here; the LLM's own
   number is never trusted.

Nothing returned by an LLM may be used before passing :func:`validate_script` (AGENTS.md).
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from chronos.content.script.denylist import find_denied

MIN_SCENES = 3  # cover + 1 story + outro (owner decision 7)
MAX_SCENES = 9
WORDS_MIN_FULL = 130  # 6-story reel, ~50 s at Kokoro's ~165 wpm
WORDS_MAX = 185  # ~68 s
WORDS_MIN_FLOOR = 40
FULL_REEL_STORIES = 6
STORY_WORDS_MIN = 15  # prompt asks 25-45; validator leaves retry-friendly slack
STORY_WORDS_MAX = 60

Bullet = Annotated[str, Field(min_length=1, max_length=48)]


class Scene(BaseModel):
    """One screen of the reel."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    type: Literal["cover", "story", "outro"]
    title: str = Field(min_length=1, max_length=60)
    kicker: str | None = Field(default=None, max_length=24)
    bullets: list[Bullet] = Field(default_factory=list[str], max_length=3)
    voiceover: str = Field(min_length=10, max_length=320)
    item_id: int | None = None
    source_name: str | None = Field(default=None, max_length=60)
    source_url: str | None = Field(default=None, max_length=2048)
    visual: Literal["image", "broll", "gradient"] = "image"
    broll_query: str | None = Field(default=None, max_length=40)


class ReelScript(BaseModel):
    """The validated script for one local day."""

    model_config = ConfigDict(extra="forbid")

    local_date: datetime.date
    greeting: str = Field(min_length=1, max_length=80)
    scenes: list[Scene] = Field(min_length=MIN_SCENES, max_length=MAX_SCENES)
    total_words: int = Field(ge=0)
    tone: Literal["calm", "energetic"] = "calm"

    @model_validator(mode="after")
    def _structure(self) -> ReelScript:
        scenes = self.scenes
        if [s.index for s in scenes] != list(range(len(scenes))):
            raise ValueError("scene indices must be 0..n-1 in order")
        if scenes[0].type != "cover":
            raise ValueError("first scene must be the cover")
        if scenes[-1].type != "outro":
            raise ValueError("last scene must be the outro")
        for scene in scenes[1:-1]:
            if scene.type != "story":
                raise ValueError(f"scene {scene.index}: only story scenes between cover and outro")
            if scene.item_id is None or scene.source_url is None:
                raise ValueError(f"scene {scene.index}: story needs item_id and source_url")
        for scene in (scenes[0], scenes[-1]):
            if scene.item_id is not None or scene.source_url is not None:
                raise ValueError(f"scene {scene.index}: {scene.type} must not reference a story")
        return self

    @property
    def stories(self) -> list[Scene]:
        return self.scenes[1:-1]


@dataclass(frozen=True, slots=True)
class ScriptCandidate:
    """One selected story offered to the script writer (from T3.3's ``SelectedStory``)."""

    item_id: int
    category: str
    source_key: str
    source_name: str
    url: str
    title: str
    summary: str | None


class ScriptValidationError(ValueError):
    """The script is unusable; ``problems`` are fed back to the LLM on the next attempt."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def count_words(text: str) -> int:
    """Whitespace tokens: the single word-count rule for budget, prompt and TTS."""
    return len(text.split())


def word_budget(n_stories: int) -> tuple[int, int]:
    """(min, max) total voiceover words for a reel with *n_stories* stories."""
    if n_stories >= FULL_REEL_STORIES:
        return WORDS_MIN_FULL, WORDS_MAX
    scaled = math.ceil(WORDS_MIN_FULL * n_stories / FULL_REEL_STORIES)
    return max(WORDS_MIN_FLOOR, scaled), WORDS_MAX


def _pydantic_problems(exc: ValidationError) -> list[str]:
    problems: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "script"
        problems.append(f"{loc}: {err['msg']}")
    return problems


def validate_script(
    raw: str | dict[str, Any] | ReelScript,
    candidates: Sequence[ScriptCandidate],
    local_date: datetime.date,
) -> ReelScript:
    """Return the validated script (with recomputed ``total_words``) or raise.

    Raises:
        ScriptValidationError: with every problem found, phrased for LLM feedback.
    """
    try:
        if isinstance(raw, ReelScript):
            script = ReelScript.model_validate(raw.model_dump())
        elif isinstance(raw, str):
            script = ReelScript.model_validate_json(raw)
        else:
            script = ReelScript.model_validate(raw)
    except ValidationError as exc:
        raise ScriptValidationError(_pydantic_problems(exc)) from exc

    problems: list[str] = []
    if script.local_date != local_date:
        problems.append(f"local_date must be {local_date.isoformat()}")

    stories = script.stories
    if len(stories) != len(candidates):
        problems.append(
            f"expected exactly {len(candidates)} story scenes (one per story, in the given "
            f"order), got {len(stories)}"
        )
    for scene, cand in zip(stories, candidates, strict=False):
        if scene.item_id != cand.item_id:
            problems.append(
                f"scene {scene.index}: item_id must be {cand.item_id} (stories in given order)"
            )
        if scene.source_url != cand.url:
            problems.append(f"scene {scene.index}: source_url must be exactly {cand.url}")

    total = sum(count_words(s.voiceover) for s in script.scenes)
    low, high = word_budget(len(candidates))
    if not low <= total <= high:
        problems.append(f"total voiceover words must be {low}-{high}, got {total}")
    for scene in stories:
        n = count_words(scene.voiceover)
        if not STORY_WORDS_MIN <= n <= STORY_WORDS_MAX:
            problems.append(f"scene {scene.index}: story voiceover must be 25-45 words, got {n}")

    texts = [script.greeting]
    for scene in script.scenes:
        texts += [scene.title, scene.voiceover, scene.kicker or "", *scene.bullets]
    denied = sorted({w for t in texts for w in find_denied(t)})
    if denied:
        problems.append(f"remove disallowed words: {', '.join(denied)}")

    if problems:
        raise ScriptValidationError(problems)
    return script.model_copy(update={"total_words": total})
