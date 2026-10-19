# Testing and Quality

## 1. Principles

- Tests are written in the same step as the code (plan steps include test names and expected values).
- Pure cores (scoring, window maths, dedupe, alignment, caption line-breaking) get property tests
  and golden fixtures; I/O layers get integration tests with fakes; hardware/model-dependent
  pieces get marked tests that run only where the dependency exists.
- Golden fixtures are the source of truth. Never edit an expected value to make a test pass; raise
  it in the PR.
- Every bug found in a real morning becomes a fixture morning before it is fixed.

## 2. Backend (`services/api`)

| Layer | Tooling | What |
|---|---|---|
| Unit | pytest, hypothesis, freezegun/time-machine | `wake/scoring` (golden mornings + properties), `wake/window` (DST, boundaries), `wake/rules` (validation), `content/normalise` (dedupe), `content/rank`, `content/script/schema` (validators), `media/captions` (alignment), `media/reel_json` |
| Integration | pytest + FastAPI `TestClient`, temp SQLite file (not `:memory:`, to test WAL/PRAGMAs), fake Expo client, fake LLM, fake HTTP transport (`httpx.MockTransport`) | pairing/auth, telemetry → state machine, scheduler with fake clock, job queue concurrency (threads), SSRF guard with fake resolver, media signing/Range, retention |
| Migration | pytest | `alembic upgrade head` then `downgrade base` on a fresh file; schema snapshot compared |
| Marked (local only) | `@pytest.mark.ollama`, `kokoro`, `whisper`, `remotion` — auto-skip when the binary/model/server is missing; CI deselects them | real script generation, real TTS length, real whisper timings, real render |
| Coverage | `pytest-cov`; fail under 85 % for `chronos/wake`, `chronos/content/script`, `chronos/http` | |

Fixture mornings (`tests/fixtures/wake/mornings/`): JSON arrays of telemetry events with a rules
version and expected per-event decisions. Required set:

1. `typical_weekday.json` — unplug 06:48, unlock, 60 steps by 06:55 → confirm at second tick.
2. `night_check.json` — 03:10 unlock, 0 steps → outside window, score 0.
3. `snooze.json` — 06:30 unplug + unlock, back to sleep (0 steps, stationary) → never confirms until 07:40 walk.
4. `pedometer_denied.json` — reweighting path: first in-window tick with unlock (base 25) + charger_disconnect (25) + foreground (40) = 90 ≥ strong 85 → **confirm on first tick** (`confirm_reason=strong`). Companion `pedometer_denied_no_app.json` shows that without foreground the maximum is 35 + 25 = 60 < confirm 70, so the day never auto-confirms.
5. `early_walk.json` — 100 steps at 05:52 (bathroom), asleep again, real wake 08:10 → first window tick 06:00 sees `steps_10m` = 100 → high; next tick low → **no confirm** (2-tick rule) — then confirm at 08:15.
6. `weekend.json` — same as 1 at 09:30 Saturday with the weekend window.
7. `dst_fall_back.json` — America/New_York, 2026-11-01.
8. `missed_ticks.json` — ticks at 07:00 and 07:12 (one missed) both high → confirm (≤ 12 min rule).
9. `foreground_only.json` — owner opens the app at 06:40 without walking → 40 + 25 + 15 = 80 → **strong? no** (85) → high; second tick with steps confirms. Documents that opening the app alone does not confirm.

## 3. Mobile (`apps/mobile`)

- `jest-expo` unit tests: `buildTelemetry` against the OpenAPI JSON schema (ajv), outbox ordering
  and expiry, `withTimeout`, deep-link parsing, caption overlay (`groupWords`, active-word index for
  a time), settings form validation mirroring `WakeRules`.
- Swift XCTest: `ActivitySummariser` (change points → durations with clamping; confidence max),
  `DeviceStateMapper`. Runs in `ios-build.yml` (manual/weekly) and `just mobile-ios-test`.
- `expo-doctor` and `expo export` in CI catch config/bundle errors.

## 4. Renderer (`packages/renderer`)

- Vitest: `sceneFrames()` (start/end frames from seconds, rounding), `layoutCaptions()`
  (3–5 words per line, no orphan, max width by measured char widths), `fitHeadline()` size steps,
  prop schema parse of `sample-reel.json`.
- CI still smoke render at frames 0, 60, 600 → artefacts.
- Owner visual checklist for `sample-reel.json` (run `just render-sample`, watch on the phone):
  headline never cut; captions never overlap the headline or the iOS home indicator; active word
  obvious at arm's length; images never stretched; b-roll has a scrim; transitions ≤ 0.5 s; audio
  starts within 0.3 s of the first word's caption.

## 5. Manual device checklist (before closing T2.4 and T5.5)

| # | Scenario | Expected |
|---|---|---|
| 1 | Phone locked on nightstand, charging, app backgrounded; server tick | event with `is_unlocked=false`, `battery_state=charging`, steps ≈ 0 within 60 s |
| 2 | Unplug, unlock, walk 60 steps; next two ticks | scores ≥ 70 twice → `confirmed`; push arrives; tap opens reel |
| 3 | Low Power Mode on | ticks may be delayed; debug shows `low_power_mode=true`; fallback still delivers |
| 4 | Force-quit app the night before | no `silent_push` events; fallback at window end; report flags it; banner on next open |
| 5 | Airplane mode during window, then Wi-Fi back | queued events flushed with original `device_ts`; skew check passes if < 5 min old, else dropped |
| 6 | Deny Motion permission | `pedometer_auth=denied`; settings prompt; day can still confirm via reweighting |
| 7 | Travel: tz change | next day's window uses the new tz |
| 8 | Laptop asleep until 07:00 | missed prewarm → pipeline runs at wake; confirm → delivery waits for reel; push arrives when ready |

## 6. End-to-end dry run (`chronos simulate-morning`)

Replays a fixture morning against the live API on the laptop using a test device token, with the
real pipeline (optionally `--fake-llm --fake-tts` for speed). Produces the nightly report and
asserts: prewarm completed before window start, confirm time, exactly one push, reel playable
(`ffprobe`). Used in T5.5 and before every release.

## 7. Definition of done for any task

- Plan steps all done; `just check` and `just security` green; CI green.
- Tests for new logic including the brief's edge cases.
- Docs updated (`docs/*`, ADR if a decision changed, `README` if setup changed).
- Reviewer verdict APPROVE with no BLOCKING findings; MINOR findings logged.
- PR opened with the template filled; owner merges.
