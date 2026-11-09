"""Tests for chronos.content.ingest — conditional GET, health, failure isolation."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlmodel import Session, select

from chronos.config import Settings
from chronos.content.ingest import fetch_all, fetch_source
from chronos.content.sources import (
    HttpConfig,
    SelectionConfig,
    SourcesConfig,
)
from chronos.http import GuardedHttpClient
from chronos.models.content import Source
from chronos.time import utc_now_iso

_NOW = utc_now_iso()


async def _fake_resolver(host: str) -> list[str]:
    return ["1.2.3.4"]


def _source(**kwargs: object) -> Source:
    defaults: dict[str, object] = {
        "key": "test",
        "kind": "rss",
        "url": "https://example.com/feed",
        "category": "ai",
        "enabled": 1,
        "quota": 1,
        "weight": 1.0,
    }
    defaults.update(kwargs)
    return Source(**defaults)  # type: ignore[arg-type]


def _config() -> SourcesConfig:
    return SourcesConfig(
        version=1,
        http=HttpConfig(user_agent="test", timeout_sec=15, max_bytes=2_000_000, max_redirects=3),
        sources=[],
        selection=SelectionConfig(
            total_stories=6, category_min={"ai": 2}, freshness_hours=20, cross_source_boost=0.25
        ),
    )


# ---------------------------------------------------------------------------
# Conditional GET / 304
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_conditional_get_304(test_settings: Settings, tmp_db_url: str) -> None:
    """Edge case 6: 304 returns empty items, resets failures, updates health."""
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304, headers={"etag": '"abc"'})

    client = GuardedHttpClient(
        user_agent="test",
        transport=httpx.MockTransport(handler),
        resolver=_fake_resolver,
    )
    with Session(engine) as session:
        s = _source(key="s1", kind="rss", url="https://h/feed", etag='"abc"')
        session.add(s)
        session.commit()

        outcome = await fetch_source(client, s, _config(), _NOW)
        assert outcome.status == "not_modified"
        assert outcome.items == []
        assert seen[0].headers["if-none-match"] == '"abc"'
        assert s.last_status == "304"
        assert s.consecutive_failures == 0
        assert s.last_fetched_at == _NOW


@pytest.mark.anyio
async def test_conditional_get_200_resets_failures(
    test_settings: Settings, tmp_db_url: str
) -> None:
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)

    body = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><item><guid>x</guid><title>T</title><link>https://x</link></item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/rss+xml"})

    client = GuardedHttpClient(
        user_agent="test",
        transport=httpx.MockTransport(handler),
        resolver=_fake_resolver,
    )
    with Session(engine) as session:
        s = _source(key="s1", kind="rss", url="https://h/feed", consecutive_failures=3)
        session.add(s)
        session.commit()

        outcome = await fetch_source(client, s, _config(), _NOW)
        assert outcome.status == "ok"
        assert s.consecutive_failures == 0
        assert s.last_status == "200"


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_one_source_failure_does_not_abort_others(
    test_settings: Settings, tmp_db_url: str
) -> None:
    """Edge case 8: one failing source, one succeeding."""
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)

    ok_body = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><item><guid>x</guid><title>T</title><link>https://x</link></item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ok.example":
            return httpx.Response(
                200, content=ok_body, headers={"content-type": "application/rss+xml"}
            )
        raise httpx.ConnectError("simulated failure")

    client = GuardedHttpClient(
        user_agent="test",
        transport=httpx.MockTransport(handler),
        resolver=_fake_resolver,
    )
    with Session(engine) as session:
        s1 = _source(key="s1", kind="rss", url="https://fail.example/feed")
        s2 = _source(key="s2", kind="rss", url="https://ok.example/feed")
        session.add(s1)
        session.add(s2)
        session.commit()

        report = await fetch_all(client, session, _config(), _NOW)
        assert len(report.outcomes) == 2
        assert report.outcomes[0].status == "error"
        assert report.outcomes[1].status == "ok"
        assert report.outcomes[1].items
        assert s1.consecutive_failures == 1
        assert s2.consecutive_failures == 0


# ---------------------------------------------------------------------------
# HN points / min_points
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_hn_min_points_filter(test_settings: Settings, tmp_db_url: str) -> None:
    """Edge case 11: HN item with points 42 < 150 excluded."""
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)

    rss_body = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Low Points</title>
      <link>https://example.com</link>
      <comments>https://news.ycombinator.com/item?id=99999</comments>
      <pubDate>Mon, 06 Oct 2026 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "algolia" in request.url.host:
            return httpx.Response(
                200, content=b'{"points":42}', headers={"content-type": "application/json"}
            )
        return httpx.Response(
            200, content=rss_body, headers={"content-type": "application/rss+xml"}
        )

    client = GuardedHttpClient(
        user_agent="test",
        transport=httpx.MockTransport(handler),
        resolver=_fake_resolver,
    )
    with Session(engine) as session:
        s = _source(key="hn", kind="hn_rss", url="https://h/feed", min_points=150)
        session.add(s)
        session.commit()

        outcome = await fetch_source(client, s, _config(), _NOW)
        assert outcome.status == "ok"
        assert outcome.items == []


@pytest.mark.anyio
async def test_hn_algolia_timeout_accepts_item(test_settings: Settings, tmp_db_url: str) -> None:
    """Edge case 10: Algolia lookup fails, item accepted anyway."""
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)

    rss_body = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Unknown Points</title>
      <link>https://example.com</link>
      <comments>https://news.ycombinator.com/item?id=99998</comments>
      <pubDate>Mon, 06 Oct 2026 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "algolia" in request.url.host:
            raise httpx.TimeoutException("timed out")
        return httpx.Response(
            200, content=rss_body, headers={"content-type": "application/rss+xml"}
        )

    client = GuardedHttpClient(
        user_agent="test",
        transport=httpx.MockTransport(handler),
        resolver=_fake_resolver,
    )
    with Session(engine) as session:
        s = _source(key="hn", kind="hn_rss", url="https://h/feed", min_points=150)
        session.add(s)
        session.commit()

        outcome = await fetch_source(client, s, _config(), _NOW)
        assert outcome.status == "ok"
        assert len(outcome.items) == 1
        assert outcome.items[0].external_id == "99998"


# ---------------------------------------------------------------------------
# Enabled / disabled
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_disabled_source_skipped(test_settings: Settings, tmp_db_url: str) -> None:
    """Edge case 19: enabled=0 skipped."""
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        s = _source(key="s1", kind="rss", enabled=0)
        session.add(s)
        session.commit()

        client = GuardedHttpClient(user_agent="test")
        report = await fetch_all(client, session, _config(), _NOW)
        assert report.total_items == 0
        assert len(report.outcomes) == 0


# ---------------------------------------------------------------------------
# Persistence, parse failures, content type (ingest level)
# ---------------------------------------------------------------------------

_RSS_ONE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><item><guid>x</guid><title>T</title><link>https://x</link></item></channel></rss>"""


@pytest.mark.anyio
async def test_fetch_all_commits_health_and_validators(
    test_settings: Settings, tmp_db_url: str
) -> None:
    """Health columns written by fetch_all survive in a fresh session (committed)."""
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_RSS_ONE,
            headers={
                "content-type": "application/rss+xml",
                "etag": '"v2"',
                "last-modified": "Mon, 06 Oct 2026 08:00:00 GMT",
            },
        )

    client = GuardedHttpClient(
        user_agent="test", transport=httpx.MockTransport(handler), resolver=_fake_resolver
    )
    with Session(engine) as session:
        session.add(_source(key="s1", url="https://h/feed", consecutive_failures=2))
        session.commit()
        await fetch_all(client, session, _config(), _NOW)

    with Session(engine) as fresh:
        row = fresh.exec(select(Source).where(Source.key == "s1")).one()
        assert row.etag == '"v2"'
        assert row.last_modified == "Mon, 06 Oct 2026 08:00:00 GMT"
        assert row.consecutive_failures == 0
        assert row.last_status == "200"
        assert row.last_fetched_at == _NOW


@pytest.mark.anyio
async def test_fetch_all_commits_failure_count(test_settings: Settings, tmp_db_url: str) -> None:
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = GuardedHttpClient(
        user_agent="test", transport=httpx.MockTransport(handler), resolver=_fake_resolver
    )
    with Session(engine) as session:
        session.add(_source(key="s1", url="https://h/feed", consecutive_failures=4))
        session.commit()
        await fetch_all(client, session, _config(), _NOW)

    with Session(engine) as fresh:
        row = fresh.exec(select(Source).where(Source.key == "s1")).one()
        assert row.consecutive_failures == 5
        assert row.last_status == "FetchFailed"
        assert row.last_error is not None


@pytest.mark.anyio
async def test_content_type_missing_rejected(test_settings: Settings, tmp_db_url: str) -> None:
    """Edge case 5: 200 without a content-type is recorded as a source error."""
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_RSS_ONE)

    client = GuardedHttpClient(
        user_agent="test", transport=httpx.MockTransport(handler), resolver=_fake_resolver
    )
    with Session(engine) as session:
        s = _source(key="s1", url="https://h/feed")
        session.add(s)
        session.commit()
        outcome = await fetch_source(client, s, _config(), _NOW)
        assert outcome.status == "error"
        assert s.last_status == "ContentTypeRejected"
        assert s.consecutive_failures == 1


@pytest.mark.anyio
async def test_parser_failure_recorded_as_error(test_settings: Settings, tmp_db_url: str) -> None:
    """Malformed JSON from an hf_api source is a source error, not a crash."""
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"{not json", headers={"content-type": "application/json"}
        )

    client = GuardedHttpClient(
        user_agent="test", transport=httpx.MockTransport(handler), resolver=_fake_resolver
    )
    with Session(engine) as session:
        s = _source(key="hf", kind="hf_api", url="https://h/api")
        session.add(s)
        session.commit()
        outcome = await fetch_source(client, s, _config(), _NOW)
        assert outcome.status == "error"
        assert s.consecutive_failures == 1


@pytest.mark.anyio
async def test_non_200_status_recorded_as_error(test_settings: Settings, tmp_db_url: str) -> None:
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"busy", headers={"content-type": "text/html"})

    client = GuardedHttpClient(
        user_agent="test", transport=httpx.MockTransport(handler), resolver=_fake_resolver
    )
    with Session(engine) as session:
        s = _source(key="s1", url="https://h/feed")
        session.add(s)
        session.commit()
        outcome = await fetch_source(client, s, _config(), _NOW)
        assert outcome.status == "error"
        assert outcome.error == "HTTP 503"


# ---------------------------------------------------------------------------
# HN lookups: points recorded, concurrency bounded
# ---------------------------------------------------------------------------


def _hn_feed(n: int) -> bytes:
    entries = "".join(
        f"<item><title>Item {i}</title><link>https://e{i}.example</link>"
        f"<comments>https://news.ycombinator.com/item?id={1000 + i}</comments></item>"
        for i in range(n)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{entries}</channel></rss>'.encode()


@pytest.mark.anyio
async def test_hn_points_recorded_and_lookups_concurrent(
    test_settings: Settings, tmp_db_url: str
) -> None:
    from chronos.content.ingest import HN_LOOKUP_CONCURRENCY
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)
    in_flight = 0
    peak = 0

    class SlowAlgolia(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            if "algolia" not in request.url.host:
                return httpx.Response(
                    200, content=_hn_feed(20), headers={"content-type": "application/rss+xml"}
                )
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            item_id = int(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(
                200,
                content=json.dumps({"points": item_id}).encode(),
                headers={"content-type": "application/json"},
            )

    client = GuardedHttpClient(user_agent="test", transport=SlowAlgolia(), resolver=_fake_resolver)
    with Session(engine) as session:
        s = _source(key="hn", kind="hn_rss", url="https://h/feed", min_points=1010)
        session.add(s)
        session.commit()
        outcome = await fetch_source(client, s, _config(), _NOW)

    assert 1 < peak <= HN_LOOKUP_CONCURRENCY
    # ids 1000..1019 -> points equal to id; min_points 1010 keeps 1010..1019 in order
    assert [i.external_id for i in outcome.items] == [str(n) for n in range(1010, 1020)]
    assert outcome.items[0].extra_json is not None
    assert json.loads(outcome.items[0].extra_json) == {"points": 1010}
