# Chronos-AI

Behavioural wake detection on iOS plus a zero-cost programmatic "Morning Reel": when your phone's
motion history says you are actually up, your laptop sends you a 60-second vertical video with the
overnight AI drops and the headlines you care about — voiced, captioned and rendered locally.

Status: **planning complete, milestone 0 not started.** Start with `docs/00-overview.md`.

## How it works (one paragraph)

An Expo dev-client app on the iPhone is woken by silent pushes every 5 minutes inside your morning
window. Each time, a small Swift module reads the last 10 minutes of CoreMotion pedometer and
activity history, whether the phone is unlocked and whether it is still on the charger, and posts
that to a FastAPI server on the MacBook. The server scores it with tunable rules (steps, walking,
unlocked, charger disconnect, app opened); two high scores in a row confirm the day. Before the
window opened, the server had already fetched Hugging Face / arXiv / Hacker News / BBC / CBS /
Indian Express / NDTV, asked a local Ollama model for a 6-story script, synthesised the voice with
Kokoro, timed the words with whisper.cpp and rendered the reel with Remotion. On confirmation it
sends a visible push; tapping it plays the reel. If nothing confirms by the end of the window, the
reel is sent anyway.

## Documents

| | |
|---|---|
| Plan and requirements | [docs/00-overview.md](docs/00-overview.md) |
| Decisions (binding) | [docs/01-decisions.md](docs/01-decisions.md) |
| Architecture | [docs/02-architecture.md](docs/02-architecture.md) |
| Wake engine and "the score" | [docs/03-wake-engine.md](docs/03-wake-engine.md) |
| Data model | [docs/04-data-model.md](docs/04-data-model.md) |
| API | [docs/05-api-contracts.md](docs/05-api-contracts.md) |
| Content and media pipelines | [docs/06-content-pipeline.md](docs/06-content-pipeline.md), [docs/07-media-pipeline.md](docs/07-media-pipeline.md) |
| Mobile app | [docs/08-mobile-app.md](docs/08-mobile-app.md) |
| Security | [docs/09-security.md](docs/09-security.md) |
| CI/CD and laptop ops | [docs/10-cicd-and-ops.md](docs/10-cicd-and-ops.md) |
| **Task list** | [docs/11-implementation-plan.md](docs/11-implementation-plan.md) |
| Testing | [docs/12-testing-and-quality.md](docs/12-testing-and-quality.md) |
| Open questions | [docs/13-open-questions.md](docs/13-open-questions.md) |
| Agent workflow | [AGENTS.md](AGENTS.md), `.pi/agents/`, `.pi/prompts/` |

## Pinned versions

| Component | Version | Pin location |
|---|---|---|
| Node | 22.22.2 | `.nvmrc` |
| pnpm | 12.10.1 | `apps/mobile/package.json` `packageManager` |
| Expo SDK | 57.0.28 | `apps/mobile/package.json` |
| expo-router | 57.0.26 | `apps/mobile/package.json` |
| expo-dev-client | 57.0.20 | `apps/mobile/package.json` |
| react-native | 0.86.3 | `apps/mobile/package.json` |
| TypeScript (mobile) | 6.0.3 | `apps/mobile/package.json` |
| Biome (mobile) | 2.5.15 | `apps/mobile/package.json` |
| jest-expo | 57.0.6 | `apps/mobile/package.json` |
| remotion + @remotion/* | 4.0.534 | `packages/renderer/package.json` |
| react (renderer) | 19.3.0 | `packages/renderer/package.json` |
| zod | 4.5.4 | `packages/renderer/package.json` |
| TypeScript (renderer) | 7.0.2 | `packages/renderer/package.json` |
| Biome (renderer) | 2.5.15 | `packages/renderer/package.json` |
| vitest | 5.0.3 | `packages/renderer/package.json` |
| Chrome Headless Shell | 157.0.8080.0 | Remotion auto-managed; cached at `packages/renderer/node_modules/.remotion` |
| Python | 3.12 | `services/api/pyproject.toml` |
| uv | 0.12.18 | `justfile` |
| semgrep | 1.178.0 | `justfile` |
| gitleaks | 8.21.2 | `justfile` |

**Note (T0.4, pnpm 12.x + Expo):** React Native's Metro bundler does not follow symlinks, so
`apps/mobile/pnpm-workspace.yaml` sets `nodeLinker: hoisted`. This is the pnpm 12.x location for
project settings (previously `.npmrc`). See `docs/plans/T0.4-plan.md` §2 Design for details.

## Setup (macOS, Apple Silicon)

1. Tools: [uv](https://docs.astral.sh/uv/), Node 22 (`.nvmrc`), pnpm via `corepack enable`,
   `brew install just gitleaks mkcert ffmpeg whisper-cpp ollama`, Xcode 16 with command-line tools.
2. `just setup` — Python 3.12, semgrep/pre-commit/actionlint pins, hooks, dependencies.
3. `just models` — Kokoro and whisper model files (checksummed), `ollama pull`, Remotion browser.
4. `just certs` — local HTTPS certificate; install the printed root CA profile on the iPhone.
5. `just laptop-setup` — LaunchAgents (API, deploy), `pmset` wake schedule, log directories.
6. `just mobile-sim` — prebuild and launch the dev-client on the iPhone 16 Pro simulator;
   the app displays "Hello from ChronosSensors!" (Swift native module). Requires Xcode with
   Swift ≥ 6.3.1 (Xcode 17 or newer; verified with Xcode 27.1 / Swift 6.4). Physical device
   install is deferred to T2.4. To override the simulator:
   `CHRONOS_SIM="iPhone 15" just mobile-sim`. After simulator launch, pair with
   `uv run chronos pair-code` (in `services/api`).

Run `just` to list recipes. `just check` before every push (CI runs the same commands);
`just security` before every PR.

## Working with the agents

Every task in the plan runs through four pi subagents defined in `.pi/agents/`:

```
/understand T1.5   # understander writes docs/plans/T1.5-brief.md, asks questions → you answer
/implement  T1.5   # implementer writes the plan and the code on branch t1-5-...
/review     T1.5   # reviewer returns APPROVE / CHANGES NEEDED (bounded fix loop)
/push       T1.5   # pusher runs checks, pushes, opens the PR → you merge
/task       T1.5   # all four with gates
```

Requirements: the `subagent` extension in `~/.pi/agent/extensions/subagent/` (installed), and
`agentScope: "both"` so project agents are used (the prompts pass it). Trust this project in pi
when asked so `.pi/prompts` become slash commands.

## Tool pins

| Tool | Version | Where pinned |
|---|---|---|
| just | 1.58.0 | CI (`uv tool install rust-just==…`) |
| uv | 0.12.18 | justfile, CI |
| semgrep | 1.178.0 | justfile `semgrep_version` |
| gitleaks | 8.21.2 | justfile `gitleaks_version` (brew may be newer; CI uses the action) |
| Node | 22.22.2 | `.nvmrc` |
| Python | 3.12.14 | `services/api/pyproject.toml` `requires-python` |
| FastAPI | 0.142.2 | `services/api/pyproject.toml` |
| uvicorn | 0.54.0 | `services/api/pyproject.toml` |
| SQLModel | 0.0.47 | `services/api/pyproject.toml` |
| SQLAlchemy | 2.0.54 | `services/api/pyproject.toml` (SQLModel 0.0.47 requires <2.1) |
| structlog | 26.1.0 | `services/api/pyproject.toml` |
| ruff | 0.16.10 | `services/api/pyproject.toml` dev deps |
| pyright | 1.1.414 | `services/api/pyproject.toml` dev deps |
| pytest | 9.1.1 | `services/api/pyproject.toml` dev deps |
| bandit | 1.9.4 | `services/api/pyproject.toml` dev deps |
| Ollama model | qwen2.5:7b-instruct @ digest | justfile `ollama_model`, `ollama_digest` (T0.6) |

Library versions are pinned by `services/api/uv.lock`, `apps/mobile/pnpm-lock.yaml` and
`packages/renderer/pnpm-lock.yaml`.
