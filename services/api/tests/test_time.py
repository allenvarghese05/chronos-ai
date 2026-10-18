"""Tests for chronos.time and chronos.ids helpers."""

from __future__ import annotations

import datetime
import re

import pytest

from chronos.ids import new_ulid
from chronos.time import parse_utc_iso, utc_now_iso

_ISO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_ULID_PATTERN = re.compile(r"^[0-9A-Z]{26}$")


class TestUtcNowIso:
    def test_format(self) -> None:
        result = utc_now_iso()
        assert _ISO_PATTERN.match(result), f"Unexpected format: {result!r}"

    def test_is_utc(self) -> None:
        result = utc_now_iso()
        parsed = parse_utc_iso(result)
        assert parsed.tzinfo == datetime.UTC

    def test_roundtrip(self) -> None:
        before = datetime.datetime.now(tz=datetime.UTC)
        iso = utc_now_iso()
        after = datetime.datetime.now(tz=datetime.UTC)
        parsed = parse_utc_iso(iso)
        assert before <= parsed <= after

    def test_monotonic_non_decreasing(self) -> None:
        """Two consecutive calls must return non-decreasing timestamps."""
        iso1 = utc_now_iso()
        iso2 = utc_now_iso()
        t1 = parse_utc_iso(iso1)
        t2 = parse_utc_iso(iso2)
        assert t1 <= t2, f"Timestamps not monotonic: {iso1!r} > {iso2!r}"

    def test_lexicographic_ordering(self) -> None:
        """ISO-8601 UTC strings with microseconds must sort lexicographically in time order."""
        import time

        iso1 = utc_now_iso()
        time.sleep(0.01)
        iso2 = utc_now_iso()
        assert iso1 < iso2, f"Lexicographic order violated: {iso1!r} >= {iso2!r}"


class TestParseUtcIso:
    def test_valid_string_seconds_precision(self) -> None:
        """Second-precision strings (legacy) must still be accepted."""
        s = "2026-10-06T07:12:04Z"
        dt = parse_utc_iso(s)
        assert dt.year == 2026
        assert dt.month == 10
        assert dt.day == 6
        assert dt.hour == 7
        assert dt.minute == 12
        assert dt.second == 4
        assert dt.tzinfo == datetime.UTC

    def test_valid_string_microseconds_precision(self) -> None:
        """Microsecond-precision strings produced by utc_now_iso must parse correctly."""
        s = "2026-10-06T07:12:04.123456Z"
        dt = parse_utc_iso(s)
        assert dt.year == 2026
        assert dt.microsecond == 123456
        assert dt.tzinfo == datetime.UTC

    def test_bad_input_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_utc_iso("not-a-date")

    def test_missing_z_suffix_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_utc_iso("2026-10-06T07:12:04")

    def test_non_string_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_utc_iso(None)  # type: ignore[arg-type]


class TestNewUlid:
    def test_format(self) -> None:
        u = new_ulid()
        assert isinstance(u, str)
        assert len(u) == 26, f"Expected 26 chars, got {len(u)}: {u!r}"
        assert _ULID_PATTERN.match(u), f"Non-ULID characters: {u!r}"

    def test_unique(self) -> None:
        seen = {new_ulid() for _ in range(100)}
        assert len(seen) == 100, "ULIDs must be unique"

    def test_sortable(self) -> None:
        """ULIDs generated in sequence must be lexicographically ordered."""
        import time

        u1 = new_ulid()
        time.sleep(0.002)
        u2 = new_ulid()
        assert u1 < u2, "ULIDs should be time-sortable"
