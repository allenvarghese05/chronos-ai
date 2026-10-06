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

## Setup (macOS, Apple Silicon)

1. Tools: [uv](https://docs.astral.sh/uv/), Node 22 (`.nvmrc`), pnpm via `corepack enable`,
   `brew install just gitleaks mkcert ffmpeg whisper-cpp ollama`, Xcode 16 with command-line tools.
2. `just setup` — Python 3.12, semgrep/pre-commit/actionlint pins, hooks, dependencies.
3. `just models` — Kokoro and whisper model files (checksummed), `ollama pull`, Remotion browser.
4. `just certs` — local HTTPS certificate; install the printed root CA profile on the iPhone.
5. `just laptop-setup` — LaunchAgents (API, deploy), `pmset` wake schedule, log directories.
6. `just mobile-build` — prebuild and install the dev-client on your iPhone, then pair with
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
| Python | 3.12 | `services/api/pyproject.toml` (T0.3) |
| Ollama model | qwen2.5:7b-instruct @ digest | justfile `ollama_model`, `ollama_digest` (T0.6) |

Library versions are pinned by `services/api/uv.lock`, `apps/mobile/pnpm-lock.yaml` and
`packages/renderer/pnpm-lock.yaml` once those apps exist (T0.3–T0.5).
