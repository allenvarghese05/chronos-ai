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

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

from chronos import __version__
from chronos.api.config_wake import router as config_wake_router
from chronos.api.day import router as day_router
from chronos.api.device import router as device_router
from chronos.api.health import router as health_router
from chronos.api.pair import router as pair_router
from chronos.api.telemetry import router as telemetry_router
from chronos.config import Settings, get_settings, override_settings
from chronos.db import create_db_engine
from chronos.errors import (
    PROBLEM_RESPONSES,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from chronos.observability import configure as configure_logging
from chronos.wake.rules import ensure_initial_version, reset_rules_cache, yaml_stale

log = structlog.get_logger(__name__)


def _operation_id(route: APIRoute) -> str:
    """Stable operationId generator: ``<tag>_<route-name>`` (T0.7).

    Produces deterministic, human-readable operationIds that survive router
    reorganisation.  The first tag is used; routes without tags fall back to
    ``"default"``.

    Args:
        route: The FastAPI :class:`~fastapi.routing.APIRoute` instance.

    Returns:
        A string of the form ``"device_get_device_me"``.
    """
    tag = route.tags[0] if route.tags else "default"
    return f"{tag}_{route.name}"


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

        # Ensure at least one active rule version exists in the DB.
        # POLICY: invalid or missing wake_rules.yaml MUST fail startup loudly.
        # An unreachable database is also a hard failure — the process must not
        # start in an inconsistent state.
        # CHRONOS_SKIP_RULES_INIT=true is a TEST-ONLY escape hatch; it is logged
        # loudly and must never be set in production.
        if resolved.skip_rules_init:
            log.warning(
                "wake_rules_startup_skipped",
                reason="CHRONOS_SKIP_RULES_INIT=true (TEST-ONLY flag — never set in production)",
            )
        else:
            from sqlmodel import Session as _Session

            with _Session(engine) as _session:
                ensure_initial_version(_session, resolved.wake_rules_path)
                if yaml_stale(_session, resolved.wake_rules_path):
                    log.warning(
                        "yaml_stale",
                        path=str(resolved.wake_rules_path),
                        hint="run `chronos config apply-yaml` to promote the updated YAML",
                    )

        # Start scheduler if enabled.
        _scheduler_task: asyncio.Task[None] | None = None
        _stop_event: asyncio.Event | None = None
        _push_client: ExpoPushClient | None = None
        if resolved.scheduler_enabled:
            import httpx as _httpx
            from sqlmodel import Session as _SchedSession

            from chronos.delivery.push import ExpoPushClient
            from chronos.scheduler import Scheduler

            _push_client = ExpoPushClient(
                access_token=resolved.expo_access_token,
                base_url=resolved.expo_push_base_url,
                http_client=_httpx.AsyncClient(),
            )
            _scheduler = Scheduler(
                session_factory=lambda: _SchedSession(engine),
                push_client=_push_client,
                settings=resolved,
            )
            _stop_event = asyncio.Event()
            app.state.scheduler = _scheduler
            _scheduler_task = asyncio.create_task(
                _scheduler.run_forever(_stop_event),
                name="chronos-scheduler",
            )
            log.info("scheduler_started")

        yield

        if _stop_event is not None:
            _stop_event.set()
        if _scheduler_task is not None:
            try:
                await asyncio.wait_for(_scheduler_task, timeout=5.0)
            except TimeoutError:
                log.warning("scheduler_stop_timeout")
        if _push_client is not None:
            await _push_client.close()

        engine.dispose()
        reset_rules_cache()
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
        # Stable <tag>_<route-name> operationIds (T0.7).
        generate_unique_id_function=_operation_id,
    )

    # 3. Exception handlers (RFC 9457 problem+json).
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # 4. Routers.
    # Public endpoints (no bearer auth required):
    app.include_router(health_router)
    app.include_router(pair_router, prefix="/v1")
    # Authenticated endpoints: add shared 401/403/422/429 Problem responses (T0.7).
    app.include_router(device_router, prefix="/v1", responses=PROBLEM_RESPONSES)
    app.include_router(config_wake_router, prefix="/v1", responses=PROBLEM_RESPONSES)
    app.include_router(telemetry_router, prefix="/v1", responses=PROBLEM_RESPONSES)
    app.include_router(day_router, prefix="/v1", responses=PROBLEM_RESPONSES)

    return app
