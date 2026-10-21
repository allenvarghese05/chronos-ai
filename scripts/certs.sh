#!/usr/bin/env bash
# scripts/certs.sh — generate mkcert TLS certificates for the Chronos LAN API (T0.6)
#
# Idempotent: skips generation if the leaf cert exists and is not expired within 7 days.
# Requires: mkcert (brew install mkcert), openssl (macOS built-in)
#
# Outputs (mode 600):
#   data/certs/chronos-lan.pem      — leaf cert (SAN: <hostname>.local + LAN IP + localhost)
#   data/certs/chronos-lan-key.pem  — private key
#   data/certs/rootCA.pem           — copy of mkcert root CA (install on iPhone)
#
# Usage: scripts/certs.sh [--force]
#   --force : regenerate even if cert exists and is valid

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CERT_DIR="${REPO_ROOT}/data/certs"
LEAF_CERT="${CERT_DIR}/chronos-lan.pem"
LEAF_KEY="${CERT_DIR}/chronos-lan-key.pem"
ROOT_CA="${CERT_DIR}/rootCA.pem"
FORCE=0

# ---- parse args -------------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# ---- helpers ----------------------------------------------------------------

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "  → $*"; }

is_interactive() { [ -t 0 ] && [ -t 1 ]; }

# ---- check mkcert is installed ----------------------------------------------
if ! command -v mkcert >/dev/null 2>&1; then
    die "mkcert not found. Install with: brew install mkcert"
fi

echo "=== Chronos cert setup ==="

# ---- idempotency check -------------------------------------------------------
if [ "${FORCE}" = "0" ] && [ -f "${LEAF_CERT}" ]; then
    # Check if cert expires in more than 7 days
    if openssl x509 -noout -checkend 604800 -in "${LEAF_CERT}" >/dev/null 2>&1; then
        EXPIRY=$(openssl x509 -noout -enddate -in "${LEAF_CERT}" 2>/dev/null | cut -d= -f2 || echo "unknown")
        info "Leaf cert already exists and is valid (expires: ${EXPIRY}). Use --force to regenerate."
        exit 0
    else
        info "Leaf cert exists but expires within 7 days — regenerating."
    fi
fi

# ---- install root CA in system keychain (requires sudo) ---------------------
CAROOT=$(mkcert -CAROOT)
info "mkcert CAROOT: ${CAROOT}"

# mkcert -install puts the root CA in the system keychain (required for iOS/macOS to trust it).
# We still generate certs even without -install; iOS trust requires the manual step below.
if [ ! -f "${CAROOT}/rootCA.pem" ]; then
    echo ""
    echo "  mkcert root CA not yet created. Generating now (certs will be created)."
    echo "  To make macOS/iOS trust them, run: mkcert -install (requires sudo)"
fi

# Print the sudo prompt or banner for mkcert -install (keychain trust, optional for generation)
if ! security find-certificate -c 'mkcert' /Library/Keychains/System.keychain >/dev/null 2>&1; then
    if is_interactive; then
        echo ""
        echo "  mkcert root CA not in system keychain. Running mkcert -install (may prompt for sudo)..."
        mkcert -install || info "mkcert -install skipped (run manually: sudo mkcert -install)"
    else
        echo ""
        echo "  ╔══════════════════════════════════════════════════════════╗"
        echo "  ║  RUN THIS (requires sudo) to trust certs on this Mac:   ║"
        echo "  ║    mkcert -install                                       ║"
        echo "  ╚══════════════════════════════════════════════════════════╝"
        echo "  (Continuing cert generation — iOS trust requires this step)"
    fi
else
    info "Root CA already installed in system keychain."
fi

# ---- detect hostname and LAN IP ---------------------------------------------
HOSTNAME_LOCAL="$(scutil --get LocalHostName 2>/dev/null || echo "allen-macbook").local"
info "mDNS hostname: ${HOSTNAME_LOCAL}"

LAN_IP="${CHRONOS_LAN_IP:-$(ipconfig getifaddr en0 2>/dev/null || echo "")}"
if [ -z "${LAN_IP}" ]; then
    echo "  WARN: Could not detect LAN IP via 'ipconfig getifaddr en0'."
    echo "        Set CHRONOS_LAN_IP env var to override."
    LAN_IP="127.0.0.1"
fi
info "LAN IP: ${LAN_IP}"

# ---- create cert dir --------------------------------------------------------
mkdir -p "${CERT_DIR}"

# ---- generate leaf cert ------------------------------------------------------
info "Generating leaf cert with SANs: ${HOSTNAME_LOCAL} ${LAN_IP} localhost 127.0.0.1"

mkcert \
    -cert-file "${LEAF_CERT}" \
    -key-file  "${LEAF_KEY}" \
    "${HOSTNAME_LOCAL}" \
    "${LAN_IP}" \
    "localhost" \
    "127.0.0.1"

# ---- set permissions ---------------------------------------------------------
chmod 600 "${LEAF_CERT}" "${LEAF_KEY}"
info "Set mode 600 on cert and key."

# ---- copy root CA to data/certs/ --------------------------------------------
cp "${CAROOT}/rootCA.pem" "${ROOT_CA}"
chmod 600 "${ROOT_CA}"
info "Copied root CA to ${ROOT_CA}"

# ---- print expiry and instructions ------------------------------------------
EXPIRY=$(openssl x509 -noout -enddate -in "${LEAF_CERT}" 2>/dev/null | cut -d= -f2 || echo "unknown")
FINGERPRINT=$(openssl x509 -noout -fingerprint -sha256 -in "${LEAF_CERT}" 2>/dev/null \
    | sed 's/SHA256 Fingerprint=//' || echo "unavailable")

echo ""
echo "=== Cert generated successfully ==="
echo "  Leaf cert:    ${LEAF_CERT}"
echo "  Private key:  ${LEAF_KEY}"
echo "  Root CA:      ${ROOT_CA}"
echo "  Expires:      ${EXPIRY}"
echo "  SHA-256:      ${FINGERPRINT}"
echo ""
echo "=== Install root CA on your iPhone (one-time) ==="
echo "  Option A – AirDrop:"
echo "    open '${ROOT_CA}'"
echo ""
echo "  Option B – HTTP (run from repo root):"
echo "    python3 -m http.server 8080 --directory '${CERT_DIR}'"
echo "    Then on iPhone Safari: http://${LAN_IP}:8080/rootCA.pem"
echo ""
echo "  After downloading: Settings → Profile Downloaded → Install → Trust"
echo ""
echo "=== Done. Run 'just laptop-setup' next. ==="
