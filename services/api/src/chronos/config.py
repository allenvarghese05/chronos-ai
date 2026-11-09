"""Chronos-AI runtime configuration.

All configuration comes from environment variables with the ``CHRONOS_`` prefix
(loaded from ``.env`` if present). Sensitive fields use ``SecretStr`` so that
their values are never printed in logs or stack-traces.

Instantiate once via :func:`get_settings` and store in ``app.state.settings``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root: services/api/ (three levels up from this file's package).
_PROJECT_ROOT: Path = Path(__file__).parents[2]


class Env(StrEnum):
    """Deployment environment."""

    dev = "dev"
    test = "test"
    prod = "prod"


class Settings(BaseSettings):
    """Application settings loaded from ``CHRONOS_*`` environment variables.

    All fields have safe defaults for local development; ``CHRONOS_SECRET_KEY``
    is the only truly required field (no default).

    ``extra="forbid"`` ensures that unexpected env vars raise a validation error
    rather than being silently ignored.
    """

    model_config = SettingsConfigDict(
        env_prefix="CHRONOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # ── environment ─────────────────────────────────────────────────────────
    env: Env = Env.dev

    # ── database ────────────────────────────────────────────────────────────
    database_url: str = "sqlite:///data/chronos.db"

    # ── security ────────────────────────────────────────────────────────────
    #: 32+ random bytes; required in all environments.
    secret_key: SecretStr

    # ── server ──────────────────────────────────────────────────────────────
    api_bind_host: str = "127.0.0.1"
    api_bind_port: int = 8443

    #: Path to TLS certificate file (PEM). Required in prod.
    tls_cert: Path | None = None
    #: Path to TLS private key file (PEM). Required in prod.
    tls_key: Path | None = None

    # ── wake rules ───────────────────────────────────────────────────────────
    #: Path to the versioned wake-rules YAML (resolved relative to project root).
    wake_rules_path: Path = _PROJECT_ROOT / "config" / "wake_rules.yaml"

    #: Path to the content-sources YAML.
    sources_path: Path = _PROJECT_ROOT / "config" / "sources.yaml"

    #: TEST-ONLY — skip loading wake_rules.yaml at startup.
    #: Must never be set in production. Logged loudly at startup if set.
    skip_rules_init: bool = False

    # ── logging ─────────────────────────────────────────────────────────────
    log_dir: Path = Path("data/logs")

    # ── reports ──────────────────────────────────────────────────────────────
    #: Directory where nightly Markdown reports are written.
    #: Relative paths are resolved from the working directory at runtime.
    reports_dir: Path = Path("data/logs/reports")

    # ── tool endpoints ───────────────────────────────────────────────────────
    ollama_endpoint: str = "http://localhost:11434"
    #: Primary is always Ollama; ``openai``/``anthropic`` add that provider as the second step
    #: of the script chain when its API key is set (D11, T3.4 owner decision 5).
    llm_provider: Literal["ollama", "openai", "anthropic"] = "ollama"
    ollama_model: str = "qwen2.5:7b-instruct"
    #: Expected ``sha256:...`` digest; a mismatch is recorded in briefs and flagged by doctor.
    ollama_digest: str = ""
    ollama_timeout_sec: float = 80.0
    openai_api_key: SecretStr | None = None
    openai_model: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    cloud_llm_timeout_sec: float = 60.0

    # Reel preferences: consumed by the future pipeline, not mobile appearance.
    theme: Literal["auto", "dawn", "mono", "ink"] = "auto"
    render_mode: Literal["eager", "on_trigger"] = "eager"
    pexels_enabled: bool = False
    pexels_api_key: SecretStr | None = None

    # ── scheduler ────────────────────────────────────────────────────────────
    #: Set to False to disable the background scheduler (tests, debug).
    scheduler_enabled: bool = True

    # ── job worker ───────────────────────────────────────────────────────────
    #: Set to False to disable the in-process serial job worker.
    #: Automatically disabled in env=test unless explicitly set to True.
    #: Also controlled via --no-worker CLI flag or CHRONOS_WORKER_ENABLED=false.
    worker_enabled: bool | None = None

    #: Canonical data directory for execution locks, job work dirs and media artifacts.
    #: Must be an absolute path or relative to the working directory at startup.
    data_root: Path = Path("data")

    @property
    def effective_worker_enabled(self) -> bool:
        """True when the worker is enabled; default off in env=test, on elsewhere."""
        if self.worker_enabled is not None:
            return self.worker_enabled
        return self.env is not Env.test

    # ── Expo push ────────────────────────────────────────────────────────────
    expo_access_token: SecretStr | None = None
    #: Base URL for the Expo push API. Override in tests with a mock server URL.
    expo_push_base_url: str = "https://exp.host/--/api/v2"

    # ── public URL (pairing response) ────────────────────────────────────────
    #: The base URL returned in the pairing response (docs/05 §1).
    #: Defaults to scheme://api_bind_host:api_bind_port/v1.
    #: Must never be set to a 0.0.0.0 address.
    public_base_url: str = ""

    # ── rate limits ──────────────────────────────────────────────────────────
    #: Max pairing attempts per minute per IP (docs/05 §0).
    rate_limit_pairing_per_minute: int = Field(default=5, ge=1)
    #: Max requests per minute per IP for all other endpoints.
    rate_limit_default_per_minute: int = Field(default=120, ge=1)

    @model_validator(mode="after")
    def _derive_public_base_url(self) -> Settings:
        """Compute ``public_base_url`` from bind host/port when not supplied."""
        if not self.public_base_url:
            host = self.api_bind_host
            if host in ("0.0.0.0", "::"):  # nosec B104  # noqa: S104
                raise ValueError(
                    "CHRONOS_PUBLIC_BASE_URL must be set when CHRONOS_API_BIND_HOST "
                    "is 0.0.0.0 or :: (cannot derive a routable URL)"
                )
            scheme = "https" if (self.tls_cert is not None or self.env is Env.prod) else "http"
            self.public_base_url = f"{scheme}://{host}:{self.api_bind_port}/v1"
        else:
            # Explicitly supplied — validate it is not a 0.0.0.0 URL.
            for bad in ("://0.0.0.0", "://[::]"):
                if bad in self.public_base_url:
                    raise ValueError("CHRONOS_PUBLIC_BASE_URL must not use 0.0.0.0 or [::]")
        return self

    @model_validator(mode="after")
    def require_tls_in_prod(self) -> Settings:
        """Refuse to start in production without valid TLS certificate files."""
        if self.env is not Env.prod:
            return self

        if self.tls_cert is None or self.tls_key is None:
            raise ValueError("CHRONOS_TLS_CERT and CHRONOS_TLS_KEY are required in prod")

        for label, path in (("CHRONOS_TLS_CERT", self.tls_cert), ("CHRONOS_TLS_KEY", self.tls_key)):
            if not path.exists():
                raise ValueError(f"{label} path does not exist: {path}")
            # Reject world-readable cert/key files (mode bits o+r).
            mode = path.stat().st_mode & 0o777
            if mode & 0o004:
                raise ValueError(f"{label} is world-readable (mode {oct(mode)}); chmod 600 it")

        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the module-level :class:`Settings` singleton, creating it on first call.

    The singleton is reset between test cases by patching this module's
    ``_settings`` variable (or by calling :func:`reset_settings`).
    """
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]  # pydantic-settings reads secret_key from env
    return _settings


def reset_settings() -> None:
    """Clear the cached singleton (used in tests)."""
    global _settings
    _settings = None


def override_settings(s: Settings) -> None:
    """Replace the singleton with *s* (used in test fixtures)."""
    global _settings
    _settings = s


# Silence "unused import" from env.py which imports Env.
__all__ = ["Env", "Settings", "get_settings", "override_settings", "reset_settings"]


def settings_snapshot(settings: Settings) -> str:
    """JSON of *settings* with secrets revealed, for :func:`settings_from_snapshot` in a
    sandboxed job process (written 0600 into the attempt's work dir, deleted on read)."""
    import json

    data: dict[str, object] = {}
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        elif isinstance(value, Path):
            value = str(value.resolve())
        data[name] = value
    return json.dumps(data, default=str)


def settings_from_snapshot(text: str) -> Settings:
    return Settings.model_validate_json(text)
