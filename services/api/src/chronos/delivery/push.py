"""Expo Push API client for Chronos-AI (T1.7).

Public API
----------
- :class:`PushMessage`    — one outbound push notification (token never logged).
- :class:`Ticket`         — Expo ticket returned from push/send.
- :class:`Receipt`        — Expo receipt returned from push/getReceipts.
- :class:`PushClientError` — raised when all retries are exhausted.
- :class:`PushClient`     — structural Protocol (send + get_receipts).
- :class:`ExpoPushClient` — production httpx-based client.
- :class:`FakePushClient` — in-memory client for tests.

Security notes
--------------
- The ``to`` field of :class:`PushMessage` is the Expo device token. It is
  **never** passed to structlog or included in any log event.  Log events
  reference ``device_id`` only (caller's responsibility).
- The ``Authorization`` header is a secret. It is set via ``SecretStr.get_secret_value()``
  just before the request and is never stored in a format that could appear in
  repr or tracebacks.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, SecretStr

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class PushClientError(Exception):
    """Raised when all send/receipt retries are exhausted."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class PushMessage(BaseModel):
    """One outbound push notification.

    The ``to`` field holds the Expo device token; it must never appear in logs.
    ``_content_available`` maps to the JSON key ``_contentAvailable`` (silent push).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    to: str = Field(repr=False)  # repr=False prevents accidental log/print of token
    content_available: bool = Field(default=True, alias="_contentAvailable")
    priority: str = "high"
    data: dict[str, Any] = Field(default_factory=dict)

    def to_expo_dict(self) -> dict[str, Any]:
        """Serialise to the dict Expo expects, using the wire key names."""
        return {
            "to": self.to,
            "_contentAvailable": self.content_available,
            "priority": self.priority,
            "data": self.data,
        }

    def to_payload_dict_without_token(self) -> dict[str, Any]:
        """Serialise without the token (safe for DB storage / logging)."""
        return {
            "_contentAvailable": self.content_available,
            "priority": self.priority,
            "data": self.data,
        }


class Ticket(BaseModel):
    """Expo ticket returned from POST /push/send for one message."""

    # extra="allow": Expo may add new fields to tickets in future API versions;
    # permitting extra fields avoids validation errors on forward-compatible changes.
    model_config = ConfigDict(extra="allow")

    status: str  # "ok" | "error"
    id: str | None = None  # Expo ticket ID (absent on error)
    message: str | None = None  # error description (e.g. "DeviceNotRegistered")
    details: dict[str, Any] | None = None


class Receipt(BaseModel):
    """Expo receipt returned from POST /push/getReceipts for one ticket."""

    # extra="allow": same rationale as Ticket \ Expo may add fields in future API versions.
    model_config = ConfigDict(extra="allow")

    status: str  # "ok" | error code
    message: str | None = None
    details: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PushClient(Protocol):
    """Structural protocol for push clients (production and fake)."""

    async def send(self, messages: Sequence[PushMessage]) -> list[Ticket]:
        """Send *messages* and return one :class:`Ticket` per message."""
        ...

    async def get_receipts(self, ticket_ids: Sequence[str]) -> dict[str, Receipt]:
        """Fetch receipt statuses for *ticket_ids*.

        Returns:
            Mapping of ticket_id → :class:`Receipt`.  Unknown IDs are absent.
        """
        ...


# ---------------------------------------------------------------------------
# ExpoPushClient
# ---------------------------------------------------------------------------


class ExpoPushClient:
    """Production Expo push client using httpx.

    Chunks messages into batches of ≤ 100 per Expo limit.  Retries HTTP-level
    5xx / 429 responses with capped exponential backoff up to :attr:`MAX_RETRIES`
    times.  Per-message errors (DeviceNotRegistered, etc.) are returned as
    :class:`Ticket` objects — they do NOT cause retries.

    The device token is never included in any log event.
    """

    CHUNK_SIZE: int = 100
    RECEIPT_CHUNK_SIZE: int = 1000
    MAX_RETRIES: int = 3
    BASE_BACKOFF_S: float = 1.0
    MAX_BACKOFF_S: float = 16.0

    def __init__(
        self,
        *,
        access_token: SecretStr | None = None,
        base_url: str = "https://exp.host/--/api/v2",
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._http_client = http_client
        self._closed: bool = False

    async def close(self) -> None:
        """Close the underlying :class:`httpx.AsyncClient` if one was injected.

        Called during lifespan shutdown so connections are released cleanly.
        If no client was injected (per-request ``async with`` mode), this is a no-op.
        Subsequent calls after the first are silently ignored (idempotent).
        """
        if self._closed or self._http_client is None:
            return
        self._closed = True
        await self._http_client.aclose()

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        if self._access_token is not None:
            # get_secret_value() is called here only; value not stored in a variable.
            headers["Authorization"] = f"Bearer {self._access_token.get_secret_value()}"
        return headers

    async def _post(self, path: str, body: object) -> dict[str, Any]:
        """POST to *path* (relative) and return parsed JSON.

        Retries on 5xx / 429 with exponential backoff.  Raises
        :class:`PushClientError` after :attr:`MAX_RETRIES` failed attempts.
        """
        url = f"{self._base_url}/{path.lstrip('/')}"
        payload = json.dumps(body)
        last_exc: Exception | None = None

        for attempt in range(self.MAX_RETRIES):
            if attempt > 0:
                backoff = min(self.BASE_BACKOFF_S * (2 ** (attempt - 1)), self.MAX_BACKOFF_S)
                await asyncio.sleep(backoff)

            try:
                if self._http_client is not None:
                    resp = await self._http_client.post(
                        url,
                        content=payload,
                        headers=self._build_headers(),
                        timeout=self._timeout,
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            url,
                            content=payload,
                            headers=self._build_headers(),
                            timeout=self._timeout,
                        )
            except httpx.RequestError as exc:
                last_exc = exc
                log.warning("expo_push_request_error", attempt=attempt + 1, error=str(exc))
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                log.warning(
                    "expo_push_http_error",
                    status=resp.status_code,
                    attempt=attempt + 1,
                )
                last_exc = PushClientError(f"HTTP {resp.status_code}")
                continue

            try:
                return resp.json()  # type: ignore[no-any-return]
            except Exception as exc:
                raise PushClientError(f"Invalid JSON from Expo: {exc}") from exc

        raise PushClientError(f"Expo push failed after {self.MAX_RETRIES} attempts") from last_exc

    async def send(self, messages: Sequence[PushMessage]) -> list[Ticket]:
        """Send *messages* in chunks of ≤ 100; return one Ticket per message."""
        if not messages:
            return []

        tickets: list[Ticket] = []
        msgs = list(messages)

        for i in range(0, len(msgs), self.CHUNK_SIZE):
            chunk = msgs[i : i + self.CHUNK_SIZE]
            body = [m.to_expo_dict() for m in chunk]
            resp = await self._post("push/send", body)
            data = resp.get("data", [])
            for item in data:
                tickets.append(Ticket.model_validate(item))

        return tickets

    async def get_receipts(self, ticket_ids: Sequence[str]) -> dict[str, Receipt]:
        """Fetch receipts for *ticket_ids* in chunks of ≤ 1000."""
        if not ticket_ids:
            return {}

        result: dict[str, Receipt] = {}
        ids = list(ticket_ids)

        for i in range(0, len(ids), self.RECEIPT_CHUNK_SIZE):
            chunk = ids[i : i + self.RECEIPT_CHUNK_SIZE]
            resp = await self._post("push/getReceipts", {"ids": chunk})
            data = resp.get("data", {})
            for tid, rec in data.items():
                result[tid] = Receipt.model_validate(rec)

        return result


# ---------------------------------------------------------------------------
# FakePushClient
# ---------------------------------------------------------------------------


class FakePushClient:
    """In-memory push client for tests.

    Outcomes are scripted via *outcomes* (list of status strings per message, in
    order; last entry is repeated for subsequent messages) and *receipt_outcomes*
    (dict of ticket_id → status string).
    """

    def __init__(
        self,
        *,
        outcomes: list[str] | None = None,
        receipt_outcomes: dict[str, str] | None = None,
    ) -> None:
        # outcomes: per-message status; "ok" | "DeviceNotRegistered" | "MessageRateExceeded" | ...
        self._outcomes: list[str] = outcomes if outcomes is not None else []
        self._receipt_outcomes: dict[str, str] = (
            receipt_outcomes if receipt_outcomes is not None else {}
        )
        self._outcome_index: int = 0

        # Inspection fields — cleared between scenarios in tests.
        self.sent: list[PushMessage] = []
        self.receipts_checked: list[str] = []

    def _next_outcome(self) -> str:
        if not self._outcomes:
            return "ok"
        idx = min(self._outcome_index, len(self._outcomes) - 1)
        self._outcome_index += 1
        return self._outcomes[idx]

    async def send(self, messages: Sequence[PushMessage]) -> list[Ticket]:
        self.sent.extend(messages)
        tickets: list[Ticket] = []
        for _ in messages:
            outcome = self._next_outcome()
            if outcome == "ok":
                ticket_id = str(uuid.uuid4())
                tickets.append(Ticket(status="ok", id=ticket_id))
            else:
                tickets.append(Ticket(status="error", id=None, message=outcome))
        return tickets

    async def get_receipts(self, ticket_ids: Sequence[str]) -> dict[str, Receipt]:
        self.receipts_checked.extend(ticket_ids)
        result: dict[str, Receipt] = {}
        for tid in ticket_ids:
            if tid in self._receipt_outcomes:
                result[tid] = Receipt(status=self._receipt_outcomes[tid])
            else:
                result[tid] = Receipt(status="ok")
        return result
