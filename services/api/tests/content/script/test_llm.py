"""Provider wire formats and the error contract, via httpx.MockTransport."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from chronos.content.script.llm import (
    ANTHROPIC_TOOL,
    AnthropicScriptGenerator,
    FakeScriptGenerator,
    OllamaScriptGenerator,
    OpenAIScriptGenerator,
    ProviderError,
    ProviderUnavailable,
)

MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
SCHEMA: dict[str, Any] = {"type": "object"}
CANARY = "sk-canary-0123456789"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ollama(handler: Callable[[httpx.Request], httpx.Response]) -> OllamaScriptGenerator:
    return OllamaScriptGenerator("http://ollama.test", "qwen2.5:7b-instruct", _client(handler))


def test_ollama_request_and_result() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": "qwen2.5:7b-instruct", "digest": "abc123"}]})  # fmt: skip
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={
            "message": {"content": '{"ok": 1}'}, "done_reason": "stop",
            "prompt_eval_count": 900, "eval_count": 400})  # fmt: skip

    result = _ollama(handler).generate(MSGS, SCHEMA, 30)
    assert seen["format"] == SCHEMA and seen["stream"] is False
    assert seen["options"] == {"temperature": 0.4, "num_ctx": 8192}
    assert seen["keep_alive"] == "2m"
    assert result.content == '{"ok": 1}'
    assert result.digest == "sha256:abc123"
    assert (result.tokens_in, result.tokens_out) == (900, 400)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(404, json={"error": "model not found"}), ProviderUnavailable),
        (httpx.Response(500, text="boom"), ProviderError),
        (httpx.Response(200, json={"message": {"content": "{}"}, "done_reason": "length"}),
         ProviderError),
        (httpx.Response(200, json={"message": {"content": "  "}}), ProviderError),
        (httpx.Response(200, text="not json"), ProviderError),
        (httpx.Response(200, json=[1]), ProviderError),
    ],
)  # fmt: skip
def test_ollama_errors(response: httpx.Response, error: type[Exception]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return response

    with pytest.raises(error):
        _ollama(handler).generate(MSGS, SCHEMA, 30)


def test_ollama_connect_refused_is_unavailable_and_timeout_is_error() -> None:
    def refused(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ProviderUnavailable):
        _ollama(refused).generate(MSGS, SCHEMA, 30)
    with pytest.raises(ProviderError):
        _ollama(slow).generate(MSGS, SCHEMA, 30)


def test_ollama_digest_unknown_when_tags_fail() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            raise httpx.ConnectError("x")
        return httpx.Response(200, json={"message": {"content": "{}"}})

    assert _ollama(handler).generate(MSGS, SCHEMA, 30).digest == ""


def test_openai_request_and_result() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "model": "gpt-x-2026", "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"a": 1}'}}]})  # fmt: skip

    gen = OpenAIScriptGenerator(SecretStr(CANARY), "gpt-x", "https://oa.test/v1", _client(handler))
    result = gen.generate(MSGS, SCHEMA, 30)
    assert seen["auth"] == f"Bearer {CANARY}"
    assert seen["body"]["response_format"]["json_schema"]["schema"] == SCHEMA
    assert (result.content, result.model, result.tokens_out) == ('{"a": 1}', "gpt-x-2026", 20)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"error": "bad key"}, ProviderUnavailable),
        (429, {"error": "slow down"}, ProviderError),
        (200, {"choices": []}, ProviderError),
        (200, {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]},
         ProviderError),
    ],
)  # fmt: skip
def test_openai_errors(status: int, body: dict[str, Any], error: type[Exception]) -> None:
    gen = OpenAIScriptGenerator(
        SecretStr(CANARY), "m", "https://oa.test/v1",
        _client(lambda req: httpx.Response(status, json=body)),
    )  # fmt: skip
    with pytest.raises(error) as info:
        gen.generate(MSGS, SCHEMA, 30)
    assert CANARY not in str(info.value)  # secret never in error text


def test_anthropic_request_and_result() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["key"] = req.headers["x-api-key"]
        seen["version"] = req.headers["anthropic-version"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "model": "claude-sonnet-5-5", "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 6},
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "name": ANTHROPIC_TOOL, "input": {"a": 1}}]})  # fmt: skip

    gen = AnthropicScriptGenerator(
        SecretStr(CANARY), "claude-sonnet-5-5", "https://an.test/v1", _client(handler)
    )
    result = gen.generate(MSGS, SCHEMA, 30)
    assert seen["key"] == CANARY and seen["version"] == "2023-06-01"
    body = seen["body"]
    assert body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"][0]["input_schema"] == SCHEMA
    assert body["tool_choice"] == {"type": "tool", "name": ANTHROPIC_TOOL}
    assert json.loads(result.content) == {"a": 1}
    assert (result.tokens_in, result.tokens_out) == (5, 6)


@pytest.mark.parametrize(
    "body",
    [
        {"stop_reason": "max_tokens", "content": []},
        {"content": [{"type": "text", "text": "no tool"}]},
    ],
)
def test_anthropic_errors(body: dict[str, Any]) -> None:
    gen = AnthropicScriptGenerator(
        SecretStr(CANARY), "m", "https://an.test/v1",
        _client(lambda req: httpx.Response(200, json=body)),
    )  # fmt: skip
    with pytest.raises(ProviderError):
        gen.generate(MSGS, SCHEMA, 30)


def test_fake_generator_replays_and_exhausts() -> None:
    fake = FakeScriptGenerator(["a", ProviderError("x")])
    assert fake.generate(MSGS, SCHEMA, 1).content == "a"
    with pytest.raises(ProviderError):
        fake.generate(MSGS, SCHEMA, 1)
    with pytest.raises(ProviderUnavailable):
        fake.generate(MSGS, SCHEMA, 1)
    assert len(fake.calls) == 3
