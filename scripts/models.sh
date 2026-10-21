#!/usr/bin/env bash
# scripts/models.sh — download and verify Chronos AI model files (T0.6)
#
# Usage:
#   scripts/models.sh <ollama_model> <ollama_digest> [--pin] [--dry-run]
#
# Arguments:
#   <ollama_model>   Ollama model name+tag (e.g. "qwen2.5:7b-instruct")
#   <ollama_digest>  Expected Ollama digest from justfile (may be empty "")
#   --pin            Compute SHA256 on first download; write to scripts/models.lock
#   --dry-run        Print what would be done without downloading
#
# Model files are stored under data/models/ (gitignored).
# models.lock (committed) contains URLs, SHA256, sizes for non-Ollama models.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOCK_FILE="${SCRIPT_DIR}/models.lock"

OLLAMA_MODEL="${1:-qwen2.5:7b-instruct}"
OLLAMA_DIGEST="${2:-}"
PIN=0
DRY_RUN=0

# Parse flags (remaining args after positional)
shift 2 2>/dev/null || true
for arg in "$@"; do
    case "$arg" in
        --pin)     PIN=1     ;;
        --dry-run) DRY_RUN=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "  → $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }

# ---- SHA256 helper (portable macOS / Linux) ---------------------------------
sha256_file() {
    local path="$1"
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "${path}" | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum "${path}" | awk '{print $1}'
    else
        die "No SHA256 tool found (need shasum or sha256sum)"
    fi
}

# ---- resumable download with retry ------------------------------------------
download_file() {
    local url="$1"
    local dest="$2"
    local label="$3"

    if [ "${DRY_RUN}" = "1" ]; then
        echo "  [dry-run] Would download: ${url}"
        echo "  [dry-run]            to: ${dest}"
        return 0
    fi

    local tmp="${dest}.download"
    info "Downloading ${label}..."
    info "  URL: ${url}"
    info "  Dest: ${dest}"

    # Resume if partial download exists (--continue-at -)
    curl \
        --fail \
        --location \
        --retry 3 \
        --retry-delay 5 \
        --continue-at - \
        --progress-bar \
        --output "${tmp}" \
        "${url}" || { rm -f "${tmp}"; die "Download failed: ${url}"; }

    mv "${tmp}" "${dest}"
    ok "Downloaded: $(basename "${dest}")"
}

# ---- verify or pin SHA256 ---------------------------------------------------
# Returns 0 on success; exits non-zero on mismatch.
verify_or_pin() {
    local name="$1"
    local path="$2"
    local expected_sha="$3"

    if [ "${DRY_RUN}" = "1" ]; then
        return 0
    fi

    local actual_sha
    actual_sha="$(sha256_file "${path}")"

    if [ "${PIN}" = "1" ] && [ "${expected_sha}" = "PLACEHOLDER" ]; then
        info "Pinning SHA256 for ${name}: ${actual_sha}"
        # Update models.lock using Python (portable sed alternative)
        python3 - "${LOCK_FILE}" "${name}" "${actual_sha}" "$(wc -c < "${path}" | tr -d ' ')" <<'PYEOF'
import json, sys
lock_path, name, sha, size_str = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(lock_path) as f:
    lock = json.load(f)
if name in lock:
    lock[name]["sha256"] = sha
    lock[name]["size_bytes"] = int(size_str)
with open(lock_path, "w") as f:
    json.dump(lock, f, indent=2)
    f.write("\n")
print(f"  → Pinned {name}: sha256={sha[:16]}...")
PYEOF
        return 0
    fi

    if [ "${expected_sha}" = "PLACEHOLDER" ]; then
        warn "SHA256 not pinned for ${name} (run with --pin to lock it)"
        return 0
    fi

    if [ "${actual_sha}" = "${expected_sha}" ]; then
        ok "SHA256 OK: $(basename "${path}")"
    else
        echo "ERROR: SHA256 mismatch for ${name}!" >&2
        echo "  Expected: ${expected_sha}" >&2
        echo "  Got:      ${actual_sha}" >&2
        rm -f "${path}"
        exit 1
    fi
}

# ---- read models.lock -------------------------------------------------------
[ -f "${LOCK_FILE}" ] || die "models.lock not found at ${LOCK_FILE}"

# Use Python to parse the lock file (handles JSON reliably)
parse_lock() {
    local field="$1"   # kokoro_onnx, kokoro_voices, whisper_small_en
    local key="$2"     # url, path, sha256
    python3 -c "
import json, sys
with open('${LOCK_FILE}') as f:
    lock = json.load(f)
print(lock['${field}']['${key}'])
"
}

echo ""
echo "=== Chronos model downloads ==="

# ─────────────────────────────────────────────────────────────────────────────
# Kokoro ONNX model
# ─────────────────────────────────────────────────────────────────────────────
KOKORO_URL="$(parse_lock kokoro_onnx url)"
KOKORO_PATH="${REPO_ROOT}/$(parse_lock kokoro_onnx path)"
KOKORO_SHA="$(parse_lock kokoro_onnx sha256)"

mkdir -p "$(dirname "${KOKORO_PATH}")"

if [ -f "${KOKORO_PATH}" ] && [ "${KOKORO_SHA}" != "PLACEHOLDER" ]; then
    ACTUAL=$(sha256_file "${KOKORO_PATH}")
    if [ "${ACTUAL}" = "${KOKORO_SHA}" ]; then
        ok "kokoro_onnx: already present and verified."
    else
        warn "kokoro_onnx: SHA mismatch — re-downloading."
        rm -f "${KOKORO_PATH}"
        download_file "${KOKORO_URL}" "${KOKORO_PATH}" "Kokoro ONNX model"
        verify_or_pin "kokoro_onnx" "${KOKORO_PATH}" "${KOKORO_SHA}"
    fi
else
    download_file "${KOKORO_URL}" "${KOKORO_PATH}" "Kokoro ONNX model"
    verify_or_pin "kokoro_onnx" "${KOKORO_PATH}" "${KOKORO_SHA}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Kokoro voices
# ─────────────────────────────────────────────────────────────────────────────
VOICES_URL="$(parse_lock kokoro_voices url)"
VOICES_PATH="${REPO_ROOT}/$(parse_lock kokoro_voices path)"
VOICES_SHA="$(parse_lock kokoro_voices sha256)"

if [ -f "${VOICES_PATH}" ] && [ "${VOICES_SHA}" != "PLACEHOLDER" ]; then
    ACTUAL=$(sha256_file "${VOICES_PATH}")
    if [ "${ACTUAL}" = "${VOICES_SHA}" ]; then
        ok "kokoro_voices: already present and verified."
    else
        warn "kokoro_voices: SHA mismatch — re-downloading."
        rm -f "${VOICES_PATH}"
        download_file "${VOICES_URL}" "${VOICES_PATH}" "Kokoro voices"
        verify_or_pin "kokoro_voices" "${VOICES_PATH}" "${VOICES_SHA}"
    fi
else
    download_file "${VOICES_URL}" "${VOICES_PATH}" "Kokoro voices"
    verify_or_pin "kokoro_voices" "${VOICES_PATH}" "${VOICES_SHA}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Whisper small.en model
# ─────────────────────────────────────────────────────────────────────────────
WHISPER_URL="$(parse_lock whisper_small_en url)"
WHISPER_PATH="${REPO_ROOT}/$(parse_lock whisper_small_en path)"
WHISPER_SHA="$(parse_lock whisper_small_en sha256)"

mkdir -p "$(dirname "${WHISPER_PATH}")"

if [ -f "${WHISPER_PATH}" ] && [ "${WHISPER_SHA}" != "PLACEHOLDER" ]; then
    ACTUAL=$(sha256_file "${WHISPER_PATH}")
    if [ "${ACTUAL}" = "${WHISPER_SHA}" ]; then
        ok "whisper_small_en: already present and verified."
    else
        warn "whisper_small_en: SHA mismatch — re-downloading."
        rm -f "${WHISPER_PATH}"
        download_file "${WHISPER_URL}" "${WHISPER_PATH}" "whisper ggml-small.en"
        verify_or_pin "whisper_small_en" "${WHISPER_PATH}" "${WHISPER_SHA}"
    fi
else
    download_file "${WHISPER_URL}" "${WHISPER_PATH}" "whisper ggml-small.en"
    verify_or_pin "whisper_small_en" "${WHISPER_PATH}" "${WHISPER_SHA}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Ollama model pull
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "--- Ollama model: ${OLLAMA_MODEL} ---"

if [ "${DRY_RUN}" = "1" ]; then
    echo "  [dry-run] Would run: ollama pull ${OLLAMA_MODEL}"
else
    if ! command -v ollama >/dev/null 2>&1; then
        warn "ollama not found in PATH. Skipping pull (run 'brew install ollama' first)."
    else
        info "Running: ollama pull ${OLLAMA_MODEL}"
        ollama pull "${OLLAMA_MODEL}"

        # Read digest after pull
        ACTUAL_DIGEST=$(ollama show --modelfile "${OLLAMA_MODEL}" 2>/dev/null \
            | grep -i "^#.*FROM\|digest\|sha256" | head -1 || true)
        # Try 'ollama list' approach
        ACTUAL_DIGEST=$(ollama list 2>/dev/null \
            | awk -v model="${OLLAMA_MODEL}" '$1 == model {print $3}' \
            | head -1 || echo "")

        if [ -n "${ACTUAL_DIGEST}" ]; then
            ok "Ollama model pulled. Digest from list: ${ACTUAL_DIGEST}"
            if [ -n "${OLLAMA_DIGEST}" ]; then
                if [ "${ACTUAL_DIGEST}" = "${OLLAMA_DIGEST}" ]; then
                    ok "Ollama digest matches justfile pin."
                else
                    warn "Ollama digest mismatch!"
                    warn "  Pinned:  ${OLLAMA_DIGEST}"
                    warn "  Current: ${ACTUAL_DIGEST}"
                fi
            else
                echo ""
                echo "  ╔══════════════════════════════════════════════════════════════╗"
                echo "  ║  Pin the Ollama digest in justfile:                          ║"
                printf "  ║  %-62s║\n" "ollama_digest := \"${ACTUAL_DIGEST}\""
                echo "  ╚══════════════════════════════════════════════════════════════╝"
            fi
        else
            warn "Could not read Ollama digest from 'ollama list'. Run manually:"
            warn "  ollama list | grep ${OLLAMA_MODEL%:*}"
        fi
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Remotion browser (Chromium headless shell)
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "--- Remotion browser ---"

RENDERER_DIR="${REPO_ROOT}/packages/renderer"
if [ -f "${RENDERER_DIR}/package.json" ]; then
    if [ "${DRY_RUN}" = "1" ]; then
        echo "  [dry-run] Would run: pnpm exec remotion browser ensure"
    else
        info "Ensuring Remotion browser (Chromium) is cached..."
        (cd "${RENDERER_DIR}" && pnpm exec remotion browser ensure)
        ok "Remotion browser ready."
    fi
else
    warn "packages/renderer not found — skipping Remotion browser."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "=== Model downloads complete ==="
if [ "${PIN}" = "1" ]; then
    echo "  SHA256 hashes pinned in ${LOCK_FILE}"
    echo "  Commit models.lock: git add scripts/models.lock && git commit -m 'chore(ops): pin model SHA256'"
fi
echo ""
