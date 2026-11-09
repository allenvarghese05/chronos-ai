"""Prompt v1 assembly for the reel script (docs/06 §5, docs/09 Content and LLM).

Stories go into the prompt as a JSON array (data, not instructions) with HTML already
stripped and summaries capped. ``prompt_version`` identifies the exact prompt text + example
and is stored on every brief for provenance.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any, cast

from chronos.content.script.schema import ReelScript, ScriptCandidate, word_budget

PROMPT_DIR = Path(__file__).parent / "prompts"
PROMPT_FILE = PROMPT_DIR / "reel_script.v1.md"
EXAMPLE_FILE = PROMPT_DIR / "example_reel.json"
SUMMARY_PROMPT_CHARS = 600

Message = dict[str, str]


@cache
def _sections() -> tuple[str, str]:
    text = PROMPT_FILE.read_text(encoding="utf-8")
    _, _, rest = text.partition("## System\n")
    system, _, user = rest.partition("## User\n")
    if not system.strip() or not user.strip():
        raise ValueError(f"{PROMPT_FILE.name} must have '## System' and '## User' sections")
    return system.strip(), user.strip()


@cache
def example() -> dict[str, Any]:
    """The few-shot example: ``{"local_date", "stories": [...], "script": {...}}``."""
    return cast("dict[str, Any]", json.loads(EXAMPLE_FILE.read_text(encoding="utf-8")))


@cache
def prompt_version() -> str:
    """``v1:<sha256[:16]>`` of the prompt file and the example."""
    digest = hashlib.sha256(PROMPT_FILE.read_bytes() + EXAMPLE_FILE.read_bytes()).hexdigest()
    return f"v1:{digest[:16]}"


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        mapping = cast("dict[str, Any]", node)
        ref = mapping.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return _inline_refs(defs[ref.removeprefix("#/$defs/")], defs)
        return {k: _inline_refs(v, defs) for k, v in mapping.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in cast("list[Any]", node)]
    return node


@cache
def _schema_json() -> str:
    schema = ReelScript.model_json_schema()
    defs = cast("dict[str, Any]", schema.get("$defs", {}))
    return json.dumps(_inline_refs(schema, defs), sort_keys=True)


def script_json_schema() -> dict[str, Any]:
    """``ReelScript`` JSON schema with local ``$ref``s inlined (for constrained decoding)."""
    return cast("dict[str, Any]", json.loads(_schema_json()))


def stories_payload(candidates: Sequence[ScriptCandidate]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in candidates:
        summary = c.summary or ""
        if len(summary) > SUMMARY_PROMPT_CHARS:
            summary = summary[:SUMMARY_PROMPT_CHARS].rsplit(" ", 1)[0] + "…"
        out.append(
            {
                "item_id": c.item_id,
                "category": c.category,
                "source_name": c.source_name,
                "source_url": c.url,
                "title": c.title,
                "summary": summary,
            }
        )
    return out


def build_messages(
    candidates: Sequence[ScriptCandidate],
    local_date: datetime.date,
    feedback: Sequence[str] = (),
    previous: str | None = None,
) -> list[Message]:
    """System + user messages; on retry, the rejected answer and the problems to fix."""
    system, user_tpl = _sections()
    low, high = word_budget(len(candidates))
    ex = example()
    replacements = {
        "{local_date}": local_date.isoformat(),
        "{weekday}": local_date.strftime("%A"),
        "{n_stories}": str(len(candidates)),
        "{word_min}": str(low),
        "{word_max}": str(high),
        "{stories_json}": json.dumps(stories_payload(candidates), ensure_ascii=False, indent=1),
        "{example_json}": json.dumps(ex["script"], ensure_ascii=False),
    }
    user = user_tpl
    for key, value in replacements.items():
        user = user.replace(key, value)
    messages: list[Message] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if feedback:
        if previous:
            messages.append({"role": "assistant", "content": previous[:6000]})
        problems = "\n".join(f"- {p}" for p in feedback)
        messages.append(
            {
                "role": "user",
                "content": f"That answer was rejected. Fix every problem and return the full "
                f"corrected JSON:\n{problems}",
            }
        )
    return messages
