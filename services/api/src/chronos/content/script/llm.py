"""Script generators: Ollama (primary), OpenAI / Anthropic (opt-in), Fake (tests) (D11, T3.4).

All return the raw JSON text plus provenance; nothing here validates or trusts the content -
:func:`chronos.content.script.schema.validate_script` does that.

Error contract:

- :class:`ProviderUnavailable` - this provider cannot work right now (connection refused,
  model missing, no/invalid API key). The service skips straight to the next provider.
- :class:`ProviderError` - this attempt failed (timeout, 5xx, rate limit, truncated or
  non-JSON reply). The service may retry the same provider.

LLM endpoints are operator configuration (localhost Ollama, fixed vendor APIs), not feed URLs,
so they do not go through the SSRF-guarded client. API keys are only ever placed in request
headers; they are never logged or included in error messages.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import httpx
from pydantic import SecretStr

from chronos.content.script.prompt import Message

TEMPERATURE = 0.4
OLLAMA_NUM_CTX = 8192
OLLAMA_KEEP_ALIVE = "2m"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_TOOL = "submit_reel_script"
MAX_OUTPUT_TOKENS = 2048


class ProviderUnavailable(Exception):
    """The provider cannot serve requests now; skip it."""


class ProviderError(Exception):
    """One attempt failed; the provider may be retried."""


@dataclass(frozen=True, slots=True)
class GenerationResult:
    content: str
    provider: str
    model: str
    digest: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0


class ScriptGenerator(Protocol):
    name: str
    model: str

    def generate(
        self, messages: Sequence[Message], schema: dict[str, Any], timeout_sec: float
    ) -> GenerationResult: ...


def _post(
    client: httpx.Client, url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    try:
        response = client.post(url, json=body, headers=headers, timeout=timeout)
    except httpx.ConnectError as exc:
        raise ProviderUnavailable(f"cannot connect to {httpx.URL(url).host}") from exc
    except httpx.TimeoutException as exc:
        raise ProviderError(f"timed out after {timeout:.0f}s") from exc
    except httpx.RequestError as exc:
        raise ProviderError(f"request failed: {type(exc).__name__}") from exc
    if response.status_code in (401, 403, 404):
        raise ProviderUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")
    if response.status_code >= 400:
        raise ProviderError(f"HTTP {response.status_code}: {response.text[:200]}")
    try:
        data: object = response.json()
    except ValueError as exc:
        raise ProviderError("response was not JSON") from exc
    if not isinstance(data, dict):
        raise ProviderError("response JSON was not an object")
    return cast("dict[str, Any]", data)


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


@dataclass
class OllamaScriptGenerator:
    """``POST /api/chat`` with ``format`` = the JSON schema (structured outputs)."""

    endpoint: str
    model: str
    client: httpx.Client = field(default_factory=httpx.Client)
    name: str = "ollama"
    _digest: str | None = None

    def digest(self) -> str:
        """The local model's digest from ``/api/tags`` (``""`` if unknown); cached."""
        if self._digest is None:
            self._digest = ""
            try:
                data = self.client.get(f"{self.endpoint}/api/tags", timeout=5.0).json()
                for m in cast("list[dict[str, Any]]", data.get("models", [])):
                    if m.get("name") == self.model or m.get("model") == self.model:
                        raw = str(m.get("digest", ""))
                        self._digest = (
                            raw if raw.startswith("sha256:") or not raw else (f"sha256:{raw}")
                        )
                        break
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
        return self._digest

    def generate(
        self, messages: Sequence[Message], schema: dict[str, Any], timeout_sec: float
    ) -> GenerationResult:
        start = time.perf_counter()
        data = _post(
            self.client,
            f"{self.endpoint}/api/chat",
            {
                "model": self.model,
                "messages": list(messages),
                "stream": False,
                "format": schema,
                "options": {"temperature": TEMPERATURE, "num_ctx": OLLAMA_NUM_CTX},
                "keep_alive": OLLAMA_KEEP_ALIVE,
            },
            {},
            timeout_sec,
        )
        if data.get("done_reason") == "length":
            raise ProviderError("reply truncated (done_reason=length)")
        message = data.get("message")
        content = (
            cast("dict[str, Any]", message).get("content") if isinstance(message, dict) else None
        )
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("empty reply")
        return GenerationResult(
            content=content,
            provider=self.name,
            model=self.model,
            digest=self.digest(),
            tokens_in=_opt_int(data.get("prompt_eval_count")),
            tokens_out=_opt_int(data.get("eval_count")),
            latency_ms=_ms(start),
        )


@dataclass
class OpenAIScriptGenerator:
    """Chat Completions with ``response_format: json_schema`` over plain httpx."""

    api_key: SecretStr
    model: str
    base_url: str = "https://api.openai.com/v1"
    client: httpx.Client = field(default_factory=httpx.Client)
    name: str = "openai"

    def generate(
        self, messages: Sequence[Message], schema: dict[str, Any], timeout_sec: float
    ) -> GenerationResult:
        start = time.perf_counter()
        data = _post(
            self.client,
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": list(messages),
                "temperature": TEMPERATURE,
                "max_completion_tokens": MAX_OUTPUT_TOKENS,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "reel_script", "schema": schema, "strict": False},
                },
            },
            {"Authorization": f"Bearer {self.api_key.get_secret_value()}"},
            timeout_sec,
        )
        try:
            choice = cast("list[dict[str, Any]]", data["choices"])[0]
            content = cast("dict[str, Any]", choice["message"])["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("unexpected response shape") from exc
        if choice.get("finish_reason") == "length":
            raise ProviderError("reply truncated (finish_reason=length)")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("empty reply")
        usage = cast("dict[str, Any]", data.get("usage") or {})
        return GenerationResult(
            content=content,
            provider=self.name,
            model=str(data.get("model") or self.model),
            tokens_in=_opt_int(usage.get("prompt_tokens")),
            tokens_out=_opt_int(usage.get("completion_tokens")),
            latency_ms=_ms(start),
        )


@dataclass
class AnthropicScriptGenerator:
    """Messages API with a forced tool whose ``input_schema`` is the script schema."""

    api_key: SecretStr
    model: str
    base_url: str = "https://api.anthropic.com/v1"
    client: httpx.Client = field(default_factory=httpx.Client)
    name: str = "anthropic"

    def generate(
        self, messages: Sequence[Message], schema: dict[str, Any], timeout_sec: float
    ) -> GenerationResult:
        start = time.perf_counter()
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        data = _post(
            self.client,
            f"{self.base_url}/messages",
            {
                "model": self.model,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": TEMPERATURE,
                "system": system,
                "messages": turns,
                "tools": [
                    {
                        "name": ANTHROPIC_TOOL,
                        "description": "Submit the complete reel script.",
                        "input_schema": schema,
                    }
                ],
                "tool_choice": {"type": "tool", "name": ANTHROPIC_TOOL},
            },
            {
                "x-api-key": self.api_key.get_secret_value(),
                "anthropic-version": ANTHROPIC_VERSION,
            },
            timeout_sec,
        )
        if data.get("stop_reason") == "max_tokens":
            raise ProviderError("reply truncated (stop_reason=max_tokens)")
        blocks = data.get("content")
        tool_input: object = None
        if isinstance(blocks, list):
            for block in cast("list[dict[str, Any]]", blocks):
                if block.get("type") == "tool_use" and block.get("name") == ANTHROPIC_TOOL:
                    tool_input = block.get("input")
                    break
        if not isinstance(tool_input, dict):
            raise ProviderError("no tool_use block in reply")
        usage = cast("dict[str, Any]", data.get("usage") or {})
        return GenerationResult(
            content=json.dumps(tool_input),
            provider=self.name,
            model=str(data.get("model") or self.model),
            tokens_in=_opt_int(usage.get("input_tokens")),
            tokens_out=_opt_int(usage.get("output_tokens")),
            latency_ms=_ms(start),
        )


@dataclass
class FakeScriptGenerator:
    """Replays queued replies (str) or raises queued exceptions, in order."""

    replies: list[str | Exception]
    name: str = "fake"
    model: str = "fake-model"
    calls: list[list[Message]] = field(default_factory=list[list[Message]])

    def generate(
        self, messages: Sequence[Message], schema: dict[str, Any], timeout_sec: float
    ) -> GenerationResult:
        self.calls.append(list(messages))
        if not self.replies:
            raise ProviderUnavailable("fake generator exhausted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return GenerationResult(content=reply, provider=self.name, model=self.model)
