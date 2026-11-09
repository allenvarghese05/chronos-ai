"""Prompt v1: example validity, placeholders, feedback turn, schema inlining."""

from __future__ import annotations

import dataclasses
import datetime
import json
from typing import Any, cast

from chronos.content.script.prompt import (
    build_messages,
    example,
    prompt_version,
    script_json_schema,
    stories_payload,
)
from chronos.content.script.schema import ScriptCandidate, count_words, validate_script
from tests.content.script.helpers import DAY, candidates


def _example_candidates() -> list[ScriptCandidate]:
    return [
        ScriptCandidate(
            item_id=s["item_id"], category=s["category"], source_key="x",
            source_name=s["source_name"], url=s["source_url"], title=s["title"],
            summary=s["summary"],
        )
        for s in cast("list[dict[str, Any]]", example()["stories"])
    ]  # fmt: skip


def test_example_is_itself_a_valid_script() -> None:
    ex = example()
    day = datetime.date.fromisoformat(ex["local_date"])
    script = validate_script(ex["script"], _example_candidates(), day)
    # the example must teach the right arithmetic too
    assert ex["script"]["total_words"] == script.total_words
    for scene in script.stories:
        assert 25 <= count_words(scene.voiceover) <= 45


def test_messages_fill_every_placeholder() -> None:
    msgs = build_messages(candidates(6), DAY)
    assert [m["role"] for m in msgs] == ["system", "user"]
    user = msgs[1]["content"]
    assert "{" + "local_date}" not in user and "{stories_json}" not in user
    assert "2026-10-06 (Tuesday)" in user
    assert "130 to 185 words" in user
    assert "scenes 1..6" in user
    assert '"item_id": 100' in user
    assert "untrusted" in msgs[0]["content"]


def test_feedback_turn_appended() -> None:
    msgs = build_messages(candidates(2), DAY, ["scene 1: bad"], previous='{"x": 1}')
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "- scene 1: bad" in msgs[-1]["content"]


def test_summary_capped_in_payload() -> None:
    c = candidates(1)[0]
    long = dataclasses.replace(c, summary="word " * 400)
    assert len(stories_payload([long])[0]["summary"]) <= 601


def test_schema_has_no_refs() -> None:
    text = json.dumps(script_json_schema())
    assert "$ref" not in text and "$defs" not in text
    assert script_json_schema()["properties"]["scenes"]["items"]["properties"]["type"]


def test_prompt_version_stable() -> None:
    assert prompt_version() == prompt_version()
    assert prompt_version().startswith("v1:") and len(prompt_version()) == 19
