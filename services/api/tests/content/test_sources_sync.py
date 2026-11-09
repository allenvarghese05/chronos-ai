"""Tests for chronos.content.sources.sync_sources DB behaviour."""

from __future__ import annotations

from sqlmodel import Session, select

from chronos.config import Settings
from chronos.content.sources import (
    HttpConfig,
    SelectionConfig,
    SourceDef,
    SourcesConfig,
    sync_sources,
)
from chronos.enums import ContentCategory, SourceKind
from chronos.models.content import Source


def _make_config(sources: list[SourceDef]) -> SourcesConfig:
    return SourcesConfig(
        version=1,
        http=HttpConfig(user_agent="test", timeout_sec=15, max_bytes=2_000_000, max_redirects=3),
        sources=sources,
        selection=SelectionConfig(
            total_stories=6, category_min={"ai": 2}, freshness_hours=20, cross_source_boost=0.25
        ),
    )


def test_sync_creates_new_sources(test_settings: Settings, tmp_db_url: str) -> None:
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        cfg = _make_config(
            [
                SourceDef(
                    key="s1",
                    kind=SourceKind.rss,
                    url="https://a",
                    category=ContentCategory.ai,
                    quota=1,
                    weight=1.0,
                    min_points=None,
                ),
            ]
        )
        sync_sources(session, cfg, "2026-01-01T00:00:00Z")
        session.commit()
        rows = session.exec(select(Source)).all()
        assert len(rows) == 1
        assert rows[0].key == "s1"
        assert rows[0].enabled == 1


def test_sync_updates_definition_fields(test_settings: Settings, tmp_db_url: str) -> None:
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        cfg = _make_config(
            [
                SourceDef(
                    key="s1",
                    kind=SourceKind.rss,
                    url="https://a",
                    category=ContentCategory.ai,
                    quota=1,
                    weight=1.0,
                    min_points=None,
                ),
            ]
        )
        sync_sources(session, cfg, "2026-01-01T00:00:00Z")
        session.commit()

        cfg2 = _make_config(
            [
                SourceDef(
                    key="s1",
                    kind=SourceKind.rss,
                    url="https://b",
                    category=ContentCategory.tech,
                    quota=2,
                    weight=0.5,
                    min_points=100,
                ),
            ]
        )
        sync_sources(session, cfg2, "2026-01-01T00:00:00Z")
        session.commit()
        row = session.exec(select(Source).where(Source.key == "s1")).one()
        assert row.url == "https://b"
        assert row.category == "tech"
        assert row.quota == 2
        assert row.weight == 0.5
        assert row.min_points == 100


def test_sync_preserves_health_fields(test_settings: Settings, tmp_db_url: str) -> None:
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        cfg = _make_config(
            [
                SourceDef(
                    key="s1",
                    kind=SourceKind.rss,
                    url="https://a",
                    category=ContentCategory.ai,
                    quota=1,
                    weight=1.0,
                    min_points=None,
                ),
            ]
        )
        sync_sources(session, cfg, "2026-01-01T00:00:00Z")
        session.commit()
        # Manually set health fields
        row = session.exec(select(Source).where(Source.key == "s1")).one()
        row.etag = '"abc"'
        row.consecutive_failures = 3
        row.last_error = "timeout"
        session.add(row)
        session.commit()

        sync_sources(session, cfg, "2026-01-01T00:00:00Z")
        session.commit()
        row = session.exec(select(Source).where(Source.key == "s1")).one()
        assert row.etag == '"abc"'
        assert row.consecutive_failures == 3
        assert row.last_error == "timeout"


def test_sync_disables_removed_keys(test_settings: Settings, tmp_db_url: str) -> None:
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        cfg = _make_config(
            [
                SourceDef(
                    key="s1",
                    kind=SourceKind.rss,
                    url="https://a",
                    category=ContentCategory.ai,
                    quota=1,
                    weight=1.0,
                    min_points=None,
                ),
                SourceDef(
                    key="s2",
                    kind=SourceKind.rss,
                    url="https://b",
                    category=ContentCategory.tech,
                    quota=1,
                    weight=1.0,
                    min_points=None,
                ),
            ]
        )
        sync_sources(session, cfg, "2026-01-01T00:00:00Z")
        session.commit()

        cfg2 = _make_config(
            [
                SourceDef(
                    key="s1",
                    kind=SourceKind.rss,
                    url="https://a",
                    category=ContentCategory.ai,
                    quota=1,
                    weight=1.0,
                    min_points=None,
                ),
            ]
        )
        sync_sources(session, cfg2, "2026-01-01T00:00:00Z")
        session.commit()
        rows = {r.key: r for r in session.exec(select(Source)).all()}
        assert rows["s1"].enabled == 1
        assert rows["s2"].enabled == 0


def test_sync_does_not_delete_removed_keys(test_settings: Settings, tmp_db_url: str) -> None:
    from chronos.db import create_db_engine

    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        cfg = _make_config(
            [
                SourceDef(
                    key="s1",
                    kind=SourceKind.rss,
                    url="https://a",
                    category=ContentCategory.ai,
                    quota=1,
                    weight=1.0,
                    min_points=None,
                ),
            ]
        )
        sync_sources(session, cfg, "2026-01-01T00:00:00Z")
        session.commit()

        cfg2 = _make_config([])
        sync_sources(session, cfg2, "2026-01-01T00:00:00Z")
        session.commit()
        row = session.exec(select(Source).where(Source.key == "s1")).one_or_none()
        assert row is not None
        assert row.enabled == 0
