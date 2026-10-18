"""In-memory token-bucket rate limiter (docs/09-security.md §2, owner answer 3).

One bucket per ``(name, IP)`` pair.  The clock is injectable so tests can
advance time without sleeping.  Limits come from Settings; the factory
:func:`rate_limit` returns a FastAPI dependency that raises
:class:`~chronos.errors.ProblemException` (429) with a ``Retry-After`` header
on exhaustion.

Thread-safety: each :class:`TokenBucket` uses an internal :class:`threading.Lock`.
"""

from __future__ import annotations

import threading
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Request

from chronos.errors import ProblemException


@dataclass
class _Bucket:
    """State for a single token-bucket slot."""

    tokens: float
    last_refill: float = field(default_factory=_time.monotonic)


class TokenBucket:
    """Thread-safe sliding-window token bucket.

    Args:
        per_minute: Maximum allowed requests per 60-second window.
        clock:      Callable returning monotonic seconds.  Defaults to
                    ``time.monotonic``.  Inject a fake in tests.

    Example::

        clock = iter([0.0, 1.0, 2.0, 61.0]).__next__
        bucket = TokenBucket(per_minute=2, clock=clock)
        assert bucket.check("ip") == (True, 0)    # 1st request: token consumed
        assert bucket.check("ip") == (True, 0)    # 2nd request: last token consumed
        allowed, retry = bucket.check("ip")       # 3rd: exhausted
        assert not allowed and retry > 0
    """

    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._per_minute = per_minute
        self._rate: float = per_minute / 60.0  # tokens per second
        self._clock: Callable[[], float] = clock or _time.monotonic
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Consume one token for *key* and report whether the request is allowed.

        Args:
            key: Rate-limit key (typically the client IP address).

        Returns:
            ``(allowed, retry_after_seconds)``: *allowed* is ``True`` when a
            token was consumed; *retry_after_seconds* is 0 when allowed and a
            positive integer when the bucket is exhausted.
        """
        now = self._clock()
        with self._lock:
            if key not in self._buckets:
                # First request: create a full bucket minus one token.
                self._buckets[key] = _Bucket(
                    tokens=float(self._per_minute) - 1.0,
                    last_refill=now,
                )
                return True, 0

            bucket = self._buckets[key]
            elapsed = now - bucket.last_refill
            bucket.tokens = min(float(self._per_minute), bucket.tokens + elapsed * self._rate)
            bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0

            # Exhausted: compute how many seconds until one token refills.
            retry_after = max(1, int((1.0 - bucket.tokens) / self._rate) + 1)
            return False, retry_after

    def reset(self) -> None:
        """Clear all buckets (tests only)."""
        with self._lock:
            self._buckets.clear()


# Module-level registry maps ``"name:per_minute"`` → ``TokenBucket``.
# Using a registry ensures that the same name always hits the same bucket,
# even when the dependency factory is called multiple times (once per router).
_registry: dict[str, TokenBucket] = {}
_registry_lock = threading.Lock()


def _get_or_create(
    name: str,
    per_minute: int,
    clock: Callable[[], float] | None,
) -> TokenBucket:
    """Return (and cache) the :class:`TokenBucket` for this name/rate pair."""
    reg_key = f"{name}:{per_minute}"
    with _registry_lock:
        if reg_key not in _registry:
            _registry[reg_key] = TokenBucket(per_minute=per_minute, clock=clock)
        return _registry[reg_key]


def reset_all() -> None:
    """Reset all registered bucket states (call in test setup/teardown).

    Resets the internal token counts of every registered bucket so that tests
    start with a clean slate.  The registry itself is *not* cleared — removing
    buckets would break module-level ``Depends`` closures that hold direct
    references to the bucket objects.
    """
    with _registry_lock:
        for bucket in _registry.values():
            bucket.reset()


def rate_limit(
    name: str,
    per_minute: int,
    clock: Callable[[], float] | None = None,
) -> Callable[[Request], None]:
    """Return a FastAPI dependency that enforces a per-IP rate limit.

    The dependency raises :class:`~chronos.errors.ProblemException` (429) with
    a ``Retry-After: <seconds>`` header when the bucket is exhausted.  The
    client IP is taken from ``request.client.host`` — ``X-Forwarded-For`` is
    deliberately ignored (LAN-direct per D16 / docs/09-security.md §2).

    Args:
        name:       Logical limiter name (e.g. ``"pairing"``).  Each name has
                    its own set of per-IP buckets.
        per_minute: Maximum requests per 60-second window per IP.
        clock:      Injectable monotonic clock (``Callable[[], float]``).
                    Defaults to ``time.monotonic``.  Pass a fake here in tests
                    to control time without sleeping.

    Returns:
        A callable ``(request: Request) -> None`` suitable for
        ``Depends(rate_limit(...))``.

    Example::

        _pairing_limit = rate_limit("pairing", per_minute=5)

        @router.post("/v1/pair")
        def pair(_rl: None = Depends(_pairing_limit)) -> ...:
            ...
    """
    bucket = _get_or_create(name, per_minute, clock)

    def _check(request: Request) -> None:
        ip: str = request.client.host if request.client else "unknown"
        allowed, retry_after = bucket.check(ip)
        if not allowed:
            raise ProblemException(
                status_code=429,
                code="rate_limited",
                title="Too Many Requests",
                detail=f"Rate limit exceeded. Retry after {retry_after} second(s).",
                headers={"Retry-After": str(retry_after)},
            )

    return _check
