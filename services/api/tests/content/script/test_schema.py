"""ReelScript shape rules and validate_script context rules (brief edge cases 1-7, 14)."""

from __future__ import annotations

import copy
import datetime
from typing import Any

import pytest

from chronos.content.script.denylist import find_denied
from chronos.content.script.schema import (
    ReelScript,
    ScriptValidationError,
    count_words,
    validate_script,
    word_budget,
)
from tests.content.script.helpers import DAY, candidates, valid_script, words


def _problems(raw: Any, n: int = 6) -> list[str]:
    with pytest.raises(ScriptValidationError) as info:
        validate_script(raw, candidates(n), DAY)
    return info.value.problems


def test_valid_script_passes_and_total_recomputed() -> None:
    raw = valid_script(candidates())
    raw["total_words"] = 9999  # edge case 6: LLM arithmetic is ignored
    script = validate_script(raw, candidates(), DAY)
    assert script.total_words == 6 * 25 + 5 + 5
    assert [s.item_id for s in script.stories] == [100, 101, 102, 103, 104, 105]


def test_json_string_and_model_inputs() -> None:
    raw = valid_script(candidates())
    model = ReelScript.model_validate(raw)
    assert validate_script(model.model_dump_json(), candidates(), DAY).total_words == 160
    assert validate_script(model, candidates(), DAY).total_words == 160


def test_hallucinated_item_rejected() -> None:
    raw = valid_script(candidates())
    raw["scenes"][2]["item_id"] = 999  # edge case 1
    assert any("item_id must be 101" in p for p in _problems(raw))


def test_url_must_match_exactly() -> None:
    raw = valid_script(candidates())
    raw["scenes"][1]["source_url"] = "https://news.example.com/story-0/"  # edge case 2
    assert any("source_url must be exactly" in p for p in _problems(raw))


def test_reordered_stories_rejected() -> None:
    raw = valid_script(candidates())
    s = raw["scenes"]
    s[1]["item_id"], s[2]["item_id"] = s[2]["item_id"], s[1]["item_id"]  # edge case 3
    s[1]["source_url"], s[2]["source_url"] = s[2]["source_url"], s[1]["source_url"]
    assert len(_problems(raw)) >= 2


def test_dropped_story_rejected() -> None:
    raw = valid_script(candidates(), story_words=30)
    del raw["scenes"][3]  # edge case 4
    for i, scene in enumerate(raw["scenes"]):
        scene["index"] = i
    assert any("expected exactly 6 story scenes" in p for p in _problems(raw))


@pytest.mark.parametrize("story_words", [40])  # 6*40 + 10 = 250 > 185
def test_over_budget_rejected(story_words: int) -> None:
    raw = valid_script(candidates(), story_words=story_words)
    assert any("total voiceover words must be 130-185" in p for p in _problems(raw))  # case 5


def test_story_too_short_rejected() -> None:
    raw = valid_script(candidates())
    raw["scenes"][1]["voiceover"] = words(5)
    assert any("scene 1: story voiceover must be 25-45 words" in p for p in _problems(raw))


def test_denied_word_rejected() -> None:
    raw = valid_script(candidates())
    raw["scenes"][2]["title"] = "This is Bullshit news"  # edge case 7
    assert any("disallowed words: bullshit" in p for p in _problems(raw))


def test_wrong_local_date_rejected() -> None:
    raw = valid_script(candidates())
    raw["local_date"] = "2026-10-07"
    assert any("local_date must be 2026-10-06" in p for p in _problems(raw))


@pytest.mark.parametrize(
    ("scene", "key", "value", "fragment"),
    [
        (0, "type", "story", "cover"),
        (-1, "type", "story", "outro"),
        (1, "index", 5, "indices"),
        (2, "type", "outro", "only story scenes"),
        (1, "item_id", None, "needs item_id"),
        (0, "item_id", 1, "must not reference"),
        (1, "title", "x" * 61, "at most 60"),
        (1, "bullets", ["a"] * 4, "at most 3"),
        (1, "bullets", ["b" * 49], "at most 48"),
        (None, "tone", "friendly", "tone"),
        (None, "surprise", 1, "Extra inputs"),
    ],
)
def test_shape_rules(scene: int | None, key: str, value: object, fragment: str) -> None:
    raw = copy.deepcopy(valid_script(candidates()))
    target: dict[str, Any] = raw if scene is None else raw["scenes"][scene]
    target[key] = value
    assert any(fragment in p for p in _problems(raw)), _problems(raw)


def test_invalid_json_string() -> None:
    assert _problems("{not json")


def test_word_budget_scales_for_short_days() -> None:
    assert word_budget(6) == (130, 185)
    assert word_budget(7) == (130, 185)
    assert word_budget(3) == (65, 185)
    assert word_budget(1) == (40, 185)


def test_one_story_script_valid() -> None:
    cands = candidates(1)
    script = validate_script(valid_script(cands, story_words=35), cands, DAY)
    assert len(script.scenes) == 3


def test_count_words_tokens() -> None:
    # edge case 14: one whitespace token each
    assert count_words("Qwen3-VL hits 5.4 % on ₹2,000  benchmark\n") == 7
    assert count_words("") == 0


def test_find_denied_whole_words_only() -> None:
    assert find_denied("Scunthorpe shitake mushrooms") == []
    assert find_denied("What the FUCK, shit.") == ["fuck", "shit"]


def test_date_type() -> None:
    script = validate_script(valid_script(candidates()), candidates(), DAY)
    assert script.local_date == datetime.date(2026, 10, 6)
