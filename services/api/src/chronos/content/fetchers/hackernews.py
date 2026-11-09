"""Hacker News RSS parser (points lookup lives in ingest.py)."""

from __future__ import annotations

import re

import feedparser

from chronos.content.fetchers import ParsedItem
from chronos.content.fetchers.rss import parse_date

_HN_ITEM_RE = re.compile(r"id=(\d+)")


def parse(body: bytes, now: str) -> list[ParsedItem]:
    """Parse the HN RSS feed and extract item IDs from comments links."""
    parsed = feedparser.parse(body)
    items: list[ParsedItem] = []
    for entry in parsed.get("entries", []):
        title = entry.get("title", "")
        url = entry.get("link", "")
        comments_url = entry.get("comments", "")
        external_id = ""
        m = _HN_ITEM_RE.search(comments_url)
        if m:
            external_id = m.group(1)
        if not external_id:
            # Fallback: try to extract from link
            m2 = _HN_ITEM_RE.search(url)
            if m2:
                external_id = m2.group(1)
        published_at = parse_date(entry)
        items.append(
            ParsedItem(
                external_id=external_id,
                url=url,
                title=title,
                published_at=published_at,
            )
        )
    return items
