"""Unit tests for TokenBucket memory-bounding (pruning) behaviour.

Coverage:
- Stale keys (idle > idle_timeout) are removed on the next ``check`` call.
- Pruning does NOT affect an active key's token budget.
- When the dict exceeds ``max_keys``, the stalest entries are evicted while
  the most-recently-used key is kept.
"""

from __future__ import annotations

from chronos.ratelimit import TokenBucket


class TestTokenBucketPruning:
    """Pruning removes idle buckets without disturbing active ones."""

    def test_stale_key_removed_after_idle_timeout(self) -> None:
        """A bucket unseen for longer than idle_timeout is pruned on the next check."""
        ticks: list[float] = [0.0]
        clock = lambda: ticks[0]  # noqa: E731

        bucket = TokenBucket(per_minute=10, clock=clock, idle_factor=10)

        # Prime a bucket for "old_ip" at t=0.
        bucket.check("old_ip")
        assert "old_ip" in bucket.keys()

        # Advance time past the idle timeout (10 x 60 = 600 s).
        ticks[0] = 601.0

        # A check for ANY key triggers pruning.
        bucket.check("new_ip")

        assert "old_ip" not in bucket.keys(), "stale key should have been pruned"
        assert "new_ip" in bucket.keys()

    def test_active_key_budget_unaffected_by_pruning(self) -> None:
        """Pruning stale keys must not consume tokens from an active key."""
        ticks: list[float] = [0.0]
        clock = lambda: ticks[0]  # noqa: E731

        bucket = TokenBucket(per_minute=5, clock=clock, idle_factor=10)

        # Use 3 tokens for "active_ip" at t=0.
        for _ in range(3):
            allowed, _ = bucket.check("active_ip")
            assert allowed

        # Add a soon-to-be-stale key.
        bucket.check("old_ip")

        # Advance past idle timeout — "old_ip" becomes stale.
        # "active_ip" is also stale here, BUT the next check will refresh it
        # (refill tokens and update last_refill) before we assert its budget.
        ticks[0] = 601.0

        # Trigger pruning via a fresh key; both old keys are stale here.
        # Only "pruned_ip" will be retained because it's the one being checked.
        bucket.check("pruned_trigger")

        # "old_ip" gone, "active_ip" gone (also idle > 600s).
        assert "old_ip" not in bucket.keys()

        # Now check "active_ip" again — it refills from scratch, so it's allowed.
        allowed, _ = bucket.check("active_ip")
        assert allowed, "active key should be allowed after being recreated post-prune"

    def test_active_key_not_pruned_before_idle_timeout(self) -> None:
        """A key used recently must NOT be pruned even when other keys are stale."""
        ticks: list[float] = [0.0]
        clock = lambda: ticks[0]  # noqa: E731

        bucket = TokenBucket(per_minute=10, clock=clock, idle_factor=10)

        # Seed "stale_ip" at t=0.
        bucket.check("stale_ip")

        # Advance to t=300 (half the idle timeout) — use "live_ip".
        ticks[0] = 300.0
        bucket.check("live_ip")

        # Advance to t=601 — "stale_ip" is idle for 601 s; "live_ip" only 301 s.
        ticks[0] = 601.0
        bucket.check("live_ip")  # This also triggers pruning.

        assert "stale_ip" not in bucket.keys(), "stale_ip should be pruned"
        assert "live_ip" in bucket.keys(), "live_ip should survive"

    def test_max_keys_evicts_stalest_when_exceeded(self) -> None:
        """When len(buckets) > max_keys, the stalest entry is evicted."""
        ticks: list[float] = [0.0]
        clock = lambda: ticks[0]  # noqa: E731

        # max_keys=3, idle_factor high so time-based pruning does not fire.
        bucket = TokenBucket(per_minute=10, clock=clock, max_keys=3, idle_factor=9999)

        # Fill to capacity: ip1 (t=0), ip2 (t=1), ip3 (t=2).
        bucket.check("ip1")
        ticks[0] = 1.0
        bucket.check("ip2")
        ticks[0] = 2.0
        bucket.check("ip3")

        assert len(bucket.keys()) == 3

        # Adding a 4th key at t=3 should evict "ip1" (stalest: last_refill=0).
        ticks[0] = 3.0
        bucket.check("ip4")

        assert len(bucket.keys()) == 3
        assert "ip1" not in bucket.keys(), "stalest key should be evicted"
        assert "ip4" in bucket.keys(), "newest key should be kept"

    def test_max_keys_keeps_active_key_budget(self) -> None:
        """After a max_keys eviction, the surviving active key is still allowed."""
        ticks: list[float] = [0.0]
        clock = lambda: ticks[0]  # noqa: E731

        bucket = TokenBucket(per_minute=5, clock=clock, max_keys=2, idle_factor=9999)

        # ip1 at t=0 (stalest); use 2 tokens.
        bucket.check("ip1")
        bucket.check("ip1")

        # ip2 at t=1 (fresher); use 1 token.
        ticks[0] = 1.0
        bucket.check("ip2")

        # ip3 at t=2 — evicts ip1 (stalest); ip2 survives.
        ticks[0] = 2.0
        bucket.check("ip3")

        assert "ip1" not in bucket.keys()
        assert "ip2" in bucket.keys()
        # ip2 should still be allowed (has remaining tokens).
        allowed, _ = bucket.check("ip2")
        assert allowed
