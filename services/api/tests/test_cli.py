"""Tests for the `chronos serve` CLI flags (T3.1 review finding 4).

Specifically covers the `--no-worker` flag, which must set
``CHRONOS_WORKER_ENABLED=false`` in the environment before the server starts so
that ``Settings.effective_worker_enabled`` returns ``False``.

These tests invoke the CLI in-process via Typer's ``CliRunner`` with
``catch_exceptions=True``; the ``serve`` command normally never returns (it
runs uvicorn), so we use a monkeypatch to intercept the subprocess launch and
assert on the environment state captured at that point.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from chronos.cli import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_ENV = {
    "CHRONOS_ENV": "test",
    "CHRONOS_SECRET_KEY": "test-secret-key-32-bytes-exactly!",
}


@pytest.fixture(autouse=True)
def _clean_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore CHRONOS_WORKER_ENABLED after each test.

    ``chronos serve --no-worker`` sets this env var directly on ``os.environ``
    (not through monkeypatch), so it would otherwise leak across tests in the
    same process.
    """
    monkeypatch.delenv("CHRONOS_WORKER_ENABLED", raising=False)
    monkeypatch.delenv("CHRONOS_SCHEDULER_ENABLED", raising=False)


class TestServeNoWorkerFlag:
    """chronos serve --no-worker must disable the worker before spawning uvicorn."""

    def test_no_worker_sets_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--no-worker`` sets ``CHRONOS_WORKER_ENABLED=false`` before the
        subprocess is launched, ensuring the Settings loaded by uvicorn see the
        override even when the env var was not pre-set."""
        captured: dict[str, str] = {}

        def _fake_run(args: list[str], **kwargs: Any) -> None:
            # Capture the environment at the moment the subprocess would start.
            captured.update(os.environ.copy())
            raise SystemExit(0)  # terminate cleanly without actually starting uvicorn

        monkeypatch.setenv("CHRONOS_ENV", "test")
        monkeypatch.setenv("CHRONOS_SECRET_KEY", "test-secret-key-32-bytes-exactly!")

        with patch("subprocess.run", side_effect=_fake_run):
            result = runner.invoke(app, ["serve", "--no-worker"], catch_exceptions=False)

        # The runner exits with 0 (our SystemExit(0)).
        assert result.exit_code == 0, result.output
        assert captured.get("CHRONOS_WORKER_ENABLED") == "false", (
            "Expected CHRONOS_WORKER_ENABLED=false in the environment passed to uvicorn, "
            f"got: {captured.get('CHRONOS_WORKER_ENABLED')!r}"
        )

    def test_without_no_worker_flag_does_not_force_disable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without ``--no-worker``, the flag must *not* inject
        ``CHRONOS_WORKER_ENABLED=false`` (the env var must be absent or
        whatever the caller had set, not forced to false by the CLI)."""
        captured: dict[str, str] = {}

        def _fake_run(args: list[str], **kwargs: Any) -> None:
            captured.update(os.environ.copy())
            raise SystemExit(0)

        monkeypatch.setenv("CHRONOS_ENV", "test")
        monkeypatch.setenv("CHRONOS_SECRET_KEY", "test-secret-key-32-bytes-exactly!")
        # Ensure the var is not pre-set by a parent test.
        monkeypatch.delenv("CHRONOS_WORKER_ENABLED", raising=False)

        with patch("subprocess.run", side_effect=_fake_run):
            result = runner.invoke(app, ["serve"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert captured.get("CHRONOS_WORKER_ENABLED") != "false", (
            "Expected CHRONOS_WORKER_ENABLED to be absent or non-false without --no-worker, "
            f"got: {captured.get('CHRONOS_WORKER_ENABLED')!r}"
        )

    def test_no_worker_and_no_scheduler_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--no-worker --no-scheduler`` must set both env vars."""
        captured: dict[str, str] = {}

        def _fake_run(args: list[str], **kwargs: Any) -> None:
            captured.update(os.environ.copy())
            raise SystemExit(0)

        monkeypatch.setenv("CHRONOS_ENV", "test")
        monkeypatch.setenv("CHRONOS_SECRET_KEY", "test-secret-key-32-bytes-exactly!")

        with patch("subprocess.run", side_effect=_fake_run):
            result = runner.invoke(
                app,
                ["serve", "--no-worker", "--no-scheduler"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert captured.get("CHRONOS_WORKER_ENABLED") == "false"
        assert captured.get("CHRONOS_SCHEDULER_ENABLED") == "false"


class TestServeEffectiveWorkerEnabled:
    """Unit tests for ``Settings.effective_worker_enabled`` logic, without
    spawning any subprocess."""

    def test_env_test_defaults_worker_disabled(self) -> None:
        """``env=test`` with no explicit override → worker disabled."""
        from chronos.config import Settings

        s = Settings.model_validate(
            {
                "env": "test",
                "secret_key": "test-secret-key-32-bytes-exactly!",
            }
        )
        assert s.effective_worker_enabled is False

    def test_explicit_false_overrides_non_test_env(self) -> None:
        """``worker_enabled=False`` beats the default-on behaviour for dev/prod."""
        from chronos.config import Settings

        s = Settings.model_validate(
            {
                "env": "dev",
                "secret_key": "test-secret-key-32-bytes-exactly!",
                "worker_enabled": False,
            }
        )
        assert s.effective_worker_enabled is False

    def test_explicit_true_in_test_env_enables_worker(self) -> None:
        """``worker_enabled=True`` can force the worker on even in test env."""
        from chronos.config import Settings

        s = Settings.model_validate(
            {
                "env": "test",
                "secret_key": "test-secret-key-32-bytes-exactly!",
                "worker_enabled": True,
            }
        )
        assert s.effective_worker_enabled is True

    def test_dev_env_defaults_worker_enabled(self) -> None:
        """``env=dev`` with no override → worker enabled by default."""
        from chronos.config import Settings

        s = Settings.model_validate(
            {
                "env": "dev",
                "secret_key": "test-secret-key-32-bytes-exactly!",
            }
        )
        assert s.effective_worker_enabled is True
