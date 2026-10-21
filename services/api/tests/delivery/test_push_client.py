"""Tests for chronos.delivery.push — ExpoPushClient and FakePushClient.

Security: device token must never appear in log output (caplog assertions).
Chunking: 101 messages trigger 2 POST calls.
Retry: 429 → retry; 3x5xx → PushClientError.
Error mapping: Expo error tickets are returned as Ticket(status=error, ...).
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from chronos.delivery.push import (
    ExpoPushClient,
    FakePushClient,
    PushClientError,
    PushMessage,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOKEN = "ExponentPushToken[test_token_abc123]"


def _make_msg(token: str = TOKEN, tick_seq: int = 0) -> PushMessage:
    return PushMessage(
        to=token,
        data={"type": "tick", "tick_seq": tick_seq},
    )


def _expo_ok_response(n: int = 1) -> bytes:
    data = [{"status": "ok", "id": str(uuid.uuid4())} for _ in range(n)]
    return json.dumps({"data": data}).encode()


def _expo_error_response(error: str, n: int = 1) -> bytes:
    data = [{"status": "error", "message": error} for _ in range(n)]
    return json.dumps({"data": data}).encode()


def _expo_receipt_ok_response(ticket_ids: list[str]) -> bytes:
    data = {tid: {"status": "ok"} for tid in ticket_ids}
    return json.dumps({"data": data}).encode()


class _MockTransport(httpx.AsyncBaseTransport):
    """HTTPX async transport that replays a list of responses in order."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._index >= len(self._responses):
            return httpx.Response(200, content=_expo_ok_response())
        resp = self._responses[self._index]
        self._index += 1
        return resp


# ---------------------------------------------------------------------------
# FakePushClient tests
# ---------------------------------------------------------------------------


class TestFakePushClient:
    @pytest.mark.anyio
    async def test_fake_records_messages(self) -> None:
        client = FakePushClient()
        msgs = [_make_msg() for _ in range(3)]
        tickets = await client.send(msgs)
        assert len(tickets) == 3
        assert len(client.sent) == 3
        assert all(t.status == "ok" for t in tickets)

    @pytest.mark.anyio
    async def test_fake_scripted_error(self) -> None:
        client = FakePushClient(outcomes=["ok", "DeviceNotRegistered"])
        msgs = [_make_msg(), _make_msg()]
        tickets = await client.send(msgs)
        assert tickets[0].status == "ok"
        assert tickets[1].status == "error"
        assert tickets[1].message == "DeviceNotRegistered"

    @pytest.mark.anyio
    async def test_fake_last_outcome_repeated(self) -> None:
        client = FakePushClient(outcomes=["MessageRateExceeded"])
        tickets = await client.send([_make_msg(), _make_msg()])
        assert all(t.message == "MessageRateExceeded" for t in tickets)

    @pytest.mark.anyio
    async def test_fake_receipts_default_ok(self) -> None:
        client = FakePushClient()
        receipts = await client.get_receipts(["id1", "id2"])
        assert receipts["id1"].status == "ok"
        assert receipts["id2"].status == "ok"
        assert client.receipts_checked == ["id1", "id2"]

    @pytest.mark.anyio
    async def test_fake_receipts_scripted(self) -> None:
        client = FakePushClient(receipt_outcomes={"id1": "DeviceNotRegistered"})
        receipts = await client.get_receipts(["id1", "id2"])
        assert receipts["id1"].status == "DeviceNotRegistered"
        assert receipts["id2"].status == "ok"

    @pytest.mark.anyio
    async def test_fake_send_empty(self) -> None:
        client = FakePushClient()
        tickets = await client.send([])
        assert tickets == []

    @pytest.mark.anyio
    async def test_fake_receipts_empty(self) -> None:
        client = FakePushClient()
        receipts = await client.get_receipts([])
        assert receipts == {}


# ---------------------------------------------------------------------------
# ExpoPushClient tests
# ---------------------------------------------------------------------------


def _expo_client(transport: _MockTransport) -> ExpoPushClient:
    http = httpx.AsyncClient(transport=transport)
    return ExpoPushClient(
        access_token=None,
        base_url="https://exp.host/--/api/v2",
        timeout=5.0,
        http_client=http,
    )


class TestExpoPushClient:
    @pytest.mark.anyio
    async def test_send_ok(self) -> None:
        transport = _MockTransport([httpx.Response(200, content=_expo_ok_response(1))])
        client = _expo_client(transport)
        tickets = await client.send([_make_msg()])
        assert len(tickets) == 1
        assert tickets[0].status == "ok"
        assert tickets[0].id is not None

    @pytest.mark.anyio
    async def test_send_device_not_registered(self) -> None:
        content = _expo_error_response("DeviceNotRegistered")
        transport = _MockTransport([httpx.Response(200, content=content)])
        client = _expo_client(transport)
        tickets = await client.send([_make_msg()])
        assert tickets[0].status == "error"
        assert tickets[0].message == "DeviceNotRegistered"

    @pytest.mark.anyio
    async def test_send_message_rate_exceeded(self) -> None:
        content = _expo_error_response("MessageRateExceeded")
        transport = _MockTransport([httpx.Response(200, content=content)])
        client = _expo_client(transport)
        tickets = await client.send([_make_msg()])
        assert tickets[0].status == "error"
        assert tickets[0].message == "MessageRateExceeded"

    @pytest.mark.anyio
    async def test_send_chunks_101_messages(self) -> None:
        """101 messages → 2 POST calls (chunks of 100 + 1)."""
        # First chunk: 100 ok; second chunk: 1 ok
        first = httpx.Response(200, content=_expo_ok_response(100))
        second = httpx.Response(200, content=_expo_ok_response(1))
        transport = _MockTransport([first, second])
        client = _expo_client(transport)
        msgs = [_make_msg(tick_seq=i) for i in range(101)]
        tickets = await client.send(msgs)
        assert len(tickets) == 101
        assert len(transport.requests) == 2

    @pytest.mark.anyio
    async def test_send_429_retries(self) -> None:
        """HTTP 429 triggers a retry; second attempt succeeds."""
        # Patch asyncio.sleep to avoid real waits
        from unittest.mock import patch

        transport = _MockTransport(
            [
                httpx.Response(429, content=b"too many requests"),
                httpx.Response(200, content=_expo_ok_response(1)),
            ]
        )
        client = _expo_client(transport)
        with patch("asyncio.sleep"):
            tickets = await client.send([_make_msg()])
        assert len(tickets) == 1
        assert tickets[0].status == "ok"
        assert len(transport.requests) == 2

    @pytest.mark.anyio
    async def test_send_5xx_max_retries_raises(self) -> None:
        """3 consecutive 5xx responses → PushClientError raised."""
        from unittest.mock import patch

        transport = _MockTransport(
            [
                httpx.Response(500, content=b"error"),
                httpx.Response(500, content=b"error"),
                httpx.Response(500, content=b"error"),
            ]
        )
        client = _expo_client(transport)
        with patch("asyncio.sleep"), pytest.raises(PushClientError):
            await client.send([_make_msg()])

    @pytest.mark.anyio
    async def test_get_receipts_ok(self) -> None:
        tid = str(uuid.uuid4())
        content = _expo_receipt_ok_response([tid])
        transport = _MockTransport([httpx.Response(200, content=content)])
        client = _expo_client(transport)
        receipts = await client.get_receipts([tid])
        assert receipts[tid].status == "ok"

    @pytest.mark.anyio
    async def test_get_receipts_empty(self) -> None:
        transport = _MockTransport([])
        client = _expo_client(transport)
        receipts = await client.get_receipts([])
        assert receipts == {}

    @pytest.mark.anyio
    async def test_no_token_in_request_body(self) -> None:
        """The Expo token must appear in the request body (to field), but the
        payload_dict_without_token helper must strip it for DB storage."""
        msg = _make_msg()
        # Wire form: token IS in the Expo payload (required by Expo)
        expo_dict = msg.to_expo_dict()
        assert expo_dict["to"] == TOKEN
        # Storage form: token NOT in the payload
        safe_dict = msg.to_payload_dict_without_token()
        assert "to" not in safe_dict

    @pytest.mark.anyio
    async def test_no_token_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Token must never appear in structured log events."""
        import logging

        transport = _MockTransport([httpx.Response(200, content=_expo_ok_response(1))])
        client = _expo_client(transport)
        with caplog.at_level(logging.DEBUG):
            await client.send([_make_msg()])
        for record in caplog.records:
            assert TOKEN not in record.getMessage(), (
                f"Token found in log message: {record.getMessage()!r}"
            )
            assert TOKEN not in str(record.__dict__), (
                f"Token found in log record: {record.__dict__!r}"
            )

    @pytest.mark.anyio
    async def test_authorization_header_set(self) -> None:
        """Authorization header is set when access_token provided."""
        from pydantic import SecretStr

        transport = _MockTransport([httpx.Response(200, content=_expo_ok_response(1))])
        http = httpx.AsyncClient(transport=transport)
        client = ExpoPushClient(
            access_token=SecretStr("test-expo-token"),
            base_url="https://exp.host/--/api/v2",
            http_client=http,
        )
        await client.send([_make_msg()])
        req = transport.requests[0]
        assert req.headers.get("Authorization") == "Bearer test-expo-token"

    @pytest.mark.anyio
    async def test_send_empty(self) -> None:
        transport = _MockTransport([])
        client = _expo_client(transport)
        tickets = await client.send([])
        assert tickets == []
        assert len(transport.requests) == 0


# ---------------------------------------------------------------------------
# pytest-anyio configuration
# ---------------------------------------------------------------------------

pytest_plugins = ("anyio",)


# ---------------------------------------------------------------------------
# MINOR 3: ExpoPushClient.close() closes the httpx.AsyncClient
# ---------------------------------------------------------------------------


class TestExpoPushClientClose:
    """close() must call aclose() on the injected http_client."""

    @pytest.mark.anyio
    async def test_close_calls_aclose_on_injected_client(self) -> None:
        """close() releases the httpx.AsyncClient connection pool."""
        from unittest.mock import AsyncMock, MagicMock

        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.aclose = AsyncMock()

        client = ExpoPushClient(
            base_url="https://exp.host/--/api/v2",
            http_client=mock_http,
        )
        await client.close()

        mock_http.aclose.assert_awaited_once()

    @pytest.mark.anyio
    async def test_close_noop_without_injected_client(self) -> None:
        """close() is a no-op when no http_client was injected (per-request mode)."""
        client = ExpoPushClient(base_url="https://exp.host/--/api/v2")
        # Must not raise
        await client.close()

    @pytest.mark.anyio
    async def test_close_is_idempotent(self) -> None:
        """close() can be called multiple times; aclose() runs exactly once."""
        from unittest.mock import AsyncMock, MagicMock

        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.aclose = AsyncMock()

        client = ExpoPushClient(
            base_url="https://exp.host/--/api/v2",
            http_client=mock_http,
        )
        await client.close()
        await client.close()

        # _closed flag ensures aclose() is called exactly once, not twice.
        assert mock_http.aclose.await_count == 1
