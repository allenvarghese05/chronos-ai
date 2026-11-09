"""Prompt v2 for the reel script (ADR D37: one call per story + one for the cover/outro).

The model only writes words. Story ids, URLs, source names, kickers and scene order are
attached by code (``service.assemble``), so they cannot be dropped, misplaced or invented.
Stories go into the prompt as JSON (data, not instructions) with HTML already stripped and
summaries capped. ``prompt_version`` identifies the prompt text + example for provenance.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any, cast

from chronos.content.script.schema import (
    FRAME_WORDS_MAX,
    FRAME_WORDS_MIN,
    FrameDraft,
    ScriptCandidate,
    StoryDraft,
)

PROMPT_DIR = Path(__file__).parent / "prompts"
PROMPT_FILE = PROMPT_DIR / "reel_script.v2.md"
EXAMPLE_FILE = PROMPT_DIR / "example_story.json"
SUMMARY_PROMPT_CHARS = 600

Message = dict[str, str]


@cache
def _sections() -> dict[str, str]:
    text = PROMPT_FILE.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    for chunk in text.split("## ")[1:]:
        name, _, body = chunk.partition("\n")
        sections[name.strip().lower()] = body.strip()
    if set(sections) != {"system", "story", "frame"}:
        raise ValueError(f"{PROMPT_FILE.name} needs System, Story and Frame sections")
    return sections


@cache
def example() -> dict[str, Any]:
    """The per-story few-shot example: ``{"story": {...}, "answer": {...}}``."""
    return cast("dict[str, Any]", json.loads(EXAMPLE_FILE.read_text(encoding="utf-8")))


@cache
def prompt_version() -> str:
    """``v2:<sha256[:16]>`` of the prompt file and the example."""
    digest = hashlib.sha256(PROMPT_FILE.read_bytes() + EXAMPLE_FILE.read_bytes()).hexdigest()
    return f"v2:{digest[:16]}"


def _strict(schema: dict[str, Any]) -> dict[str, Any]:
    """Flat object schema with every property required (small models skip optional keys)."""
    props = cast("dict[str, Any]", schema.get("properties", {}))
    return {"type": "object", "properties": props, "required": sorted(props)}


@cache
def _story_schema_json() -> str:
    return json.dumps(_strict(StoryDraft.model_json_schema()), sort_keys=True)


@cache
def _frame_schema_json() -> str:
    return json.dumps(_strict(FrameDraft.model_json_schema()), sort_keys=True)


def story_json_schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(_story_schema_json()))


def frame_json_schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(_frame_schema_json()))


def story_payload(cand: ScriptCandidate) -> dict[str, str]:
    summary = cand.summary or ""
    if len(summary) > SUMMARY_PROMPT_CHARS:
        summary = summary[:SUMMARY_PROMPT_CHARS].rsplit(" ", 1)[0] + "…"
    return {
        "category": cand.category,
        "source": cand.source_name,
        "title": cand.title,
        "summary": summary,
    }


def _with_feedback(
    messages: list[Message], feedback: Sequence[str], previous: str | None
) -> list[Message]:
    if feedback:
        if previous:
            messages.append({"role": "assistant", "content": previous[:4000]})
        problems = "\n".join(f"- {p}" for p in feedback)
        messages.append(
            {"role": "user", "content": f"That answer was rejected. Fix every problem and "
             f"return the full corrected JSON:\n{problems}"}
        )  # fmt: skip
    return messages


def build_story_messages(
    cand: ScriptCandidate,
    word_range: tuple[int, int],
    feedback: Sequence[str] = (),
    previous: str | None = None,
) -> list[Message]:
    s = _sections()
    ex = example()
    user = (
        s["story"]
        .replace("{story_json}", json.dumps(story_payload(cand), ensure_ascii=False))
        .replace("{min_words}", str(word_range[0]))
        .replace("{max_words}", str(word_range[1]))
        .replace("{example_json}", json.dumps(ex, ensure_ascii=False))
    )
    return _with_feedback(
        [{"role": "system", "content": s["system"]}, {"role": "user", "content": user}],
        feedback,
        previous,
    )


def build_frame_messages(
    local_date: datetime.date, feedback: Sequence[str] = (), previous: str | None = None
) -> list[Message]:
    s = _sections()
    user = (
        s["frame"]
        .replace("{weekday}", local_date.strftime("%A"))
        .replace("{month_day}", f"{local_date.strftime('%B')} {local_date.day}")
        .replace("{frame_min}", str(FRAME_WORDS_MIN))
        .replace("{frame_max}", str(FRAME_WORDS_MAX))
    )
    return _with_feedback(
        [{"role": "system", "content": s["system"]}, {"role": "user", "content": user}],
        feedback,
        previous,
    )
