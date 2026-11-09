"""arXiv Atom feed parser (version-stripped external_id)."""

from __future__ import annotations

import json
import re

import feedparser

from chronos.content.fetchers import ParsedItem
from chronos.content.fetchers.rss import parse_date, strip_html

_ARXIV_ID_RE = re.compile(r"arxiv\.org/abs/(\d+\.\d+)")


def parse(body: bytes, now: str) -> list[ParsedItem]:
    """Parse an arXiv Atom feed and strip version suffixes from IDs."""
    parsed = feedparser.parse(body)
    items: list[ParsedItem] = []
    for entry in parsed.get("entries", []):
        raw_id = entry.get("id", "")
        m = _ARXIV_ID_RE.search(raw_id)
        external_id = m.group(1) if m else raw_id
        title = entry.get("title", "")
        url = raw_id if raw_id.startswith("http") else f"http://arxiv.org/abs/{external_id}"
        summary = strip_html(entry.get("summary", "")) or None
        author = entry.get("author") or None
        published_at = parse_date(entry)
        categories = [t["term"] for t in entry.get("tags", []) if "term" in t]
        extra_json = json.dumps({"categories": categories}) if categories else None
        items.append(
            ParsedItem(
                external_id=external_id,
                url=url,
                title=title,
                summary=summary,
                author=author,
                published_at=published_at,
                extra_json=extra_json,
            )
        )
    return items
