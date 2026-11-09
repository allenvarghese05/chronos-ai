"""Migration 0005: reels + reel_assets, CHECKs, reversible."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from chronos.db import create_db_engine


def _cfg(tmp_path: Path) -> tuple[Config, str]:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, url


def test_0005_up_down(tmp_path: Path) -> None:
    cfg, url = _cfg(tmp_path)
    command.upgrade(cfg, "0005")
    engine = create_db_engine(url)
    assert {"reels", "reel_assets"} <= set(inspect(engine).get_table_names())
    engine.dispose()
    command.downgrade(cfg, "0004")
    engine = create_db_engine(url)
    assert not {"reels", "reel_assets"} & set(inspect(engine).get_table_names())
    engine.dispose()


def test_0005_checks(tmp_path: Path) -> None:
    cfg, url = _cfg(tmp_path)
    command.upgrade(cfg, "0005")
    engine = create_db_engine(url)
    try:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO reel_assets(reel_id, kind, path, sha256) VALUES (1, 'video', 'x', 'h')"
            ))  # fmt: skip
    finally:
        engine.dispose()
