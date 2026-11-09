"""Ingest orchestration: fetch, parse, and track source health (T3.2).

Public API
----------
- :class:`SourceFetchOutcome`
- :class:`IngestReport`
- :func:`fetch_source`
- :func:`fetch_all`

T3.2 keeps parsed items in memory; persisting them to ``items`` is T3.3.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, cast

import structlog
from sqlmodel import Session, col, select

from chronos.content.fetchers import ParsedItem, arxiv, hackernews, hf_daily_papers, rss
from chronos.content.sources import SourcesConfig
from chronos.enums import SourceKind
from chronos.http import FetchError, GuardedHttpClient
from chronos.models.content import Source

log = structlog.get_logger(__name__)

FEED_CONTENT_TYPES = frozenset(
    {
        "application/xml",
        "text/xml",
        "application/json",
        "application/atom+xml",
        "text/html",
        "application/rss+xml",
    }
)

#: Algolia item lookups run concurrently, bounded so the HN source stays inside the
#: ingest budget (30 items x 5 s worst case serially would be 150 s).
HN_LOOKUP_CONCURRENCY = 8
HN_LOOKUP_TIMEOUT_SEC = 5.0

Parser = Callable[[bytes, str], list[ParsedItem]]

_PARSERS: dict[SourceKind, Parser] = {
    SourceKind.rss: rss.parse,
    SourceKind.hf_api: hf_daily_papers.parse,
    SourceKind.arxiv_api: arxiv.parse,
    SourceKind.hn_rss: hackernews.parse,
}

OutcomeStatus = Literal["ok", "not_modified", "error"]


@dataclass(frozen=True, slots=True)
class SourceFetchOutcome:
    """Result of fetching a single source."""

    source_key: str
    status: OutcomeStatus
    items: list[ParsedItem]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Aggregated result of fetching all enabled sources."""

    outcomes: list[SourceFetchOutcome]
    total_items: int


async def fetch_source(
    client: GuardedHttpClient,
    source: Source,
    config: SourcesConfig,
    now: str,
) -> SourceFetchOutcome:
    """Fetch a single source with conditional GET, parse, and update its health fields.

    Mutates the health columns on *source* in memory only; :func:`fetch_all` commits them
    in a short transaction after the network call returns. Never raises.
    """
    req_headers: dict[str, str] = {}
    if source.etag:
        req_headers["If-None-Match"] = source.etag
    if source.last_modified:
        req_headers["If-Modified-Since"] = source.last_modified

    try:
        result = await client.fetch(
            source.url,
            allowed_content_types=set(FEED_CONTENT_TYPES),
            headers=req_headers,
        )
    except FetchError as exc:
        return _record_error(source, exc, now)

    if result.status == 304:
        return _record_not_modified(source, now)
    if result.status != 200:
        return _record_error(source, FetchError(f"HTTP {result.status}"), now)

    try:
        kind = SourceKind(source.kind)
    except ValueError:
        return _record_error(source, FetchError(f"Unknown kind {source.kind!r}"), now)

    try:
        items = _PARSERS[kind](result.body, now)
    except Exception as exc:  # untrusted feed bytes: any parser failure is a source error
        return _record_error(source, exc, now)

    if kind == SourceKind.hn_rss:
        items = await _apply_hn_points(client, source, items)

    source.last_fetched_at = now
    source.last_status = "200"
    source.consecutive_failures = 0
    source.last_error = None
    source.etag = result.headers.get("etag")
    source.last_modified = result.headers.get("last-modified")
    return SourceFetchOutcome(source_key=source.key, status="ok", items=items)


async def fetch_all(
    client: GuardedHttpClient,
    session: Session,
    config: SourcesConfig,
    now: str,
) -> IngestReport:
    """Fetch every enabled source; one failing source never aborts the pass.

    Each source's health update is committed right after its own fetch returns, so no
    transaction is held open across network I/O.
    """
    enabled = session.exec(select(Source).where(Source.enabled == 1).order_by(col(Source.id))).all()
    outcomes: list[SourceFetchOutcome] = []
    for source in enabled:
        try:
            outcome = await fetch_source(client, source, config, now)
        except Exception as exc:  # defensive: fetch_source should never raise
            outcome = _record_error(source, exc, now)
        session.add(source)
        session.commit()
        log.info(
            "source_fetched",
            source=outcome.source_key,
            status=outcome.status,
            items=len(outcome.items),
            error=outcome.error,
        )
        outcomes.append(outcome)
    return IngestReport(outcomes=outcomes, total_items=sum(len(o.items) for o in outcomes))


def _record_error(source: Source, exc: Exception, now: str) -> SourceFetchOutcome:
    msg = str(exc) or type(exc).__name__
    source.last_fetched_at = now
    source.last_status = type(exc).__name__
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    source.last_error = msg[:500]
    return SourceFetchOutcome(source_key=source.key, status="error", items=[], error=msg)


def _record_not_modified(source: Source, now: str) -> SourceFetchOutcome:
    source.last_fetched_at = now
    source.last_status = "304"
    source.consecutive_failures = 0
    source.last_error = None
    return SourceFetchOutcome(source_key=source.key, status="not_modified", items=[])


async def _apply_hn_points(
    client: GuardedHttpClient,
    source: Source,
    items: list[ParsedItem],
) -> list[ParsedItem]:
    """Attach Algolia points to each HN item (best-effort) and apply ``min_points``.

    An item whose points cannot be resolved is kept with ``points: null``.
    """
    semaphore = asyncio.Semaphore(HN_LOOKUP_CONCURRENCY)

    async def lookup(item: ParsedItem) -> int | None:
        if not item.external_id:
            return None
        async with semaphore:
            return await _hn_lookup_points(client, item.external_id)

    points_list = await asyncio.gather(*(lookup(item) for item in items))
    kept: list[ParsedItem] = []
    for item, points in zip(items, points_list, strict=True):
        if source.min_points is not None and points is not None and points < source.min_points:
            continue
        kept.append(replace(item, extra_json=json.dumps({"points": points})))
    return kept


async def _hn_lookup_points(client: GuardedHttpClient, item_id: str) -> int | None:
    if not item_id.isdigit():
        return None
    url = f"https://hn.algolia.com/api/v1/items/{item_id}"
    try:
        result = await client.fetch(
            url,
            allowed_content_types={"application/json"},
            timeout_sec=HN_LOOKUP_TIMEOUT_SEC,
        )
        if result.status != 200:
            return None
        data: object = json.loads(result.body)
    except (FetchError, ValueError) as exc:
        log.debug("hn_points_lookup_failed", item_id=item_id, error=type(exc).__name__)
        return None
    if isinstance(data, dict):
        points = cast("dict[str, object]", data).get("points")
        if isinstance(points, int) and not isinstance(points, bool):
            return points
    return None
