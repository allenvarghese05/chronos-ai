"""Golden fixture tests for all 8 source fetchers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from chronos.content.fetchers import ParsedItem, arxiv, hackernews, hf_daily_papers, rss

FIXTURES = Path(__file__).parents[2] / "fixtures" / "feeds"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _read_expected(name: str) -> list[dict[str, Any]]:
    return cast(
        "list[dict[str, Any]]", json.loads((FIXTURES / f"{name}.expected.json").read_text())
    )


def _to_dict(item: ParsedItem) -> dict[str, Any]:
    return {
        "external_id": item.external_id,
        "url": item.url,
        "title": item.title,
        "summary": item.summary,
        "author": item.author,
        "published_at": item.published_at,
        "image_url": item.image_url,
        "extra_json": item.extra_json,
    }


# ---------------------------------------------------------------------------
# Golden fixtures
# ---------------------------------------------------------------------------


def test_hf_daily_papers_golden() -> None:
    body = _read("hf_daily_papers.json")
    items = hf_daily_papers.parse(body, "now")
    expected = _read_expected("hf_daily_papers")
    assert len(items) == len(expected)
    assert _to_dict(items[0]) == expected[0]


def test_arxiv_golden() -> None:
    body = _read("arxiv_cs_ai_cl_lg.xml")
    items = arxiv.parse(body, "now")
    expected = _read_expected("arxiv_cs_ai_cl_lg")
    assert len(items) == len(expected)
    assert _to_dict(items[0]) == expected[0]


def test_hn_rss_golden() -> None:
    body = _read("hn_front.xml")
    items = hackernews.parse(body, "now")
    expected = _read_expected("hn_front")
    assert len(items) == len(expected)
    for got, exp in zip(items, expected, strict=True):
        assert _to_dict(got) == exp


def test_bbc_world_golden() -> None:
    body = _read("bbc_world.xml")
    items = rss.parse(body, "now")
    expected = _read_expected("bbc_world")
    assert len(items) == len(expected)
    assert _to_dict(items[0]) == expected[0]


def test_cbs_top_golden() -> None:
    body = _read("cbs_top.xml")
    items = rss.parse(body, "now")
    expected = _read_expected("cbs_top")
    assert len(items) == len(expected)
    assert _to_dict(items[0]) == expected[0]


def test_ie_india_golden() -> None:
    body = _read("ie_india.xml")
    items = rss.parse(body, "now")
    expected = _read_expected("ie_india")
    assert len(items) == len(expected)
    assert _to_dict(items[0]) == expected[0]


def test_ie_tech_golden() -> None:
    body = _read("ie_tech.xml")
    items = rss.parse(body, "now")
    expected = _read_expected("ie_tech")
    assert len(items) == len(expected)
    assert _to_dict(items[0]) == expected[0]


def test_ndtv_top_golden() -> None:
    body = _read("ndtv_top.xml")
    items = rss.parse(body, "now")
    expected = _read_expected("ndtv_top")
    assert len(items) == len(expected)
    assert _to_dict(items[0]) == expected[0]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_arxiv_version_stripped() -> None:
    """Edge case 9: arXiv entry id with version suffix."""
    body = _read("arxiv_cs_ai_cl_lg.xml")
    items = arxiv.parse(body, "now")
    assert items[0].external_id == "2401.12345"


def test_rss_html_stripped() -> None:
    """Edge case 13: HTML in description is stripped."""
    body = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <guid>x</guid>
      <title>T</title>
      <link>https://example.com</link>
      <description>&lt;script&gt;alert(1)&lt;/script&gt;&lt;p&gt;Hello world&lt;/p&gt;</description>
    </item>
  </channel>
</rss>"""
    items = rss.parse(body, "now")
    assert items[0].summary == "Hello world"


def test_rss_missing_date() -> None:
    """Edge case 12: missing published date → published_at None."""
    body = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <guid>no-date</guid>
      <title>T</title>
      <link>https://example.com</link>
    </item>
  </channel>
</rss>"""
    items = rss.parse(body, "now")
    assert items[0].published_at is None


def test_rss_duplicate_guid() -> None:
    """Edge case 14: duplicate guid in one feed."""
    body = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item><guid>dup</guid><title>A</title><link>https://a</link></item>
    <item><guid>dup</guid><title>B</title><link>https://b</link></item>
  </channel>
</rss>"""
    items = rss.parse(body, "now")
    assert len(items) == 2
    assert items[0].title == "A"
    assert items[1].title == "B"
