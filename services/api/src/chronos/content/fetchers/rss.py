"""Generic RSS/Atom feed parser using feedparser + selectolax."""

from __future__ import annotations

import datetime
from typing import Any

import feedparser
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from chronos.content.fetchers import ParsedItem
from chronos.time import format_utc_iso


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    try:
        parsed = HTMLParser(raw)
        text = parsed.body.text(separator=" ") if parsed.body else ""
    except Exception:
        # Fallback for malformed HTML
        text = raw
    return " ".join(text.split())


def _extract_image(entry: Any) -> str | None:
    # media:content
    if "media_content" in entry:
        for m in entry.media_content:
            if m.get("medium") == "image" or str(m.get("type", "")).startswith("image/"):
                return m.get("url")
    # media:thumbnail
    if "media_thumbnail" in entry:
        for m in entry.media_thumbnail:
            url = m.get("url")
            if url:
                return url
    # enclosures
    if "enclosures" in entry:
        for e in entry.enclosures:
            if str(e.get("type", "")).startswith("image/"):
                return e.get("url")
    return None


def parse_date(entry: Any) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            dt = datetime.datetime(*parsed[:6], tzinfo=datetime.UTC)
            return format_utc_iso(dt)
        except (TypeError, ValueError):
            pass
    return None


def parse(body: bytes, now: str) -> list[ParsedItem]:
    """Parse *body* as an RSS/Atom feed and return items."""
    parsed = feedparser.parse(body)
    items: list[ParsedItem] = []
    for entry in parsed.get("entries", []):
        external_id = (
            entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title", "")
        )
        title = entry.get("title", "")
        url = entry.get("link", "")
        summary = strip_html(entry.get("summary", "")) or None
        author = entry.get("author") or None
        published_at = parse_date(entry)
        image_url = _extract_image(entry)
        items.append(
            ParsedItem(
                external_id=external_id,
                url=url,
                title=title,
                summary=summary,
                author=author,
                published_at=published_at,
                image_url=image_url,
            )
        )
    return items
