#!/usr/bin/env bash
set -euo pipefail
command -v actionlint >/dev/null 2>&1 || { echo "actionlint not installed (uv tool install actionlint-py), skipping"; exit 0; }
actionlint "$@"
