"""Tests for chronos.content.sources loader and schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chronos.content.sources import load_sources_yaml


# Helper: build a minimal valid YAML dict and dump it.
def _minimal_yaml() -> str:
    return (
        "version: 1\n"
        "http:\n"
        "  user_agent: Test/1.0\n"
        "  timeout_sec: 15\n"
        "  max_bytes: 2000000\n"
        "  max_redirects: 3\n"
        "sources:\n"
        "  - key: test_src\n"
        "    kind: rss\n"
        "    url: https://example.com/feed\n"
        "    category: ai\n"
        "    quota: 1\n"
        "    weight: 1.0\n"
        "selection:\n"
        "  total_stories: 6\n"
        "  category_min:\n"
        "    ai: 2\n"
        "  freshness_hours: 20\n"
        "  cross_source_boost: 0.25\n"
    )


def test_load_valid_yaml_ok(tmp_path: Path) -> None:
    p = tmp_path / "sources.yaml"
    p.write_text(_minimal_yaml())
    cfg = load_sources_yaml(p)
    assert cfg.version == 1
    assert cfg.http.user_agent == "Test/1.0"
    assert len(cfg.sources) == 1
    assert cfg.sources[0].key == "test_src"
    assert cfg.sources[0].quota == 1


def test_duplicate_key_rejected(tmp_path: Path) -> None:
    text = _minimal_yaml().replace(
        "sources:",
        "sources:\n"
        "  - key: test_src\n"
        "    kind: rss\n"
        "    url: https://example.com/feed\n"
        "    category: ai\n"
        "    quota: 1\n"
        "    weight: 1.0\n",
    )
    p = tmp_path / "sources.yaml"
    p.write_text(text)
    with pytest.raises(ValidationError) as exc_info:
        load_sources_yaml(p)
    assert "duplicate source key" in str(exc_info.value)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    text = _minimal_yaml().replace("    quota: 1", "    quota: 1\n    unknown_field: x")
    p = tmp_path / "sources.yaml"
    p.write_text(text)
    with pytest.raises(ValidationError):
        load_sources_yaml(p)


def test_bad_quota_rejected(tmp_path: Path) -> None:
    text = _minimal_yaml().replace("    quota: 1", "    quota: -1")
    p = tmp_path / "sources.yaml"
    p.write_text(text)
    with pytest.raises(ValidationError):
        load_sources_yaml(p)


def test_bad_kind_rejected(tmp_path: Path) -> None:
    text = _minimal_yaml().replace("    kind: rss", "    kind: atom")
    p = tmp_path / "sources.yaml"
    p.write_text(text)
    with pytest.raises(ValidationError):
        load_sources_yaml(p)


def test_load_sources_yaml_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "nonexistent.yaml"
    with pytest.raises(FileNotFoundError):
        load_sources_yaml(p)
