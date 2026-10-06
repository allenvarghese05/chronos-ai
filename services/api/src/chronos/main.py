"""Chronos-AI FastAPI application factory.

Usage::

    from chronos.main import create_app
    app = create_app()          # uses Settings() singleton
    app = create_app(settings)  # inject settings (tests, CLI)

The app factory is the single place that wires together settings, logging,
the DB engine, exception handlers and API routers.  The ``lifespan`` context
manager runs startup and shutdown logic so tests can also use ``TestClient``
without a separate lifespan mock.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from chronos import __version__
from chronos.api.health import router as health_router
from chronos.config import Settings, get_settings, override_settings
from chronos.db import create_db_engine
from chronos.errors import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from chronos.observability import configure as configure_logging

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and return a configured :class:`~fastapi.FastAPI` application.

    Args:
        settings: Optional pre-built :class:`~chronos.config.Settings`; if
                  omitted the module-level singleton from
                  :func:`~chronos.config.get_settings` is used.

    Returns:
        A fully wired :class:`~fastapi.FastAPI` instance ready for
        ``uvicorn`` or ``TestClient``.
    """
    if settings is not None:
        override_settings(settings)
    resolved = settings if settings is not None else get_settings()

    # 1. Logging must be configured first so every import that logs is covered.
    log_dir = resolved.log_dir if str(resolved.env) == "prod" else None
    configure_logging(env=str(resolved.env), log_dir=log_dir)

    # 2. Build the engine once; store on app.state so endpoints can reach it.
    engine = create_db_engine(resolved.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.db_engine = engine
        app.state.settings = resolved
        log.info("chronos_startup", version=__version__, env=str(resolved.env))
        yield
        engine.dispose()
        log.info("chronos_shutdown")

    app = FastAPI(
        title="Chronos-AI API",
        version=__version__,
        description="Behavioural wake detection + morning reel delivery.",
        lifespan=lifespan,
        # OpenAPI served at /openapi.json for gen-types (T0.7).
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )

    # 3. Exception handlers (RFC 9457 problem+json).
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # 4. Routers.
    app.include_router(health_router)

    return app
