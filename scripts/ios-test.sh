#!/usr/bin/env bash
# Run the Swift XCTests for the ChronosSensorsCore package.
# Usage: scripts/ios-test.sh   (also called by `just mobile-ios-test`)
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$REPO_ROOT/apps/mobile/modules/chronos-sensors/ios/ChronosSensorsCore"

echo "=== ChronosSensorsCore swift test ==="
echo "Package: $PKG"
swift test --package-path "$PKG"
echo "=== swift test: all tests passed ==="
