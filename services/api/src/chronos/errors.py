"""RFC 9457 problem+json error handling (docs/05-api-contracts.md §errors).

Every HTTP error returned by the Chronos API uses the ``application/problem+json``
content type and the schema::

    {
      "type":     "urn:chronos:error:<code>",
      "title":    "<human-readable title>",
      "status":   <http_status_int>,
      "detail":   "<instance-specific message>",
      "instance": "<request path>"
    }

The three exception handlers below are registered on the FastAPI app inside
:func:`chronos.main.create_app`.
"""

from __future__ import annotations

import traceback
from typing import Any

import structlog
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

log = structlog.get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"


class ProblemException(HTTPException):
    """HTTPException subclass that carries the problem+json code and title explicitly.

    Raise this instead of a bare :class:`~starlette.exceptions.HTTPException` when
    you need a specific problem code (e.g. ``pairing_invalid``, ``rate_limited``).
    The ``headers`` kwarg is forwarded to the response (useful for ``Retry-After``).

    Example::

        raise ProblemException(
            status_code=429,
            code="rate_limited",
            title="Too Many Requests",
            detail="Retry after 12 seconds.",
            headers={"Retry-After": "12"},
        )
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        title: str,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.problem_code = code
        self.problem_title = title


def make_problem(
    status: int,
    code: str,
    title: str,
    detail: str,
    instance: str,
) -> JSONResponse:
    """Build an RFC 9457 problem+json :class:`~fastapi.responses.JSONResponse`.

    Args:
        status:   HTTP status code (e.g. 404).
        code:     Short machine-readable code (e.g. ``"not_found"``).
        title:    Human-readable problem title (stable across requests).
        detail:   Instance-specific explanation.
        instance: Request path that triggered the error.

    Returns:
        A :class:`JSONResponse` with ``Content-Type: application/problem+json``.
    """
    body: dict[str, Any] = {
        "type": f"urn:chronos:error:{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
    }
    return JSONResponse(
        content=body,
        status_code=status,
        media_type=PROBLEM_CONTENT_TYPE,
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert :class:`~starlette.exceptions.HTTPException` to problem+json.

    If the exception is a :class:`ProblemException` its ``problem_code`` and
    ``problem_title`` are used directly; otherwise the status code is mapped to
    a default code.  Extra headers (e.g. ``Retry-After``) are forwarded.
    """
    assert isinstance(exc, HTTPException)

    # ProblemException carries the code explicitly.
    if isinstance(exc, ProblemException):
        response = make_problem(
            status=exc.status_code,
            code=exc.problem_code,
            title=exc.problem_title,
            detail=str(exc.detail),
            instance=str(request.url.path),
        )
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    code_map: dict[int, tuple[str, str]] = {
        400: ("pairing_invalid", "Bad Request"),
        401: ("unauthorized", "Unauthorized"),
        403: ("revoked", "Forbidden"),
        404: ("not_found", "Not Found"),
        405: ("method_not_allowed", "Method Not Allowed"),
        409: ("conflict", "Conflict"),
        410: ("pairing_expired", "Gone"),
        422: ("validation", "Unprocessable Entity"),
        429: ("rate_limited", "Too Many Requests"),
    }
    code, title = code_map.get(exc.status_code, ("internal", "HTTP Error"))
    response = make_problem(
        status=exc.status_code,
        code=code,
        title=title,
        detail=str(exc.detail),
        instance=str(request.url.path),
    )
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert :class:`~fastapi.exceptions.RequestValidationError` to problem+json."""
    assert isinstance(exc, RequestValidationError)
    errors = exc.errors()
    detail = "; ".join(
        f"{' -> '.join(str(loc) for loc in e.get('loc', ()))}: {e.get('msg', '')}" for e in errors
    )
    return make_problem(
        status=422,
        code="validation",
        title="Unprocessable Entity",
        detail=detail or "Validation error",
        instance=str(request.url.path),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert any unhandled :class:`Exception` to a 500 problem+json response."""
    log.error(
        "unhandled_exception",
        path=str(request.url.path),
        exc_type=type(exc).__name__,
        traceback=traceback.format_exc(),
    )
    return make_problem(
        status=500,
        code="internal",
        title="Internal Server Error",
        detail="An unexpected error occurred.",
        instance=str(request.url.path),
    )
