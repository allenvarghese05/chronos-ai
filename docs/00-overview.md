# Chronos-AI — Project Overview and Product Requirements

- Version: 1.0 (planning complete, no code yet)
- Owner: Allen Varghese (single user)
- Platform: iPhone (iOS 17+), MacBook Pro M1 Pro 16 GB (macOS 15) on the same home Wi-Fi

## 1. Problem

A static alarm fires at a fixed time whether or not you are up, and a news app shows the same
feed to everyone whenever they open it. Chronos-AI does the opposite: it notices when you are
*actually* up and moving, and only then hands you a one-minute vertical video with the overnight
AI drops and the headlines that matter to you.

## 2. Goals

| # | Goal | Measure |
|---|---|---|
| G1 | Detect "genuinely awake" without an alarm, a Shortcut, a beacon or a watch | Confirmed within 10 min of getting out of bed on ≥ 90 % of mornings; no false confirmation from a 3 a.m. phone check |
| G2 | Deliver a ~60 s 1080×1920 reel with voiceover, word-synced captions, images and b-roll | Push arrives ≤ 90 s after confirmation (eager mode) |
| G3 | Zero recurring cost | Only free/local components: Ollama, Kokoro, whisper.cpp, Remotion, Expo push, public feeds |
| G4 | Tunable, explainable wake detection | Every evaluation stored with its breakdown; weights editable without a deploy; replay tool |
| G5 | Production-grade engineering | CI parity with `just check`, security scanning, protected `main`, PR-only, agent pipeline |

## 3. Non-goals (v1)

- Multi-user accounts, App Store distribution, Android.
- Cloud hosting. The laptop is the server (a later task may sync reels to free object storage).
- Reel history beyond one day unless the owner saves a reel.
- One video per story (v1 is one reel per day).

## 4. Daily experience

1. **05:30 (weekday) / 07:30 (weekend)** — the laptop wakes itself (pmset), the worker fetches the
   sources, has Ollama write the script, synthesises the voice, times the captions and renders the
   reel. The reel sits ready.
2. **Window opens 06:00 / 08:00** — every 5 minutes the server sends a *silent* push. iOS wakes the
   app for ~30 s; it samples the last 10 minutes of pedometer and motion-activity history, whether
   the phone is unlocked, and the charger state, and POSTs a telemetry event.
3. **You get up** — you unplug the phone, unlock it, walk to the bathroom/kitchen. The next tick
   scores high; a second consecutive high tick confirms (or one very high tick). The day moves to
   `confirmed`.
4. **Delivery** — the server sends a visible push ("Your morning reel is ready"). Tapping opens the
   app on the player. You swipe through scenes, read captions with sound off or listen.
5. **Fallback** — if nothing confirmed by 11:00 (weekday) / 14:00 (weekend), the reel is sent anyway.
6. **Night** — unsaved reels older than 24 h are deleted. Telemetry and evaluations are kept
   30 days for tuning.

## 5. Scope by component

| Component | Responsibilities |
|---|---|
| **Mobile app** (`apps/mobile`) | Pairing with the laptop; permissions; background wake handling (silent push, BGAppRefresh, foreground); sampling CoreMotion + device state through a Swift Expo Module; telemetry POST with an offline queue; reel feed and player; save/delete; debug panel showing live signals and the server's last score |
| **API + worker** (`services/api`) | Device pairing and auth; telemetry ingestion; scoring engine; per-day state machine; scheduler (ticks, prewarm, fallback, retention); content ingestion; LLM script; TTS; captions; render orchestration; media serving; push delivery; config and replay CLI |
| **Renderer** (`packages/renderer`) | Remotion compositions that turn `reel.json` (scenes + word timestamps + assets) into the MP4; design system for the reel; still-frame smoke test in CI |
| **Ops** (`scripts/`, launchd) | Pull-based deploy on the laptop, scheduled wake, service supervision, log rotation |

## 6. Constraints and facts that shaped the design

- iOS gives no Wi-Fi RSSI, no screen-unlock event and no guaranteed background execution. The
  original "RSSI spike + unlocked 2 min" idea is replaced by signals that *are* available when iOS
  briefly wakes the app (see `docs/03-wake-engine.md`).
- Background pedometer **history** (`CMPedometer.queryPedometerData`) and motion-activity history
  (`CMMotionActivityManager.queryActivityStarting`) are readable at any wake, so the app does not
  need to run continuously.
- A silent push is the only wake the *server* controls; iOS may throttle them (budget ~2–3 per
  hour per app in the worst case, usually much better when the device is charging/on Wi-Fi). The
  design tolerates missed ticks.
- 16 GB RAM: Ollama 7B-class model (~5 GB) + Remotion Chrome render (~1.5 GB) + whisper.cpp small
  (~0.5 GB) fit, but must not run concurrently with the script step. The worker serialises stages.
- Expo Go cannot load the custom Swift module; a dev-client built with `expo prebuild` + local
  Xcode (or EAS Build) is mandatory.

## 7. Documents

| File | Content |
|---|---|
| `01-decisions.md` | Architecture decision records D01–D30 (binding) |
| `02-architecture.md` | System design, components, sequence diagrams, failure modes |
| `03-wake-engine.md` | iOS signals, what the "score" is, rules, state machine, tuning/replay |
| `04-data-model.md` | SQLite schema, retention, migrations |
| `05-api-contracts.md` | REST endpoints, auth, payloads, error model |
| `06-content-pipeline.md` | Sources, normalisation, ranking, LLM script schema and prompts |
| `07-media-pipeline.md` | TTS, whisper.cpp captions, Remotion design and render, ffmpeg checks |
| `08-mobile-app.md` | Expo project, Swift module, background modes, screens |
| `09-security.md` | Threat model, controls, secrets, SSRF, supply chain |
| `10-cicd-and-ops.md` | justfile, pre-commit, GitHub Actions, rulesets, laptop deploy, launchd |
| `11-implementation-plan.md` | Milestones M0–M6, tasks with acceptance criteria and order |
| `12-testing-and-quality.md` | Test strategy per layer, fixtures, golden files, e2e dry run |
| `13-open-questions.md` | Assumptions made in this plan that the owner should confirm |
