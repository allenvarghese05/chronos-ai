# CI/CD and Operations

Modelled on typist-ml (just-parity CI, SHA-pinned actions, rulesets) and extended with secret
scanning, workflow linting, dependency review, pull-based CD and a manual iOS build.

## 1. Local task runner (`justfile`)

| Recipe | Does |
|---|---|
| `just` | list recipes |
| `just setup` | install tool pins (uv python, pnpm via corepack, pre-commit hooks), `uv sync --locked`, `pnpm install --frozen-lockfile` in both JS apps |
| `just models` | download Kokoro + whisper models with sha256 check, `ollama pull <model>@<digest>`, `remotion browser ensure` |
| `just certs` | mkcert root + leaf into `data/certs`, prints the profile install instructions and fingerprint |
| `just lint` / `format-check` / `types` / `test` / `build` | all three apps (each skips apps not scaffolded yet) |
| `just check` | lint + format-check + types + test + build — **CI parity** |
| `just check-api` / `check-mobile` / `check-renderer` | exactly what the CI job of that name runs |
| `just security` | semgrep, bandit, pip-audit, pnpm audit (both), gitleaks; skip-with-message when a tool/registry is missing; fail at end if any part failed |
| `just gen-types` | `scripts/gen-types.sh`: `chronos openapi export` → `services/api/openapi.json`; `openapi-typescript` → `apps/mobile/src/api/schema.d.ts`; renderer zod hook guarded (T3.4); idempotent |
| `just dev` | API (reload) + Remotion studio + Expo dev server |
| `just serve` | production API as launchd would run it |
| `just run <date>` | `chronos run --date <date>` full pipeline now |
| `just replay ...` | `chronos wake replay` |
| `just doctor` | `chronos doctor` |
| `just migrate` | alembic upgrade head |
| `just mobile-build` | `expo prebuild` + `expo run:ios --device` |
| `just mobile-ios-test` | XCTest for the Swift module (macOS only) |
| `just laptop-setup` | install LaunchAgents, pmset wake schedule, firewall rule, log dirs; idempotent |
| `just deploy` | what `scripts/deploy.sh` runs (fast-forward main, sync deps, migrate, restart) |

Version pins live at the top of the justfile (`semgrep_version`, `gitleaks_version`,
`whisper_cpp_version`, `ollama_model`, `ollama_digest`) and are read by CI with `just --evaluate`.

## 2. pre-commit (`.pre-commit-config.yaml`, local hooks only)

| Hook | Files | Tool (version from lockfile) |
|---|---|---|
| ruff + ruff-format | `services/api/**/*.py` | uv run |
| bandit | `services/api/src/**/*.py` | uv run, `-ll` |
| biome (mobile) | `apps/mobile/**` | pnpm exec |
| biome (renderer) | `packages/renderer/**` | pnpm exec |
| actionlint | `.github/workflows/*.yml` | uv tool |
| gitleaks protect | staged | pinned binary |
| config-validate | `services/api/config/*.yaml` | `uv run chronos config validate` |

## 3. GitHub Actions

All workflows: `permissions: contents: read` at top level (jobs raise only what they need),
`timeout-minutes`, `concurrency` cancelling superseded PR runs, `actions/checkout` with
`persist-credentials: false`, every action pinned to a full SHA with a version comment.

### `ci.yml` — required checks (names are fixed: `api`, `mobile`, `renderer`, `secrets`)

| Job | Runner | Steps |
|---|---|---|
| `api` | ubuntu | setup-uv (pinned), `uv sync --locked`, `ruff check`, `ruff format --check`, `pyright`, `pytest -m "not ollama and not kokoro and not whisper and not remotion"` with coverage ≥ 85 % on `chronos/wake` and `chronos/content/script`, `bandit -r src -ll`, `chronos config validate`, **staleness check**: `chronos openapi export --out /tmp/openapi.json && diff -u services/api/openapi.json /tmp/openapi.json` (no pnpm needed), semgrep (`p/security-audit p/python`, pinned, `--error`) |
| `mobile` | ubuntu | setup-node from `.nvmrc`, corepack pnpm pin check, `pnpm install --frozen-lockfile`, **schema.d.ts staleness check**: `openapi-typescript … --output /tmp/schema.d.ts && diff -u src/api/schema.d.ts /tmp/schema.d.ts`, `biome ci`, `tsc --noEmit`, `jest --ci`, `expo-doctor` (live-manifest version sub-check disabled via `EXPO_DOCTOR_SKIP_DEPENDENCY_VERSION_CHECK=1` in the `check-mobile` recipe — see ADR D32; all other 20 checks run), `expo export --platform ios` (bundle compiles), semgrep `p/typescript` |
| `renderer` | ubuntu | same JS setup, `biome ci`, `tsc --noEmit`, `vitest run`, Remotion **still smoke render** of `sample-reel.json` (headless shell cached by `actions/cache`), upload `smoke.png` artefact (7 days) |
| `secrets` | ubuntu | gitleaks (pinned action) over the full history of the PR branch; fails on any finding |

### Non-required workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `zizmor.yml` | PR touching `.github/**`, weekly | Workflow security lint, plain output, pedantic persona, fails on any finding |
| `audit.yml` | weekly + manual | `just security-py` (pip-audit), `just security-web` (pnpm audit ×2); advisory DBs change without code changes so it is never required |
| `pr-title.yml` | PR opened/edited | Conventional-commit PR title `type(scope): T1.5 subject` (`amannn/action-semantic-pull-request`, pinned); scopes `api, mobile, renderer, ci, docs, ops, agents, release` |
| `ios-build.yml` | manual (`workflow_dispatch`) + weekly Monday | `macos-14`: `pnpm install`, `expo prebuild --platform ios`, `xcodebuild -scheme ChronosAI -sdk iphonesimulator build`, XCTest for `chronos-sensors`, CodeQL `swift`. Costly minutes → never required |
| `eas-build.yml` | manual, environment `mobile-build` (owner approval) | `eas build --platform ios --profile development --non-interactive` with `EXPO_TOKEN`; link posted in the run summary |
| `release.yml` | tag `v*` | Creates a GitHub Release with auto-generated notes and attaches `smoke.png` + SBOMs (`uv export`, `pnpm licenses list --json`) |

### Dependabot (`.github/dependabot.yml`)
- `github-actions` weekly (keeps SHA pins current); PRs use `ci(deps):` title, which is in the
  allowed scopes list (D33).
- `pip` (`services/api`) and `npm` (`apps/mobile`, `packages/renderer`): **security updates only**
  via `open-pull-requests-limit: 0` (GitHub ignores the limit for security PRs, so CVE patches
  still open; version-bump PRs do not — D33). Grouped so each ecosystem produces at most one PR.

### Repository settings (`scripts/bootstrap-github.sh` + `scripts/github-ruleset.json`)
- Private repo `chronos-ai` (owner decision); squash-only merges; auto-delete branches; auto-merge
  disabled (owner merges by hand); Dependabot alerts enabled. **Not available on a private
  free-plan repo** (recorded 2026-10-06): rulesets/required checks, secret scanning + push
  protection, CodeQL/code scanning, dependency review, environment reviewers. gitleaks (CI +
  pre-commit), semgrep, zizmor and the agents' never-push-to-main rule are the controls; the
  ruleset JSON is kept for when the repo goes public or the plan changes.
- Ruleset `protect-main`: PR required, 0 approvals (solo), **required checks `api`, `mobile`,
  `renderer`, `secrets`** with strict up-to-date policy, required conversation resolution, block
  force-push and deletion, no bypass actors, **require signed commits** (owner's SSH signing key and
  the agents' commits are signed with the same local key — see `13-open-questions.md`).
- `CODEOWNERS`: `* @allenvarghese05`. `SECURITY.md` with the incident steps.
- PR template sections: What / Why (task id, decisions) / How tested (`just check`, `just security`,
  device checklist if mobile) / Dependencies added / Security notes / Notes for review.

## 4. CD: pull-based deploy on the laptop (D19)

Implemented in `scripts/deploy.sh`, run by `com.chronos.deploy` every 600 s:

```
launchd com.chronos.deploy  (StartInterval 600 s)
  └─ scripts/deploy.sh [--dry-run]
       ├─ flock data/deploy.lock   (prevents overlap)
       ├─ git fetch origin main --quiet
       ├─ up-to-date check → log status=up-to-date, exit 0
       ├─ chronos ops in-deploy-window  (exit 0 = inside exclusion, skip) → log status=skipped
       ├─ git verify-commit origin/main  (warning if signing not configured)
       ├─ git merge --ff-only origin/main  (fail → log status=failed, exit 1)
       ├─ uv sync --locked (services/api) + pnpm install --frozen-lockfile (packages/renderer)
       ├─ chronos db migrate + chronos config validate
       ├─ launchctl kickstart -k gui/$UID/com.chronos.api
       ├─ curl -fsS --cacert data/certs/rootCA.pem https://<host>:<port>/readyz (retries ×3)
       │   └─ on failure: git reset --hard <prev_sha> + kickstart + log status=rolled-back
       └─ log status=ok to data/logs/deploy.jsonl
```

Deploys never happen between `window_start − 45 min` and `window_end` (the
`chronos ops in-deploy-window` guard reads `config/wake_rules.yaml`) so a morning is
never interrupted; the pending deploy runs after.

`--dry-run` flag: prints each action without executing; useful for testing the script path.

## 5. Laptop runtime (launchd + pmset)

Plists are rendered from `scripts/launchd/*.plist.template` by `scripts/laptop-setup.sh`
(substituting `__REPO_ROOT__`, `__HOME__`, `__UV__`). Both are validated with `plutil -lint`
before `launchctl bootstrap`.

| Item | Detail |
|---|---|
| `~/Library/LaunchAgents/com.chronos.api.plist` | `ProgramArguments: [<uv>, run, --locked, chronos, serve]`, `WorkingDirectory: <repo>/services/api`, `KeepAlive: true`, `RunAtLoad: true`, `StandardOutPath/ErrorPath: <repo>/data/logs/api.{out,err}.log`, `EnvironmentVariables: CHRONOS_ENV=prod`, `ProcessType: Interactive` (prevents App Nap throttling) |
| `com.chronos.deploy.plist` | `StartInterval: 600`, `ProgramArguments: [/bin/bash, <repo>/scripts/deploy.sh]` |
| Ollama | `brew services start ollama` (user LaunchAgent, idempotent) |
| `pmset repeat wakeorpoweron MTWRF 05:25:00 SU 07:25:00` | requires `sudo`; `laptop-setup.sh` prints a RUN-THIS banner in non-interactive shells |
| Lid closed | scheduled wake works with lid closed **only** when on power + external display/keyboard; otherwise leave lid open at night |
| App Nap / idle sleep during render | worker wraps jobs in `caffeinate -i -w <pid>` |
| Time sync | `sntp` check in doctor (clock skew rejects phone events) |
| Logs | structlog JSONL, `logrotate`-style rotation in-process (size 50 MB / 14 days) |
| Backups | `data/chronos.db` nightly `VACUUM INTO data/backups/chronos-<date>.db` (7 kept); media not backed up |

## 6. Observability

- `/metrics`: `chronos_job_duration_seconds{kind}`, `chronos_ticks_sent_total`,
  `chronos_telemetry_received_total{source}`, `chronos_wake_score` (last), `chronos_day_state`,
  `chronos_push_errors_total`, process RSS. Scraped by nothing by default; visible in the debug
  screen through `/v1/report`.
- Nightly report `data/logs/reports/<date>.md`: timeline of ticks/scores, confirm time vs actual,
  pipeline stage durations, failures, source health, disk usage. Also posted as a visible push if
  there was a failure (`kind=report`, opt-in).
- `chronos doctor` (`just doctor`) — implemented in `services/api/src/chronos/ops/doctor.py`
  with fully injectable probes (no subprocess calls in tests). Checks:

| Check | Blocking? |
|---|---|
| TLS cert files present, mode 600, expiry > 7 days | yes if missing / bad mode |
| `.env` mode 600 | warn |
| Model files SHA256 vs `scripts/models.lock` | yes if file missing or hash mismatch |
| Ollama reachable + model loaded + digest matches justfile `ollama_digest` | yes if unreachable or model absent |
| `com.chronos.api` + `com.chronos.deploy` in `launchctl list` | yes if API not loaded |
| `pmset -g sched` entries at 05:25 + 07:25 | warn |
| Firewall state | warn |
| FileVault | warn |
| Disk free ≥ 2 GB (< 1 GB = blocking) | yes if < 1 GB |
| SQLite `PRAGMA integrity_check` | yes if fails |
| `public_base_url` hostname resolves | warn |

Prints a PASS/WARN/FAIL table; exits 1 on any blocking failure.

## 7. Release and versioning

- Single version in `VERSION` at the repo root, surfaced by `/healthz`, the app settings screen
  and the renderer's `renderer_version`. Bump by a `chore(release): vX.Y.Z` PR; tag on merge;
  `release.yml` publishes notes. Mobile builds are tied to the same version via `app.config.ts`.
