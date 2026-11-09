"""Generation chain: retries with feedback, provider skipping, deadline, template fallback."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from chronos.content.script.llm import (
    AnthropicScriptGenerator,
    FakeScriptGenerator,
    OllamaScriptGenerator,
    OpenAIScriptGenerator,
    ProviderError,
    ProviderUnavailable,
)
from chronos.content.script.prompt import prompt_version
from chronos.content.script.service import ProviderPlan, build_plans, generate_script
from tests.conftest import make_settings
from tests.content.script.helpers import DAY, candidates, valid_script

GOOD = json.dumps(valid_script(candidates()))
BAD = json.dumps({**valid_script(candidates()), "local_date": "2026-01-01"})


def plan(*replies: str | Exception, attempts: int = 3, name: str = "fake") -> ProviderPlan:
    return ProviderPlan(FakeScriptGenerator(list(replies), name=name), attempts, 60.0)


def test_first_attempt_success_records_provenance() -> None:
    script, prov = generate_script(candidates(), DAY, [plan(GOOD)])
    assert script.total_words == 160
    assert (prov.provider, prov.attempts, prov.used_fallback_template) == ("fake", 1, False)
    assert prov.prompt_version == prompt_version()


def test_invalid_then_valid_feeds_back_problems() -> None:
    p = plan(BAD, GOOD)
    script, prov = generate_script(candidates(), DAY, [p])
    assert prov.attempts == 2 and not prov.used_fallback_template
    fake = p.generator
    assert isinstance(fake, FakeScriptGenerator)
    retry = fake.calls[1]
    assert retry[-2] == {"role": "assistant", "content": BAD}
    assert "local_date must be 2026-10-06" in retry[-1]["content"]
    assert script.local_date == DAY


def test_unavailable_skips_to_next_provider_without_retrying() -> None:
    first = plan(ProviderUnavailable("refused"), GOOD, name="ollama")
    second = plan(GOOD, name="anthropic")
    _, prov = generate_script(candidates(), DAY, [first, second])
    assert prov.provider == "anthropic"
    assert prov.attempts == 2  # one on ollama (skipped), one on anthropic
    assert prov.errors[0].startswith("ollama: unavailable")


def test_all_fail_falls_back_to_template() -> None:
    # "fallback used when provider raises"
    _, prov = generate_script(
        candidates(),
        DAY,
        [plan(ProviderError("t1"), ProviderError("t2"), BAD, name="ollama"),
         plan(ProviderUnavailable("no key"), name="openai")],
    )  # fmt: skip
    assert prov.used_fallback_template and prov.provider == "template"
    assert prov.attempts == 4
    assert len(prov.errors) == 4


def test_no_providers_uses_template() -> None:
    script, prov = generate_script(candidates(3), DAY, [])
    assert prov.used_fallback_template and prov.attempts == 0
    assert len(script.stories) == 3


def test_deadline_exhausted_goes_to_template_without_calls() -> None:
    times = iter([0.0, 300.0])
    p = plan(GOOD)
    _, prov = generate_script(candidates(), DAY, [p], clock=lambda: next(times))
    assert prov.used_fallback_template and prov.attempts == 0
    assert isinstance(p.generator, FakeScriptGenerator) and p.generator.calls == []


def test_request_timeout_bounded_by_remaining_deadline() -> None:
    seen: list[float] = []

    class Spy(FakeScriptGenerator):
        def generate(self, messages, schema, timeout_sec):  # type: ignore[override]
            seen.append(timeout_sec)
            return super().generate(messages, schema, timeout_sec)

    clock = iter([0.0, 250.0, 250.0])
    generate_script(
        candidates(), DAY, [ProviderPlan(Spy([GOOD]), 3, 80.0)], deadline_sec=270,
        clock=lambda: next(clock),
    )  # fmt: skip
    assert seen == [15.0]  # 270 - 250 - 5 margin


def test_empty_candidates_rejected() -> None:
    with pytest.raises(ValueError):
        generate_script([], DAY, [])


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
        {"llm_provider": "openai", "openai_api_key": "k"},  # model unset
        {"llm_provider": "openai", "openai_model": "m"},  # key unset
    ],
)
def test_build_plans_skips_unkeyed_cloud_provider(overrides: dict[str, object]) -> None:
    assert len(build_plans(make_settings(**overrides))) == 1


def test_settings_keys_are_secret() -> None:
    s = make_settings(anthropic_api_key="sk-canary-1")
    assert isinstance(s.anthropic_api_key, SecretStr)
    assert "sk-canary-1" not in repr(s)
