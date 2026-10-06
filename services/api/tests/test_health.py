"""Tests for /healthz and /readyz endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.testclient import TestClient

from chronos.config import Settings
from chronos.main import create_app


class TestHealthz:
    def test_healthz_200(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200

    def test_healthz_body_ok(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert body["ok"] is True
        assert "version" in body

    def test_healthz_version_matches_file(self, client: TestClient) -> None:
        """The version in /healthz must match the repo-root VERSION file."""
        # test file: services/api/tests/test_health.py → parents[3] = repo root
        version_file = Path(__file__).parents[3] / "VERSION"
        expected = version_file.read_text(encoding="utf-8").strip()
        body = client.get("/healthz").json()
        assert body["version"] == expected

    def test_healthz_content_type_json(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert "application/json" in response.headers["content-type"]


class TestReadyz:
    def test_readyz_200_when_db_ok(self, client: TestClient) -> None:
        response = client.get("/readyz")
        assert response.status_code == 200

    def test_readyz_body_ok(self, client: TestClient) -> None:
        body = client.get("/readyz").json()
        assert body["ok"] is True
        assert body["db"] == "ok"

    def test_readyz_503_when_db_fails(self, tmp_path: Path) -> None:
        """When the DB URL is unreachable, /readyz must return 503."""
        bad_settings = Settings(  # type: ignore[call-arg]
            env="test",
            # Point to a directory that cannot be a SQLite file.
            database_url=f"sqlite:///{tmp_path / 'nonexistent' / 'bad.db'}",
            secret_key="test-secret-key-32-bytes-exactly!",
        )
        app = create_app(settings=bad_settings)
        with TestClient(app, raise_server_exceptions=False) as c:
            response = c.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["db"].startswith("error:")


class TestProblemJson:
    def test_404_is_problem_json(self, client: TestClient) -> None:
        response = client.get("/this/does/not/exist")
        assert response.status_code == 404
        body = response.json()
        assert body["type"].startswith("urn:chronos:error:")
        assert "status" in body

    def test_404_content_type(self, client: TestClient) -> None:
        response = client.get("/no-such-path")
        assert "problem+json" in response.headers.get("content-type", "")


class TestErrorHandlers:
    """Tests for the 422 validation and 500 unhandled-exception handlers."""

    def _client_with_extra_routes(self, test_settings: Settings) -> TestClient:
        """Build a TestClient whose app has extra test-only routes."""
        # Register before create_app so the lifespan wires everything.
        _extra = APIRouter()

        @_extra.get("/test-validation")
        async def _validation_route(x: int) -> dict[str, int]:
            """Requires ?x=<int>; returns 422 when x is not an int."""
            return {"x": x}

        @_extra.get("/test-crash")
        async def _crash_route() -> None:
            """Always raises; triggers the 500 unhandled-exception handler."""
            raise RuntimeError("intentional test crash — secret=hidden")

        app = create_app(settings=test_settings)
        app.include_router(_extra)
        return TestClient(app, raise_server_exceptions=False)

    def test_422_validation_type(self, test_settings: Settings) -> None:
        """A bad query param must yield 422 with type urn:chronos:error:validation."""
        with self._client_with_extra_routes(test_settings) as c:
            response = c.get("/test-validation?x=not-an-int")
        assert response.status_code == 422
        body = response.json()
        assert body["type"] == "urn:chronos:error:validation"
        assert "problem+json" in response.headers.get("content-type", "")

    def test_500_internal_type(self, test_settings: Settings) -> None:
        """An unhandled exception must yield 500 with urn:chronos:error:internal."""
        with self._client_with_extra_routes(test_settings) as c:
            response = c.get("/test-crash")
        assert response.status_code == 500
        body = response.json()
        assert body["type"] == "urn:chronos:error:internal"
        assert "problem+json" in response.headers.get("content-type", "")

    def test_500_body_contains_no_exception_text(self, test_settings: Settings) -> None:
        """The 500 response body must not leak exception details or stack traces."""
        with self._client_with_extra_routes(test_settings) as c:
            response = c.get("/test-crash")
        body_text = response.text
        assert "intentional test crash" not in body_text
        assert "secret=hidden" not in body_text
        assert "Traceback" not in body_text
