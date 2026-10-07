"""Chronos-AI API package."""

from pathlib import Path

# Read the version from the repo-root VERSION file.
# Layout: src/chronos/__init__.py → src/chronos → src → api → services → repo-root
_VERSION_FILE = Path(__file__).parents[4] / "VERSION"

__version__: str = _VERSION_FILE.read_text(encoding="utf-8").strip()
