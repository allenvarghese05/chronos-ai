"""Tests for chronos.config — Settings loading, validation and security."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chronos.config import Env, Settings


def _make_settings(**overrides: str) -> Settings:
    """Build a minimal valid Settings with test-safe defaults."""
    defaults: dict[str, str] = {
        "CHRONOS_ENV": "test",
        "CHRONOS_SECRET_KEY": "test-secret-key-32-bytes-exactly!",
        "CHRONOS_DATABASE_URL": "sqlite:///data/test.db",
    }
    defaults.update(overrides)
    return Settings(**{k.removeprefix("CHRONOS_").lower(): v for k, v in defaults.items()})  # type: ignore[arg-type]


class TestSettingsDefaults:
    def test_missing_secret_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SECRET_KEY has no default; omitting it must raise ValidationError."""
        monkeypatch.delenv("CHRONOS_SECRET_KEY", raising=False)
        with pytest.raises(ValidationError, match="secret_key"):
            Settings(env=Env.test, _env_file=None)  # type: ignore[call-arg]

    def test_env_default_is_dev(self) -> None:
        s = Settings(secret_key="any-key-at-least-16-chars")  # type: ignore[call-arg]
        assert s.env is Env.dev

    def test_database_url_default(self) -> None:
        s = Settings(secret_key="any-key-at-least-16-chars")  # type: ignore[call-arg]
        assert s.database_url == "sqlite:///data/chronos.db"

    def test_api_bind_host_default(self) -> None:
        s = Settings(secret_key="any-key-at-least-16-chars")  # type: ignore[call-arg]
        assert s.api_bind_host == "127.0.0.1"


class TestSettingsLoads:
    def test_loads_from_kwargs(self) -> None:
        s = _make_settings()
        assert s.env is Env.test
        assert s.database_url == "sqlite:///data/test.db"

    def test_loads_from_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHRONOS_ENV", "test")
        monkeypatch.setenv("CHRONOS_SECRET_KEY", "env-secret-key-32-bytes-exactly!!")
        monkeypatch.setenv("CHRONOS_DATABASE_URL", "sqlite:///data/env_test.db")
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.env is Env.test
        assert s.database_url == "sqlite:///data/env_test.db"

    def test_extra_fields_are_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Settings(secret_key="k", unknown_field="x")  # type: ignore[call-arg]


class TestSecretStrNotInRepr:
    def test_secret_key_hidden_in_repr(self) -> None:
        s = _make_settings(CHRONOS_SECRET_KEY="super-secret-value-for-test-repr!!")
        r = repr(s.secret_key)
        assert "super-secret-value-for-test-repr!!" not in r
        assert "**" in r or "SecretStr" in r

    def test_secret_key_hidden_in_str(self) -> None:
        s = _make_settings(CHRONOS_SECRET_KEY="super-secret-value-for-test-str!!!")
        assert "super-secret-value-for-test-str!!!" not in str(s.secret_key)

    def test_secret_key_accessible_via_get_secret_value(self) -> None:
        s = _make_settings(CHRONOS_SECRET_KEY="readable-via-get-secret-value!!!!!!")
        assert s.secret_key.get_secret_value() == "readable-via-get-secret-value!!!!!!"


class TestProdRequiresTls:
    def test_prod_without_tls_raises(self) -> None:
        """Missing TLS cert/key in prod must be a startup error."""
        with pytest.raises(ValidationError, match="CHRONOS_TLS_CERT"):
            Settings(  # type: ignore[call-arg]
                env=Env.prod,
                secret_key="prod-secret-key-32-bytes-exactly!",
            )

    def test_prod_tls_missing_files_raises(self, tmp_path: pytest.TempPathFactory) -> None:
        """Pointing to non-existent cert/key paths in prod must raise."""
        with pytest.raises(ValidationError, match="does not exist"):
            Settings(  # type: ignore[call-arg]
                env=Env.prod,
                secret_key="prod-secret-key-32-bytes-exactly!",
                tls_cert=tmp_path / "missing.pem",  # type: ignore[operator]
                tls_key=tmp_path / "missing-key.pem",  # type: ignore[operator]
            )

    def test_prod_world_readable_cert_raises(self, tmp_path: pytest.TempPathFactory) -> None:
        """World-readable cert files must be rejected in prod."""
        cert = tmp_path / "cert.pem"  # type: ignore[operator]
        key = tmp_path / "key.pem"  # type: ignore[operator]
        cert.write_text("cert")  # type: ignore[union-attr]
        key.write_text("key")  # type: ignore[union-attr]
        cert.chmod(0o644)  # world-readable  # type: ignore[union-attr]
        key.chmod(0o600)  # type: ignore[union-attr]
        with pytest.raises(ValidationError, match="world-readable"):
            Settings(  # type: ignore[call-arg]
                env=Env.prod,
                secret_key="prod-secret-key-32-bytes-exactly!",
                tls_cert=cert,
                tls_key=key,
            )

    def test_dev_does_not_require_tls(self) -> None:
        """dev env must boot without TLS cert/key."""
        s = Settings(env=Env.dev, secret_key="dev-secret")  # type: ignore[call-arg]
        assert s.tls_cert is None
        assert s.tls_key is None

    def test_test_does_not_require_tls(self) -> None:
        """test env must boot without TLS cert/key."""
        s = Settings(env=Env.test, secret_key="test-secret")  # type: ignore[call-arg]
        assert s.tls_cert is None
