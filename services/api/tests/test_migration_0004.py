"""Migration 0004: nullable sources.min_points with CHECK, reversible, rows preserved."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from chronos.db import create_db_engine

_INSERT = (
    "INSERT INTO sources(key, kind, url, category, enabled, quota, weight, consecutive_failures)"
    " VALUES (:key, 'rss', 'https://example.com/feed', 'world', 1, 1, 1.0, 0)"
)


def _config(tmp_path: Path) -> tuple[Config, str]:
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, url


def _columns(url: str) -> set[str]:
    engine = create_db_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("sources")}
    finally:
        engine.dispose()


def test_0004_adds_min_points_and_downgrade_preserves_rows(tmp_path: Path) -> None:
    cfg, url = _config(tmp_path)
    command.upgrade(cfg, "0003")
    engine = create_db_engine(url)
    with engine.begin() as connection:
        connection.execute(text(_INSERT), {"key": "kept"})
    engine.dispose()
    assert "min_points" not in _columns(url)

    command.upgrade(cfg, "0004")
    assert "min_points" in _columns(url)
    engine = create_db_engine(url)
    with engine.begin() as connection:
        row = connection.execute(text("SELECT min_points FROM sources WHERE key='kept'")).one()
        assert row[0] is None
        connection.execute(text("UPDATE sources SET min_points = 150 WHERE key='kept'"))
    engine.dispose()

    command.downgrade(cfg, "0003")
    assert "min_points" not in _columns(url)
    engine = create_db_engine(url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM sources")).scalar() == 1
    engine.dispose()


def test_0004_check_rejects_negative_min_points(tmp_path: Path) -> None:
    cfg, url = _config(tmp_path)
    command.upgrade(cfg, "0004")
    engine = create_db_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text(_INSERT), {"key": "s1"})
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text("UPDATE sources SET min_points = -1 WHERE key='s1'"))
    finally:
        engine.dispose()
