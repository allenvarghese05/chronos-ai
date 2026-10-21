#!/usr/bin/env bash
# scripts/tests/test_laptop_setup_dry_run.sh
#
# Verifies that `laptop-setup.sh --dry-run` writes no files.
# Runs the script twice and asserts the working tree is unchanged.
#
# Usage: bash scripts/tests/test_laptop_setup_dry_run.sh
#        (also invoked via `just test-dry-run`)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SETUP="${REPO_ROOT}/scripts/laptop-setup.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $*"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $*" >&2; FAIL=$((FAIL + 1)); }

echo ""
echo "=== laptop-setup.sh --dry-run smoke tests ==="
echo ""

# --------------------------------------------------------------------------
# Helper: snapshot the set of files in a directory tree, sorted.
# --------------------------------------------------------------------------
snapshot() {
    local dir="$1"
    if [ -d "${dir}" ]; then
        find "${dir}" -type f | sort
    fi
}

# --------------------------------------------------------------------------
# Test 1: first --dry-run run
# --------------------------------------------------------------------------
echo "Run 1: laptop-setup.sh --dry-run --skip-certs --skip-plists"

# Capture before-state for areas the script writes to.
BEFORE_DATA="$(snapshot "${REPO_ROOT}/data")"
BEFORE_ENV=""
[ -f "${REPO_ROOT}/services/api/.env" ] && BEFORE_ENV="exists" || BEFORE_ENV="absent"

# Run with --dry-run (non-interactive; no TTY).
output1="$(bash "${SETUP}" --dry-run --skip-certs --skip-plists 2>&1)" || true

# Assert no files created under data/
AFTER_DATA="$(snapshot "${REPO_ROOT}/data")"
if [ "${BEFORE_DATA}" = "${AFTER_DATA}" ]; then
    pass "run 1: data/ unchanged"
else
    fail "run 1: data/ changed; new files: $(comm -13 <(echo "${BEFORE_DATA}") <(echo "${AFTER_DATA}"))"
fi

# Assert .env not created/destroyed
AFTER_ENV=""
[ -f "${REPO_ROOT}/services/api/.env" ] && AFTER_ENV="exists" || AFTER_ENV="absent"
if [ "${BEFORE_ENV}" = "${AFTER_ENV}" ]; then
    pass "run 1: .env state unchanged (${AFTER_ENV})"
else
    fail "run 1: .env state changed from ${BEFORE_ENV} to ${AFTER_ENV}"
fi

# Assert output mentions the plist dry-run skip.
for agent in api deploy; do
    if echo "${output1}" | grep -q "Would render.*${agent}"; then
        pass "run 1: printed dry-run notice for ${agent} plist"
    fi
done

# Assert output contains at least one [dry-run] marker
if echo "${output1}" | grep -q "\[dry-run\]"; then
    pass "run 1: output contains [dry-run] markers"
else
    fail "run 1: no [dry-run] markers in output"
fi

echo ""

# --------------------------------------------------------------------------
# Test 2: second --dry-run run (idempotent)
# --------------------------------------------------------------------------
echo "Run 2: laptop-setup.sh --dry-run --skip-certs --skip-plists (again)"

BEFORE_DATA2="$(snapshot "${REPO_ROOT}/data")"
BEFORE_ENV2=""
[ -f "${REPO_ROOT}/services/api/.env" ] && BEFORE_ENV2="exists" || BEFORE_ENV2="absent"

output2="$(bash "${SETUP}" --dry-run --skip-certs --skip-plists 2>&1)" || true

AFTER_DATA2="$(snapshot "${REPO_ROOT}/data")"
if [ "${BEFORE_DATA2}" = "${AFTER_DATA2}" ]; then
    pass "run 2: data/ unchanged"
else
    fail "run 2: data/ changed; new files: $(comm -13 <(echo "${BEFORE_DATA2}") <(echo "${AFTER_DATA2}"))"
fi

AFTER_ENV2=""
[ -f "${REPO_ROOT}/services/api/.env" ] && AFTER_ENV2="exists" || AFTER_ENV2="absent"
if [ "${BEFORE_ENV2}" = "${AFTER_ENV2}" ]; then
    pass "run 2: .env state unchanged (${AFTER_ENV2})"
else
    fail "run 2: .env state changed from ${BEFORE_ENV2} to ${AFTER_ENV2}"
fi

if echo "${output2}" | grep -q "\[dry-run\]"; then
    pass "run 2: output contains [dry-run] markers"
else
    fail "run 2: no [dry-run] markers in output"
fi

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
echo ""

if [ "${FAIL}" -gt 0 ]; then
    echo "FAILED" >&2
    exit 1
fi
echo "OK"
