"""Device authentication: token hashing, pairing code generation, bearer dependency.

Implements docs/05-api-contracts.md §1 and docs/09-security.md §2 auth controls.

Key functions
-------------
:func:`hash_token`            — SHA-256 hex of any token/code string.
:func:`new_token`             — Generate a 256-bit bearer token (43 base64url chars).
:func:`generate_pairing_code` — 8-char code from the no-ambiguity alphabet.
:func:`normalise_pairing_code`— Uppercase + strip dashes/spaces (canonical form).
:func:`require_device`        — FastAPI Depends: validate Bearer → Device or 401/403.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Request
from sqlalchemy.orm import Session
from sqlmodel import select

from chronos.db import get_session_dep
from chronos.errors import ProblemException
from chronos.models.devices import Device
from chronos.time import parse_utc_iso, utc_now_iso

# ── pairing code alphabet (no 0, O, 1, I to avoid visual ambiguity) ──────────
#: 32-character Crockford-like alphabet used for pairing codes.
PAIRING_ALPHABET: str = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: Number of seconds before a ``last_seen_at`` bump is written to the DB.
LAST_SEEN_THROTTLE_SEC: int = 60


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of *token* (used for both bearer tokens and pairing codes).

    Args:
        token: Plaintext token or normalised pairing code.

    Returns:
        A 64-character lowercase hex string.

    Example::

        >>> hash_token("abc")
        'ba7816bf8f01cfea414140de5dae2ec73b00361bbef0469f490f4c339d84a272'  # noqa: RUF003
    """
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    """Generate a 256-bit bearer token encoded as URL-safe base64 (43 chars).

    The plaintext is returned **once** (pairing response) and must never be
    logged.  The SHA-256 hash is stored in ``devices.token_hash``.

    Returns:
        A 43-character base64url string.
    """
    return secrets.token_urlsafe(32)


def generate_pairing_code() -> str:
    """Generate an 8-character pairing code from :data:`PAIRING_ALPHABET`.

    The code is returned **without** the display dash (use ``code[:4] + '-' +
    code[4:]`` for printing).  It must be hashed with
    ``hash_token(normalise_pairing_code(code))`` before storing.

    Returns:
        An 8-character uppercase alphanumeric string (no 0/O/1/I).
    """
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))


def normalise_pairing_code(code: str) -> str:
    """Canonical form of a pairing code: uppercase with dashes and spaces stripped.

    This function is the **only** place that normalises a code before hashing.
    Apply it consistently on both the server side (before storing the hash) and
    on submission (before comparing).

    Args:
        code: Raw code as the user typed it (e.g. ``"7k2m-9qxd"`` or ``"7K2M9QXD"``).

    Returns:
        Canonical string (e.g. ``"7K2M9QXD"``).
    """
    return code.upper().replace("-", "").replace(" ", "")


def _constant_time_equal(a: str, b: str) -> bool:
    """Constant-time string comparison using :func:`hmac.compare_digest`."""
    return hmac.compare_digest(a.encode(), b.encode())


def _should_bump_last_seen(last_seen_at: str, now: datetime) -> bool:
    """Return ``True`` if the stored ``last_seen_at`` is older than :data:`LAST_SEEN_THROTTLE_SEC`.

    Args:
        last_seen_at: ISO-8601 UTC string from the device row.
        now:          Current UTC datetime (tz-aware).
    """
    try:
        last = parse_utc_iso(last_seen_at)
    except ValueError:
        return True
    return (now - last) >= timedelta(seconds=LAST_SEEN_THROTTLE_SEC)


def require_device(
    request: Request,
    session: Session = Depends(get_session_dep),
) -> Device:
    """FastAPI dependency: extract and validate the bearer token → return :class:`Device`.

    Raises:
        :class:`~chronos.errors.ProblemException` 401 ``unauthorized``:
            Missing or malformed ``Authorization`` header, or token hash not found.
        :class:`~chronos.errors.ProblemException` 403 ``revoked``:
            Token found in DB but device status is ``revoked``.

    On success the device's ``last_seen_at`` is updated synchronously at most
    once per :data:`LAST_SEEN_THROTTLE_SEC` seconds.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise ProblemException(
            status_code=401,
            code="unauthorized",
            title="Unauthorized",
            detail="Missing or malformed Authorization header.",
        )

    raw_token = auth_header[len("Bearer ") :]
    if not raw_token:
        raise ProblemException(
            status_code=401,
            code="unauthorized",
            title="Unauthorized",
            detail="Bearer token is empty.",
        )

    token_hash = hash_token(raw_token)

    # Lookup by hash — never compare plaintext.
    device = session.exec(select(Device).where(Device.token_hash == token_hash)).first()

    if device is None:
        raise ProblemException(
            status_code=401,
            code="unauthorized",
            title="Unauthorized",
            detail="Invalid token.",
        )

    if device.status == "revoked":
        raise ProblemException(
            status_code=403,
            code="revoked",
            title="Forbidden",
            detail="Device token has been revoked.",
        )

    # Bump last_seen_at at most once per LAST_SEEN_THROTTLE_SEC.
    now = datetime.now(tz=UTC)
    if _should_bump_last_seen(device.last_seen_at, now):
        device.last_seen_at = utc_now_iso()
        session.add(device)
        session.commit()
        session.refresh(device)

    return device
