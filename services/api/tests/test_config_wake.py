"""Tests for GET / PUT /v1/config/wake and the CLI commands (T1.4).

Coverage
--------
- GET 200 — fields present, correct types.
- GET 401 — missing auth.
- PUT 201 — new version created.
- PUT 200 — identical SHA is a no-op.
- PUT 422 rules_invalid — invalid cap; errors[].loc contains field path.
- PUT 422 rules_invalid — unknown signal.
- PUT 401 — missing auth.
- PUT 403 — revoked device.
- GET after PUT — cache invalidated; version incremented.
- CLI validate ok — exit 0.
- CLI validate invalid YAML — exit 1.
- CLI apply-yaml ok — exit 0, version in DB.
"""

from __future__ import annotations

import copy
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from typer.testing import CliRunner

from chronos.auth import hash_token, new_token
from chronos.cli import app as cli_app
from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.enums import DeviceStatus
from chronos.ids import new_ulid
from chronos.main import create_app
from chronos.models.devices import Device
from chronos.models.wake import RuleVersion
from chronos.time import utc_now_iso
from chronos.wake.rules import reset_rules_cache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_BODY: dict[str, Any] = {
    "version": 1,
    "window": {
        "weekday": {"start": "06:00", "end": "11:00"},
        "weekend": {"start": "08:00", "end": "14:00"},
        "day_boundary": "03:00",
        "holidays_as_weekend": [],
        "tick_interval_min": 5,
    },
    "thresholds": {"confirm": 70, "strong": 85, "dedupe_min": 2},
    "rules": [
        {"id": "steps", "signal": "steps_10m", "points": [[35, 30], [20, 18]], "cap": 30},
    ],
    "penalties": [],
    "reweight_without_pedometer": {},
}


def make_settings(tmp_db_url: str) -> Settings:
    from alembic import command
    from alembic.config import Config

    ini_path = Path(__file__).parents[1] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", tmp_db_url)
    command.upgrade(cfg, "head")

    return Settings.model_validate(
        {
            "env": "test",
            "secret_key": "test-secret-key-32-bytes-exactly!",
            "database_url": tmp_db_url,
        }
    )


def _insert_device(
    settings: Settings,
    *,
    status: DeviceStatus = DeviceStatus.active,
) -> tuple[str, Device]:
    """Insert a Device and return (plaintext_token, device_row)."""
    engine = create_db_engine(settings.database_url)
    plain = new_token()
    now = utc_now_iso()
    device = Device(
        public_id=new_ulid(),
        name="Test iPhone",
        token_hash=hash_token(plain),
        expo_push_token=None,
        tz="America/New_York",
        app_version="1.0.0",
        ios_version="17.4",
        model="iPhone 15",
        status=status,
        created_at=now,
        last_seen_at=now,
        revoked_at=None,
    )
    with Session(engine) as session:
        session.add(device)
        session.commit()
        session.refresh(device)
    return plain, device


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return make_settings(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture()
def authed_client(settings: Settings) -> Generator[tuple[TestClient, str], None, None]:
    """TestClient + bearer token for an active device."""
    reset_rules_cache()
    app = create_app(settings=settings)
    plain, _ = _insert_device(settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, plain


# ---------------------------------------------------------------------------
# GET /v1/config/wake
# ---------------------------------------------------------------------------


class TestGetConfigWake:
    def test_get_config_wake_ok(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        resp = client.get("/v1/config/wake", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "source" in data
        assert "rules" in data
        assert "sha256" in data
        assert "yaml_sha256" in data
        assert "yaml_stale" in data
        assert "created_at" in data
        assert "created_by" in data
        assert isinstance(data["version"], int)
        assert isinstance(data["yaml_stale"], bool)

    def test_get_config_wake_no_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/v1/config/wake")
        assert resp.status_code == 401

    def test_get_config_wake_response_shape(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        resp = client.get("/v1/config/wake", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        rules = data["rules"]
        assert "window" in rules
        assert "thresholds" in rules
        assert "rules" in rules
        assert isinstance(data["version"], int)
        assert data["version"] >= 1


# ---------------------------------------------------------------------------
# PUT /v1/config/wake
# ---------------------------------------------------------------------------


class TestPutConfigWake:
    def test_put_creates_new_version(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        # First GET to know current version.
        resp = client.get("/v1/config/wake", headers=_auth(token))
        assert resp.status_code == 200
        v1 = resp.json()["version"]

        # PUT different rules (change confirm threshold).
        body = copy.deepcopy(VALID_BODY)
        body["thresholds"]["confirm"] = 75
        resp2 = client.put("/v1/config/wake", json=body, headers=_auth(token))
        assert resp2.status_code == 201
        assert resp2.json()["version"] == v1 + 1
        assert resp2.json()["created"] is True

    def test_put_identical_sha_returns_200(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        # PUT the VALID_BODY (same rules as startup YAML might or might not match,
        # so do it twice to guarantee a no-op).
        body = copy.deepcopy(VALID_BODY)
        resp1 = client.put("/v1/config/wake", json=body, headers=_auth(token))
        assert resp1.status_code in (200, 201)

        resp2 = client.put("/v1/config/wake", json=body, headers=_auth(token))
        assert resp2.status_code == 200
        assert resp2.json()["created"] is False

    def test_put_invalid_cap_returns_422(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        body = copy.deepcopy(VALID_BODY)
        # cap=5 but points max=30 → invalid
        body["rules"][0]["cap"] = 5
        resp = client.put("/v1/config/wake", json=body, headers=_auth(token))
        assert resp.status_code == 422
        data = resp.json()
        assert data["type"] == "urn:chronos:error:rules_invalid"
        assert "errors" in data
        # At least one error mentions cap or the rule index.
        locs = [".".join(str(p) for p in e["loc"]) for e in data["errors"]]
        assert any("cap" in loc or "rules" in loc for loc in locs)

    def test_put_unknown_signal_returns_422(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        body = copy.deepcopy(VALID_BODY)
        body["rules"][0]["signal"] = "nonexistent_signal"
        resp = client.put("/v1/config/wake", json=body, headers=_auth(token))
        assert resp.status_code == 422
        data = resp.json()
        assert data["type"] == "urn:chronos:error:rules_invalid"

    def test_put_no_auth_returns_401(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.put("/v1/config/wake", json=VALID_BODY)
        assert resp.status_code == 401

    def test_put_revoked_device_returns_403(self, settings: Settings) -> None:
        reset_rules_cache()
        app = create_app(settings=settings)
        plain, _ = _insert_device(settings, status=DeviceStatus.revoked)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.put("/v1/config/wake", json=VALID_BODY, headers=_auth(plain))
        assert resp.status_code == 403

    def test_get_after_put_shows_new_version(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        resp1 = client.get("/v1/config/wake", headers=_auth(token))
        assert resp1.status_code == 200
        v1 = resp1.json()["version"]

        body = copy.deepcopy(VALID_BODY)
        body["thresholds"]["confirm"] = 80
        resp2 = client.put("/v1/config/wake", json=body, headers=_auth(token))
        assert resp2.status_code == 201

        resp3 = client.get("/v1/config/wake", headers=_auth(token))
        assert resp3.status_code == 200
        assert resp3.json()["version"] == v1 + 1
        assert resp3.json()["rules"]["thresholds"]["confirm"] == 80

    def test_put_version_increments_by_one(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        resp1 = client.get("/v1/config/wake", headers=_auth(token))
        assert resp1.status_code == 200
        v1 = resp1.json()["version"]

        for i, confirm in enumerate([75, 76, 77]):
            body = copy.deepcopy(VALID_BODY)
            body["thresholds"]["confirm"] = confirm
            resp = client.put("/v1/config/wake", json=body, headers=_auth(token))
            assert resp.status_code == 201
            assert resp.json()["version"] == v1 + i + 1


# ---------------------------------------------------------------------------
# CLI: chronos config validate
# ---------------------------------------------------------------------------


class TestCliValidate:
    def test_cli_validate_ok(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_app, ["config", "validate"])
        assert result.exit_code == 0, result.output
        assert "valid" in result.output

    def test_cli_validate_invalid_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write a syntactically invalid YAML to a temp dir and redirect the CLI path."""
        import chronos.cli as _cli_module

        bad_yaml = tmp_path / "wake_rules.yaml"
        bad_yaml.write_text("{unclosed: [")

        # Monkeypatch the yaml path inside config_validate.
        monkeypatch.setattr(_cli_module, "_WAKE_YAML_PATH", bad_yaml)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["config", "validate"])
        assert result.exit_code == 1

    def test_cli_validate_schema_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write syntactically valid but schema-invalid YAML."""
        import chronos.cli as _cli_module

        bad_yaml = tmp_path / "wake_rules.yaml"
        # Missing required 'rules' key.
        bad_yaml.write_text(
            "version: 1\nwindow: {}\nthresholds: {confirm: 70, strong: 85, dedupe_min: 2}\n"
        )

        monkeypatch.setattr(_cli_module, "_WAKE_YAML_PATH", bad_yaml)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["config", "validate"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# CLI: chronos config apply-yaml
# ---------------------------------------------------------------------------


class TestCliApplyYaml:
    def test_cli_apply_yaml_ok(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ensure CLI uses the real YAML (settings has a migrated DB).
        monkeypatch.setenv("CHRONOS_DATABASE_URL", settings.database_url)
        monkeypatch.setenv("CHRONOS_SECRET_KEY", settings.secret_key.get_secret_value())
        reset_rules_cache()

        runner = CliRunner()
        result = runner.invoke(cli_app, ["config", "apply-yaml"])
        assert result.exit_code == 0, result.output
        assert "version" in result.output

        # Verify a rule version row exists.
        engine = create_db_engine(settings.database_url)
        with Session(engine) as session:
            rvs = list(session.exec(select(RuleVersion).where(RuleVersion.active == 1)).all())  # type: ignore[call-overload]
        assert len(rvs) == 1
