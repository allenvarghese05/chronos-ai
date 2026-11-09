"""Per-unit generation (ADR D37): retries, provider skipping, template patches, deadline."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import SecretStr

from chronos.content.script.llm import (
    AnthropicScriptGenerator,
    FakeScriptGenerator,
    GenerationResult,
    OllamaScriptGenerator,
    OpenAIScriptGenerator,
    ProviderError,
    ProviderUnavailable,
)
from chronos.content.script.prompt import Message, prompt_version
from chronos.content.script.service import ProviderPlan, build_plans, generate_script
from tests.conftest import make_settings
from tests.content.script.helpers import DAY, candidates, frame_reply, story_reply

FRAME = frame_reply()


def plan(*replies: str | Exception, attempts: int = 3, name: str = "fake") -> ProviderPlan:
    return ProviderPlan(FakeScriptGenerator(list(replies), name=name), attempts, 60.0)


def happy(n: int) -> list[str | Exception]:
    return [FRAME, *[story_reply(20, f"Headline {i}") for i in range(n)]]


def test_all_units_from_model_and_code_owns_structure() -> None:
    script, prov = generate_script(candidates(6), DAY, [plan(*happy(6))])
    assert (prov.provider, prov.attempts, prov.used_fallback_template) == ("fake", 7, False)
    assert prov.template_units == ()
    assert prov.prompt_version == prompt_version()
    assert [s.title for s in script.stories] == [f"Headline {i}" for i in range(6)]
    for scene, cand in zip(script.stories, candidates(6), strict=True):
        assert (scene.item_id, scene.source_url) == (cand.item_id, cand.url)
    assert script.stories[0].kicker == "AI DROP"  # code-owned label
    assert script.total_words == 6 * 20 + 16


def test_invalid_story_retried_with_feedback() -> None:
    p = plan(FRAME, story_reply(5), story_reply(20), *happy(6)[2:])
    _, prov = generate_script(candidates(6), DAY, [p])
    assert prov.attempts == 8 and prov.template_units == ()
    fake = p.generator
    assert isinstance(fake, FakeScriptGenerator)
    retry = fake.calls[2]
    assert "voiceover must be 8-24 words, got 5" in retry[-1]["content"]


def test_story_failing_everywhere_is_patched_from_template() -> None:
    replies = [FRAME, story_reply(5), story_reply(5), story_reply(5), *happy(6)[2:]]
    script, prov = generate_script(candidates(6), DAY, [plan(*replies)])
    assert not prov.used_fallback_template
    assert prov.template_units == ("story:100",)
    assert script.stories[0].voiceover.startswith("From Source 0:")
    assert script.stories[1].title == "Headline 1"


def test_unavailable_provider_is_dead_for_all_units() -> None:
    first = plan(ProviderUnavailable("refused"), name="ollama")
    second = plan(*happy(3), name="anthropic")
    _, prov = generate_script(candidates(3), DAY, [first, second])
    assert prov.provider == "anthropic" and prov.attempts == 5
    assert isinstance(first.generator, FakeScriptGenerator) and len(first.generator.calls) == 1


def test_all_fail_uses_whole_template() -> None:
    _, prov = generate_script(
        candidates(6), DAY, [plan(ProviderError("t"), ProviderUnavailable("down"), name="ollama")]
    )
    assert prov.used_fallback_template and prov.provider == "template"
    assert len(prov.template_units) == 7


def test_no_providers_uses_template() -> None:
    script, prov = generate_script(candidates(3), DAY, [])
    assert prov.used_fallback_template and prov.attempts == 0 and len(script.stories) == 3


def test_deadline_exhausted_goes_to_template_without_calls() -> None:
    times = iter([0.0, 300.0])
    p = plan(*happy(6))
    _, prov = generate_script(candidates(6), DAY, [p], clock=lambda: next(times, 300.0))
    assert prov.used_fallback_template and prov.attempts == 0
    assert isinstance(p.generator, FakeScriptGenerator) and p.generator.calls == []


def test_request_timeout_bounded_by_remaining_deadline() -> None:
    seen: list[float] = []

    class Spy(FakeScriptGenerator):
        def generate(
            self, messages: Sequence[Message], schema: dict[str, Any], timeout_sec: float
        ) -> GenerationResult:
            seen.append(timeout_sec)
            return super().generate(messages, schema, timeout_sec)

    clock = iter([0.0, 250.0])
    generate_script(
        candidates(1), DAY, [ProviderPlan(Spy(happy(1)), 3, 80.0)], deadline_sec=270,
        clock=lambda: next(clock, 255.0),
    )  # fmt: skip
    assert seen[0] == 15.0  # 270 - 250 - 5 margin


def test_tokens_and_latency_summed() -> None:
    class Counting(FakeScriptGenerator):
        def generate(
            self, messages: Sequence[Message], schema: dict[str, Any], timeout_sec: float
        ) -> GenerationResult:
            r = super().generate(messages, schema, timeout_sec)
            return GenerationResult(r.content, r.provider, r.model, "sha256:x", 10, 5, 100)

    _, prov = generate_script(candidates(2), DAY, [ProviderPlan(Counting(happy(2)), 3, 60)])
    got = (prov.tokens_in, prov.tokens_out, prov.latency_ms, prov.digest)
    assert got == (30, 15, 300, "sha256:x")


def test_empty_or_too_many_candidates_rejected() -> None:
    with pytest.raises(ValueError):
        generate_script([], DAY, [])
    with pytest.raises(ValueError):
        generate_script(candidates(8), DAY, [])


def test_build_plans_defaults_to_ollama_only() -> None:
    plans = build_plans(make_settings())
    assert [type(p.generator) for p in plans] == [OllamaScriptGenerator]
    assert plans[0].attempts == 3 and plans[0].timeout_sec == 80.0


def test_build_plans_adds_keyed_cloud_provider() -> None:
    anth = build_plans(make_settings(llm_provider="anthropic", anthropic_api_key="k"))
    assert isinstance(anth[1].generator, AnthropicScriptGenerator)
    assert anth[1].generator.model == "claude-sonnet-5-5"
    oa = build_plans(make_settings(llm_provider="openai", openai_api_key="k", openai_model="m"))
    assert isinstance(oa[1].generator, OpenAIScriptGenerator)


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_provider": "anthropic"},
        {"llm_provider": "openai", "openai_api_key": "k"},
        {"llm_provider": "openai", "openai_model": "m"},
    ],
)
def test_build_plans_skips_unkeyed_cloud_provider(overrides: dict[str, object]) -> None:
    assert len(build_plans(make_settings(**overrides))) == 1


def test_settings_keys_are_secret() -> None:
    s = make_settings(anthropic_api_key="sk-canary-1")
    assert isinstance(s.anthropic_api_key, SecretStr)
    assert "sk-canary-1" not in repr(s)


def test_frame_reply_shape() -> None:
    assert set(json.loads(FRAME)) == {"greeting", "cover_voiceover", "outro_voiceover"}
