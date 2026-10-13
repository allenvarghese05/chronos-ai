# Chronos-AI task runner. Run `just` (or `just --list`) to see the recipes.
#
# Every recipe skips an app that is not scaffolded yet: it prints "skipping" and exits 0, so the
# repo is green from T0.1 onwards. `check` and `check-*` are what CI runs (job names api, mobile,
# renderer); keep them identical (docs/10-cicd-and-ops.md). `security` is not part of `check`.
#
# Contracts later tasks must meet:
#   services/api (T0.3):      pyproject.toml with ruff, pyright, pytest, bandit, pip-audit in the dev
#                             group; committed uv.lock never rewritten by these recipes (--locked);
#                             app factory `chronos.main:create_app`; `chronos` CLI.
#   apps/mobile (T0.4):       package.json with scripts; @biomejs/biome, typescript, jest-expo.
#   packages/renderer (T0.5): package.json; @biomejs/biome, typescript, vitest, remotion.
#   config validation (T1.4): `chronos config validate`.

# Tool pins. CI reads them with `just --evaluate <name>`. Keep README.md in sync.
semgrep_version   := "1.178.0"
gitleaks_version  := "8.21.2"
uv_version        := "0.12.18"
whisper_cpp_formula := "whisper-cpp"
ollama_model      := "qwen2.5:7b-instruct"
# Filled in by T0.6 after the first verified pull (sha256:...); doctor checks it.
ollama_digest     := ""

api_dir      := justfile_directory() / "services/api"
mobile_dir   := justfile_directory() / "apps/mobile"
renderer_dir := justfile_directory() / "packages/renderer"

[private]
default:
    @"{{just_executable()}}" --list --justfile "{{justfile()}}"

# ───────────────────────────── setup ─────────────────────────────

# One-time developer setup: tool pins, hooks, dependencies.
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    command -v uv >/dev/null || { echo "install uv first: https://docs.astral.sh/uv/"; exit 1; }
    uv python install 3.12
    uv tool install "semgrep=={{semgrep_version}}" || true
    uv tool install pre-commit || true
    uv tool install actionlint-py || true
    corepack enable || true
    pre-commit install
    if [ -f services/api/pyproject.toml ]; then (cd services/api && uv sync --locked); fi
    if [ -f apps/mobile/package.json ]; then (cd apps/mobile && pnpm install --frozen-lockfile); fi
    if [ -f packages/renderer/package.json ]; then (cd packages/renderer && pnpm install --frozen-lockfile); fi
    echo "setup done. Next: just models, just certs (see README)."

# Download model files with checksum verification (Kokoro, whisper.cpp, Ollama) and the Remotion browser. (T0.6)
models:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -x scripts/models.sh ]; then echo "scripts/models.sh not yet implemented (T0.6), skipping"; exit 0; fi
    scripts/models.sh "{{ollama_model}}" "{{ollama_digest}}"

# Generate the local HTTPS certificates with mkcert into data/certs. (T0.6)
certs:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -x scripts/certs.sh ]; then echo "scripts/certs.sh not yet implemented (T0.6), skipping"; exit 0; fi
    scripts/certs.sh

# Install LaunchAgents, pmset wake schedule and log dirs on this Mac (idempotent). (T0.6)
laptop-setup:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -x scripts/laptop-setup.sh ]; then echo "scripts/laptop-setup.sh not yet implemented (T0.6), skipping"; exit 0; fi
    scripts/laptop-setup.sh

# ───────────────────────────── check ─────────────────────────────

# Lint every app (ruff; biome ci).
lint:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ -f services/api/pyproject.toml ]; then (cd services/api && uv run --locked ruff check .); else echo "services/api not yet scaffolded, skipping lint"; fi
    if [ -f apps/mobile/package.json ]; then (cd apps/mobile && pnpm exec biome ci .); else echo "apps/mobile not yet scaffolded, skipping lint"; fi
    if [ -f packages/renderer/package.json ]; then (cd packages/renderer && pnpm exec biome ci .); else echo "packages/renderer not yet scaffolded, skipping lint"; fi

# Check formatting without changing files.
format-check:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ -f services/api/pyproject.toml ]; then (cd services/api && uv run --locked ruff format --check .); else echo "services/api not yet scaffolded, skipping format-check"; fi
    if [ -f apps/mobile/package.json ]; then (cd apps/mobile && pnpm exec biome format .); else echo "apps/mobile not yet scaffolded, skipping format-check"; fi
    if [ -f packages/renderer/package.json ]; then (cd packages/renderer && pnpm exec biome format .); else echo "packages/renderer not yet scaffolded, skipping format-check"; fi

# Format in place (ruff format, biome format --write).
format:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ -f services/api/pyproject.toml ]; then (cd services/api && uv run --locked ruff format . && uv run --locked ruff check --fix .); fi
    if [ -f apps/mobile/package.json ]; then (cd apps/mobile && pnpm exec biome check --write .); fi
    if [ -f packages/renderer/package.json ]; then (cd packages/renderer && pnpm exec biome check --write .); fi

# Type-check every app (pyright; tsc).
types:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ -f services/api/pyproject.toml ]; then (cd services/api && uv run --locked pyright); else echo "services/api not yet scaffolded, skipping types"; fi
    if [ -f apps/mobile/package.json ]; then (cd apps/mobile && pnpm exec tsc --noEmit); else echo "apps/mobile not yet scaffolded, skipping types"; fi
    if [ -f packages/renderer/package.json ]; then (cd packages/renderer && pnpm exec tsc --noEmit); else echo "packages/renderer not yet scaffolded, skipping types"; fi

# Run the tests of every app (pytest without local-only marks; jest; vitest).
test:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ -f services/api/pyproject.toml ]; then (cd services/api && uv run --locked pytest -m "not ollama and not kokoro and not whisper and not remotion"); else echo "services/api not yet scaffolded, skipping test"; fi
    if [ -f apps/mobile/package.json ]; then (cd apps/mobile && pnpm exec jest --ci); else echo "apps/mobile not yet scaffolded, skipping test"; fi
    if [ -f packages/renderer/package.json ]; then (cd packages/renderer && pnpm exec vitest run); else echo "packages/renderer not yet scaffolded, skipping test"; fi

# Run the local-only tests too (needs Ollama, Kokoro, whisper.cpp, Remotion browser on this Mac).
test-local:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -f services/api/pyproject.toml ]; then echo "services/api not yet scaffolded, skipping test-local"; exit 0; fi
    cd services/api && uv run --locked pytest

# Build steps: mobile JS bundle export (compiles), renderer still smoke render. API has no build.
build:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ -f apps/mobile/package.json ]; then (cd apps/mobile && pnpm exec expo export --platform ios --output-dir .expo-export >/dev/null && rm -rf .expo-export); else echo "apps/mobile not yet scaffolded, skipping build"; fi
    if [ -f packages/renderer/package.json ]; then (cd packages/renderer && mkdir -p out && pnpm exec remotion still src/index.ts MorningReel out/smoke.png --props=src/fixtures/sample-reel.json --frame=60); else echo "packages/renderer not yet scaffolded, skipping build"; fi

# Export JSON schemas / OpenAPI from the API into the renderer (zod) and mobile (d.ts). (T0.7)
gen-types:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -f services/api/pyproject.toml ] || [ ! -x scripts/gen-types.sh ]; then echo "gen-types not yet implemented (T0.7), skipping"; exit 0; fi
    scripts/gen-types.sh

# Validate YAML config (wake rules, sources) against their schemas. (T1.4)
config-validate:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -f services/api/pyproject.toml ]; then echo "services/api not yet scaffolded, skipping config-validate"; exit 0; fi
    cd services/api
    if ! uv run --locked chronos config validate --help >/dev/null 2>&1; then echo "chronos config validate not yet implemented (T1.4), skipping"; exit 0; fi
    uv run --locked chronos config validate

# Assert required_status_checks in scripts/github-ruleset.json equal job contexts in .github/workflows/ci.yml.
# GitHub uses the job name: field as the status-check context (falls back to the key when absent).
# Runs a stdlib-only self-test first, then the real check.
ruleset-check:
    #!/usr/bin/env bash
    set -euo pipefail
    python3 "{{justfile_directory()}}/scripts/ruleset-check.py" --self-test
    python3 "{{justfile_directory()}}/scripts/ruleset-check.py" "{{justfile_directory()}}"

# Lint, format-check, type-check, test, build and config-validate everything. Run before pushing. CI parity.
check: lint format-check types test build config-validate
    @echo "just check: all steps passed (apps not yet scaffolded were skipped)"

# Exactly what CI job `api` runs.
check-api:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -f services/api/pyproject.toml ]; then echo "services/api not yet scaffolded, skipping check-api"; exit 0; fi
    cd services/api
    uv run --locked ruff check .
    uv run --locked ruff format --check .
    uv run --locked pyright
    uv run --locked pytest -m "not ollama and not kokoro and not whisper and not remotion"
    uv run --locked bandit -q -r src -ll
    if uv run --locked chronos config validate --help >/dev/null 2>&1; then uv run --locked chronos config validate; fi
    echo "just check-api: all steps passed"

# Exactly what CI job `mobile` runs.
check-mobile:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -f apps/mobile/package.json ]; then echo "apps/mobile not yet scaffolded, skipping check-mobile"; exit 0; fi
    cd apps/mobile
    pnpm exec biome ci .
    pnpm exec biome format .
    pnpm exec tsc --noEmit
    pnpm exec jest --ci
    # expo-doctor: skip live-manifest version check (deterministic CI; drift visible in audit.yml weekly advisory — ADR D32)
    EXPO_DOCTOR_SKIP_DEPENDENCY_VERSION_CHECK=1 pnpm exec expo-doctor
    pnpm exec expo export --platform ios --output-dir .expo-export >/dev/null && rm -rf .expo-export
    echo "just check-mobile: all steps passed"

# Exactly what CI job `renderer` runs.
check-renderer:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -f packages/renderer/package.json ]; then echo "packages/renderer not yet scaffolded, skipping check-renderer"; exit 0; fi
    cd packages/renderer
    pnpm exec biome ci .
    pnpm exec biome format .
    pnpm exec tsc --noEmit
    pnpm exec vitest run
    mkdir -p out
    pnpm exec remotion still src/index.ts MorningReel out/smoke.png --props=src/fixtures/sample-reel.json --frame=60
    echo "just check-renderer: all steps passed"

# ───────────────────────────── security ─────────────────────────────

# semgrep, bandit, pip-audit, pnpm audit, gitleaks. Skips a missing tool/app/registry with a message; fails at the end if any part failed.
security:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    summary=""; failed=""
    # semgrep (downloads public rulesets, uploads nothing)
    if ! command -v semgrep >/dev/null 2>&1; then
        echo "semgrep not found, skipping semgrep (uv tool install semgrep=={{semgrep_version}})"; summary="${summary}  semgrep: skipped (not installed)"$'\n'
    elif command -v curl >/dev/null 2>&1 && ! curl --silent --head --output /dev/null --connect-timeout 5 --max-time 10 https://semgrep.dev/; then
        echo "couldn't reach the registry, skipping semgrep"; summary="${summary}  semgrep: skipped (registry unreachable)"$'\n'
    else
        installed="$(SEMGREP_ENABLE_VERSION_CHECK=0 semgrep --version 2>/dev/null || true)"
        [ "$installed" = "{{semgrep_version}}" ] || echo "warning: semgrep ${installed:-unknown} installed, pin is {{semgrep_version}}"
        if SEMGREP_ENABLE_VERSION_CHECK=0 semgrep --config=p/security-audit --config=p/python --config=p/typescript --config=p/secrets --metrics=off --error --quiet .; then
            summary="${summary}  semgrep: passed"$'\n'
        else
            summary="${summary}  semgrep: FAILED"$'\n'; failed="${failed} semgrep"
        fi
    fi
    for part in security-py security-web security-secrets; do
        if out="$("{{just_executable()}}" --justfile "{{justfile()}}" "$part" 2>&1)"; then rc=0; else rc=$?; fi
        printf '%s\n' "$out"
        if [ "$rc" -ne 0 ]; then summary="${summary}  ${part}: FAILED (exit ${rc})"$'\n'; failed="${failed} ${part}"
        else case "$out" in *skipping*) summary="${summary}  ${part}: skipped"$'\n' ;; *) summary="${summary}  ${part}: passed"$'\n' ;; esac; fi
    done
    echo; echo "just security summary:"; printf '%s' "$summary"
    if [ -n "$failed" ]; then echo "just security: FAILED:${failed}"; exit 1; fi
    echo "just security: no failures (skipped parts are listed above)"

# bandit on services/api/src and pip-audit on uv.lock.
security-py:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -f services/api/pyproject.toml ]; then echo "services/api not yet scaffolded, skipping security-py"; exit 0; fi
    command -v uv >/dev/null 2>&1 || { echo "uv not found, skipping security-py"; exit 0; }
    cd services/api
    failed=""
    uv run --locked bandit -q -r src -ll || failed="${failed} bandit"
    if command -v curl >/dev/null 2>&1 && ! curl --silent --head --output /dev/null --connect-timeout 5 --max-time 10 https://pypi.org/; then
        echo "couldn't reach pypi.org, skipping pip-audit"
    else
        req="$(mktemp)"; trap 'rm -f "$req"' EXIT
        uv export --locked --no-emit-project > "$req"
        uv run --locked pip-audit --requirement "$req" --no-deps --disable-pip --progress-spinner off || failed="${failed} pip-audit"
    fi
    if [ -n "$failed" ]; then echo "security-py: FAILED:${failed}"; exit 1; fi
    echo "security-py: no failures"

# pnpm audit (high and above) for apps/mobile and packages/renderer.
security-web:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if [ ! -f apps/mobile/package.json ] && [ ! -f packages/renderer/package.json ]; then echo "no JS app scaffolded yet, skipping security-web"; exit 0; fi
    command -v pnpm >/dev/null 2>&1 || { echo "pnpm not found (corepack enable), skipping security-web"; exit 0; }
    if command -v curl >/dev/null 2>&1 && ! curl --silent --head --output /dev/null --connect-timeout 5 --max-time 10 https://registry.npmjs.org/; then echo "couldn't reach registry.npmjs.org, skipping security-web"; exit 0; fi
    failed=""
    if [ -f apps/mobile/package.json ]; then (cd apps/mobile && pnpm audit --audit-level=high) || failed="${failed} mobile"; fi
    if [ -f packages/renderer/package.json ]; then (cd packages/renderer && pnpm audit --audit-level=high) || failed="${failed} renderer"; fi
    if [ -n "$failed" ]; then echo "security-web: FAILED:${failed}"; exit 1; fi
    echo "security-web: no failures"

# gitleaks over the full git history and working tree.
security-secrets:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    command -v gitleaks >/dev/null 2>&1 || { echo "gitleaks not found (brew install gitleaks), skipping security-secrets"; exit 0; }
    gitleaks git --no-banner --redact . && gitleaks dir --no-banner --redact .
    echo "security-secrets: no findings"

# ───────────────────────────── run ─────────────────────────────

# Start API (reload), Remotion studio and Expo dev server together. Ctrl-C stops all.
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    pids=""; cleanup() { [ -n "$pids" ] && kill $pids 2>/dev/null || true; }; trap cleanup EXIT; trap 'exit 130' INT TERM
    if [ -f services/api/pyproject.toml ]; then (cd services/api && exec uv run --locked chronos serve --reload) & pids="$pids $!"; else echo "services/api not yet scaffolded, skipping api"; fi
    if [ -f packages/renderer/package.json ]; then (cd packages/renderer && exec pnpm exec remotion studio) & pids="$pids $!"; else echo "packages/renderer not yet scaffolded, skipping studio"; fi
    if [ -f apps/mobile/package.json ]; then (cd apps/mobile && exec pnpm exec expo start --dev-client) & pids="$pids $!"; else echo "apps/mobile not yet scaffolded, skipping expo"; fi
    [ -z "$pids" ] && { echo "nothing to start yet"; exit 0; }
    wait

# Run the API the way launchd does (no reload, prod env).
serve:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/services/api" 2>/dev/null || { echo "services/api not yet scaffolded, skipping serve"; exit 0; }
    CHRONOS_ENV="${CHRONOS_ENV:-prod}" uv run --locked chronos serve

# Apply database migrations.
migrate:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    [ -f services/api/alembic.ini ] || { echo "migrations not yet set up, skipping migrate"; exit 0; }
    cd services/api && uv run --locked chronos db migrate

# Run the full pipeline now for a date (default today). Extra args go to `chronos run`.
run *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/services/api" 2>/dev/null || { echo "services/api not yet scaffolded, skipping run"; exit 0; }
    uv run --locked chronos run {{args}}

# Re-score stored telemetry under candidate rules. Args go to `chronos wake replay`.
replay *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/services/api" 2>/dev/null || { echo "services/api not yet scaffolded, skipping replay"; exit 0; }
    uv run --locked chronos wake replay {{args}}

# Check this Mac: pins, models, certs, firewall, pmset schedule, Ollama, disk, DB.
doctor:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/services/api" 2>/dev/null || { echo "services/api not yet scaffolded, skipping doctor"; exit 0; }
    uv run --locked chronos doctor

# Render the sample reel locally to out/sample.mp4 for a visual check.
render-sample:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/packages/renderer" 2>/dev/null || { echo "packages/renderer not yet scaffolded, skipping render-sample"; exit 0; }
    mkdir -p out && pnpm exec remotion render src/index.ts MorningReel out/sample.mp4 --props=src/fixtures/sample-reel.json --codec=h264

# Prebuild and install the dev-client on the connected iPhone (macOS + Xcode).
mobile-build:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/apps/mobile" 2>/dev/null || { echo "apps/mobile not yet scaffolded, skipping mobile-build"; exit 0; }
    [ "$(uname -s)" = "Darwin" ] || { echo "mobile-build requires macOS, skipping"; exit 0; }
    pnpm exec expo prebuild --platform ios --clean
    pnpm exec expo run:ios --device

# Boot (or reuse) the iOS Simulator named by CHRONOS_SIM (default "iPhone 16 Pro") and
# run the dev-client. macOS only. First run compiles Pods — allow several minutes.
mobile-sim:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/apps/mobile" 2>/dev/null || { echo "apps/mobile not yet scaffolded, skipping mobile-sim"; exit 0; }
    [ "$(uname -s)" = "Darwin" ] || { echo "mobile-sim requires macOS, skipping"; exit 0; }
    sim="${CHRONOS_SIM:-iPhone 16 Pro}"
    pnpm exec expo run:ios --device "$sim"

# XCTest for the Swift module (macOS only).
mobile-ios-test:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    [ "$(uname -s)" = "Darwin" ] || { echo "mobile-ios-test requires macOS, skipping"; exit 0; }
    [ -x scripts/ios-test.sh ] || { echo "scripts/ios-test.sh not yet implemented (T2.1), skipping"; exit 0; }
    scripts/ios-test.sh

# What the laptop's deploy LaunchAgent runs: fast-forward main, sync, migrate, restart, health check.
deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    [ -x scripts/deploy.sh ] || { echo "scripts/deploy.sh not yet implemented (T0.6), skipping"; exit 0; }
    scripts/deploy.sh
