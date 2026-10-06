# Chronos-AI: instructions for coding agents

Chronos-AI is a single-user, iOS-only morning briefing system. An Expo (dev-client) app on the
owner's iPhone samples CoreMotion / device-state signals when iOS wakes it; a FastAPI backend on the
owner's MacBook Pro (M1 Pro, 16 GB) scores those signals, decides the owner is awake, builds a
~60 s vertical "Morning Reel" (Ollama script, Kokoro TTS, whisper.cpp captions, Remotion render)
and delivers it with an Expo push notification. Everything runs on the laptop and the phone, on the
same Wi-Fi. No cloud services except GitHub, APNs (via Expo push) and the public news/model feeds.

Read `docs/00-overview.md` first. The documents in `docs/` are the design; the ADRs in
`docs/01-decisions.md` are binding. The task list is `docs/11-implementation-plan.md`.

## Layout

| Path | What | Tooling |
|---|---|---|
| `apps/mobile` | Expo SDK (dev-client + prebuild), React Native, TypeScript strict, Swift Expo Module `chronos-sensors` | pnpm, Biome, tsc, jest-expo |
| `services/api` | Python 3.12, FastAPI, SQLModel + Alembic, SQLite, in-process worker + scheduler | uv, ruff, pyright, pytest, bandit |
| `packages/renderer` | Remotion project: compositions for the Morning Reel, rendered by the API via CLI | pnpm, Biome, tsc, vitest |
| `docs/` | design docs, ADRs (`docs/decisions/`), task briefs (`docs/plans/`) | markdown |
| `scripts/` | bootstrap, deploy, pre-commit wrappers, launchd plists | bash |
| `.pi/` | project agents, prompt templates, settings for pi | markdown / json |

`data/` (SQLite DB, media, model weights) is gitignored and must never be committed.

## Workflow rules

- Never commit to `main`. Work on a branch named after the task (`t1-3-telemetry-endpoint`).
- One task per PR, titled `type(scope): T1.5 short title`. If a task grows, split it and say so.
- Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`, `ci:`, `build:`.
  Scope is the app (`feat(api): ...`, `feat(mobile): ...`, `feat(renderer): ...`).
- Run `just check` (lint, format, types, tests for every app) before pushing. CI runs the same
  commands; it must be green before merge.
- `just security` (semgrep, bandit, pip-audit, pnpm audit, gitleaks) is run by the reviewer and
  before every PR.
- Never merge or approve a PR. The owner merges.
- Never add a dependency that the task plan does not list. Pin exact versions.
- Never weaken a CI check, a ruleset, a permission or a security scan to make something pass.

## Correctness rules

- Wake scoring is a pure function: `chronos.wake.scoring.evaluate(signals, rules, now) -> Evaluation`.
  No I/O inside it. Rules come from `config/wake_rules.yaml` (versioned) and the DB override.
  Every evaluation is stored with its full breakdown so the owner can replay and tune.
- Time: the phone sends its IANA timezone; the server computes the "local day" and window in that
  timezone. Never use the server's naive local time. Tests use a fixed clock.
- One reel per local day. A trigger after delivery is a no-op (logged).
- Everything the LLM returns is validated against the Pydantic `ReelScript` schema before it is used.
  Invalid output is retried, then falls back to the deterministic template script. Never render
  unvalidated LLM text.
- All subprocesses (ffmpeg, whisper-cli, remotion, ollama) run with argument lists (never a shell
  string), a timeout and a working directory under `data/`.
- Fetched assets: enforce content-type and size limits, strip EXIF, never follow more than 3
  redirects, never fetch private/loopback addresses (SSRF guard).
- Media URLs served to the phone are signed and expire. The API binds to the LAN interface only
  and requires the device bearer token on everything except `/healthz` and pairing.
- Keep `apps/mobile` free of Node-only code and `packages/renderer` free of app code; they share
  types only through `packages/renderer/src/schema.ts` (generated from the API's JSON schema).

## How work gets done (pipeline)

Every task in `docs/11-implementation-plan.md` goes through four phases. Each phase has one
owner subagent whose model is fixed in its agent file under `.pi/agents/`.

| Phase | Command | Subagent | Model | Output |
|---|---|---|---|---|
| 1 Understand | `/understand T1.3` | understander | cheap | `docs/plans/T1.3-brief.md` (facts, open questions, proposed approach) |
| 2 Implement | `/implement T1.3` | implementer | strong | `docs/plans/T1.3-plan.md`, code, tests, commits |
| 3 Review | `/review T1.3` | reviewer (+ implementer fixes) | strongest | verdict + findings |
| 4 Push | `/push T1.3` | pusher | cheap | branch pushed, PR opened |

`/task T1.3` runs all four phases with gates. Rules for the main session (the orchestrator):

- Delegate each phase to its subagent with the `subagent` tool and `agentScope: "both"`.
  Do not do a subagent's work yourself.
- Stop at every gate and wait for the owner. Never skip phase 1.
- Never change a plan during implementation; plan problems go back to the owner.
- The pusher may push and open PRs on its own. Nobody merges.

## Do not

- Add Shortcuts automations, BLE beacons, Apple Watch / HealthKit or any third-party sensor
  service to the wake engine. The signal set is defined in `docs/03-wake-engine.md`.
- Add paid cloud APIs by default. Ollama is the default LLM; OpenAI / Anthropic are opt-in
  fallbacks behind `CHRONOS_LLM_PROVIDER`.
- Log or store the owner's precise location, contacts or any content other than what is listed
  in `docs/04-data-model.md`.
- Commit anything under `data/`, `.env`, certificates, Expo/Apple credentials or push tokens.
