"""Real SQLite migration preservation, content FK/CHECKs and signed hash storage."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from chronos.db import create_db_engine
from chronos.models import Brief, BriefItem, Item, Job, Source
from chronos.models.content import decode_simhash, encode_simhash

NEW = {"sources", "items", "briefs", "brief_items", "jobs"}
LATER = {"reels", "reel_assets"}  # 0005 (T4.1); head includes them
NOW = "2026-11-01T06:00:00.000000Z"


def config(tmp_path: Path) -> tuple[Config, str]:
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, url


def test_populated_previous_head_upgrade_down_preserves_rows(tmp_path: Path) -> None:
    cfg, url = config(tmp_path)
    command.upgrade(cfg, "08a434829bca")
    engine = create_db_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO settings(key,value,updated_at,updated_by)"
                " VALUES ('sentinel','42',:now,'test')"
            ),
            {"now": NOW},
        )
    before = set(inspect(engine).get_table_names())
    command.upgrade(cfg, "head")
    assert set(inspect(engine).get_table_names()) == before | NEW | LATER
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), SQLModel.metadata) == []
    command.downgrade(cfg, "08a434829bca")
    assert set(inspect(engine).get_table_names()) == before
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT value FROM settings WHERE key='sentinel'")).scalar()
            == "42"
        )
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    engine.dispose()


@pytest.mark.parametrize("value", [0, 1, 2**63 - 1, 2**63, 2**64 - 1])
def test_unsigned_hash_reversible_in_actual_sqlite(tmp_path: Path, value: int) -> None:
    cfg, url = config(tmp_path)
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    with Session(engine) as session:
        source = Source(key="source", kind="rss", category="world", url="https://example.com/rss")
        session.add(source)
        session.flush()
        assert source.id is not None
        item = Item(
            source_id=source.id,
            external_id="id",
            url="https://example.com/story",
            url_hash="hash",
            title="Story",
            fetched_at=NOW,
            title_simhash=encode_simhash(value),
        )
        session.add(item)
        session.commit()
        loaded = session.exec(select(Item)).one()
        assert decode_simhash(loaded.title_simhash) == value
    engine.dispose()


@pytest.mark.parametrize("value", [-1, 2**64, True])
def test_unsigned_hash_rejects_invalid(value: int) -> None:
    with pytest.raises(ValueError):
        encode_simhash(value)


@pytest.mark.parametrize("value", [-(2**63) - 1, 2**63, True])
def test_signed_hash_rejects_invalid(value: int) -> None:
    with pytest.raises(ValueError):
        decode_simhash(value)


def test_content_foreign_keys_uniqueness_and_restrict(tmp_path: Path) -> None:
    cfg, url = config(tmp_path)
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    with Session(engine) as session:
        source = Source(key="rss", kind="rss", category="ai", url="https://example.com")
        session.add(source)
        session.commit()
        assert source.id is not None
        item = Item(
            source_id=source.id,
            external_id="a",
            url="https://example.com/a",
            url_hash="h",
            title="a",
            fetched_at=NOW,
        )
        session.add(item)
        session.commit()
        session.add(
            Item(
                source_id=source.id,
                external_id="a",
                url="x",
                url_hash="x",
                title="x",
                fetched_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.delete(source)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(Brief(local_date="2026-11-01", device_id=999, created_at=NOW, updated_at=NOW))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(BriefItem(brief_id=999, item_id=999, scene_index=0))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    engine.dispose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "shell"),
        ("status", "unknown"),
        ("attempts", -1),
        ("max_attempts", 0),
        ("timeout_sec", 0),
        ("priority", 101),
        ("payload_json", "x" * 16385),
    ],
)
def test_job_database_constraints(tmp_path: Path, field: str, value: object) -> None:
    cfg, url = config(tmp_path)
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    row = Job(kind="ingest", key="day", payload_json="{}", run_after=NOW, created_at=NOW)
    setattr(row, field, value)
    with Session(engine) as session:
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()
