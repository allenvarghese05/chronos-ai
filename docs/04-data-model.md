# Data Model (SQLite, WAL mode)

- Engine: SQLite 3.45+ via SQLAlchemy 2 / SQLModel; `PRAGMA journal_mode=WAL; foreign_keys=ON;
  busy_timeout=5000`. File: `data/chronos.db` (gitignored).
- Migrations: Alembic, one revision per task that changes schema, up **and** down tested.
- Timestamps: UTC ISO-8601 with microseconds and Z suffix in `*_at` columns (`TEXT`), local dates as `YYYY-MM-DD` `TEXT`.
- JSON columns are `TEXT` validated by a Pydantic model at the boundary.
- IDs: `INTEGER PRIMARY KEY` for internal tables; `TEXT` ULIDs for anything exposed in URLs
  (`reels.public_id`, `devices.public_id`).

## 1. Entity relationship overview

```
devices 1──* telemetry_events 1──1 wake_evaluations *──1 rule_versions
                                   │
days 1──* day_transitions ─────────┘ (caused_by_evaluation_id)
days 1──0..1 briefs 1──* brief_items *──1 items *──1 sources
briefs 1──0..1 reels 1──* reel_assets
reels 1──* push_notifications *──1 devices
jobs (standalone; payload references local_date / ids)
```

## 2. Tables

### 2.1 Devices and auth

**`devices`** — the paired iPhone (v1: exactly one `active` row)

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| public_id | TEXT UNIQUE | ULID, used in logs/UI |
| name | TEXT | "Allen's iPhone" |
| token_hash | TEXT UNIQUE | SHA-256 of the bearer token; token shown once at pairing |
| expo_push_token | TEXT NULL | `ExponentPushToken[...]`, updated by the app |
| tz | TEXT | last reported IANA tz |
| app_version, ios_version, model | TEXT NULL | |
| status | TEXT | `active` \| `revoked` |
| created_at, last_seen_at, revoked_at | TEXT | |

**`pairing_codes`** — short-lived codes shown by `chronos pair-code` / the laptop QR

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| code_hash | TEXT UNIQUE | SHA-256 of 8-char code |
| expires_at | TEXT | 10 min |
| consumed_at | TEXT NULL | one-time |
| attempts | INTEGER | rate limit: 5 wrong attempts burns the code |

### 2.2 Wake engine

**`rule_versions`** — every rule set ever in effect

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| version | INTEGER UNIQUE | monotonically increasing |
| source | TEXT | `yaml` \| `api` |
| rules_json | TEXT | full `WakeRules` document |
| sha256 | TEXT | of canonical JSON, for replay provenance |
| created_at, created_by | TEXT | `created_by` = `cli` \| `api:<device public_id>` |
| active | INTEGER | exactly one row = 1 (partial unique index `WHERE active = 1`) |

**`telemetry_events`** — one row per snapshot from the phone

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| device_id | FK devices | |
| received_at | TEXT | server UTC |
| device_ts | TEXT | as sent (with offset) |
| tz | TEXT | |
| local_date | TEXT | derived with `day_boundary` |
| wake_source | TEXT | enum |
| seq | INTEGER | per device; UNIQUE(device_id, seq) for idempotency |
| steps_10m, steps_since_window | INTEGER | |
| walking_sec_10m, stationary_sec_10m | INTEGER | |
| walking_conf | TEXT | enum |
| is_unlocked | INTEGER | bool |
| battery_state | TEXT | enum |
| battery_level | REAL NULL | |
| app_state | TEXT | enum |
| low_power_mode | INTEGER | bool |
| pedometer_auth | TEXT | enum |
| tick_seq | INTEGER NULL | the server tick that woke it (for missed-tick accounting) |
| app_version, ios_version | TEXT | |
| raw_json | TEXT | exact payload as received (schema-validated) |

Indexes: `(device_id, received_at)`, `(local_date)`.

**`wake_evaluations`** — the score for one telemetry event

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| telemetry_event_id | FK UNIQUE | 1:1 |
| rule_version_id | FK rule_versions | |
| local_date | TEXT | |
| gate | TEXT | `open` \| `closed` |
| score | INTEGER | 0–100 |
| breakdown_json | TEXT | `[{rule_id, input, points, cap, note}]` + penalties + derived facts |
| decision | TEXT | `outside_window` \| `low` \| `high` \| `confirm` \| `ignored_dedupe` \| `already_confirmed` |
| consecutive_high | INTEGER | count used for the 2-tick rule |
| evaluated_at | TEXT | |

**`days`** — one row per local date per device

| column | type | notes |
|---|---|---|
| local_date | TEXT PK (with device_id) | |
| device_id | FK | |
| tz | TEXT | frozen at creation |
| kind | TEXT | `weekday` \| `weekend` \| `holiday` |
| window_start_utc, window_end_utc | TEXT | computed once |
| state | TEXT | `idle` \| `monitoring` \| `confirmed` \| `fallback` \| `delivered` |
| confirmed_at | TEXT NULL | |
| confirm_evaluation_id | FK NULL | |
| delivered_at | TEXT NULL | |
| fallback_used | INTEGER | bool |
| actual_wake_local | TEXT NULL | owner annotation `HH:MM` |
| ticks_sent, ticks_received | INTEGER | counters |
| brief_id | FK NULL | |
| reel_id | FK NULL | |
| created_at, updated_at | TEXT | |

**`day_transitions`** — audit log

| column | type |
|---|---|
| id | INTEGER PK |
| local_date, device_id | FK days |
| from_state, to_state | TEXT |
| cause | TEXT (`evaluation` \| `scheduler_window_start` \| `scheduler_window_end` \| `deliver_ok` \| `manual`) |
| caused_by_evaluation_id | FK NULL |
| at | TEXT |

### 2.3 Content

**`sources`** — mirror of `config/sources.yaml` plus runtime state

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| key | TEXT UNIQUE | `hf_daily_papers`, `arxiv_cs_ai_cl_lg`, `hn_front`, `bbc_world`, `cbs_top`, `ie_india`, `ie_tech`, `ndtv_top` |
| kind | TEXT | `hf_api` \| `arxiv_api` \| `rss` \| `hn_rss` |
| url | TEXT | |
| category | TEXT | `ai` \| `tech` \| `world` \| `india` |
| enabled | INTEGER | |
| quota | INTEGER | max items into a brief |
| weight | REAL | ranking multiplier |
| min_points | INTEGER NULL | `hn_rss` only: drop items whose Algolia points are known and below this (CHECK ≥ 0; migration 0004) |
| etag, last_modified | TEXT NULL | conditional GET |
| last_fetched_at, last_status, last_error | TEXT NULL | |
| consecutive_failures | INTEGER | |

**`items`** — normalised stories

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| source_id | FK | |
| external_id | TEXT | guid / arXiv id / HF paper id / canonical URL |
| url | TEXT | canonical (tracking params stripped) |
| url_hash | TEXT | sha256(canonical url) |
| title | TEXT | |
| summary | TEXT NULL | first 1 000 chars of description/abstract, HTML stripped |
| author | TEXT NULL | |
| published_at | TEXT NULL | |
| fetched_at | TEXT | |
| image_url | TEXT NULL | from feed `media:content`/`enclosure` or `og:image` (resolved lazily) |
| extra_json | TEXT NULL | HF upvotes, HN points/comments, arXiv categories |
| title_simhash | INTEGER | near-duplicate detection across sources |
| UNIQUE(source_id, external_id); INDEX(url_hash); INDEX(published_at) | | |

**`briefs`** — the day's script

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| local_date, device_id | FK days | UNIQUE |
| status | TEXT | `drafting` \| `ready` \| `failed` |
| script_json | TEXT | validated `ReelScript` |
| llm_provider, llm_model, model_digest | TEXT | provenance |
| prompt_version | TEXT | `prompts/reel_script.v1.md` hash |
| attempts | INTEGER | |
| used_fallback_template | INTEGER | bool |
| tokens_in, tokens_out, latency_ms | INTEGER NULL | |
| created_at, updated_at | TEXT | |

**`brief_items`** — which items went into which scene

| column | type |
|---|---|
| brief_id, item_id | PK, FKs |
| scene_index | INTEGER |
| role | TEXT (`primary` \| `mention`) |

### 2.4 Media

**`reels`**

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| public_id | TEXT UNIQUE | ULID, used in URLs and push payload |
| brief_id | FK UNIQUE | |
| local_date, device_id | FK days | |
| status | TEXT | `assets` \| `tts` \| `captions` \| `rendering` \| `ready` \| `failed` |
| file_path | TEXT NULL | relative to `data/media/` |
| sha256, size_bytes | TEXT/INTEGER NULL | |
| duration_sec | REAL NULL | from ffprobe |
| width, height, fps | INTEGER | 1080, 1920, 30 |
| render_ms, tts_ms, captions_ms | INTEGER NULL | |
| renderer_version | TEXT | package version + composition id |
| saved | INTEGER | bool; saved reels are exempt from retention |
| expires_at | TEXT NULL | created_at + 24 h unless saved |
| error | TEXT NULL | |
| created_at, updated_at | TEXT | |

**`reel_assets`**

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| reel_id | FK | |
| kind | TEXT | `voice_wav` \| `voice_mp3` \| `captions_json` \| `reel_json` \| `image` \| `broll` \| `thumbnail` |
| path | TEXT | relative to `data/media/` |
| source_url | TEXT NULL | where it was downloaded from |
| licence | TEXT NULL | `pexels` \| `og_image` \| `generated` \| `kokoro` \| `edge_tts` (migration 0005) |
| width, height, duration_sec, size_bytes | nullable | |
| sha256 | TEXT | |
| scene_index | INTEGER NULL | |

### 2.5 Delivery and jobs

**`push_notifications`**

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| device_id | FK | |
| kind | TEXT | `tick` (silent) \| `reel_ready` \| `text_fallback` \| `test` |
| local_date | TEXT | |
| reel_id | INTEGER NULL | No FK constraint until T4.1 (reels table created then) |
| tick_seq | INTEGER NULL | |
| payload_json | TEXT | what was sent (no token) |
| ticket_id | TEXT NULL | Expo ticket |
| ticket_status | TEXT | `ok` \| `error` |
| receipt_status | TEXT NULL | `ok` \| `DeviceNotRegistered` \| `MessageRateExceeded` \| ... (checked ≥ 15 min later) |
| sent_at, receipt_checked_at | TEXT | |
| error | TEXT NULL | |

**`jobs`** — SQLite-backed queue

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| kind | TEXT | `ingest` \| `script` \| `assets` \| `tts` \| `captions` \| `render` \| `deliver` \| `receipts` \| `retention` \| `report` |
| key | TEXT | e.g. `render:2026-10-06`; UNIQUE(kind, key) prevents duplicates |
| payload_json | TEXT | |
| status | TEXT | `queued` \| `running` \| `done` \| `failed` \| `cancelled` |
| priority | INTEGER | lower runs first; `deliver` = 0 |
| attempts, max_attempts | INTEGER | default 3 |
| run_after | TEXT | backoff |
| locked_at, locked_by | TEXT NULL | worker id; stale lock reclaimed after `timeout_sec` |
| timeout_sec | INTEGER | per kind (render 900, script 300, tts 300, captions 300, ingest 120) |
| started_at, finished_at | TEXT NULL | |
| duration_ms | INTEGER NULL | |
| last_error | TEXT NULL | truncated traceback |
| created_at | TEXT | |

Claiming is atomic: `UPDATE jobs SET status='running', locked_at=?, locked_by=? WHERE id = (SELECT id
FROM jobs WHERE status='queued' AND run_after <= ? ORDER BY priority, id LIMIT 1) AND status='queued'`.

### 2.6 Settings

**`settings`** — small key/value for runtime toggles (`render_mode`, `pexels_enabled`, `llm_provider`)
with `updated_at` and `updated_by`. YAML/env remain the defaults; DB overrides win; `GET /v1/config`
shows the effective value and its origin.

## 3. Retention (nightly `retention` job at `day_boundary`)

| Data | Keep |
|---|---|
| Reels + assets, `saved = 0` | 24 h after `created_at` |
| Reels + assets, `saved = 1` | until owner deletes |
| telemetry_events, wake_evaluations | 30 days |
| days, day_transitions, briefs, brief_items | 90 days |
| items | 14 days (brief_items referencing them keep the row via FK until the brief is purged) |
| push_notifications | 30 days |
| jobs `done` | 7 days; `failed` 30 days |
| logs (`data/logs/*.jsonl`) | 14 days (rotated by size 50 MB too) |

## 4. File layout under `data/`

```
data/
├── chronos.db (+ -wal, -shm)
├── jobs/
│   └── execution.lock  (non-unlinked regular file; flock held by supervisor while a job runs)
├── media/<local_date>/<reel_public_id>/
│   ├── reel.json  voice.wav  voice.mp3  captions.json
│   ├── assets/scene-01.jpg  scene-02.mp4 ...
│   ├── reel.mp4  thumb.jpg
│   └── render.log
├── models/
│   ├── kokoro/kokoro-v1.0.onnx  voices-v1.0.bin
│   └── whisper/ggml-small.en.bin
├── certs/   (mkcert rootCA.pem, chronos-lan.pem, chronos-lan-key.pem — mode 600)
└── logs/    api.jsonl  worker.jsonl  reports/<local_date>.md
```
