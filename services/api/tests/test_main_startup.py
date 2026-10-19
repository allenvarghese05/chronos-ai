"""Tests for startup behaviour of chronos.main.create_app (T1.4 review fixes).

Covers
------
- Invalid YAML at wake_rules_path must fail startup loudly (BLOCKING 2).
- Missing YAML at wake_rules_path must fail startup loudly (BLOCKING 2).
- Stale YAML at startup logs a ``yaml_stale`` warning (MINOR 8).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from chronos.config import Settings
from chronos.main import create_app
from chronos.observability import configure as configure_logging
from chronos.wake.rules import reset_rules_cache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_migrations(db_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    ini_path = Path(__file__).parents[1] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def _make_settings(db_url: str, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "test",
        "secret_key": "test-secret-key-32-bytes-exactly!",
        "database_url": db_url,
    }
    base.update(overrides)
    return Settings.model_validate(base)


# ---------------------------------------------------------------------------
# BLOCKING 2 — startup must refuse to start on bad YAML
# ---------------------------------------------------------------------------


class TestStartupRefusesOnBadYaml:
    def test_startup_invalid_yaml_file_refuses_start(self, tmp_path: Path) -> None:
        """Creating the TestClient context with a syntactically invalid YAML raises."""
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        _run_migrations(db_url)

        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("{unclosed: [")  # syntactically invalid YAML

        reset_rules_cache()
        settings = _make_settings(db_url, wake_rules_path=str(bad_yaml))
        app = create_app(settings=settings)

        with pytest.raises(yaml.YAMLError):
            with TestClient(app):
                pass  # pragma: no cover

    def test_startup_missing_yaml_refuses_start(self, tmp_path: Path) -> None:
        """Creating the TestClient context with a non-existent YAML path raises."""
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        _run_migrations(db_url)

        missing_yaml = tmp_path / "nonexistent" / "wake_rules.yaml"  # does not exist

        reset_rules_cache()
        settings = _make_settings(db_url, wake_rules_path=str(missing_yaml))
        app = create_app(settings=settings)

        with pytest.raises((FileNotFoundError, OSError)):
            with TestClient(app):
                pass  # pragma: no cover

    def test_startup_invalid_schema_yaml_refuses_start(self, tmp_path: Path) -> None:
        """Valid YAML that fails WakeRules schema validation also refuses to start."""
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        _run_migrations(db_url)

        bad_schema = tmp_path / "bad_schema.yaml"
        # Valid YAML but missing required 'rules' key.
        bad_schema.write_text(
            "version: 1\n"
            "window:\n"
            "  weekday: {start: '06:00', end: '11:00'}\n"
            "  weekend: {start: '08:00', end: '14:00'}\n"
            "thresholds: {confirm: 70, strong: 85, dedupe_min: 2}\n"
        )

        reset_rules_cache()
        settings = _make_settings(db_url, wake_rules_path=str(bad_schema))
        app = create_app(settings=settings)

        with pytest.raises((ValidationError, ValueError)):
            with TestClient(app):
                pass  # pragma: no cover


# ---------------------------------------------------------------------------
# MINOR 8 — startup logs yaml_stale warning when YAML has changed
# ---------------------------------------------------------------------------


class TestStartupYamlStaleWarning:
    def test_startup_yaml_stale_warning_logged(self, tmp_path: Path) -> None:
        """Startup logs a yaml_stale warning when the YAML has changed since last activation."""

        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        _run_migrations(db_url)

        # Copy real YAML to a temp path.
        real_yaml = Path(__file__).parents[1] / "config" / "wake_rules.yaml"
        temp_yaml = tmp_path / "wake_rules.yaml"
        temp_yaml.write_text(real_yaml.read_text())

        # First startup: inserts v1 from YAML (not stale).
        reset_rules_cache()
        settings = _make_settings(db_url, wake_rules_path=str(temp_yaml))
        app1 = create_app(settings=settings)
        with TestClient(app1):
            pass
        reset_rules_cache()

        # Modify the YAML so it is now stale relative to the DB version.
        raw = yaml.safe_load(temp_yaml.read_text())
        raw["thresholds"]["confirm"] = 75
        temp_yaml.write_text(yaml.dump(raw))

        # Pre-configure logging so create_app does not override capture_logs context.
        configure_logging(env="test")

        warnings_logged: list[str] = []

        class _CapturingLog:
            def warning(self, event: str, **_kw: object) -> None:
                warnings_logged.append(event)

            def info(self, event: str, **_kw: object) -> None:
                pass

        import chronos.main as _main_module

        original_log = _main_module.log
        _main_module.log = _CapturingLog()  # type: ignore[assignment]
        try:
            reset_rules_cache()
            app2 = create_app(settings=settings)
            with TestClient(app2):
                pass
        finally:
            _main_module.log = original_log

        assert "yaml_stale" in warnings_logged, (
            f"Expected 'yaml_stale' warning at startup; got: {warnings_logged}"
        )

    def test_startup_no_stale_warning_when_yaml_unchanged(self, tmp_path: Path) -> None:
        """No yaml_stale warning when YAML matches the DB version."""
        db_url = f"sqlite:///{tmp_path / 'test.db'}"
        _run_migrations(db_url)

        real_yaml = Path(__file__).parents[1] / "config" / "wake_rules.yaml"
        temp_yaml = tmp_path / "wake_rules.yaml"
        temp_yaml.write_text(real_yaml.read_text())

        configure_logging(env="test")

        warnings_logged: list[str] = []

        class _CapturingLog:
            def warning(self, event: str, **_kw: object) -> None:
                warnings_logged.append(event)

            def info(self, event: str, **_kw: object) -> None:
                pass

        import chronos.main as _main_module

        original_log = _main_module.log
        _main_module.log = _CapturingLog()  # type: ignore[assignment]
        try:
            reset_rules_cache()
            settings = _make_settings(db_url, wake_rules_path=str(temp_yaml))
            app = create_app(settings=settings)
            with TestClient(app):
                pass
        finally:
            _main_module.log = original_log

        assert "yaml_stale" not in warnings_logged, (
            f"Did not expect 'yaml_stale' warning; got: {warnings_logged}"
        )
