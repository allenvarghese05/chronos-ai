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
from fastapi.testclient import TestClient

from chronos.config import Settings, reset_settings
from chronos.main import create_app
from chronos.observability import reset_for_testing


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


@pytest.fixture()
def tmp_db_url(tmp_path: Path) -> str:
    """Return a ``sqlite:///`` URL pointing at a temp file (never ``:memory:``)."""
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture()
def test_settings(tmp_db_url: str) -> Settings:
    """Minimal valid :class:`Settings` with ``env=test`` and a temp-file DB."""
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
