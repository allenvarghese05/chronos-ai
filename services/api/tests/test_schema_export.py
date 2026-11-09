"""ReelProps export: deterministic zod/JSON, sample fixture parity, committed file current."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chronos.cli import app as cli_app
from chronos.media.reel_props import ReelProps
from chronos.schema_export import schema_json_text, zod_source

REPO = Path(__file__).parents[3]
GENERATED = REPO / "packages" / "renderer" / "src" / "schema.generated.ts"
JSON_SCHEMA = REPO / "services" / "api" / "reel_props.schema.json"
SAMPLE = REPO / "packages" / "renderer" / "src" / "fixtures" / "sample-reel.json"


def test_committed_generated_files_are_current() -> None:
    """Editing reel_props.py without `just gen-types` fails CI here."""
    assert GENERATED.read_text(encoding="utf-8") == zod_source()
    assert JSON_SCHEMA.read_text(encoding="utf-8") == schema_json_text()


def test_sample_reel_fixture_validates_in_python() -> None:
    props = ReelProps.model_validate_json(SAMPLE.read_text(encoding="utf-8"))
    assert props.tone == "calm"
    assert json.loads(props.to_json()) == json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_to_json_omits_none() -> None:
    props = ReelProps.model_validate_json(SAMPLE.read_text(encoding="utf-8"))
    assert "null" not in props.to_json()


def test_zod_output_shape() -> None:
    src = zod_source()
    assert src.startswith("// GENERATED")
    assert "export const ReelPropsBaseSchema = z.object({" in src
    assert "tone: z.enum(['calm', 'energetic'])," in src
    assert "durationSec: z.number().positive()," in src
    assert "url: z.string().optional()," in src
    assert src.index("export const MediaSchema") < src.index("export const SceneSchema")
    assert zod_source() == src  # deterministic


@pytest.mark.parametrize(
    "node",
    [
        {"type": "boolean"},
        {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        {"type": "object", "properties": {"x": {"oneOf": []}}},
    ],
)
def test_unsupported_schema_raises(node: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        zod_source({"title": "X", "type": "object", "properties": {"f": node}, "required": ["f"]})


def test_zod_number_and_array_bounds() -> None:
    schema = {
        "title": "T",
        "type": "object",
        "properties": {
            "a": {"type": "number", "minimum": 2, "exclusiveMinimum": 1},
            "b": {"type": "array", "items": {"type": "string", "maxLength": 3}, "maxItems": 2},
            "c": {"const": "x"},
            "d": {"type": "string", "pattern": "a/b"},
        },
        "required": ["a", "b", "c", "d"],
    }
    src = zod_source(schema)
    assert "a: z.number().gt(1).min(2)," in src
    assert "b: z.array(z.string().max(3)).max(2)," in src
    assert "c: z.literal('x')," in src
    assert r"d: z.string().regex(/a\/b/)," in src


def test_cli_writes_both_files(tmp_path: Path) -> None:
    zod_out, json_out = tmp_path / "s.generated.ts", tmp_path / "s.json"
    result = CliRunner().invoke(
        cli_app, ["schema", "export-reel-props", "--zod-out", str(zod_out), "--json-out",
                  str(json_out)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert zod_out.read_text() == zod_source()
    assert json.loads(json_out.read_text())["title"] == "ReelProps"
