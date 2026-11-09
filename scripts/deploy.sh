#!/usr/bin/env bash
# scripts/deploy.sh — pull-based CD script for Chronos-AI (T0.6)
#
# Runs every 10 min via com.chronos.deploy LaunchAgent.
# Fast-forward main → sync deps → migrate → restart API → health check.
# Skips if inside the deploy-window exclusion or if already up-to-date.
# Rolls back on health-check failure.
#
# Usage: scripts/deploy.sh [--dry-run]
#   --dry-run : print actions without executing; always exits 0
#
# Exit codes:
#   0 — success (deployed, skipped, or up-to-date)
#   1 — failure (merge failed, health-check failed after rollback)

set -euo pipefail

# ---- configuration ----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
API_DIR="${REPO_ROOT}/services/api"
RENDERER_DIR="${REPO_ROOT}/packages/renderer"
LOCK_FILE="${REPO_ROOT}/data/deploy.lock"
LOG_FILE="${REPO_ROOT}/data/logs/deploy.jsonl"
ENV_FILE="${API_DIR}/.env"
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# ---- helpers ----------------------------------------------------------------
TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

log_json() {
    local status="$1"
    local sha="${2:-unknown}"
    local msg="${3:-}"
    local entry
    entry="{\"ts\":\"$(TS)\",\"sha\":\"${sha}\",\"status\":\"${status}\",\"msg\":\"${msg}\"}"
    if [ "${DRY_RUN}" != "1" ]; then
        echo "${entry}" >> "${LOG_FILE}" 2>/dev/null || true
    fi
    echo "[deploy] ${status}: ${msg}" >&2
}

die_with_log() {
    local sha="${1:-unknown}"
    local msg="${2:-unknown error}"
    log_json "failed" "${sha}" "${msg}"
    exit 1
}

dry() {
    if [ "${DRY_RUN}" = "1" ]; then
        echo "  [dry-run] $*"
        return 0
    fi
    "$@"
}

# ---- load .env for host/port (best-effort) ----------------------------------
# shellcheck source=/dev/null
[ -f "${ENV_FILE}" ] && source "${ENV_FILE}" || true
API_HOST="${CHRONOS_API_BIND_HOST:-127.0.0.1}"
API_PORT="${CHRONOS_API_BIND_PORT:-8443}"
ROOT_CA="${REPO_ROOT}/data/certs/rootCA.pem"

# ---- lockfile (prevent overlapping runs) ------------------------------------
if [ "${DRY_RUN}" != "1" ]; then
    mkdir -p "$(dirname "${LOCK_FILE}")"
    # Stock macOS has no flock(1) (and its shlock(1) will not reclaim a dead holder's
    # lock), so fall back to an atomic mkdir lock holding our PID. A lock whose PID is
    # no longer alive is stale and is taken over. Never report a missing tool as "held".
    if command -v flock >/dev/null 2>&1; then
        exec 9>"${LOCK_FILE}"
        if ! flock -n 9; then
            echo "[deploy] Another deploy is running (lockfile held). Exiting." >&2
            exit 0
        fi
    else
        LOCK_DIR="${LOCK_FILE}.d"
        if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
            HOLDER="$(cat "${LOCK_DIR}/pid" 2>/dev/null || true)"
            if [ -n "${HOLDER}" ] && kill -0 "${HOLDER}" 2>/dev/null; then
                echo "[deploy] Another deploy is running (pid ${HOLDER}). Exiting." >&2
                exit 0
            fi
            echo "[deploy] Removing stale lock (pid ${HOLDER:-unknown} not running)." >&2
            rm -f "${LOCK_DIR}/pid"
            rmdir "${LOCK_DIR}" 2>/dev/null || true
            if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
                echo "[deploy] Lost the race for the lock. Exiting." >&2
                exit 0
            fi
        fi
        echo $$ > "${LOCK_DIR}/pid"
        trap 'rm -f "${LOCK_DIR}/pid"; rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT
    fi
fi

# ---- git fetch --------------------------------------------------------------
echo "[deploy] Fetching origin/main..."
dry git -C "${REPO_ROOT}" fetch origin main --quiet

PREV="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo "unknown")"
REMOTE="$(git -C "${REPO_ROOT}" rev-parse origin/main 2>/dev/null || echo "unknown")"

# ---- up-to-date check -------------------------------------------------------
if [ "${PREV}" = "${REMOTE}" ]; then
    log_json "up-to-date" "${PREV}" "HEAD == origin/main; nothing to do"
    exit 0
fi

echo "[deploy] New commit on origin/main: ${REMOTE} (was ${PREV})"

# ---- deploy window guard ----------------------------------------------------
echo "[deploy] Checking deploy window..."
if [ "${DRY_RUN}" = "1" ]; then
    echo "  [dry-run] Would check: chronos ops in-deploy-window"
else
    # Exit 0 from in-deploy-window = INSIDE window = SKIP
    if (cd "${API_DIR}" && uv run --locked chronos ops in-deploy-window) 2>/dev/null; then
        log_json "skipped" "${REMOTE}" "inside deploy-window exclusion; will retry in 10 min"
        exit 0
    fi
fi

# ---- commit signature check (warning-only until D20) -----------------------
if ! dry git -C "${REPO_ROOT}" verify-commit "origin/main" 2>/dev/null; then
    echo "[deploy] WARNING: commit signing not configured; skipping verify (open question #10)" >&2
fi

# ---- fast-forward merge -----------------------------------------------------
echo "[deploy] Merging origin/main (ff-only)..."
if ! dry git -C "${REPO_ROOT}" merge --ff-only origin/main; then
    die_with_log "${REMOTE}" "git merge --ff-only failed (local commits on main?)"
fi
NEW="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo "${REMOTE}")"

# ---- sync Python deps -------------------------------------------------------
echo "[deploy] Syncing Python dependencies..."
dry uv sync --locked --project "${API_DIR}"

# ---- sync renderer deps (if present) ----------------------------------------
if [ -f "${RENDERER_DIR}/package.json" ]; then
    echo "[deploy] Syncing renderer dependencies..."
    dry pnpm --dir "${RENDERER_DIR}" install --frozen-lockfile
fi

# ---- database migration ------------------------------------------------------
echo "[deploy] Running database migrations..."
dry uv run --locked --project "${API_DIR}" chronos db migrate

# ---- config validate --------------------------------------------------------
echo "[deploy] Validating config..."
dry uv run --locked --project "${API_DIR}" chronos config validate

# ---- restart API ------------------------------------------------------------
echo "[deploy] Restarting com.chronos.api..."
dry launchctl kickstart -k "gui/${UID}/com.chronos.api"

# ---- health check with retries ----------------------------------------------
echo "[deploy] Waiting for API health check..."
READYZ_URL="https://${API_HOST}:${API_PORT}/readyz"
CACERT_ARG=""
[ -f "${ROOT_CA}" ] && CACERT_ARG="--cacert ${ROOT_CA}"

if [ "${DRY_RUN}" = "1" ]; then
    echo "  [dry-run] Would check: curl -fsS ${CACERT_ARG} ${READYZ_URL}"
else
    HEALTHY=0
    for attempt in 1 2 3 4 5; do
        sleep $((attempt * 2))
        # shellcheck disable=SC2086
        if curl -fsS ${CACERT_ARG} "${READYZ_URL}" >/dev/null 2>&1; then
            HEALTHY=1
            break
        fi
        echo "[deploy] Health check attempt ${attempt}/5 failed; retrying..." >&2
    done

    if [ "${HEALTHY}" != "1" ]; then
        echo "[deploy] Health check failed after 5 attempts — rolling back..." >&2
        git -C "${REPO_ROOT}" reset --hard "${PREV}"
        uv sync --locked --project "${API_DIR}" 2>/dev/null || true
        launchctl kickstart -k "gui/${UID}/com.chronos.api" 2>/dev/null || true
        die_with_log "${NEW}" "health check failed; rolled back to ${PREV}"
    fi
fi

# ---- success ----------------------------------------------------------------
log_json "ok" "${NEW}" "deployed from ${PREV}"
echo "[deploy] Deploy complete: ${PREV} → ${NEW}"
