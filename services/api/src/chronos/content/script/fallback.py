"""Deterministic template script: the last resort that always validates (docs/06 §5).

Used when every LLM provider failed. A pure function of (candidates, local_date), so it is
golden- and property-testable. For each story: ``"From {source}: {title}. {summary}"``,
trimmed or padded to an exact per-story word count chosen so the total lands inside the
word budget, with every token capped so the 320-character voiceover limit always holds.
Deny-listed words coming from feed text are masked, never passed through.

Precondition: every candidate URL is at most 2048 characters (the job filters longer ones).
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence

from chronos.content.script.denylist import mask_denied
from chronos.content.script.schema import (
    MAX_SCENES,
    STORY_WORDS_MIN,
    ReelScript,
    Scene,
    ScriptCandidate,
    count_words,
    story_word_range,
    validate_script,
    word_budget,
)

GREETING = "Good morning"
_KICKERS = {"ai": "AI DROP", "tech": "TECH", "world": "WORLD", "india": "INDIA"}
_FILLER = "Read the full story from the source link saved in your feed.".split()
_MAX_TOKEN_CHARS = 20  # 15 tokens x (20 + 1 space) <= 320, so the minimum always fits
_VOICEOVER_MAX_CHARS = 320
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _clip(text: str, limit: int) -> str:
    """Trim *text* to *limit* chars at a word boundary, with an ellipsis when cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0] or text[: limit - 1]
    return cut.rstrip(" ,;:-") + "…"


def _tokens(text: str) -> list[str]:
    return [t[:_MAX_TOKEN_CHARS] for t in mask_denied(text).split()]


def _story_voiceover(cand: ScriptCandidate, n_words: int) -> str:
    lead = f"From {cand.source_name}: {cand.title.rstrip('.')}."
    pool = _tokens(lead) + _tokens(cand.summary or "")
    while len(pool) < n_words:
        pool += _FILLER
    words = pool[:n_words]
    while len(" ".join(words)) > _VOICEOVER_MAX_CHARS and len(words) > STORY_WORDS_MIN:
        words.pop()
    return " ".join(words)


def _bullets(summary: str | None) -> list[str]:
    if not summary:
        return []
    sentences = [s for s in _SENTENCE_END.split(mask_denied(summary).strip()) if s.strip()]
    return [_clip(s, 48) for s in sentences[:2]]


KICKERS = _KICKERS
COVER_TITLE = "Morning Reel"
OUTRO_TITLE = "That's your brief"


def kicker_for(category: str) -> str | None:
    """On-screen label for a category (code-owned, ADR D37)."""
    return _KICKERS.get(category, category.upper()[:24] or None)


def cover_voiceover(local_date: datetime.date, greeting: str = GREETING) -> str:
    when = f"{local_date.strftime('%A')}, {local_date.strftime('%B')} {local_date.day}"
    return f"{greeting}. Here is your morning brief for {when}."


OUTRO_VOICEOVER = "That is the brief for this morning. The sources are saved in your feed."


def template_story_words(n_stories: int) -> int:
    """Per-story word count the template uses: the middle of the budget, inside the range."""
    low, high = word_budget(n_stories)
    fixed = count_words(cover_voiceover(datetime.date(2026, 9, 30))) + count_words(OUTRO_VOICEOVER)
    lo, hi = story_word_range(n_stories)
    return max(lo, min(hi, round(((low + high) // 2 - fixed) / n_stories)))


def template_story_scene(cand: ScriptCandidate, index: int, n_words: int) -> Scene:
    """One deterministic story scene (also used to patch a single failed LLM story)."""
    return Scene(
        index=index,
        type="story",
        title=_clip(mask_denied(cand.title), 60) or "Today's story",
        kicker=kicker_for(cand.category),
        bullets=_bullets(cand.summary),
        voiceover=_story_voiceover(cand, n_words),
        item_id=cand.item_id,
        source_name=_clip(cand.source_name, 60) or None,
        source_url=cand.url,
        visual="image",
    )


def template_script(candidates: Sequence[ScriptCandidate], local_date: datetime.date) -> ReelScript:
    """Build and validate the template script for 1..7 candidates.

    Raises:
        ValueError: with no candidates or more than fit in ``MAX_SCENES``.
    """
    n = len(candidates)
    if not 1 <= n <= MAX_SCENES - 2:
        raise ValueError(f"template needs 1..{MAX_SCENES - 2} stories, got {n}")
    per_story = template_story_words(n)
    scenes: list[Scene] = [
        Scene(index=0, type="cover", title=COVER_TITLE, voiceover=cover_voiceover(local_date),
              visual="gradient")
    ]  # fmt: skip
    scenes += [template_story_scene(c, i, per_story) for i, c in enumerate(candidates, start=1)]
    scenes.append(
        Scene(index=n + 1, type="outro", title=OUTRO_TITLE, voiceover=OUTRO_VOICEOVER,
              visual="gradient")
    )  # fmt: skip
    draft = ReelScript(
        local_date=local_date, greeting=GREETING, scenes=scenes, total_words=0, tone="calm"
    )
    return validate_script(draft, candidates, local_date)
