#!/usr/bin/env bash
# ruff check --fix + ruff format on staged files under services/api, using the locked venv.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)/services/api" 2>/dev/null || exit 0
[ -f pyproject.toml ] || exit 0
files=()
for f in "$@"; do files+=("${f#services/api/}"); done
[ ${#files[@]} -gt 0 ] || exit 0
uv run --locked ruff check --fix "${files[@]}"
uv run --locked ruff format "${files[@]}"
