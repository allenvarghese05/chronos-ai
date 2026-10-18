# Implementation Plan — milestones and tasks

Every task is one branch, one PR, run through `/task <id>` (understand → implement → review →
push). Acceptance criteria are what the reviewer checks and what the PR proves with tests. Task
ids are stable; never renumber. Dependencies are listed; tasks within a milestone without a
dependency on each other can be done in parallel branches.

Estimated effort is for one engineer with the agent pipeline; "S" ≤ ½ day, "M" ≈ 1 day, "L" 2–3 days.

---

## M0 — Foundation (repo, tooling, CI, laptop)

**Done when:** `just check` and `just security` pass locally and in CI for three scaffolded apps;
`main` is protected; the laptop runs the empty API as a service and deploys from `main`.

| ID | Task | Size | Depends | Acceptance criteria |
|---|---|---|---|---|
| T0.1 | Repo skeleton: `justfile` (all recipes, skip-when-missing), `.pre-commit-config.yaml`, `.gitignore`, `.nvmrc`, `VERSION`, `README.md`, `AGENTS.md`, `docs/` (this plan), `.pi/` agents & prompts, PR template, `CODEOWNERS`, `SECURITY.md` | M | — | `just check` exits 0 printing "skipped" for each app; `pre-commit run --all-files` passes; `just security` runs gitleaks and reports the rest skipped |
| T0.2 | GitHub bootstrap: `scripts/bootstrap-github.sh`, `scripts/github-ruleset.json`, `.github/dependabot.yml`, `ci.yml` (4 jobs with skip-when-missing), `zizmor.yml`, `audit.yml`, `pr-title.yml`, `secrets` job; `just ruleset-check` (contexts == job names) | M | T0.1 | Bootstrap idempotent: secret scanning, environment reviewers and ruleset fail with clear messages on a private free-plan repo, everything else no-ops; `just ruleset-check` exits 0; `zizmor` reports zero findings; `pre-commit run --all-files` passes; ruleset + push protection deferred to T6.7 |
| T0.3 | API scaffold `services/api`: uv project (Python 3.12), FastAPI app factory `chronos.main:create_app`, `Settings`, SQLite engine with PRAGMAs, Alembic configured (no revision yet), `/healthz`, `/readyz` (DB only), structlog JSON, `chronos` CLI (`serve`, `db migrate`, `config validate`, `doctor` stub), ruff/pyright/pytest/bandit/pip-audit in dev group, `.env.example`, tests for config and health | M | T0.1 | `just check-api` green; `uv run chronos serve` answers `/healthz`; pyright strict with 0 errors; `pytest` ≥ 5 tests; bandit clean |
| T0.4 | Mobile scaffold `apps/mobile`: Expo SDK pinned, TypeScript strict, expo-router, dev-client, Biome, jest-expo, `app.config.ts` with bundle id and placeholders for background modes, `expo-doctor` clean, empty `modules/chronos-sensors` Expo module compiling (`hello()`), `.nvmrc` shared | M | T0.1 | `just check-mobile` green locally and in CI; `expo-doctor` clean; Swift module skeleton and TS bridge type-check and are unit-tested with jest mocks |
| T0.2b | Deterministic expo-doctor in CI (hotfix): env-var skip in `check-mobile`, weekly advisory in `audit.yml`, ADR D32, bump Expo SDK patches | S | T0.4 | `just check-mobile` passes even when Expo publishes SDK patches; expo-doctor remains required (20 checks); weekly advisory in `audit.yml`; ADR D32 records policy |
| T0.2d | Dependabot security-only fix: `open-pull-requests-limit: 0` for pip/npm, `deps` scope in pr-title, ADR D33 | S | T0.2 | pip/npm Dependabot opens no version-bump PRs; Actions SHA-bump PRs pass pr-title check; `just check` and `just security` green; ADR D33 recorded |
| T0.4b | Prebuild + install dev-client on the owner's iPhone; verify hello() | S | T0.4 | `expo prebuild --platform ios --clean` succeeds (requires Xcode 16 + CocoaPods); `expo run:ios --device` installs on the owner's iPhone and the screen shows "Hello from ChronosSensors!" |
| T0.5 | Renderer scaffold `packages/renderer`: Remotion project, `MorningReel` composition with cover scene only, `sample-reel.json` fixture, zod `ReelProps` (hand-written until T3.4), vitest, Biome, CI still smoke render | M | T0.1 | `just check-renderer` green locally and in CI; `smoke.png` artefact visible on the PR |
| T0.6 | Laptop ops: `scripts/deploy.sh`, `scripts/launchd/*.plist`, `just laptop-setup`, `just certs` (mkcert), `just models` (downloads with sha256; Ollama pull with digest), `chronos doctor` real checks, `pmset` schedule, firewall note, deploy window guard | M | T0.3 | `launchctl list` shows `com.chronos.api` running; `curl --cacert ... https://chronos.local:8443/healthz` works from the phone's browser; merging a docs PR deploys within 10 min (deploy log shows new SHA) |
| T0.7 | Shared schema tooling: `just gen-types` (Pydantic → JSON schema → zod for renderer, `openapi-typescript` for mobile), CI staleness check | S | T0.3, T0.4, T0.5 | Editing a Pydantic model without regenerating fails the `api` CI job |

## M1 — Wake engine (backend)

**Done when:** telemetry POSTed by a fake device moves a day through `monitoring → confirmed`
with stored, explainable evaluations; rules are editable via API; replay works on stored data.

| ID | Task | Size | Depends | Acceptance criteria |
|---|---|---|---|---|
| T1.1 | Tables + migration 0001: `devices`, `pairing_codes`, `rule_versions`, `telemetry_events`, `wake_evaluations`, `days`, `day_transitions`, `settings` (per `04-data-model.md`) | M | T0.3 | Migration up and down both work (test); unique indexes present; `PRAGMA foreign_keys` enforced in a test |
| T1.2 | Pairing and device auth: `chronos pair-code` (prints code + QR), `POST /v1/pair`, bearer dependency, `GET/PATCH/DELETE /v1/device/me`, `chronos device revoke`, rate limiting, problem+json error model | M | T1.1 | Wrong code ×5 burns it (test); token works, revoked token → 403; tokens stored hashed; 5/min pairing + 120/min default rate limit tests |
| T1.3 | Window and local-day computation `wake/window.py`: weekday/weekend/holiday, `day_boundary`, DST days, tz change rules | S | T1.1 | Parametrised tests for Asia/Kolkata, America/New_York DST days, 03:00 boundary, 23:59 → next day; 100 % coverage of the module |
| T1.4 | Rules loader `wake/rules.py`: `WakeRules` schema, YAML load, DB override, versioning, `chronos config validate`, `GET/PUT /v1/config/wake` | M | T1.1 | Invalid YAML (cap < max points) rejected with field path; PUT creates version n+1 and activates atomically; previous versions retained |
| T1.5 | Scoring engine `wake/scoring.py`: `evaluate(signals, rules, now, ctx) -> Evaluation` pure; derived facts (charger transition, consecutive unlocked, dedupe); penalties; reweighting; breakdown | L | T1.3, T1.4 | Golden fixture `tests/fixtures/wake/mornings/*.json` (≥ 6 scripted mornings incl. "3 a.m. phone check", "unplug and go back to sleep", "pedometer denied", "walk at 05:55 then 06:05") produce the expected decisions; hypothesis property: score ∈ [0,100], monotone in steps; outside window always 0 |
| T1.6 | Telemetry endpoint + day state machine: `POST /v1/telemetry` (validation, clock skew, idempotent seq), `wake/state.py` transitions with audit rows, `GET /v1/day/*`, `PUT actual-wake` | M | T1.2, T1.5 | Integration test drives a day from `monitoring` to `confirmed` with two high ticks; duplicate `seq` returns the same evaluation; skew → 422; transition audit rows match |
| T1.7 | Scheduler + silent ticks: APScheduler in lifespan, `tick` job per device in `monitoring`, Expo push client (silent), `push_notifications` table (migration 0002), receipts job, window start/end transitions, `fallback` state | M | T1.6 | With a fake Expo client, a simulated day sends ticks every 5 min inside the window only, stops on confirm, and transitions to `fallback` at window end; receipts recorded; `MessageRateExceeded` backs off |
| T1.8 | Replay and report: `chronos wake replay`, `chronos wake annotate`, nightly report generator (Markdown) with score timeline, `GET /v1/report/{date}` | M | T1.6 | Replay of fixture mornings under two rule versions prints a comparison table; report file produced for a fixture day |

## M2 — Mobile telemetry client

**Done when:** on the owner's iPhone, silent pushes wake the app in the background, real CoreMotion
history is posted, and the debug screen shows the server's breakdown.

| ID | Task | Size | Depends | Acceptance criteria |
|---|---|---|---|---|
| T2.1 | Swift module `chronos-sensors`: `querySteps`, `queryActivity`, `deviceState`, `pedometerAuthorization`, background refresh registration; XCTest with fake CoreMotion protocols | L | T0.4 | XCTests pass for duration summation (change-point → durations), clamping, mapping; manual: debug screen shows real steps after walking 50 steps; re-verify the chronos-sensors not-found match string against the installed expo-modules-core. |
| T2.2 | Pairing flow + secure storage + API client: `/pairing` screen (code + QR scan via `expo-camera`), `expo-secure-store`, generated client, `PATCH /device/me` with push token, cert trust instructions screen | M | T1.2, T0.7 | Pair with a code printed by the laptop; token persists across restarts; revoke → app returns to pairing |
| T2.3 | Telemetry sampler + outbox: `sampleAndSend`, `buildTelemetry`, SQLite outbox, timeouts, seq counter, 401 handling | M | T2.1, T2.2 | Jest: payload matches the OpenAPI schema; outbox retries in order and drops > 2 h old; manual: airplane mode → events queued → delivered later with same seqs |
| T2.4 | Background wake handling: `expo-notifications` background task for silent push, `BGAppRefreshTask` wiring, foreground sampling, permission flow screens | L | T2.3, T1.7 | On device: server tick → event within 60 s while app is backgrounded and phone locked (verified in server logs on ≥ 3 ticks); force-quit caveat banner shown; permission denial reflected in `pedometer_auth`; install the dev-client on the physical iPhone (EAS device build or USB-paired Xcode install) and verify hello() natively on device |
| T2.5 | Debug screen: live signals, last breakdown, 24 h score timeline, missed ticks, outbox size, "send now", "mark actual wake time", versions | M | T2.3 | Screen renders from fixture data in Jest; manual: values update after "send now" |
| T2.6 | Settings screen: wake rules form (window, thresholds, weights) backed by `GET/PUT /v1/config/wake` with schema validation errors shown; theme/render-mode/pexels toggles via `/v1/config`; test push | M | T1.4, T2.2 | Changing the confirm threshold on the phone is visible in `GET /v1/config/wake` as a new version |

## M3 — Content pipeline

**Done when:** `chronos run --date today --stages ingest,script` produces a validated `ReelScript`
from live sources with Ollama, and a template script when Ollama is unavailable.

| ID | Task | Size | Depends | Acceptance criteria |
|---|---|---|---|---|
| T3.1 | Tables + migration 0003: `sources`, `items`, `briefs`, `brief_items`, `jobs`; job queue `jobs/queue.py` + serial `jobs/worker.py` (claim, retry/backoff, timeout, stale-lock reclaim, caffeinate wrapper), `GET /v1/jobs` | L | T1.1 | Migration up/down; concurrency test: two workers never run the same job; failed job retried with backoff then `failed`; timeout kills a sleeping job |
| T3.2 | SSRF-guarded HTTP client `chronos/http.py` + `sources.yaml` loader + fetchers (rss, hf_api, arxiv_api, hn_rss) with recorded fixtures; conditional GET; source health columns | L | T3.1 | Fixture responses → golden items for all 8 sources; private-IP URL rejected pre-connect and on redirect (fake resolver test); oversized body aborted at `max_bytes` |
| T3.3 | Normalise + dedupe + rank + select: canonical URLs, simhash, scoring formula, quotas, category minimums | M | T3.2 | Property test: dedupe idempotent/order-independent; selection returns exactly `total_stories` with category minimums when possible; golden selection for a fixture day |
| T3.4 | `ReelScript` schema + validators, prompt v1 + example, `ScriptGenerator` protocol with Ollama (structured output, digest pin), OpenAI/Anthropic optional providers, deterministic fallback, `script` job, `just gen-types` now exports `ReelScript`/`ReelProps` | L | T3.3, T0.7 | Validator rejects hallucinated item/url and word budget violations (tests); `FakeScriptGenerator` end-to-end job test; `@pytest.mark.ollama` smoke passes locally; fallback used when provider raises; provenance stored |
| T3.5 | Pipeline orchestration `pipeline.py` + `POST /v1/admin/run` + `chronos run`: chains stages per date, idempotent, respects `render_mode`; prewarm scheduling added to the scheduler | M | T3.4, T1.7 | Running twice does not re-fetch/re-generate unless `--force`; stage failure leaves earlier artefacts; scheduler enqueues prewarm at window start − 30 min (fake clock test) |

## M4 — Media pipeline

**Done when:** `chronos run --date today` ends with `data/media/<date>/<id>/reel.mp4` (1080×1920,
~60 s, voice, synced captions, images/b-roll) in under 6 minutes on the laptop.

| ID | Task | Size | Depends | Acceptance criteria |
|---|---|---|---|---|
| T4.1 | Tables + migration 0004: `reels`, `reel_assets`; assets job: og:image resolution, Pillow processing (EXIF strip, resize), optional Pexels b-roll, ffmpeg re-encode, limits | M | T3.5 | Image with EXIF GPS → output has no metadata (test); 10 MB limit enforced; SVG/HTML rejected; Pexels path skipped without key; fixture reel gets ≥ 1 asset per story |
| T4.2 | TTS job: `kokoro-onnx` integration, per-scene WAVs, silence padding, concat, loudnorm, `tts.json`; edge-tts fallback; model download in `just models` | M | T4.1 | `@pytest.mark.kokoro` produces a WAV of 50–70 s for the fixture script; unit tests for concat/timing with synthetic audio; fallback path tested with a fake |
| T4.3 | Captions job: whisper.cpp runner, word JSON parsing, script-word alignment (Needleman–Wunsch), interpolation, proportional fallback | M | T4.2 | Alignment unit tests on hand-made token sequences (insertions/deletions/number formats); `@pytest.mark.whisper` on a fixture WAV yields monotone non-overlapping timings covering ≥ 95 % of script words |
| T4.4 | Remotion compositions: design tokens, fonts, `CoverScene`, `StoryScene` (image / b-roll / gradient), `OutroScene`, `WordCaptions`, transitions, themes, `ReelProps` from generated zod; `renderer_version` | L | T0.5, T3.4 | Vitest for timing maths and caption line-breaking; still smoke in CI at frames 0/60/600; visual review of `sample-reel.json` render by the owner (checklist in `12-testing-and-quality.md` §4) |
| T4.5 | Render job + verification: `reel_json.py` builder, per-render `public/` staging, `remotion render` runner, `ffprobe` verification, thumbnail, sha256, timing metrics, `caffeinate`; end-to-end `chronos run` | L | T4.3, T4.4 | `@pytest.mark.remotion` end-to-end on the fixture produces a valid MP4; ffprobe assertions; total stage durations logged and under budget table in `07-media-pipeline.md` §5 |

## M5 — Delivery and the app feed

**Done when:** the owner wakes up, walks, and within ~2 minutes gets a push that opens the reel in
the app; unsaved reels disappear after a day.

| ID | Task | Size | Depends | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| T5.1 | Media serving + retention: signed URLs, `GET /v1/media`, Range support, `GET /v1/reels*`, save/delete, retention job (reels 24 h, telemetry 30 d, items 14 d, jobs), nightly VACUUM backup | M | T4.5 | | Expired signature → 410; path traversal attempts → 404 (tests); retention test with frozen clock deletes exactly the expected rows and files |
| T5.2 | Delivery job: visible push on confirm/fallback with reel id, text fallback when render failed, receipts, `POST /v1/admin/deliver`, `push-test` | M | T5.1, T1.7 | Fake Expo client: confirm → exactly one `reel_ready` push even if confirm and window-end race; failed render → `text_fallback` push with headlines |
| T5.3 | Feed + player screens: vertical pager, `expo-video` player, captions overlay from `captions.json`, source sheet, save/delete, empty state with day status, thumbnail list | L | T2.2, T5.1 | | Jest for pager state/caption overlay; manual: swipe through today's reel, toggle captions, open a source link, save, delete |
| T5.4 | Notification tap deep link `chronos://reel/<id>` → player; cold-start and warm-start handling; badge clear | S | T5.3 | | Tapping the push from lock screen opens the reel in both cold and warm start |
| T5.5 | First full morning dry run: `chronos run --now` + `chronos simulate-morning` (fake device events) end-to-end on the laptop; nightly report reviewed; fix list captured as tasks | M | T5.4 | | Report shows: prewarm done before window, ≥ 1 tick/5 min, confirm within 10 min of annotated wake, push received, reel plays |

## M6 — Hardening and stretch

| ID | Task | Size | Depends | Acceptance criteria |
|---|---|---|---|---|
| T6.1 | Observability polish: `/metrics`, log redaction tests, report push on failure, `doctor` completeness | S | T5.5 | Secret never appears in logs (test with a canary token) |
| T6.2 | Tuning after 7 real mornings: run replay, adjust `wake_rules.yaml` v2, write `docs/decisions/D31-wake-rules-v2.md` with data | S | T5.5 | Median confirm latency ≤ 10 min; zero early confirms in the 7-day sample |
| T6.3 | Security review pass: run the reviewer checklist across the repo, threat-model deltas, `SECURITY.md` update, firewall/FileVault verification in doctor | M | T5.5 | All BLOCKING findings fixed; `just security` clean |
| T6.4 | Optional cloud sync of saved reels (Cloudflare R2 free tier) behind a flag | M | T5.1 | Saved reel uploaded with private ACL + signed GET; disabled by default |
| T6.5 | Optional per-story reels (one MP4 per story, feed shows a reel per story) | L | T5.3 | Behind `reel_mode: per_story`; default unchanged |
| T6.6 | Optional Tailscale access away from home (cert SAN for the tailnet name; no code change expected) | S | T0.6 | Documented in `docs/decisions/` |
| T6.7 | Enable server-side protection when the plan allows: apply `scripts/github-ruleset.json` ruleset to main, enable secret scanning + push protection, add environment reviewers to `mobile-build`, re-run `scripts/bootstrap-github.sh` to verify all steps succeed | S | T0.2 | `gh api repos/*/rulesets` lists the protect-main ruleset; secret scanning enabled; push protection enabled; `mobile-build` environment has reviewer set; re-run of bootstrap prints no errors |

---

## Order of work (critical path)

```
T0.1 → T0.2 → T0.3 → T0.6
            ↘ T0.4 → T2.1
            ↘ T0.5
T0.3 → T0.7
T0.3 → T1.1 → T1.2 → T1.6 → T1.7 → T1.8
       T1.1 → T1.3 ┐
       T1.1 → T1.4 ┴→ T1.5 → T1.6
T1.2 + T0.7 → T2.2 → T2.3 → T2.4 → T2.5, T2.6
T1.1 → T3.1 → T3.2 → T3.3 → T3.4 → T3.5
T3.5 → T4.1 → T4.2 → T4.3 ┐
T0.5 + T3.4 → T4.4        ┴→ T4.5 → T5.1 → T5.2
T2.2 + T5.1 → T5.3 → T5.4 → T5.5 → M6
```

First usable morning (push with a reel, fallback-only wake) after: M0 + T1.1–T1.2 + T1.7 (ticks
not needed yet) + M3 + M4 + T5.1–T5.2 + T2.2 + T5.3. Full behavioural wake after M1 + M2.
