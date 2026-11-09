"""HuggingFace daily_papers API parser."""

from __future__ import annotations

import datetime
import json
from typing import Any, cast

from chronos.content.fetchers import ParsedItem
from chronos.time import format_utc_iso


def _as_dict(value: object) -> dict[str, Any]:
    """Return *value* as a JSON object, or an empty dict for anything else."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _iso_utc(value: object) -> str | None:
    """HF ``publishedAt`` (ISO-8601, usually ``...Z``) to the project's UTC string format."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return format_utc_iso(dt)


def parse(body: bytes, now: str) -> list[ParsedItem]:
    """Parse the HF daily_papers JSON list.

    Each entry is ``{"paper": {...}, "thumbnail": ...}``; a bare paper object is also
    accepted. Non-object entries are skipped. The feed is untrusted, so every field is
    type-checked before use.
    """
    data: object = json.loads(body)
    if not isinstance(data, list):
        return []
    items: list[ParsedItem] = []
    for raw in cast("list[object]", data):
        entry = _as_dict(raw)
        if not entry:
            continue
        paper = _as_dict(entry.get("paper")) or entry
        paper_id = _opt_str(paper.get("id")) or ""
        upvotes = paper.get("upvotes")
        extra: dict[str, Any] = {}
        if isinstance(upvotes, int) and not isinstance(upvotes, bool):
            extra["upvotes"] = upvotes
        items.append(
            ParsedItem(
                external_id=paper_id,
                url=f"https://huggingface.co/papers/{paper_id}" if paper_id else "",
                title=_opt_str(paper.get("title")) or "",
                summary=_opt_str(paper.get("summary")),
                published_at=_iso_utc(entry.get("publishedAt"))
                or _iso_utc(paper.get("publishedAt")),
                image_url=_opt_str(entry.get("thumbnail")),
                extra_json=json.dumps(extra) if extra else None,
            )
        )
    return items
