"""Shared pytest fixtures for the Chronos-AI test suite.

Key fixtures
------------
``tmp_db_url``      — ``sqlite:///`` URL pointing at a temp *file* (never ``:memory:``).
``test_settings``   — :class:`~chronos.config.Settings` with env=test, temp DB, fake secret.
``client``          — :class:`~fastapi.testclient.TestClient` backed by ``test_settings``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from chronos.config import Settings, reset_settings
from chronos.main import create_app
from chronos.observability import reset_for_testing
from chronos.ratelimit import reset_all as reset_rate_limiters
from chronos.wake.rules import reset_rules_cache


def _run_migrations(db_url: str) -> None:
    """Apply all Alembic migrations to *db_url* (runs ``upgrade head``)."""
    ini_path = Path(__file__).parents[1] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def make_settings(**overrides: object) -> Settings:
    """Build a :class:`Settings` via ``model_validate``; avoids pyright ``call-arg``
    errors caused by pydantic-settings dynamic field injection.
    """
    base: dict[str, object] = {
        "env": "test",
        "secret_key": "test-secret-key-32-bytes-exactly!",
        "database_url": "sqlite:///data/test.db",
    }
    base.update(overrides)
    return Settings.model_validate(base)


@pytest.fixture(autouse=True)
def _reset_globals() -> None:
    """Reset module-level singletons between tests."""
    reset_settings()
    reset_for_testing()
    reset_rate_limiters()
    reset_rules_cache()


@pytest.fixture()
def tmp_db_url(tmp_path: Path) -> str:
    """Return a ``sqlite:///`` URL pointing at a temp file (never ``:memory:``)."""
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture()
def test_settings(tmp_db_url: str) -> Settings:
    """Minimal valid :class:`Settings` with ``env=test`` and a migrated temp-file DB."""
    _run_migrations(tmp_db_url)
    return make_settings(database_url=tmp_db_url)


@pytest.fixture()
def client(test_settings: Settings) -> Iterator[TestClient]:
    """Synchronous :class:`~fastapi.testclient.TestClient` for the app.

    The app is created fresh for each test via :func:`~chronos.main.create_app`.
    ``raise_server_exceptions=False`` lets us assert on 5xx responses instead
    of having the test itself raise.
    """
    app = create_app(settings=test_settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
