"""Persist normalised items into ``items`` (T3.3).

Upsert by ``(source_id, external_id)`` (owner decision 5):

- definition fields (url, url_hash, title, summary, author, image_url, extra_json,
  title_simhash) are refreshed on every fetch (HN points change through the morning);
- ``fetched_at`` keeps the first-seen time;
- ``published_at`` is the feed's date when it has one; when it does not, a new row gets
  ``fetched_at`` and an existing row keeps whatever it already had (so an undated item
  does not look brand new again every morning).
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session

from chronos.content.normalise import NormalisedItem
from chronos.models.content import Item, Source, encode_simhash


def upsert_items(session: Session, source: Source, items: Iterable[NormalisedItem]) -> int:
    """Insert or refresh *items* for *source*; returns the number of rows written.

    The caller owns the transaction and commits.
    """
    if source.id is None:
        raise ValueError("source must be persisted before its items")
    count = 0
    for item in items:
        values: dict[str, object] = {
            "source_id": source.id,
            "external_id": item.external_id,
            "url": item.url,
            "url_hash": item.url_hash,
            "title": item.title,
            "summary": item.summary,
            "author": item.author,
            "published_at": item.effective_published_at,
            "fetched_at": item.fetched_at,
            "image_url": item.image_url,
            "extra_json": item.extra_json,
            "title_simhash": encode_simhash(item.title_simhash),
        }
        stmt = insert(Item).values(**values)
        refresh = {
            key: stmt.excluded[key]
            for key in (
                "url",
                "url_hash",
                "title",
                "summary",
                "author",
                "image_url",
                "extra_json",
                "title_simhash",
            )
        }
        if item.published_at is not None:
            refresh["published_at"] = stmt.excluded["published_at"]
        session.exec(
            stmt.on_conflict_do_update(index_elements=["source_id", "external_id"], set_=refresh)
        )
        count += 1
    return count
