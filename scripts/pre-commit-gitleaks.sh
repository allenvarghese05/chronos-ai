#!/usr/bin/env bash
set -euo pipefail
command -v gitleaks >/dev/null 2>&1 || { echo "gitleaks not installed (brew install gitleaks), skipping"; exit 0; }
gitleaks git --pre-commit --staged --no-banner --redact
