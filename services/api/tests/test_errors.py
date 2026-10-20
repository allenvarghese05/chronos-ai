"""Tests for chronos.errors — RFC 9457 problem+json helpers."""

from __future__ import annotations

import pytest

from chronos.errors import make_problem


class TestMakeProblemExtensions:
    """Extension-member collision guard (review finding MINOR 7)."""

    # reserved members that must be protected
    _RESERVED = ("type", "title", "status", "detail", "instance", "code")

    def _base_kwargs(self) -> dict[str, object]:
        return {
            "status": 400,
            "code": "bad_request",
            "title": "Bad Request",
            "detail": "Something went wrong.",
            "instance": "/v1/test",
        }

    @pytest.mark.parametrize("key", _RESERVED)
    def test_colliding_key_raises(self, key: str) -> None:
        """Each reserved member, when passed as an extension key, raises ValueError."""
        with pytest.raises(ValueError, match="collide with reserved RFC 9457 members"):
            make_problem(**self._base_kwargs(), extensions={key: "override"})  # type: ignore[arg-type]

    def test_non_colliding_key_is_merged(self) -> None:
        """A non-reserved extension key is merged into the response body."""
        import json

        response = make_problem(
            **self._base_kwargs(),  # type: ignore[arg-type]
            extensions={"server_time": "2026-01-01T07:00:00.000000Z"},
        )
        body: dict[str, object] = json.loads(bytes(response.body))
        assert body["server_time"] == "2026-01-01T07:00:00.000000Z"
        # Standard members are still present and unmodified
        assert body["code"] == "bad_request"
        assert body["status"] == 400

    def test_no_extensions_still_works(self) -> None:
        """Calling make_problem without extensions returns a valid body."""
        import json

        response = make_problem(**self._base_kwargs())  # type: ignore[arg-type]
        body: dict[str, object] = json.loads(bytes(response.body))
        assert body["type"] == "urn:chronos:error:bad_request"
        assert body["status"] == 400
