"""SSRF-guarded async HTTP client for Chronos-AI (T3.2).

Every outbound fetch goes through :class:`GuardedHttpClient`.

Security notes
--------------
- DNS is resolved *before* each request and each redirect hop via an
  injectable :data:`Resolver`.  All returned IPs are checked with
  :func:`is_public_ip`.  If any IP is non-public the request is rejected
  **before** a TCP connection is opened.
- The client does a manual redirect loop (max ``max_redirects``) so that
  every ``Location`` host is re-resolved and re-checked.
- Body bytes are streamed via ``aiter_bytes``; a running counter aborts at
  ``max_bytes``.  The response is closed after abort so the connection pool
  is not poisoned.
- Full TOCTOU mitigation (pinning the resolved IP and overriding SNI) would
  require a private ``httpcore`` API; the residual window is accepted for
  this threat model and documented here explicitly.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger(__name__)

Resolver = Callable[[str], Awaitable[list[str]]]


async def _default_resolver(hostname: str) -> list[str]:
    """Resolve *hostname* to IPv4/IPv6 addresses via ``asyncio.getaddrinfo``."""
    import asyncio
    import socket

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def is_public_ip(ip_str: str) -> bool:
    """Return ``True`` only if *ip_str* is a globally routable unicast address.

    Allow-list semantics: the address must be ``is_global`` (IANA special-purpose
    registries), which also rejects ranges that are neither "private" nor "reserved"
    in :mod:`ipaddress`, such as carrier-grade NAT ``100.64.0.0/10`` (used by
    Tailscale). Private, loopback, link-local, multicast, unspecified and reserved
    ranges are rejected for IPv4 and IPv6. IPv4-mapped IPv6 (``::ffff:a.b.c.d``) is
    judged by its embedded IPv4 address.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FetchError(Exception):
    """Base for all fetch failures."""


class SsrfBlocked(FetchError):
    """The resolved IP was in a blocked range."""


class TooLarge(FetchError):
    """Body exceeded ``max_bytes``."""


class TooManyRedirects(FetchError):
    """More than ``max_redirects`` hops."""


class ContentTypeRejected(FetchError):
    """Response content-type not in the caller allowlist."""


class FetchFailed(FetchError):
    """Network-level failure (timeout, DNS, reset, etc.)."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Immutable result of a successful (or 304) fetch."""

    status: int
    final_url: str
    headers: Mapping[str, str]
    body: bytes
    content_type: str
    duration_ms: float


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GuardedHttpClient:
    """Async HTTP client with SSRF guard, redirect cap, and byte limit."""

    def __init__(
        self,
        user_agent: str,
        *,
        timeout_sec: float = 15.0,
        max_bytes: int = 2_000_000,
        max_redirects: int = 3,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = httpx.Timeout(timeout_sec)
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.resolver = resolver or _default_resolver
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=self.timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> GuardedHttpClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def fetch(
        self,
        url: str,
        *,
        allowed_content_types: set[str] | None = None,
        headers: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> FetchResult:
        """Fetch *url* with the configured guard rules.

        Args:
            url: Target URL.  Only ``http`` and ``https`` schemes are allowed.
            allowed_content_types: Lower-cased content-type values to accept,
                e.g. ``{"application/xml", "text/xml", "application/json"}``.
                If the response ``Content-Type`` (header, stripped of charset)
                is not in this set, :exc:`ContentTypeRejected` is raised.
            headers: Extra request headers merged with the default UA.
            timeout_sec: Per-request timeout override (seconds).

        Raises:
            SsrfBlocked: Resolved IP is non-public.
            TooManyRedirects: More than ``max_redirects`` hops.
            ContentTypeRejected: Content-type not in allowlist.
            TooLarge: Body exceeded ``max_bytes``.
            FetchFailed: DNS, TCP, or HTTP transport failure.
        """
        start = time.perf_counter()
        current_url = httpx.URL(url)

        if current_url.scheme not in ("http", "https"):
            raise SsrfBlocked(f"Scheme {current_url.scheme!r} not allowed")

        # Resolve and check initial host
        await self._check_host(current_url.host)

        req_headers: dict[str, str] = {"User-Agent": self.user_agent}
        if headers:
            req_headers.update(headers)

        effective_timeout = httpx.Timeout(timeout_sec) if timeout_sec is not None else self.timeout

        method = "GET"
        redirect_count = 0
        response: httpx.Response | None = None

        while True:
            request = self._client.build_request(
                method, current_url, headers=req_headers, timeout=effective_timeout
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.RequestError as exc:  # timeouts, connect/read/protocol errors
                raise FetchFailed(f"Transport error for {current_url.host}: {exc}") from exc

            # Redirect handling (304 Not Modified is not a redirect)
            if 300 <= response.status_code < 400 and response.status_code != 304:
                if redirect_count >= self.max_redirects:
                    await response.aclose()
                    raise TooManyRedirects(f"Exceeded {self.max_redirects} redirects")
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise FetchFailed("Redirect with no Location header")
                current_url = current_url.join(location)
                if current_url.scheme not in ("http", "https"):
                    raise SsrfBlocked(f"Redirect scheme {current_url.scheme!r} not allowed")
                await self._check_host(current_url.host)
                redirect_count += 1
                continue

            break

        assert response is not None

        # 304 Not Modified and 204 No Content have no body; skip content-type check.
        if response.status_code in (304, 204):
            await response.aclose()
            duration_ms = (time.perf_counter() - start) * 1000.0
            raw_ct = response.headers.get("content-type", "")
            ct = raw_ct.split(";")[0].strip().lower()
            return FetchResult(
                status=response.status_code,
                final_url=str(current_url),
                headers=dict(response.headers),
                body=b"",
                content_type=ct,
                duration_ms=duration_ms,
            )

        # Content-type check before streaming body
        raw_ct = response.headers.get("content-type", "")
        ct = raw_ct.split(";")[0].strip().lower()
        if allowed_content_types is not None and ct not in allowed_content_types:
            await response.aclose()
            raise ContentTypeRejected(f"Content-Type {ct!r} not in {allowed_content_types}")

        # Stream body with byte cap
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > self.max_bytes:
                    raise TooLarge(f"Body exceeded max_bytes ({self.max_bytes})")
                body.extend(chunk)
        except httpx.RequestError as exc:  # read timeout / reset / bad encoding mid-body
            raise FetchFailed(
                f"Transport error reading body from {current_url.host}: {exc}"
            ) from exc
        finally:
            await response.aclose()

        duration_ms = (time.perf_counter() - start) * 1000.0

        return FetchResult(
            status=response.status_code,
            final_url=str(current_url),
            headers=dict(response.headers),
            body=bytes(body),
            content_type=ct,
            duration_ms=duration_ms,
        )

    async def _check_host(self, host: str) -> None:
        """Resolve *host* and raise :exc:`SsrfBlocked` if any IP is non-public."""
        try:
            ips = await self.resolver(host)
        except Exception as exc:
            raise FetchFailed(f"DNS resolution failed for {host}: {exc}") from exc
        if not ips:
            raise FetchFailed(f"No DNS results for {host}")
        for ip in ips:
            if not is_public_ip(ip):
                raise SsrfBlocked(f"Host {host!r} resolved to blocked IP {ip}")
