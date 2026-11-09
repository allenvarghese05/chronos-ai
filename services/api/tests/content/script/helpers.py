"""Shared builders for script tests."""

from __future__ import annotations

import datetime
from typing import Any

from chronos.content.script.schema import ScriptCandidate

DAY = datetime.date(2026, 10, 6)


def candidates(n: int = 6) -> list[ScriptCandidate]:
    cats = ["ai", "ai", "tech", "world", "world", "india", "india", "tech", "world"]
    return [
        ScriptCandidate(
            item_id=100 + i,
            category=cats[i % len(cats)],
            source_key=f"src{i}",
            source_name=f"Source {i}",
            url=f"https://news.example.com/story-{i}",
            title=f"Story number {i} headline",
            summary=f"Summary sentence for story {i}. A second sentence with more detail.",
        )
        for i in range(n)
    ]


def words(n: int, stem: str = "word") -> str:
    return " ".join(f"{stem}{i}" for i in range(n))


def valid_script(cands: list[ScriptCandidate], story_words: int = 25) -> dict[str, Any]:
    """A script dict that passes validate_script for *cands* (6 x 25 + 2 x 5 = 160 words)."""
    scenes: list[dict[str, Any]] = [
        {"index": 0, "type": "cover", "title": "Morning Reel", "voiceover": words(5, "hello")}
    ]
    for i, c in enumerate(cands, start=1):
        scenes.append(
            {
                "index": i,
                "type": "story",
                "title": c.title,
                "kicker": c.category.upper(),
                "bullets": ["one point", "another point"],
                "voiceover": words(story_words, f"s{i}w"),
                "item_id": c.item_id,
                "source_name": c.source_name,
                "source_url": c.url,
            }
        )
    scenes.append(
        {"index": len(scenes), "type": "outro", "title": "That's all", "voiceover": words(5, "bye")}
    )
    return {
        "local_date": DAY.isoformat(),
        "greeting": "Good morning",
        "scenes": scenes,
        "total_words": 0,
        "tone": "calm",
    }
