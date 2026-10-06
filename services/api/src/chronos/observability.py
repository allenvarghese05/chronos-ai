"""Chronos-AI structured logging (D28).

Calls :func:`configure` **once** at the very start of :func:`chronos.main.main`
(before the FastAPI app is constructed) so every import that logs benefits from
the correct processors.

Environments
------------
- ``dev`` / ``test``: human-readable console output to stdout.
- ``prod``: JSON lines to stdout **and** to a rotating file at
  ``<log_dir>/api.jsonl`` (rotated at 50 MB, kept for 14 rotations).

Redaction
---------
:func:`redact_secrets` is inserted **before** the final renderer in the
processor chain, so any log call that includes a key in :data:`REDACT_KEYS`
will have its value replaced with ``"[REDACTED]"`` regardless of nesting.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

# Keys whose values must never appear in log output.
REDACT_KEYS: frozenset[str] = frozenset({"token", "sig", "authorization", "secret", "secret_key"})

_configured: bool = False


def redact_secrets(
    logger: structlog.types.WrappedLogger,
    method: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Structlog processor: replace values for sensitive keys with ``'[REDACTED]'``.

    Applied before serialisation so the raw value never touches the renderer.
    Case-insensitive key matching (``Authorization`` → ``authorization``).
    """
    for key in list(event_dict.keys()):
        if key.lower() in REDACT_KEYS:
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure(env: str = "dev", log_dir: Path | None = None) -> None:
    """Configure structlog for the given *env*.

    Safe to call multiple times; only the first call takes effect (guarded by
    ``_configured``).  Tests may reset the guard by calling
    :func:`reset_for_testing`.

    Args:
        env:     One of ``"dev"``, ``"test"``, ``"prod"``.
        log_dir: Directory for the rotating JSONL file (prod only).  Created if
                 it does not exist.
    """
    global _configured
    if _configured:
        return

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        redact_secrets,
    ]

    if env == "prod":
        # JSON lines to stdout + rotating file.
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
        processors: list[structlog.types.Processor] = [
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ]
        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(sys.stdout),
            cache_logger_on_first_use=True,
        )
        # Set up rotating file handler for prod.
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            _setup_file_handler(log_dir)
    else:
        # dev / test: human-readable console renderer.
        dev_processors: list[structlog.types.Processor] = [
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ]
        structlog.configure(
            processors=dev_processors,
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(sys.stdout),
            cache_logger_on_first_use=True,
        )

    # Also configure stdlib logging so third-party libraries use structlog.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

    _configured = True


def reset_for_testing() -> None:
    """Reset the ``_configured`` guard (test helper only)."""
    global _configured
    _configured = False
    structlog.reset_defaults()


def _setup_file_handler(log_dir: Path) -> None:
    """Attach a rotating file handler to the root logger for prod JSONL output."""
    import logging.handlers

    handler = logging.handlers.RotatingFileHandler(
        filename=log_dir / "api.jsonl",
        maxBytes=50 * 1024 * 1024,  # 50 MB
        backupCount=14,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
