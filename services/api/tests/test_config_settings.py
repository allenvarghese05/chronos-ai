"""Projected settings are atomic, typed and secret-free."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlmodel import Session, select

from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.main import create_app
from chronos.models.settings import Setting
from tests.test_config_wake import auth_headers, insert_device_for_test


def test_preferences_subset_preserves_other_keys(test_settings: Settings) -> None:
    token, _ = insert_device_for_test(test_settings)
    engine = create_db_engine(test_settings.database_url)
    with Session(engine) as session:
        session.add(
            Setting(key="unrelated", value="preserved", updated_at="now", updated_by="test")
        )
        session.commit()
    with TestClient(create_app(test_settings)) as client:
        before = client.get("/v1/config", headers=auth_headers(token)).json()
        assert before["theme"] == "auto" and before["render_mode"] == "eager"
        response = client.patch(
            "/v1/config", headers=auth_headers(token), json={"theme": "ink", "pexels_enabled": True}
        )
        assert response.status_code == 200
        saved = response.json()
        assert saved["theme"] == "ink" and saved["pexels_effective"] is False
        assert saved["origins"] == {"theme": "db", "render_mode": "default", "pexels_enabled": "db"}
        assert saved["rendering_available"] is False
        assert client.get("/v1/config", headers=auth_headers(token)).json() == saved
    with Session(engine) as session:
        assert (
            session.exec(select(Setting).where(Setting.key == "unrelated")).one().value
            == "preserved"
        )


@pytest.mark.parametrize(
    "body",
    [
        {"theme": None},
        {"pexels_enabled": "true"},
        {"secret_key": "bad"},
        {"theme": "bad", "render_mode": "on_trigger"},
        {"render_mode": None},
    ],
)
def test_invalid_patch_leaves_everything_unchanged(
    test_settings: Settings, body: dict[str, object]
) -> None:
    token, _ = insert_device_for_test(test_settings)
    with TestClient(create_app(test_settings)) as client:
        before = client.get("/v1/config", headers=auth_headers(token)).json()
        assert client.patch("/v1/config", headers=auth_headers(token), json=body).status_code == 422
        assert client.get("/v1/config", headers=auth_headers(token)).json() == before


def test_projection_bounds_and_secret_capability(test_settings: Settings) -> None:
    test_settings.pexels_api_key = SecretStr("private-test-key-not-projected")
    token, _ = insert_device_for_test(test_settings)
    with TestClient(create_app(test_settings)) as client:
        assert client.get("/v1/config").status_code == 401
        response = client.patch(
            "/v1/config", headers=auth_headers(token), json={"pexels_enabled": True}
        )
        assert response.json()["pexels_key_configured"] is True
        assert response.json()["pexels_effective"] is True
        assert "private-test-key" not in response.text and "secret_key" not in response.text
        assert (
            client.patch(
                "/v1/config", headers=auth_headers(token), content=b"x" * 65537
            ).status_code
            == 413
        )
        assert (
            client.patch("/v1/config", headers=auth_headers(token), content=b"{bad").status_code
            == 422
        )
        assert client.patch("/v1/config", headers=auth_headers(token), json={}).status_code == 200
