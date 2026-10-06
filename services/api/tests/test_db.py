"""Tests for chronos.db — engine creation and PRAGMA enforcement."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from chronos.db import create_db_engine


@pytest.fixture()
def tmp_engine(tmp_path: Path) -> Iterator[Engine]:
    """SQLAlchemy engine backed by a temp-file SQLite DB (never :memory:)."""
    db_path = tmp_path / "test.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    yield engine
    engine.dispose()


class TestPragmas:
    def test_wal_mode(self, tmp_engine: Engine) -> None:
        """journal_mode must be 'wal' after engine creation."""
        with Session(tmp_engine) as session:
            result = session.execute(text("PRAGMA journal_mode")).scalar()
        assert result == "wal"

    def test_foreign_keys_on(self, tmp_engine: Engine) -> None:
        """foreign_keys must be 1 (ON) on every new connection."""
        with Session(tmp_engine) as session:
            result = session.execute(text("PRAGMA foreign_keys")).scalar()
        assert result == 1

    def test_busy_timeout(self, tmp_engine: Engine) -> None:
        """busy_timeout must be 5000 ms."""
        with Session(tmp_engine) as session:
            result = session.execute(text("PRAGMA busy_timeout")).scalar()
        assert result == 5000

    def test_pragmas_on_second_connection(self, tmp_path: Path) -> None:
        """PRAGMAs must apply to every new connection, not just the first."""
        db_path = tmp_path / "second.db"
        engine = create_db_engine(f"sqlite:///{db_path}")
        try:
            # Open two separate connections.
            for _ in range(2):
                with engine.connect() as conn:
                    result = conn.execute(text("PRAGMA journal_mode")).scalar()
                    assert result == "wal"
                    result2 = conn.execute(text("PRAGMA foreign_keys")).scalar()
                    assert result2 == 1
        finally:
            engine.dispose()


class TestCheckDb:
    def test_check_ok(self, tmp_engine: Engine) -> None:
        from chronos.db import check_db

        assert check_db(tmp_engine) == "ok"

    def test_check_error_on_bad_url(self) -> None:
        from chronos.db import check_db, create_db_engine

        bad_engine = create_db_engine("sqlite:////nonexistent/path/db.sqlite")
        result = check_db(bad_engine)
        assert result.startswith("error:")
