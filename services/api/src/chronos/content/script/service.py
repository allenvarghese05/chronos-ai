"""Script generation chain: providers with retries, then the template (D11, T3.4).

Order (owner decision 5): Ollama, then the ``CHRONOS_LLM_PROVIDER`` provider when it is
``openai``/``anthropic`` and keyed, then the deterministic template, which always validates.

Per provider: up to ``attempts`` tries; a validation failure feeds the problems (and the
rejected answer) into the next try; :class:`ProviderUnavailable` skips the provider at once.
All tries share one deadline so the ``script`` job (300 s timeout) always finishes: each
request gets ``min(provider timeout, remaining - margin)`` and the chain drops to the template
when too little time is left.
"""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx
import structlog

from chronos.config import Settings
from chronos.content.script.fallback import template_script
from chronos.content.script.llm import (
    AnthropicScriptGenerator,
    OllamaScriptGenerator,
    OpenAIScriptGenerator,
    ProviderError,
    ProviderUnavailable,
    ScriptGenerator,
)
from chronos.content.script.prompt import build_messages, prompt_version, script_json_schema
from chronos.content.script.schema import (
    ReelScript,
    ScriptCandidate,
    ScriptValidationError,
    validate_script,
)

log = structlog.get_logger(__name__)

DEFAULT_DEADLINE_SEC = 270.0
DEADLINE_MARGIN_SEC = 5.0
MIN_REQUEST_SEC = 10.0
TEMPLATE_PROVIDER = "template"


@dataclass(frozen=True, slots=True)
class ProviderPlan:
    generator: ScriptGenerator
    attempts: int
    timeout_sec: float


@dataclass(frozen=True, slots=True)
class Provenance:
    """What produced the script; stored on the ``briefs`` row."""

    provider: str
    model: str
    digest: str
    prompt_version: str
    attempts: int
    used_fallback_template: bool
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int
    errors: tuple[str, ...]


def _add(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else a + b


def generate_script(
    candidates: Sequence[ScriptCandidate],
    local_date: datetime.date,
    plans: Sequence[ProviderPlan],
    *,
    deadline_sec: float = DEFAULT_DEADLINE_SEC,
    expected_digest: str = "",
    clock: Callable[[], float] = time.monotonic,
) -> tuple[ReelScript, Provenance]:
    """Return a validated script and its provenance; never raises for provider failures.

    Raises:
        ValueError: if *candidates* is empty or too long for a script (template precondition).
    """
    start = clock()
    schema = script_json_schema()
    attempts = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency = 0
    errors: list[str] = []

    for plan in plans:
        gen = plan.generator
        feedback: list[str] = []
        previous: str | None = None
        for _ in range(plan.attempts):
            remaining = deadline_sec - (clock() - start)
            if remaining - DEADLINE_MARGIN_SEC < MIN_REQUEST_SEC:
                errors.append(f"{gen.name}: deadline reached")
                break
            attempts += 1
            timeout = min(plan.timeout_sec, remaining - DEADLINE_MARGIN_SEC)
            messages = build_messages(candidates, local_date, feedback, previous)
            try:
                result = gen.generate(messages, schema, timeout)
            except ProviderUnavailable as exc:
                errors.append(f"{gen.name}: unavailable: {exc}")
                log.warning("script_provider_unavailable", provider=gen.name, error=str(exc))
                break
            except ProviderError as exc:
                errors.append(f"{gen.name}: {exc}")
                log.warning("script_provider_error", provider=gen.name, error=str(exc))
                continue
            tokens_in = _add(tokens_in, result.tokens_in)
            tokens_out = _add(tokens_out, result.tokens_out)
            latency += result.latency_ms
            try:
                script = validate_script(result.content, candidates, local_date)
            except ScriptValidationError as exc:
                feedback, previous = exc.problems, result.content
                errors.append(f"{gen.name}: invalid: {len(exc.problems)} problem(s)")
                log.info(
                    "script_invalid",
                    provider=gen.name,
                    problems=exc.problems[:10],
                    attempt=attempts,
                )
                continue
            if expected_digest and result.digest and result.digest != expected_digest:
                log.warning(
                    "ollama_digest_mismatch", expected=expected_digest, actual=result.digest
                )
            log.info(
                "script_generated", provider=result.provider, model=result.model, attempts=attempts
            )
            return script, Provenance(
                provider=result.provider,
                model=result.model,
                digest=result.digest,
                prompt_version=prompt_version(),
                attempts=attempts,
                used_fallback_template=False,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency,
                errors=tuple(errors),
            )

    script = template_script(candidates, local_date)
    log.warning("script_template_fallback", attempts=attempts, errors=errors[-5:])
    return script, Provenance(
        provider=TEMPLATE_PROVIDER,
        model="",
        digest="",
        prompt_version=prompt_version(),
        attempts=attempts,
        used_fallback_template=True,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency,
        errors=tuple(errors),
    )


def build_plans(settings: Settings, client: httpx.Client | None = None) -> list[ProviderPlan]:
    """Provider chain from settings (Ollama always first)."""
    http = client or httpx.Client()
    plans = [
        ProviderPlan(
            OllamaScriptGenerator(settings.ollama_endpoint, settings.ollama_model, http),
            attempts=3,
            timeout_sec=settings.ollama_timeout_sec,
        )
    ]
    if settings.llm_provider == "openai":
        if settings.openai_api_key is None or not settings.openai_model:
            log.warning("script_provider_skipped", provider="openai", reason="key or model unset")
        else:
            plans.append(
                ProviderPlan(
                    OpenAIScriptGenerator(
                        settings.openai_api_key, settings.openai_model, settings.openai_base_url,
                        http,
                    ),
                    attempts=2,
                    timeout_sec=settings.cloud_llm_timeout_sec,
                )
            )  # fmt: skip
    elif settings.llm_provider == "anthropic":
        if settings.anthropic_api_key is None:
            log.warning("script_provider_skipped", provider="anthropic", reason="key unset")
        else:
            plans.append(
                ProviderPlan(
                    AnthropicScriptGenerator(
                        settings.anthropic_api_key, settings.anthropic_model,
                        settings.anthropic_base_url, http,
                    ),
                    attempts=2,
                    timeout_sec=settings.cloud_llm_timeout_sec,
                )
            )  # fmt: skip
    return plans
