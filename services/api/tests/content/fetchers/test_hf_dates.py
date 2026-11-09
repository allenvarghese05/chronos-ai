"""HF daily_papers publishedAt parsing (T3.3 owner decision 7)."""

from __future__ import annotations

import json

import pytest

from chronos.content.fetchers import hf_daily_papers

NOW = "2026-10-06T06:00:00.000000Z"


def _one(entry: dict[str, object]) -> str | None:
    (item,) = hf_daily_papers.parse(json.dumps([entry]).encode(), NOW)
    return item.published_at


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"paper": {"id": "p"}, "publishedAt": "2026-10-06T04:30:00.000Z"},
         "2026-10-06T04:30:00.000000Z"),
        ({"paper": {"id": "p", "publishedAt": "2026-10-05T17:58:12Z"}},
         "2026-10-05T17:58:12.000000Z"),
        ({"paper": {"id": "p"}, "publishedAt": "2026-10-06T10:00:00+05:30"},
         "2026-10-06T04:30:00.000000Z"),
        ({"paper": {"id": "p"}, "publishedAt": "2026-10-06T04:30:00"},  # naive -> UTC
         "2026-10-06T04:30:00.000000Z"),
        ({"paper": {"id": "p"}, "publishedAt": "yesterday"}, None),
        ({"paper": {"id": "p"}, "publishedAt": 1728189000}, None),
        ({"paper": {"id": "p"}}, None),
    ],
)  # fmt: skip
def test_hf_published_at(entry: dict[str, object], expected: str | None) -> None:
    assert _one(entry) == expected


def test_hf_non_list_and_non_object_entries() -> None:
    assert hf_daily_papers.parse(b'{"not": "a list"}', NOW) == []
    assert hf_daily_papers.parse(b'[1, "x", null]', NOW) == []
