"""chronos.content.store.upsert_items against a migrated SQLite DB."""

from __future__ import annotations

import dataclasses

import pytest
from sqlmodel import Session, select

from chronos.config import Settings
from chronos.content.fetchers import ParsedItem
from chronos.content.normalise import NormalisedItem, normalise
from chronos.content.store import upsert_items
from chronos.db import create_db_engine
from chronos.models.content import Item, Source, decode_simhash

DAY1 = "2026-10-06T05:30:00.000000Z"
DAY2 = "2026-10-07T05:30:00.000000Z"


def _norm(**kw: str | None) -> NormalisedItem:
    base: dict[str, str | None] = {
        "external_id": "g1",
        "url": "https://a.com/x?utm_source=rss",
        "title": "First headline about something",
        "summary": "s1",
        "author": None,
        "published_at": None,
        "image_url": None,
        "extra_json": '{"points": 10}',
    }
    now = kw.pop("now", DAY1) or DAY1
    base.update(kw)
    n = normalise(ParsedItem(**base), now)  # type: ignore[arg-type]
    assert n is not None
    return n


def _source(session: Session) -> Source:
    s = Source(key="s1", kind="rss", url="https://a.com/feed", category="world")
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def test_insert_then_refresh_keeps_id_and_first_seen(
    test_settings: Settings, tmp_db_url: str
) -> None:
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        src = _source(session)
        assert upsert_items(session, src, [_norm()]) == 1
        session.commit()
        first = session.exec(select(Item)).one()
        first_id = first.id
        assert first.published_at == DAY1  # edge case 9 fallback on insert
        assert first.fetched_at == DAY1

        # edge case 17: next morning, same item, points changed
        upsert_items(
            session,
            src,
            [_norm(now=DAY2, title="First headline, updated", extra_json='{"points": 99}')],
        )
        session.commit()
        session.expire_all()
        rows = session.exec(select(Item)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == first_id
        assert row.title == "First headline, updated"
        assert row.extra_json == '{"points": 99}'
        assert row.fetched_at == DAY1  # first-seen kept
        assert row.published_at == DAY1  # undated item does not become "new" again


def test_real_published_date_overrides_fallback(test_settings: Settings, tmp_db_url: str) -> None:
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        src = _source(session)
        upsert_items(session, src, [_norm()])
        upsert_items(session, src, [_norm(now=DAY2, published_at="2026-10-06T01:00:00.000000Z")])
        session.commit()
        session.expire_all()
        assert session.exec(select(Item)).one().published_at == "2026-10-06T01:00:00.000000Z"


def test_simhash_high_bit_round_trips(test_settings: Settings, tmp_db_url: str) -> None:
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        src = _source(session)
        item = dataclasses.replace(_norm(), title_simhash=2**64 - 1)
        upsert_items(session, src, [item])
        session.commit()
        stored = session.exec(select(Item)).one().title_simhash
        assert stored < 0  # signed storage convention
        assert decode_simhash(stored) == 2**64 - 1


def test_distinct_sources_same_external_id_coexist(
    test_settings: Settings, tmp_db_url: str
) -> None:
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        a = _source(session)
        b = Source(key="s2", kind="rss", url="https://b.com/feed", category="world")
        session.add(b)
        session.commit()
        session.refresh(b)
        upsert_items(session, a, [_norm()])
        upsert_items(session, b, [_norm()])
        session.commit()
        assert len(session.exec(select(Item)).all()) == 2


def test_unsaved_source_rejected(test_settings: Settings, tmp_db_url: str) -> None:
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session, pytest.raises(ValueError):
        upsert_items(session, Source(key="x", kind="rss", url="u", category="ai"), [_norm()])
