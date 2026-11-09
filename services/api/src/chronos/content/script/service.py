"""Script generation: per-unit LLM calls, code assembly, template patches (D11, ADR D37).

Units: one *frame* call (greeting, cover and outro lines) and one call per story (title,
bullets, voiceover). Code assembles the :class:`ReelScript` - scene order, cover/outro, story
ids, URLs, source names and kickers - so structure can never be wrong or invented.

Each unit walks the provider chain (owner decision 5: Ollama, then the keyed
``CHRONOS_LLM_PROVIDER`` provider): up to ``attempts`` tries per provider with the unit's own
validation problems fed back; :class:`ProviderUnavailable` marks the provider dead for the rest
of the run. A unit that fails everywhere is filled from the deterministic template, so one
stubborn story no longer costs the whole reel. All calls share one deadline so the ``script``
job (300 s timeout) always finishes. The assembled script still passes
:func:`~chronos.content.script.schema.validate_script` before it is returned.
"""

from __future__ import annotations

import datetime
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from chronos.config import Settings
from chronos.content.script.fallback import (
    COVER_TITLE,
    OUTRO_TITLE,
    OUTRO_VOICEOVER,
    cover_voiceover,
    kicker_for,
    template_script,
    template_story_scene,
    template_story_words,
)
from chronos.content.script.llm import (
    AnthropicScriptGenerator,
    GenerationResult,
    OllamaScriptGenerator,
    OpenAIScriptGenerator,
    ProviderError,
    ProviderUnavailable,
    ScriptGenerator,
)
from chronos.content.script.prompt import (
    Message,
    build_frame_messages,
    build_story_messages,
    frame_json_schema,
    prompt_version,
    story_json_schema,
)
from chronos.content.script.schema import (
    MAX_SCENES,
    FrameDraft,
    ReelScript,
    Scene,
    ScriptCandidate,
    ScriptValidationError,
    StoryDraft,
    check_frame_draft,
    check_story_draft,
    story_word_range,
    validate_script,
)

log = structlog.get_logger(__name__)

DEFAULT_DEADLINE_SEC = 270.0
DEADLINE_MARGIN_SEC = 5.0
MIN_REQUEST_SEC = 8.0
TEMPLATE_PROVIDER = "template"


@dataclass(frozen=True, slots=True)
class ProviderPlan:
    generator: ScriptGenerator
    attempts: int
    timeout_sec: float


@dataclass(frozen=True, slots=True)
class Provenance:
    """What produced the script; stored on the ``briefs`` row (and logged)."""

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
    template_units: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Unit[D]:
    key: str
    build: Callable[[Sequence[str], str | None], list[Message]]
    schema: dict[str, Any]
    check: Callable[[str], D]


@dataclass
class _Run:
    plans: Sequence[ProviderPlan]
    deadline_sec: float
    clock: Callable[[], float]
    start: float
    dead: set[str] = field(default_factory=set[str])
    out_of_time: bool = False
    attempts: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0
    errors: list[str] = field(default_factory=list[str])
    wins: Counter[str] = field(default_factory=Counter[str])
    results: dict[str, GenerationResult] = field(default_factory=dict[str, GenerationResult])

    def _account(self, result: GenerationResult) -> None:
        if result.tokens_in is not None:
            self.tokens_in = (self.tokens_in or 0) + result.tokens_in
        if result.tokens_out is not None:
            self.tokens_out = (self.tokens_out or 0) + result.tokens_out
        self.latency_ms += result.latency_ms

    def run[D](self, unit: _Unit[D]) -> D | None:
        for plan in self.plans:
            gen = plan.generator
            if gen.name in self.dead or self.out_of_time:
                continue
            feedback: list[str] = []
            previous: str | None = None
            for _ in range(plan.attempts):
                remaining = self.deadline_sec - (self.clock() - self.start)
                if remaining - DEADLINE_MARGIN_SEC < MIN_REQUEST_SEC:
                    self.out_of_time = True
                    self.errors.append(f"{unit.key}: deadline reached")
                    return None
                self.attempts += 1
                timeout = min(plan.timeout_sec, remaining - DEADLINE_MARGIN_SEC)
                try:
                    result = gen.generate(unit.build(feedback, previous), unit.schema, timeout)
                except ProviderUnavailable as exc:
                    self.dead.add(gen.name)
                    self.errors.append(f"{gen.name}: unavailable: {exc}")
                    log.warning("script_provider_unavailable", provider=gen.name, error=str(exc))
                    break
                except ProviderError as exc:
                    self.errors.append(f"{unit.key}/{gen.name}: {exc}")
                    log.warning("script_provider_error", provider=gen.name, unit=unit.key,
                                error=str(exc))  # fmt: skip
                    continue
                self._account(result)
                try:
                    draft = unit.check(result.content)
                except ScriptValidationError as exc:
                    feedback, previous = exc.problems, result.content
                    self.errors.append(f"{unit.key}/{gen.name}: {'; '.join(exc.problems)}")
                    log.info("script_unit_invalid", unit=unit.key, provider=gen.name,
                             problems=exc.problems[:5])  # fmt: skip
                    continue
                self.wins[gen.name] += 1
                self.results[gen.name] = result
                return draft
        return None


def assemble(
    candidates: Sequence[ScriptCandidate],
    local_date: datetime.date,
    frame: FrameDraft | None,
    stories: Sequence[StoryDraft | None],
) -> ReelScript:
    """Build the script from drafts; missing pieces come from the template. Validated."""
    n = len(candidates)
    scenes: list[Scene] = [
        Scene(
            index=0,
            type="cover",
            title=COVER_TITLE,
            voiceover=frame.cover_voiceover if frame else cover_voiceover(local_date),
            visual="gradient",
        )
    ]
    fill_words = template_story_words(n)
    for i, (cand, draft) in enumerate(zip(candidates, stories, strict=True), start=1):
        if draft is None:
            scenes.append(template_story_scene(cand, i, fill_words))
            continue
        scenes.append(
            Scene(
                index=i,
                type="story",
                title=draft.title,
                kicker=kicker_for(cand.category),
                bullets=draft.bullets,
                voiceover=draft.voiceover,
                item_id=cand.item_id,
                source_name=cand.source_name[:60] or None,
                source_url=cand.url,
                visual="image",
            )
        )
    scenes.append(
        Scene(
            index=n + 1,
            type="outro",
            title=OUTRO_TITLE,
            voiceover=frame.outro_voiceover if frame else OUTRO_VOICEOVER,
            visual="gradient",
        )
    )
    draft_script = ReelScript(
        local_date=local_date,
        greeting=frame.greeting if frame else "Good morning",
        scenes=scenes,
        total_words=0,
        tone="calm",
    )
    return validate_script(draft_script, candidates, local_date)


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
        ValueError: if *candidates* is empty or longer than a script can hold.
    """
    n = len(candidates)
    if not 1 <= n <= MAX_SCENES - 2:
        raise ValueError(f"need 1..{MAX_SCENES - 2} stories, got {n}")
    run = _Run(plans=plans, deadline_sec=deadline_sec, clock=clock, start=clock())
    word_range = story_word_range(n)

    frame = run.run(
        _Unit(
            key="frame",
            build=lambda fb, prev: build_frame_messages(local_date, fb, prev),
            schema=frame_json_schema(),
            check=check_frame_draft,
        )
    )
    stories: list[StoryDraft | None] = []
    for cand in candidates:
        stories.append(
            run.run(
                _Unit(
                    key=f"story:{cand.item_id}",
                    build=lambda fb, prev, c=cand: build_story_messages(c, word_range, fb, prev),
                    schema=story_json_schema(),
                    check=lambda raw: check_story_draft(raw, word_range),
                )
            )
        )

    template_units = tuple(
        ([] if frame else ["frame"])
        + [f"story:{c.item_id}" for c, d in zip(candidates, stories, strict=True) if d is None]
    )
    all_template = frame is None and all(d is None for d in stories)
    try:
        script = (
            template_script(candidates, local_date)
            if all_template
            else assemble(candidates, local_date, frame, stories)
        )
    except ScriptValidationError as exc:  # pragma: no cover - pieces always fit the budget
        log.error("script_assembly_invalid", problems=exc.problems)
        script, all_template = template_script(candidates, local_date), True

    if all_template:
        provider, model, digest = TEMPLATE_PROVIDER, "", ""
        log.warning("script_template_fallback", attempts=run.attempts, errors=run.errors[-5:])
    else:
        provider = run.wins.most_common(1)[0][0]
        best = run.results[provider]
        model, digest = best.model, best.digest
        if expected_digest and digest and digest != expected_digest:
            log.warning("ollama_digest_mismatch", expected=expected_digest, actual=digest)
        log.info("script_generated", provider=provider, attempts=run.attempts,
                 template_units=list(template_units))  # fmt: skip
    return script, Provenance(
        provider=provider,
        model=model,
        digest=digest,
        prompt_version=prompt_version(),
        attempts=run.attempts,
        used_fallback_template=all_template,
        tokens_in=run.tokens_in,
        tokens_out=run.tokens_out,
        latency_ms=run.latency_ms,
        errors=tuple(run.errors),
        template_units=template_units,
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
