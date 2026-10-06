"""Tests for chronos.observability — structlog redaction and configuration."""

from __future__ import annotations

import pytest

from chronos.observability import configure, redact_secrets, reset_for_testing


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    """Ensure structlog is re-configured fresh for every test."""
    reset_for_testing()


class TestRedactSecrets:
    """Unit tests for the redact_secrets processor (pure function)."""

    def _call(self, event_dict: dict) -> dict:  # type: ignore[type-arg]
        return redact_secrets(None, "info", event_dict)

    def test_redact_token(self) -> None:
        result = self._call({"event": "login", "token": "abc123"})
        assert result["token"] == "[REDACTED]"
        assert result["event"] == "login"

    def test_redact_sig(self) -> None:
        result = self._call({"event": "verify", "sig": "0xdeadbeef"})
        assert result["sig"] == "[REDACTED]"

    def test_redact_authorization(self) -> None:
        result = self._call({"event": "request", "authorization": "Bearer xyz"})
        assert result["authorization"] == "[REDACTED]"

    def test_redact_secret(self) -> None:
        result = self._call({"event": "cfg", "secret": "s3cr3t"})
        assert result["secret"] == "[REDACTED]"

    def test_redact_secret_key(self) -> None:
        result = self._call({"event": "cfg", "secret_key": "super-secret"})
        assert result["secret_key"] == "[REDACTED]"

    def test_non_secret_not_redacted(self) -> None:
        result = self._call({"event": "ok", "user_id": "u42", "status": 200})
        assert result["user_id"] == "u42"
        assert result["status"] == 200

    def test_case_insensitive_redaction(self) -> None:
        result = self._call({"Authorization": "Bearer secret", "Token": "abc"})
        assert result["Authorization"] == "[REDACTED]"
        assert result["Token"] == "[REDACTED]"

    def test_empty_dict_passes(self) -> None:
        result = self._call({})
        assert result == {}


class TestConfigure:
    def test_configure_dev_no_error(self) -> None:
        configure("dev")  # must not raise

    def test_configure_test_no_error(self) -> None:
        configure("test")  # must not raise

    def test_configure_idempotent(self) -> None:
        configure("dev")
        configure("dev")  # second call is a no-op, must not raise

    def test_configure_prod_no_log_dir(self) -> None:
        configure("prod")  # log_dir=None → no file handler, no error

    def test_configure_prod_with_log_dir(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        log_dir = tmp_path / "logs"
        configure("prod", log_dir=log_dir)
        assert log_dir.exists()
