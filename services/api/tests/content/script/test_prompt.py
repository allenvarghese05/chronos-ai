"""Prompt v2 (ADR D37): per-story and frame messages, schemas, example validity."""

from __future__ import annotations

import dataclasses
import json

from chronos.content.script.prompt import (
    build_frame_messages,
    build_story_messages,
    example,
    frame_json_schema,
    prompt_version,
    story_json_schema,
    story_payload,
)
from chronos.content.script.schema import check_story_draft, story_word_range
from tests.content.script.helpers import DAY, candidates


def test_example_answer_passes_the_story_check() -> None:
    check_story_draft(json.dumps(example()["answer"]), (8, 45))


def test_story_messages() -> None:
    msgs = build_story_messages(candidates(1)[0], (8, 24))
    assert [m["role"] for m in msgs] == ["system", "user"]
    user = msgs[1]["content"]
    assert "8 to 24 words" in user
    assert '"title": "Story number 0 headline"' in user
    assert "{" not in user.replace(json.dumps(example(), ensure_ascii=False), "").replace(
        json.dumps(story_payload(candidates(1)[0]), ensure_ascii=False), ""
    )
    assert "untrusted" in msgs[0]["content"]
    assert "not who made it" in msgs[0]["content"]  # attribution rule
    assert "item_id" not in user and "http" not in user  # model never sees ids/urls


def test_frame_messages() -> None:
    msgs = build_frame_messages(DAY)
    assert "Today is Tuesday, October 6." in msgs[1]["content"]
    assert "6 to 20 words" in msgs[1]["content"]


def test_feedback_turn_appended() -> None:
    msgs = build_story_messages(candidates(1)[0], (12, 24), ["too short"], previous='{"x": 1}')
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "- too short" in msgs[-1]["content"]


def test_summary_capped_in_payload() -> None:
    long = dataclasses.replace(candidates(1)[0], summary="word " * 400)
    assert len(story_payload(long)["summary"]) <= 601


def test_schemas_flat_and_all_required() -> None:
    for schema in (story_json_schema(), frame_json_schema()):
        assert "$ref" not in json.dumps(schema)
        assert set(schema["required"]) == set(schema["properties"])
    assert set(story_json_schema()["properties"]) == {"title", "bullets", "voiceover"}


def test_story_word_range_fits_budget() -> None:
    assert story_word_range(6) == (8, 24)
    assert story_word_range(1) == (8, 45)
    assert story_word_range(7) == (8, 20)


def test_prompt_version_stable() -> None:
    assert prompt_version() == prompt_version()
    assert prompt_version().startswith("v2:") and len(prompt_version()) == 19
