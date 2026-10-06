# System Architecture

## 1. Component diagram

```
┌──────────────────────── iPhone (iOS 17+) ─────────────────────────┐
│  Expo dev-client app  (apps/mobile)                                │
│  ┌──────────────┐  ┌───────────────────┐  ┌─────────────────────┐ │
│  │ WakeHandlers │  │ chronos-sensors   │  │ Feed / Player UI    │ │
│  │ silent push  │─▶│ (Swift Expo Mod.) │  │ pairing, debug      │ │
│  │ BGAppRefresh │  │ CMPedometer hist. │  └─────────▲───────────┘ │
│  │ foreground   │  │ CMMotionActivity  │            │ signed URL  │
│  └──────┬───────┘  │ isProtectedData   │            │             │
│         │          │ battery state     │            │             │
│         ▼          └───────────────────┘            │             │
│  TelemetryClient (queue, retry, bearer token) ──────┼──────────┐  │
└─────────────────────────────────────────────────────┼──────────┼──┘
                     HTTPS (mkcert CA) over home LAN  │          │
┌────────────────────── MacBook Pro (macOS 15) ───────┼──────────┼──┐
│  launchd: com.chronos.api        launchd: com.chronos.deploy      │
│  ┌─────────────────── services/api (one process) ───────────────┐ │
│  │ FastAPI                      │ Worker + Scheduler (APScheduler)│ │
│  │  /v1/pair  /v1/telemetry     │  tick(): silent push every 5m  │ │
│  │  /v1/day   /v1/reels         │  prewarm(): pipeline at W-30m  │ │
│  │  /v1/media (signed)          │  fallback(): deliver at W end  │ │
│  │  /v1/config/wake             │  retention(): purge nightly    │ │
│  │  /healthz /metrics           │  jobs table runner (1 at a time)│ │
│  ├──────────────────────────────┴────────────────────────────────┤ │
│  │ Wake engine: scoring.evaluate() → DayStateMachine             │ │
│  │ Content: fetchers → normalise → rank → LLM script (Ollama)    │ │
│  │ Media: Kokoro TTS → whisper.cpp → reel.json → Remotion → MP4  │ │
│  │ Delivery: Expo push (silent / visible), receipts              │ │
│  ├───────────────────────────────────────────────────────────────┤ │
│  │ SQLite (WAL)  data/chronos.db   │ data/media/  data/models/   │ │
│  └───────────────────────────────────────────────────────────────┘ │
│  Subprocesses: ollama serve · whisper-cli · npx remotion render ·  │
│                ffmpeg/ffprobe · caffeinate                          │
└───────────────────────────────────────────────────────────────────┘
          │ HTTPS                       │ HTTPS (Expo push API)
          ▼                             ▼
  Public feeds (HF, arXiv, HN,    exp.host → APNs → iPhone
  BBC, CBS, IE, NDTV, og:images,
  optional Pexels)
```

## 2. Runtime processes on the laptop

| Process | Started by | Purpose |
|---|---|---|
| `chronos serve` (uvicorn, FastAPI + worker thread + scheduler) | launchd `com.chronos.api`, KeepAlive | Everything in `services/api` |
| `ollama serve` | Homebrew service (`brew services start ollama`) | Local LLM; API calls `http://127.0.0.1:11434` |
| `whisper-cli`, `npx remotion render`, `ffmpeg` | Spawned by the worker per job, timeout-bound | Media stages |
| `scripts/deploy.sh` | launchd `com.chronos.deploy`, every 10 min | Pull-based CD: fast-forward `main`, `uv sync`, `pnpm install`, migrate, restart |
| `caffeinate -i` | Wrapped around pipeline jobs | Prevent idle sleep mid-render |
| `pmset repeat` schedule | One-time setup (`just laptop-setup`) | Wake laptop at 05:25 weekdays / 07:25 weekends |

Single-process API+worker is deliberate (D10). The worker runs pipeline stages **serially** (one
job at a time) because Ollama, Chrome and whisper.cpp together would exceed 16 GB comfortably.

## 3. Daily sequence (eager render mode, D09)

```
05:25  pmset wakes laptop ─▶ launchd ensures chronos serve + ollama are up
05:30  scheduler.prewarm(local_date)
        ├─ job: ingest        fetch all sources (ETag/If-Modified-Since), normalise, dedupe, store items
        ├─ job: script        rank items → prompt Ollama → ReelScript JSON (validated) → briefs row
        ├─ job: assets        download og:images / Pexels b-roll → resize, strip EXIF → reel_assets
        ├─ job: tts           Kokoro → voice.wav per scene → concat → loudnorm → voice.mp3
        ├─ job: captions      whisper-cli -oj → word timestamps → align to scenes → captions.json
        ├─ job: render        write reel.json → npx remotion render → reel.mp4 → ffprobe verify
        └─ day.state = ready   (reel row status=ready)
06:00  window opens → scheduler.tick every 5 min:
        ├─ POST exp.host silent push {"type":"tick","date":...}
        └─ iPhone wakes app ≤30 s → sample signals → POST /v1/telemetry
             └─ API: evaluate() → store evaluation → DayStateMachine.apply()
                  └─ if confirmed: job: deliver → visible push with reel id
11:00  if day.state ∉ {confirmed, delivered}: job: deliver (fallback=true)
03:00  retention(): delete unsaved reels > 24 h, telemetry > 30 d, items > 14 d
```

`on_trigger` mode is the same with `prewarm` reduced to `ingest` and the remaining jobs enqueued
by the state machine on confirmation.

## 4. Wake evaluation sequence (per tick)

```
iPhone                                  API                             DB
  │ silent push received                 │                               │
  │ sensors.sample(window=10m)           │                               │
  │ POST /v1/telemetry {signals, tz}     │                               │
  │─────────────────────────────────────▶│ validate (pydantic, clock skew ≤ 5 min)
  │                                      │ insert telemetry_events ─────▶│
  │                                      │ rules = effective_rules()     │
  │                                      │ ev = evaluate(signals, rules, now, day_ctx)
  │                                      │ insert wake_evaluations ─────▶│
  │                                      │ day = DayStateMachine.apply(ev)
  │                                      │ update days ─────────────────▶│
  │                                      │ if transition → confirmed: enqueue deliver
  │◀─────────────────────────────────────│ 200 {score, breakdown, state}
  │ store last result for debug panel    │
```

## 5. Module layout

### `services/api/src/chronos/`

```
chronos/
├── main.py              create_app(); mounts routers, starts worker+scheduler in lifespan
├── cli.py               `chronos` Typer CLI: serve, db migrate, run-pipeline, wake replay, doctor, config validate, pair-code
├── config.py            Settings (pydantic-settings, CHRONOS_ prefix), paths, feature flags
├── db.py                engine (SQLite WAL, foreign_keys=ON), session factory
├── models/              SQLModel tables (one file per aggregate: devices, telemetry, days, content, media, jobs)
├── api/                 routers: pair, telemetry, day, reels, media, config, health, admin
├── auth.py              bearer token verification, pairing codes, signed media URLs
├── wake/
│   ├── signals.py       Signals (pydantic) — the normalised input
│   ├── rules.py         WakeRules schema, loader (YAML + DB override), versioning
│   ├── scoring.py       evaluate(): pure function → Evaluation(score, breakdown, decision)
│   ├── window.py        local day/window computation (tz-aware), weekday/weekend, holidays
│   ├── state.py         DayStateMachine: monitoring → confirmed → delivered; fallback
│   └── replay.py        re-score stored telemetry with candidate rules; report
├── content/
│   ├── sources.py       Source registry from config/sources.yaml
│   ├── fetchers/        hf_daily_papers.py, arxiv.py, hackernews.py, rss.py (BBC/CBS/IE/NDTV)
│   ├── normalise.py     Item model, canonical URL, hashing, dedupe
│   ├── rank.py          per-source quotas, recency, cross-source overlap boost
│   └── script/          schema.py (ReelScript), prompts/, llm.py (Ollama/OpenAI/Anthropic), fallback.py
├── media/
│   ├── assets.py        safe fetch (SSRF guard, size/type limits), resize, EXIF strip, Pexels
│   ├── tts.py           Kokoro (kokoro-onnx), edge-tts fallback, loudnorm
│   ├── captions.py      whisper-cli runner, word alignment to scenes
│   ├── reel_json.py     builds packages/renderer input (validated against shared JSON schema)
│   ├── render.py        remotion CLI runner, ffprobe verification, sha256
│   └── subprocess.py    run([...], timeout, cwd, env allowlist) — the only way to spawn
├── delivery/
│   ├── push.py          Expo push client (silent + visible), tickets, receipts
│   └── retention.py     purge policy
├── jobs/
│   ├── queue.py         SQLite-backed job queue (claim with UPDATE ... WHERE status='queued')
│   ├── worker.py        serial runner, retries with backoff, timeouts, caffeinate wrapper
│   └── scheduler.py     APScheduler: ticks, prewarm, fallback, retention
├── observability.py     structlog setup, metrics registry, nightly report
└── pipeline.py          orchestrates job chain for a local_date
```

### `apps/mobile/`

```
app/                     expo-router screens: index (feed), reel/[id], pairing, settings, debug
src/
├── api/                 typed client (openapi-typescript generated), auth storage (expo-secure-store)
├── wake/                registerBackgroundTasks(), onSilentPush(), onForeground(), sampleAndSend()
├── telemetry/           queue (SQLite via expo-sqlite), retry, clock-skew guard
├── player/              vertical pager, expo-video player, captions overlay toggle
└── state/               zustand store, persisted
modules/chronos-sensors/ Expo Module (Swift): ChronosSensorsModule.swift, ios podspec, TS bindings
```

### `packages/renderer/`

```
src/
├── Root.tsx             registers compositions
├── compositions/MorningReel.tsx     1080×1920, 30 fps, duration from props
├── scenes/              Cover, Story (image/b-roll variants), Stat, Outro
├── captions/            WordCaptions.tsx (karaoke-style, word timestamps)
├── design/              tokens (type scale, palette, spacing, motion curves), fonts (self-hosted)
├── schema.ts            ReelProps zod schema (generated from API JSON schema, checked in CI)
└── fixtures/sample-reel.json        used by vitest + CI smoke render
```

## 6. Key design properties

| Property | How it is achieved |
|---|---|
| Idempotent pipeline | Every job keyed by `(kind, local_date)`; re-running skips stages whose artefact exists and hash matches |
| Tolerant of missed ticks | Scoring uses *history* windows (10 min) from CoreMotion, not deltas between ticks; a missed tick only delays, never loses, evidence |
| No false positive at 3 a.m. | Window gate is a hard precondition, not a weighted signal; outside the window the score is always 0 |
| No false positive from phone-in-bed | Unlocked alone caps at 25; steps/walking required to cross the threshold; 2-tick confirmation |
| Explainable | `wake_evaluations.breakdown` lists every rule, its input, points awarded and cap |
| Tunable live | `PUT /v1/config/wake` writes a new `rule_versions` row; next evaluation uses it; replay CLI scores history under any version |
| Memory-safe on 16 GB | Serial worker; Ollama model unloaded (`keep_alive: 0`) before render; Remotion concurrency 2 |
| Safe subprocesses | Single `media/subprocess.run` wrapper: list args, timeout, cwd under `data/`, minimal env |
| Secure on LAN | mkcert HTTPS, bearer token hashed at rest, signed media URLs, bind to LAN interface, rate limit on pairing |

## 7. Failure modes and responses

| Failure | Detection | Response |
|---|---|---|
| Laptop asleep at 05:25 | `pmset` schedule not applied / lid closed on battery | `just laptop-setup` verifies `pmset -g sched`; doctor warns; deliver job runs late when it wakes; fallback still at window end |
| Ollama down / model missing | HTTP error / timeout 120 s | Retry ×2 → provider fallback if configured → deterministic template script; day still delivers |
| Source feed down | HTTP error / parse error | Skip source, log, quotas rebalanced; need ≥ 4 items to proceed, else template script from cached items |
| Kokoro failure | exception / empty WAV | `edge-tts` fallback (network) → if offline, render captions-only reel with music bed |
| whisper.cpp failure | non-zero exit / no words | Fallback: proportional word timing from TTS per-scene durations |
| Remotion failure | non-zero exit / ffprobe mismatch | Retry once with concurrency 1 → push text-only notification with the script |
| Silent pushes throttled | No telemetry for 20 min inside window | Tick continues; BGAppRefresh and foreground still work; fallback at window end |
| Clock skew on phone | `device_ts` vs server > 5 min | Reject with 422 `clock_skew`; app shows warning |
| Phone token revoked / app reinstalled | 401 | App returns to pairing screen |
| Disk pressure | free space < 2 GB | retention runs early; render refuses to start; doctor warns |
