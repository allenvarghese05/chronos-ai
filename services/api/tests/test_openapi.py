"""Tests for chronos openapi export command (T0.7).

Covers:
- test_export_deterministic          : two consecutive exports → identical bytes
- test_committed_openapi_is_current  : committed openapi.json equals fresh export
- test_security_scheme_present       : HTTPBearer present in securitySchemes
- test_problem_component_present     : Problem present in components.schemas
- test_operation_ids_unique          : all operationIds are unique
- test_operation_ids_pattern         : all operationIds match ``<tag>_<name>``
- test_responses_401_on_auth_routes  : 401 declared on device/config/telemetry/day
- test_info_version_from_version_file: info.version matches repo VERSION file
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

# Repo-root VERSION file (services/api/tests/ → services/api/ → services/ → repo root).
_REPO_ROOT: Path = Path(__file__).parents[3]
_VERSION_FILE: Path = _REPO_ROOT / "VERSION"
_COMMITTED_OPENAPI: Path = _REPO_ROOT / "services" / "api" / "openapi.json"


def _build_schema_parsed() -> dict[str, Any]:
    """Build + round-trip through JSON so every value has a known Python type."""
    return json.loads(_export_json(_build_schema()))  # type: ignore[no-any-return]


def _build_schema() -> dict[str, Any]:
    """Build the OpenAPI schema the same way the CLI does."""
    from pydantic import SecretStr

    from chronos.config import Settings
    from chronos.main import create_app

    settings = Settings(
        secret_key=SecretStr("test-key-for-schema"),
        database_url="sqlite:///",
        skip_rules_init=True,
        scheduler_enabled=False,
    )
    app = create_app(settings)
    schema: dict[str, Any] = app.openapi()  # type: ignore[assignment]
    return schema


def _export_json(schema: dict[str, Any]) -> str:
    """Serialise schema the same way the CLI does: sort_keys, indent=2, trailing newline."""
    return json.dumps(schema, sort_keys=True, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_export_deterministic() -> None:
    """Running the export twice in one process yields byte-identical output."""
    out1 = _export_json(_build_schema())
    out2 = _export_json(_build_schema())
    assert out1 == out2, "OpenAPI export is not deterministic across two calls"


def test_committed_openapi_is_current() -> None:
    """The committed services/api/openapi.json must equal a fresh export.

    This is the staleness gate: if a developer changes a router without
    running ``just gen-types``, this test will fail in CI.
    """
    assert _COMMITTED_OPENAPI.exists(), (
        f"services/api/openapi.json not found at {_COMMITTED_OPENAPI}; "
        "run `just gen-types` to generate it."
    )
    committed = _COMMITTED_OPENAPI.read_text(encoding="utf-8")
    fresh = _export_json(_build_schema())
    assert committed == fresh, (
        "services/api/openapi.json is stale. Run `just gen-types` and commit the result."
    )


# ---------------------------------------------------------------------------
# Security scheme
# ---------------------------------------------------------------------------


def test_security_scheme_present() -> None:
    """HTTPBearer must appear in components.securitySchemes."""
    schema = _build_schema()
    schemes = schema.get("components", {}).get("securitySchemes", {})
    assert schemes, "components.securitySchemes is empty"
    # FastAPI names the scheme after the class: HTTPBearer
    assert "HTTPBearer" in schemes, (
        f"HTTPBearer not in securitySchemes; found: {list(schemes.keys())}"
    )
    bearer = schemes["HTTPBearer"]
    assert bearer.get("type") == "http"
    assert bearer.get("scheme") == "bearer"


# ---------------------------------------------------------------------------
# Problem component
# ---------------------------------------------------------------------------


def test_problem_component_present() -> None:
    """The Problem schema must be in components.schemas."""
    schema = _build_schema()
    schemas = schema.get("components", {}).get("schemas", {})
    assert "Problem" in schemas, (
        f"Problem not in components.schemas; found: {sorted(schemas.keys())}"
    )
    problem = schemas["Problem"]
    required_fields = {"type", "title", "status", "detail", "instance"}
    props = set(problem.get("properties", {}).keys())
    missing = required_fields - props
    assert not missing, f"Problem schema missing fields: {missing}"


# ---------------------------------------------------------------------------
# OperationIds
# ---------------------------------------------------------------------------


def test_operation_ids_unique() -> None:
    """All operationIds in the schema must be unique."""
    schema = _build_schema_parsed()
    seen: list[str] = []
    paths: dict[str, Any] = schema.get("paths") or {}
    for path_item in paths.values():
        ops = cast(dict[str, Any], path_item if isinstance(path_item, dict) else {})
        for op in ops.values():
            if not isinstance(op, dict):
                continue
            op_d = cast(dict[str, Any], op)
            if "operationId" in op_d:
                seen.append(str(op_d.get("operationId", "")))
    assert len(seen) == len(set(seen)), (
        f"Duplicate operationIds: {[x for x in seen if seen.count(x) > 1]}"
    )


def test_operation_ids_pattern() -> None:
    """All operationIds must match the pattern ``<tag>_<route-name>``."""
    schema = _build_schema_parsed()
    pattern = re.compile(r"^[a-z][a-z0-9]*_[a-z][a-z0-9_]*$")
    bad: list[str] = []
    paths: dict[str, Any] = schema.get("paths") or {}
    for path_item in paths.values():
        ops = cast(dict[str, Any], path_item if isinstance(path_item, dict) else {})
        for op in ops.values():
            if not isinstance(op, dict):
                continue
            op_d = cast(dict[str, Any], op)
            if "operationId" in op_d:
                oid = str(op_d.get("operationId", ""))
                if not pattern.match(oid):
                    bad.append(oid)
    assert not bad, f"operationIds not matching pattern <tag>_<name>: {bad}"


# ---------------------------------------------------------------------------
# 401 on authenticated routes
# ---------------------------------------------------------------------------

# Paths that are authenticated (require bearer).
_AUTH_PATH_PREFIXES = ("/v1/device", "/v1/config", "/v1/telemetry", "/v1/day")


def test_responses_401_on_auth_routes() -> None:
    """Authenticated routes must declare a 401 response."""
    schema = _build_schema_parsed()
    missing: list[str] = []
    paths: dict[str, Any] = schema.get("paths") or {}
    for path, path_item in paths.items():
        if not any(str(path).startswith(p) for p in _AUTH_PATH_PREFIXES):
            continue
        ops = cast(dict[str, Any], path_item if isinstance(path_item, dict) else {})
        for method, op in ops.items():
            if not isinstance(op, dict):
                continue
            op_d = cast(dict[str, Any], op)
            if "operationId" not in op_d:
                continue
            responses = cast(dict[str, Any], op_d.get("responses") or {})
            if "401" not in responses:
                missing.append(f"{str(method).upper()} {path}")
    assert not missing, f"Authenticated routes without 401 response: {missing}"


# ---------------------------------------------------------------------------
# Version from VERSION file
# ---------------------------------------------------------------------------


def test_info_version_from_version_file() -> None:
    """info.version must match the content of the repo-root VERSION file."""
    assert _VERSION_FILE.exists(), f"VERSION file not found at {_VERSION_FILE}"
    expected = _VERSION_FILE.read_text(encoding="utf-8").strip()
    schema = _build_schema()
    actual = schema.get("info", {}).get("version", "")
    assert actual == expected, (
        f"info.version mismatch: schema has {actual!r}, VERSION file has {expected!r}"
    )


def test_telemetry_publishes_exact_signals_body() -> None:
    schema = _build_schema_parsed()
    body = schema["paths"]["/v1/telemetry"]["post"]["requestBody"]
    assert body["required"] is True
    assert body["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Signals"}
    signals = schema["components"]["schemas"]["Signals"]
    assert signals["additionalProperties"] is False
    assert "seq" in signals["required"]
    assert "tick_seq" not in signals["required"]
    assert signals["properties"]["tick_seq"]["anyOf"][0]["minimum"] == 0
