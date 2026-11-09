"""Tests for chronos.http — SSRF guard, redirects, streaming abort, content-type."""

from __future__ import annotations

import httpx
import pytest

from chronos.http import (
    ContentTypeRejected,
    FetchFailed,
    GuardedHttpClient,
    SsrfBlocked,
    TooLarge,
    TooManyRedirects,
    is_public_ip,
)


async def _fake_resolver(mapping: dict[str, list[str]], host: str) -> list[str]:
    return mapping.get(host, [])


# ---------------------------------------------------------------------------
# is_public_ip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip,expected",
    [
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("192.168.1.1", False),
        ("10.0.0.1", False),
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("2001:4860:4860::8888", True),
        ("100.64.1.1", False),  # carrier-grade NAT / Tailscale
        ("0.0.0.0", False),  # noqa: S104 - test input, not a bind address
        ("::", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("::ffff:8.8.8.8", True),  # IPv4-mapped public
        ("198.18.0.1", False),  # benchmarking
        ("not-an-ip", False),
    ],
)
def test_is_public_ip(ip: str, expected: bool) -> None:
    assert is_public_ip(ip) is expected


# ---------------------------------------------------------------------------
# SSRF pre-connect rejection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ssrf_rejects_private_ipv4() -> None:
    mapping = {"evil.example": ["192.168.1.1"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
    )
    with pytest.raises(SsrfBlocked):
        await client.fetch("http://evil.example/path")


@pytest.mark.anyio
async def test_ssrf_rejects_loopback() -> None:
    for ip in ("127.0.0.1", "::1"):
        mapping = {"h": [ip]}
        client = GuardedHttpClient(
            user_agent="test",
            resolver=lambda h, _m=mapping: _fake_resolver(_m, h),
        )
        with pytest.raises(SsrfBlocked):
            await client.fetch("http://h/")


@pytest.mark.anyio
async def test_ssrf_rejects_link_local() -> None:
    mapping = {"h": ["169.254.169.254"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
    )
    with pytest.raises(SsrfBlocked):
        await client.fetch("http://h/")


@pytest.mark.anyio
async def test_ssrf_rejects_ula_ipv6() -> None:
    mapping = {"h": ["fc00::1"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
    )
    with pytest.raises(SsrfBlocked):
        await client.fetch("http://h/")


@pytest.mark.anyio
async def test_ssrf_rejects_multicast() -> None:
    mapping = {"h": ["224.0.0.1"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
    )
    with pytest.raises(SsrfBlocked):
        await client.fetch("http://h/")


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_redirect_to_private_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://private.example/secret"})
        return httpx.Response(200, content=b"ok")

    mapping = {"public.example": ["1.2.3.4"], "private.example": ["10.0.0.5"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SsrfBlocked):
        await client.fetch("http://public.example/start")


@pytest.mark.anyio
async def test_too_many_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/next"})

    mapping = {"h": ["1.2.3.4"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
        transport=httpx.MockTransport(handler),
        max_redirects=3,
    )
    with pytest.raises(TooManyRedirects):
        await client.fetch("http://h/")


@pytest.mark.anyio
async def test_relative_redirect_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://h/start":
            return httpx.Response(302, headers={"location": "/end"})
        return httpx.Response(200, content=b"done")

    mapping = {"h": ["1.2.3.4"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
        transport=httpx.MockTransport(handler),
    )
    result = await client.fetch("http://h/start")
    assert result.status == 200
    assert result.body == b"done"


@pytest.mark.anyio
async def test_redirect_chain_blocks_on_third_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example":
            return httpx.Response(302, headers={"location": "http://b.example/"})
        if request.url.host == "b.example":
            return httpx.Response(302, headers={"location": "http://c.example/"})
        return httpx.Response(200, content=b"ok")

    mapping = {
        "a.example": ["1.2.3.4"],
        "b.example": ["5.6.7.8"],
        "c.example": ["169.254.1.1"],
    }
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
        transport=httpx.MockTransport(handler),
        max_redirects=3,
    )
    with pytest.raises(SsrfBlocked):
        await client.fetch("http://a.example/")


# ---------------------------------------------------------------------------
# Body size / content-type
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_max_bytes_aborts_stream() -> None:
    big = b"x" * 5_000_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "text/plain"})

    mapping = {"h": ["1.2.3.4"]}
    client = GuardedHttpClient(
        user_agent="test",
        max_bytes=2_000_000,
        resolver=lambda h: _fake_resolver(mapping, h),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TooLarge):
        await client.fetch("http://h/", allowed_content_types={"text/plain"})


@pytest.mark.anyio
async def test_content_type_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x", headers={"content-type": "application/octet-stream"}
        )

    mapping = {"h": ["1.2.3.4"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ContentTypeRejected):
        await client.fetch("http://h/", allowed_content_types={"application/xml"})


@pytest.mark.anyio
async def test_missing_content_type_rejected_when_allowlist_given() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x")

    mapping = {"h": ["1.2.3.4"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ContentTypeRejected):
        await client.fetch("http://h/", allowed_content_types={"application/xml"})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"hello", headers={"content-type": "text/html"})

    mapping = {"h": ["1.2.3.4"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
        transport=httpx.MockTransport(handler),
    )
    result = await client.fetch("http://h/", allowed_content_types={"text/html"})
    assert result.status == 200
    assert result.body == b"hello"
    assert result.content_type == "text/html"


@pytest.mark.anyio
async def test_fetch_304() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    mapping = {"h": ["1.2.3.4"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
        transport=httpx.MockTransport(handler),
    )
    result = await client.fetch("http://h/")
    assert result.status == 304
    assert result.body == b""


@pytest.mark.anyio
async def test_timeout_override() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    mapping = {"h": ["1.2.3.4"]}
    client = GuardedHttpClient(
        user_agent="test",
        resolver=lambda h: _fake_resolver(mapping, h),
        transport=httpx.MockTransport(handler),
    )
    result = await client.fetch("http://h/", timeout_sec=5.0)
    assert result.status == 200


# ---------------------------------------------------------------------------
# Transport errors are always FetchFailed (send and mid-body); client closes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_protocol_error_on_send_is_fetch_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("peer closed connection")

    client = GuardedHttpClient(
        user_agent="test",
        transport=httpx.MockTransport(handler),
        resolver=lambda h: _fake_resolver({"h": ["1.2.3.4"]}, h),
    )
    with pytest.raises(FetchFailed):
        await client.fetch("https://h/feed")


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield b"<rss>"
        raise httpx.ReadTimeout("stalled")


@pytest.mark.anyio
async def test_read_error_mid_body_is_fetch_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/rss+xml"}, stream=_BrokenStream()
        )

    async with GuardedHttpClient(
        user_agent="test",
        transport=httpx.MockTransport(handler),
        resolver=lambda h: _fake_resolver({"h": ["1.2.3.4"]}, h),
    ) as client:
        with pytest.raises(FetchFailed):
            await client.fetch("https://h/feed", allowed_content_types={"application/rss+xml"})
    assert client._client.is_closed  # pyright: ignore[reportPrivateUsage]
