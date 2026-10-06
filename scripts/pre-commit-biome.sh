#!/usr/bin/env bash
# biome check --write on staged files of apps/mobile or packages/renderer (first arg = app dir).
set -euo pipefail
app="$1"; shift
cd "$(git rev-parse --show-toplevel)/$app" 2>/dev/null || exit 0
[ -f package.json ] || exit 0
files=()
for f in "$@"; do files+=("${f#$app/}"); done
[ ${#files[@]} -gt 0 ] || exit 0
pnpm exec biome check --write --no-errors-on-unmatched "${files[@]}"
