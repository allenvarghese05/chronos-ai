#!/usr/bin/env bash
# gen-types.sh — T0.7 shared schema tooling
#
# Exports the FastAPI OpenAPI schema from services/api, then generates
# TypeScript types for apps/mobile using openapi-typescript.
#
# The renderer zod step (T3.4) is guarded at the bottom; uncomment when the
# ReelScript/ReelProps Pydantic models exist in chronos/content/script/schema.py.
#
# Usage:
#   scripts/gen-types.sh           # from repo root or any subdir
#   just gen-types                 # via the justfile recipe
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── Step 1: export OpenAPI schema from the API ────────────────────────────────
echo "→ Exporting OpenAPI schema …"
(
  cd "$REPO_ROOT/services/api"
  uv run --locked chronos openapi export \
    --out "$REPO_ROOT/services/api/openapi.json"
)
echo "✓ services/api/openapi.json updated"

# ── Step 2: generate TypeScript types for apps/mobile ────────────────────────
if [ -f "$REPO_ROOT/apps/mobile/package.json" ]; then
  echo "→ Generating mobile TypeScript types …"
  mkdir -p "$REPO_ROOT/apps/mobile/src/api"
  (
    cd "$REPO_ROOT/apps/mobile"
    pnpm exec openapi-typescript \
      "$REPO_ROOT/services/api/openapi.json" \
      --output src/api/schema.d.ts
  )
  echo "✓ apps/mobile/src/api/schema.d.ts updated"
else
  echo "apps/mobile not scaffolded — skipping openapi-typescript"
fi

# ── Step 3: renderer zod schema (T3.4 — not yet active) ──────────────────────
# Uncomment when chronos.content.script.schema and chronos.media.reel_json exist:
#
# if (cd "$REPO_ROOT/services/api" && uv run --locked python -c \
#     "import chronos.content.script.schema" 2>/dev/null); then
#   echo "→ Generating renderer zod schema …"
#   (
#     cd "$REPO_ROOT/services/api"
#     uv run --locked chronos schema export-reel-props \
#       --out "$REPO_ROOT/packages/renderer/src/schema.json"
#   )
#   echo "✓ packages/renderer/src/schema.json updated"
# else
#   echo "ReelProps model not yet defined — skipping renderer zod (T3.4)"
# fi
