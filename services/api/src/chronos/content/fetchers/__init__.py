"""Content fetchers: parse raw bytes into in-memory ParsedItem objects.

Each fetcher is a pure function ``parse(body: bytes, now: str) -> list[ParsedItem]``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedItem:
    """A single story extracted from a source, before canonicalisation/persistence."""

    external_id: str
    url: str
    title: str
    summary: str | None = None
    author: str | None = None
    published_at: str | None = None
    image_url: str | None = None
    extra_json: str | None = None
