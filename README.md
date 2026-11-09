# Chronos-AI

**Behavioural wake detection on iOS plus a fully local, zero-cost "Morning Reel."** The iPhone
reports motion and device-state signals while you sleep. When the laptop decides you are actually
up, it sends a ~60-second vertical video covering the overnight AI releases and the headlines you
care about. The script is written by a local LLM, voiced by a local TTS model, captioned word by
word and rendered with React. Everything runs on one MacBook Pro (M1 Pro, 16 GB) and one iPhone on
the home Wi-Fi. There are no paid APIs and no cloud backend.

| | |
|---|---|
| **Status** | Milestones M0–M5 are built and merged (35 tasks, 42 PRs). The full pipeline runs live end to end. T5.5 (`simulate-morning` dry run) is in PR review. M6 (hardening and tuning after 7 real mornings) is next. |
| **Code** | ~18 k lines of Python (API), ~10 k lines of TypeScript/Swift (mobile), ~1.6 k lines of TSX (renderer), ~20 k lines of Python tests |
| **Tests** | 1,165 pytest tests (unit, property-based with Hypothesis, integration on real SQLite, opt-in live model tests), ~180 jest tests and ~50 vitest tests. Coverage gates: wake engine ≥ 90 %, HTTP client ≥ 85 %, content pipeline ≥ 85 % |
| **Design** | 14 design documents and 37 binding architecture decisions (ADRs D01–D37) in [`docs/`](docs/00-overview.md) |

---

## Contents

1. [How a morning works](#how-a-morning-works)
2. [Architecture](#architecture)
3. [The wake engine (the maths)](#the-wake-engine)
4. [Content pipeline: fetch, dedupe, rank, select](#content-pipeline)
5. [How the LLM is used](#how-the-llm-is-used)
6. [Media pipeline: images, voice, captions, render](#media-pipeline)
7. [Delivery, serving and the app](#delivery-serving-and-the-app)
8. [Reliability and job execution](#reliability-and-job-execution)
9. [Security and privacy](#security-and-privacy)
10. [Engineering process, CI and quality](#engineering-process)
11. [Key decisions](#key-decisions)
12. [Setup](#setup-macos-apple-silicon)
13. [Commands](#commands)
14. [Pinned versions](#pinned-versions)
15. [Documents](#documents)

---

## How a morning works

```
 window start − 30 min        window (weekday 06:00–11:00, weekend 08:00–14:00, phone's timezone)
 ───────────┬───────────────────────────┬──────────────────────────────────────────────┬──────
            │ PREWARM (eager mode)      │ silent push every 5 min ──► phone wakes      │ window end
            │ ingest → script → assets  │ phone samples CoreMotion + lock + battery    │ no confirm?
            │ → TTS → captions → render │ POST /v1/telemetry ──► evaluate() → score    │ deliver anyway
            │ reel is ready before you  │ 2 ticks ≥ 70 or 1 tick ≥ 85 ──► CONFIRMED    │ (fallback)
            │ wake                      │ ──► visible push "Your Morning Reel is ready"│
            ▼                           ▼                                              ▼
```

1. **Prewarm.** Thirty minutes before the window opens, the scheduler enqueues an idempotent chain
   of stage jobs (`ingest → script → assets → tts → captions → render`). In the default *eager*
   mode the reel is finished before you wake. *on_trigger* mode defers the work until wake-up.
2. **Sensing.** Inside the window the server sends a silent Expo/APNs push every 5 minutes. iOS
   wakes the app briefly (budgeted to 18 s). A Swift module reads the last 10 minutes of pedometer
   and motion-activity history, lock state and charger state, and posts them to the laptop.
3. **Scoring.** A pure function scores each snapshot from 0 to 100 against versioned YAML rules.
   The full breakdown is stored so any morning can be replayed and tuned.
4. **Delivery.** On confirmation, or at window end as a fallback, the server atomically claims the
   day and sends exactly one visible push. Tapping it deep-links into the player
   (`chronos://reel/<id>`). The video streams from a signed URL that expires.

## Architecture

```
┌──────────── iPhone (Expo dev-client, React Native, TS strict) ───────────┐
│ Swift Expo Module "ChronosSensors": CMPedometer, CMMotionActivityManager, │
│ protected-data (lock) state, battery/charger                             │
│ headless notification task → sampler → SQLite outbox → HTTPS client      │
│ Screens: pairing (QR), home, wake diagnostics, settings, reel feed,      │
│ vertical player with karaoke captions, deep links                        │
└───────────────▲──────────────────────────────┬───────────────────────────┘
                │ Expo Push (APNs)             │ HTTPS on LAN (mkcert CA), bearer token
┌───────────────┴──────────────────────────────▼───────────────────────────┐
│ MacBook: ONE FastAPI process (uvicorn, launchd KeepAlive)                │
│  • API routers: pairing, telemetry, wake, settings, reels, media, admin  │
│  • asyncio scheduler: silent ticks, prewarm, deliveries, retention       │
│  • serial job worker → supervised subprocess per job (flock exec lock)   │
│  • SQLite (WAL) via SQLModel + Alembic: 16 tables, 5 migrations          │
├──────────────────────────────────────────────────────────────────────────┤
│ Local models and tools (all subprocesses: argv lists, timeouts, cwd under│
│ data/): Ollama qwen2.5:7b-instruct (digest-pinned) · Kokoro-82M ONNX ·   │
│ whisper.cpp ggml-small.en · Remotion (headless Chrome) · ffmpeg/ffprobe  │
└──────────────────────────────────────────────────────────────────────────┘
```

| Path | What | Tooling |
|---|---|---|
| `apps/mobile` | Expo SDK 57 dev-client, React Native 0.86, expo-router, Swift module `chronos-sensors` | pnpm, Biome, tsc, jest-expo |
| `services/api` | Python 3.12, FastAPI, SQLModel/SQLAlchemy 2, Alembic, SQLite, in-process worker and scheduler | uv, ruff, pyright (strict), pytest, Hypothesis, bandit |
| `packages/renderer` | Remotion 4 compositions for the Morning Reel, with zod props generated from Pydantic | pnpm, Biome, tsc, vitest |
| `docs/` | design, ADRs, per-task briefs and plans | markdown |
| `scripts/` | bootstrap, deploy, launchd templates, model fetch, pre-commit wrappers | bash, shellcheck |

Shared types flow one way: **Pydantic → JSON Schema → TypeScript**. `openapi.json` produces
`apps/mobile/src/api/schema.d.ts`. `ReelProps` produces `packages/renderer/src/schema.generated.ts`
(zod) through an in-repo codegen. CI fails if the generated files are stale.

## The wake engine

`chronos.wake.scoring.evaluate(signals, rules, now) -> Evaluation` is a **pure function** with no
I/O and no clock reads, so every score can be reproduced exactly. The rules are versioned YAML
([`services/api/config/wake_rules.yaml`](services/api/config/wake_rules.yaml)) with a
schema-validated DB override that the phone's Settings screen edits.

**Additive score, 0–100, with per-rule caps:**

| Signal (last 10 min unless noted) | Points | Cap |
|---|---|---|
| steps ≥ 35 / ≥ 20 / ≥ 10 | 30 / 18 / 8 | 30 |
| walking ≥ 60 s / ≥ 30 s (medium/high confidence only) | 20 / 12 | 20 |
| phone unlocked (+10 if unlocked 2 ticks in a row) | 15 (+10) | 25 |
| charger disconnected | 15 | 15 |
| ≥ 100 steps since the window opened | 5 | 5 |
| app opened in the foreground | 40 | 40 |
| *penalty*: stationary ≥ 540 s and < 10 steps | −20 | |

`score = clamp(Σ min(cap_r, points_r) + penalties, 0, 100)`

**Confirmation:** two consecutive ticks with score ≥ 70 (at most 12 minutes apart, so one missed
tick is tolerated), or a single tick ≥ 85. Snapshots less than 2 minutes after the previous one are
deduplicated. **Pedometer denied:** if Motion permission is denied or restricted, the remaining
signals are reweighted (unlocked 35, charger 25, foreground 40, summing to 100) so the threshold
can still be reached. **Time:** the phone sends its IANA timezone on every event. The server
computes the local "day", which starts at 03:00 local to avoid midnight edge cases, and the
weekday/weekend/holiday window in that timezone, never in the server's local time.

A day moves through a small state machine (`monitoring → confirmed → delivered`, with fallback at
window end). Each transition is stored. `chronos wake replay` re-scores a stored morning under
different rules, and `chronos wake annotate` records your actual wake time for tuning.

## Content pipeline

**Fetching.** Eight sources (Hugging Face Daily Papers, arXiv cs.AI/CL/LG, Hacker News, BBC World,
CBS, Indian Express India and Tech, NDTV) go through one **SSRF-guarded HTTP client**:

- DNS is resolved *before* connecting and every address must be `is_global`. IPv4-mapped IPv6 is
  unwrapped first, so loopback, RFC 1918, link-local and metadata addresses are refused.
- At most 3 redirects, each re-checked. Streaming byte caps. Content-type allowlist.
- Conditional GET (ETag / Last-Modified) and per-source health tracking. One failing feed never
  aborts the pass.
- Hacker News points are fetched concurrently from Algolia, bounded by a semaphore of 8.

**Normalising.** URLs are canonicalised: lowercase host, default ports dropped, fragment and
`utm_*`/`fbclid`/etc. removed, query sorted. A separate **dedupe key** folds `www.`/`amp.` hosts
and AMP paths and queries, so the AMP and canonical copies of one story hash to the same
SHA-256 value.

**Near-duplicate titles (ADR D35).** A 64-bit SimHash is computed and stored. Calibration showed it
was too unstable on 6–10-word headlines, so the deciding test is **Jaccard similarity on
stopword-filtered, NFKC-casefolded token sets**: `|A∩B| / |A∪B| ≥ 0.6`. Both titles must have at
least 4 content tokens, and the two items must be published within 36 h of each other. Duplicates
are merged with **union-find**, so the clusters do not depend on processing order. The best item
becomes the primary and the others are kept as "mentions".

**Ranking:**

```
score = source_weight × recency × popularity × (1 + 0.25 · (n_sources − 1))

recency    = linear 1.0 → 0.3 over 20 h (floor 0.3)
popularity = 1 + 0.6 · min(1, log10(1 + x) / log10(1 + cap))      cap: 300 HF upvotes, 1000 HN points
```

The log scale means the tenth upvote counts more than the thousandth. The cross-source boost
rewards a story covered by several outlets.

**Selection.** Category minimums are filled first (AI ≥ 2, world ≥ 1, India ≥ 1), then the rest
by score, with per-source quotas so one feed cannot dominate. The candidate pool is a 36-hour
window. If a category has no fresh stories, slightly older ones top it up to its minimum
(ADR D36), so a quiet weekend still has AI coverage. Stories are ordered into a narrative
sequence by category.

## How the LLM is used

**Model:** Ollama `qwen2.5:7b-instruct` running locally on the M1 Pro. The model is **pinned by
digest**, which is checked before use, so a silent model update cannot change behaviour. Output is
constrained with Ollama's **JSON-schema structured output**. OpenAI and Anthropic are optional
fallbacks behind `CHRONOS_LLM_PROVIDER`, called over plain `httpx` with no SDK dependency.

**Per-story generation (ADR D37).** The first design asked the 7B model for the whole six-story
script in one call. Measured against the validator, it often failed structurally: wrong story
order, swapped URLs, blown word budgets. The redesign makes **one small call per story** (title,
bullets, voiceover for that story) plus one call for the opening and closing lines. Code, not the
model, assembles the final `ReelScript`. Code also copies story IDs, URLs, source names and order
directly from the database, so the model never has a chance to corrupt them.

**Word budget maths.** The reel targets ~60 s at Kokoro's ~165 wpm, so the total is capped at
**185 words** (≈ 67 s):

```
per-story max   = min(45, ⌊(185 − 40) / n⌋)      → 24 words for 6 stories
per-story min   = 8   (a headline-only story gets one honest sentence, never padding)
frame lines     = 6–20 words each (opening + closing)
total           ∈ [8n + 12, 185]
```

**Validation, before anything is rendered.** Every response is parsed into Pydantic models and
checked against these rules:

- every story comes from the candidate set
- the source URL matches exactly
- story order is preserved
- word budgets are met
- a deny list of unsafe or sensational terms is clean
- no meta-phrases such as "no details were provided" leak in

**Retries and fallback:**

- Each failed unit is retried with the validator's error messages fed back into the prompt.
- A provider that fails outright is skipped for the rest of the run.
- All calls share one 270-second deadline.
- If a unit still fails, a **deterministic template** fills in that one piece only.
- A Hypothesis property test shows the template output *always* validates.

Unvalidated LLM text is never rendered.

**Prompt hardening.** Prompts are versioned files (`reel_script.v2.md` with a few-shot example).
Fetched article titles and summaries are framed explicitly as **untrusted data** to resist prompt
injection, and the prompt requires attribution ("according to BBC News"). Provenance is stored with
every brief: provider, model, digest, prompt version, attempts, whether the fallback was used,
tokens in and out, and latency.

## Media pipeline

| Stage | What happens |
|---|---|
| **Assets** | Downloads `og:image` / feed thumbnails through the SSRF guard and upgrades feed thumbnails under 800 px to the page's `og:image`. Pillow enforces a 40 MP decompression-bomb limit, **rebuilds each image from raw pixels so no EXIF or other metadata survives**, and center-crops to 1080×1350 JPEG q85. When no image is usable, the renderer draws a themed gradient instead. |
| **TTS** | Kokoro-82M (ONNX, local) at 24 kHz and speed 1.15, one WAV per scene with 350 ms gaps, then ffmpeg `loudnorm` to I = −16 LUFS, TP −1.5. `edge-tts` is the network fallback. |
| **Captions** | whisper.cpp (`ggml-small.en`, `-ojf -ml 1 -sow`) produces word timestamps from the generated audio. The known script words are then aligned to whisper's output with **Needleman–Wunsch global alignment**. Scoring: exact match +2, partial match +1 (difflib ratio ≥ 0.6 or a prefix match), mismatch or gap −1. Unmatched words are interpolated between their aligned neighbours and clamped to scene bounds (minimum 40 ms per word). If alignment quality is poor, timings fall back to a proportional split by character length. |
| **Render** | Python builds `reel.json`, stages assets into a per-render `--public-dir` inside `data/`, and runs `remotion render` (argv list, timeout). A failed render is retried at concurrency 1. Scenes: cover → stories (Ken Burns image, blurred dark text card, karaoke word captions, 12-frame fades) → outro. |
| **Verify** | `ffprobe` must report h264 1080×1920 at 30 fps with AAC audio, and a duration of `(⌈audio·30⌉ + 15) / 30` s ± 0.5 s. A thumbnail is extracted at frame 45. A reel that fails verification is never delivered. |

## Delivery, serving and the app

- **Exactly-once delivery.** `UPDATE days SET delivered_at = … WHERE delivered_at IS NULL` is an
  atomic claim, so concurrent triggers (confirmation, fallback, manual admin call) can never
  double-send. The push carries either `reel_ready` with the reel id or a `text_fallback` with the
  headlines if rendering failed. A trigger after delivery is logged and ignored.
- **Signed media URLs.** HMAC-SHA256 over path and expiry, valid for 1 hour, compared in constant
  time. Only whitelisted file names under a ULID-validated reel directory are served. HTTP Range
  requests are supported (206/416) for video seeking. An expired URL returns 410; a bad signature
  returns 404.
- **Retention.** Unsaved reels are deleted after 24 h and telemetry after 30 days, in
  foreign-key-safe order. Nightly `VACUUM INTO` backups keep the last 7.
- **App.** Reel feed, then a vertical scene pager on `expo-video` with a caption overlay. Reels
  can be saved or deleted. `chronos://reel/<id>` deep links work from a notification tap on both
  cold and warm start. The badge is cleared on open. The API client only calls allowlisted paths.

## Reliability and job execution

- **Durable queue in SQLite.** Jobs are keyed `<stage>:<local_date>:<device>`, so enqueueing is
  idempotent; `--force` adds a token to re-run. Retries back off exponentially,
  `min(300, 5·2^(attempts−1))` s, up to `max_attempts`.
- **Supervised execution.** A serial worker holds an OS `flock` while a job runs and hands the
  job to an ephemeral supervisor process. The supervisor watches a liveness pipe, escalates
  TERM → 1 s → KILL on timeout, and releases the lock only after all descendants are reaped. If
  cleanup is uncertain, the worker stops claiming jobs (fail-closed).
- **Settings for sandboxed job processes.** Job subprocesses receive a one-shot settings snapshot
  file (mode 0600, created with `O_EXCL`, deleted on read) instead of re-reading the environment.
- **Idempotent stages.** Each stage checks for its finished output and skips it unless forced. A
  failure in one stage never corrupts the previous stage's output.
- **Dry run.** `chronos simulate-morning` (T5.5) runs the whole morning on a fake clock in an
  isolated database and asserts every checkpoint.

## Security and privacy

- The API binds to the LAN interface only over HTTPS (local mkcert CA installed on the phone). It
  requires a per-device bearer token, issued through a one-time pairing code or QR and stored in
  the iOS Keychain, on every route except `/healthz` and pairing.
- Every subprocess (ffmpeg, whisper-cli, remotion, ollama) runs from an **argument list (never a
  shell string)** with a timeout and a working directory under `data/`.
- Fetched content is guarded against SSRF, size- and type-limited, and stripped of metadata. LLM
  output is schema-validated before use.
- Telemetry stores only the fields in `docs/04-data-model.md`: no location, no Wi-Fi identifiers,
  no contacts.
- `just security` runs semgrep (security-audit, python and typescript rulesets), bandit,
  pip-audit, pnpm audit and gitleaks. GitHub Actions are SHA-pinned with `contents: read`.
  Dependabot is limited to security updates.

## Engineering process

Every task in [`docs/11-implementation-plan.md`](docs/11-implementation-plan.md) went through four
gated phases with AI coding agents. The owner approved the plan at each gate and merged every PR.

| Phase | Output |
|---|---|
| Understand | `docs/plans/T*.md-brief.md`: facts, edge cases, open questions for the owner |
| Implement | `docs/plans/T*-plan.md`, code and tests on a task branch |
| Review | Verdict and findings, with a bounded fix loop |
| Push | `just check` and `just security`, then the PR. Nobody but the owner merges. |

Plan problems found during implementation went back to the owner as decisions (that is how
D35–D37 came about) instead of being patched silently. `just check` runs lint, format, strict
types and tests for all three packages, which is exactly what CI runs. Pre-commit runs ruff,
Biome, shellcheck, actionlint, bandit and gitleaks.

## Key decisions

The full list is in [`docs/01-decisions.md`](docs/01-decisions.md) and [`docs/decisions/`](docs/decisions/).

| ADR | Decision | Why |
|---|---|---|
| D03–D05 | Wake signals only from what iOS lets the app read when woken (pedometer, motion activity, lock, charger, foreground). No Shortcuts, BLE beacons or HealthKit. | Zero setup, no extra hardware, privacy |
| D04 | Server-sent silent pushes every 5 min as the primary wake mechanism, BGAppRefresh as opportunistic backup | iOS gives no reliable background schedule otherwise |
| D06 | Additive capped score, 2-tick confirm plus a single-tick strong threshold | Explainable, tunable, robust to one noisy tick |
| D08 / D09 | One reel per local day, rendered *eagerly* before the window | Delivery is instant on wake |
| D10 | One Python process (API + scheduler + worker) on SQLite WAL | Single user, single laptop: no Redis, Celery or Postgres to run |
| D11 | Local Ollama with JSON-schema output, digest-pinned; cloud LLMs opt-in | Zero cost, offline, reproducible |
| D12 / D13 | Kokoro TTS + whisper.cpp alignment of *known* script words | Best free voice quality; exact caption text with accurate timing |
| D14 | Remotion (React) driven by Python through the CLI | Declarative, testable video templates |
| D16 / D17 | LAN-only HTTPS with a local CA; signed, expiring media URLs | No public exposure |
| D25 | IANA timezone from the phone; day boundary at 03:00 local | Travel- and DST-safe |
| D35 | Jaccard ≥ 0.6 instead of SimHash Hamming for title dedupe | SimHash failed calibration on short headlines |
| D36 | Stale stories may top up a category to its minimum | Weekends had no AI stories |
| D37 | One LLM call per story; code assembles the script | A 7B model failed structurally on the whole script; per-story calls pass validation |

## Setup (macOS, Apple Silicon)

1. Tools: [uv](https://docs.astral.sh/uv/), Node 22 (`.nvmrc`), pnpm via `corepack enable`,
   `brew install just gitleaks mkcert shellcheck ffmpeg whisper-cpp ollama`, Xcode with command-line tools.
2. `just setup`: Python 3.12, pinned tools, git hooks, dependencies.
3. `just certs`: mkcert leaf cert in `data/certs/` (mode 600). Install `data/certs/rootCA.pem` as a
   profile on the iPhone.
4. `just models`: Kokoro ONNX and voices, the whisper.cpp model (SHA-256-verified against
   `scripts/models.lock`), `ollama pull qwen2.5:7b-instruct`, the Remotion browser. Copy the printed
   digest into `ollama_digest` in the `justfile`.
5. `just laptop-setup`: creates `services/api/.env` (mode 600, random secret) and loads the
   `com.chronos.api` and `com.chronos.deploy` LaunchAgents.
   **Keep the checkout outside `~/Desktop`, `~/Documents`, `~/Downloads` and iCloud Drive.** macOS
   privacy protection (TCC) blocks launchd agents from those folders, and the API crash-loops with
   `Current directory does not exist`. The setup script refuses to install agents from there unless
   you have granted Full Disk Access and set `CHRONOS_ALLOW_PROTECTED_PATH=1`.
6. `just doctor`: checks system health.
7. `just mobile-sim` (simulator) or `pnpm exec expo run:ios --device` (iPhone), then pair with
   `uv run chronos pair-code` in `services/api`. Expo Go cannot load the custom sensor module.

Real silent pushes on a physical iPhone require APNs, which needs a paid Apple Developer
membership. The simulator path works without one.

## Commands

| Command | What |
|---|---|
| `just check` | Lint, format, types and tests for all packages (what CI runs) |
| `just security` | semgrep, bandit, pip-audit, pnpm audit, gitleaks |
| `just gen-types` | Regenerate `openapi.json`, mobile `schema.d.ts` and renderer zod schema |
| `uv run chronos serve` | API + scheduler + worker (`--no-worker` to disable the worker) |
| `uv run chronos run [--date YYYY-MM-DD] [--stages …] [--force]` | Run the pipeline (or chosen stages) for a day right now |
| `uv run chronos simulate-morning` | Full dry run on a fake clock in an isolated DB (T5.5) |
| `uv run chronos wake replay` / `wake annotate` | Re-score a morning under new rules; record the actual wake time |
| `uv run chronos schema export-reel-props` | Export the `ReelProps` JSON schema for the renderer |
| `uv run chronos pair-code` | Print a one-time pairing code and QR |
| `POST /v1/admin/run`, `POST /v1/admin/deliver` | Authenticated manual pipeline run and delivery |

`uv run` commands run from `services/api`. Run `just` to list every recipe. After changing a
Pydantic model or router, run `just gen-types` and commit the results; CI rejects stale files.

## Pinned versions

| Component | Version | Pin location |
|---|---|---|
| Node / pnpm | 22.22.2 / 12.10.1 | `.nvmrc`, `apps/mobile/package.json` |
| Expo SDK / react-native | 57.0.28 / 0.86.3 | `apps/mobile/package.json` |
| TypeScript (mobile / renderer) | 6.0.3 / 7.0.2 | `package.json` per package |
| remotion | 4.0.534 | `packages/renderer/package.json` |
| zod | 4.5.4 | `packages/renderer/package.json` |
| Python / uv | 3.12 / 0.12.18 | `services/api/pyproject.toml`, `justfile` |
| FastAPI / SQLModel / SQLAlchemy | 0.142.2 / 0.0.47 / 2.0.54 | `services/api/pyproject.toml` |
| ruff / pyright / pytest | 0.16.10 / 1.1.414 / 9.1.1 | dev deps |
| semgrep / gitleaks | 1.178.0 / 8.21.2 | `justfile` |
| Ollama model | `qwen2.5:7b-instruct` @ digest | `justfile` `ollama_model`, `ollama_digest` |

Library versions are locked by `services/api/uv.lock`, `apps/mobile/pnpm-lock.yaml` and
`packages/renderer/pnpm-lock.yaml`. Metro does not follow symlinks, so
`apps/mobile/pnpm-workspace.yaml` sets `nodeLinker: hoisted`.

## Documents

| | |
|---|---|
| Plan and requirements | [docs/00-overview.md](docs/00-overview.md) |
| Decisions (binding) | [docs/01-decisions.md](docs/01-decisions.md), [docs/decisions/](docs/decisions/) |
| Architecture | [docs/02-architecture.md](docs/02-architecture.md) |
| Wake engine | [docs/03-wake-engine.md](docs/03-wake-engine.md) |
| Data model | [docs/04-data-model.md](docs/04-data-model.md) |
| API | [docs/05-api-contracts.md](docs/05-api-contracts.md) |
| Content and media pipelines | [docs/06-content-pipeline.md](docs/06-content-pipeline.md), [docs/07-media-pipeline.md](docs/07-media-pipeline.md) |
| Mobile app | [docs/08-mobile-app.md](docs/08-mobile-app.md) |
| Security | [docs/09-security.md](docs/09-security.md) |
| CI/CD and laptop ops | [docs/10-cicd-and-ops.md](docs/10-cicd-and-ops.md) |
| Task list | [docs/11-implementation-plan.md](docs/11-implementation-plan.md) |
| Testing | [docs/12-testing-and-quality.md](docs/12-testing-and-quality.md) |
| Per-task briefs and plans | [docs/plans/](docs/plans/) |
| Agent workflow | [AGENTS.md](AGENTS.md), `.pi/agents/`, `.pi/prompts/` |
