# API Contracts

- Base URL: `https://<laptop-lan-host>:8443/v1` (mkcert certificate, D16). `/healthz`, `/readyz`,
  `/metrics` are at the root.
- Auth: `Authorization: Bearer <device_token>` on every `/v1` route except `POST /v1/pair`.
  Tokens are 32 random bytes, base64url; stored hashed. 401 on missing/invalid; 403 on revoked.
- Content type: JSON; Pydantic v2 models with `extra="forbid"`. OpenAPI served at `/openapi.json`
  (used to generate the mobile client types; `just gen-types`).
- Errors: RFC 9457 problem+json: `{ "type": "urn:chronos:error:<code>", "title", "status", "detail", "instance" }`.
  Codes: `validation`, `clock_skew`, `unauthorized`, `revoked`, `pairing_invalid`, `pairing_expired`,
  `rate_limited`, `not_found`, `conflict`, `rules_invalid`, `media_expired`, `internal`.
  RFC 9457 extension members may appear alongside the required fields.
  `clock_skew` 422 includes `"server_time": "<ISO-8601 UTC Z>"` so the client can
  correct its clock without parsing the `detail` string.
- Rate limits (in-memory token bucket, per IP): pairing 5/min; telemetry 30/min; everything else 120/min.
- Idempotency: telemetry `(device, seq)` unique → repeat returns the stored evaluation with 200.

## 1. Pairing and device

### `POST /v1/pair`
Exchange a one-time code (from `chronos pair-code` on the laptop, shown as text + QR) for a token.

```json
// request
{ "code": "7K2M-9QXD", "device": { "name": "Allen's iPhone", "model": "iPhone15,2", "ios_version": "18.3", "app_version": "0.1.0", "tz": "Asia/Kolkata" } }
// 201
{ "device_id": "01J9...", "token": "b64url...", "api_base": "https://chronos.local:8443/v1", "server_time": "2026-10-06T05:00:00Z" }
```
Errors: 400 `pairing_invalid`, 410 `pairing_expired`, 429 `rate_limited`, 409 `conflict` (an active device exists; revoke first with `chronos device revoke`).

### `GET /v1/device/me` → device record (no token). `PATCH /v1/device/me`
```json
{ "expo_push_token": "ExponentPushToken[xxx]", "tz": "Asia/Kolkata", "app_version": "0.1.1", "ios_version": "18.3" }
```
### `DELETE /v1/device/me` → revokes token (app returns to pairing).

## 2. Telemetry and day

### `POST /v1/telemetry`
Body = the signals table in `docs/03-wake-engine.md` §3.

```json
{
  "device_ts": "2026-10-06T07:12:04+05:30", "tz": "Asia/Kolkata", "wake_source": "silent_push", "seq": 412, "tick_seq": 15,
  "steps_10m": 48, "steps_since_window": 112, "walking_sec_10m": 95, "stationary_sec_10m": 410, "walking_conf": "high",
  "is_unlocked": true, "battery_state": "unplugged", "battery_level": 0.93, "app_state": "background",
  "low_power_mode": false, "pedometer_auth": "authorized", "app_version": "0.1.0", "ios_version": "18.3"
}
```
Response 200:
```json
{
  "evaluation_id": 9912, "local_date": "2026-10-06", "gate": "open", "score": 85, "decision": "confirm",
  "confirm_reason": "both",
  "threshold": { "confirm": 70, "strong": 85 }, "consecutive_high": 2, "rule_version": 3,
  "breakdown": [
    { "rule_id": "steps_10m", "input": 48, "points": 30, "cap": 30 },
    { "rule_id": "walking_10m", "input": 95, "points": 20, "cap": 20, "note": "conf=high" },
    { "rule_id": "unlocked", "input": true, "points": 25, "cap": 25, "note": "consecutive bonus +10" },
    { "rule_id": "charger_disconnect", "input": true, "points": 15, "cap": 15 },
    { "rule_id": "steps_window", "input": 112, "points": 5, "cap": 5 },
    { "rule_id": "foreground", "input": "silent_push", "points": 0, "cap": 40 }
  ],
  "penalties": [],
  "day_state": "confirmed",
  "next_tick_in_sec": 265
}
```
`next_tick_in_sec`: seconds until the next scheduled tick (integer >= 0); `null` when
the day is not in `monitoring` state or the scheduler is disabled.

Errors: 422 `validation`, 422 `clock_skew` (|device_ts − server| > 300 s; detail includes server time), 401/403.

### `GET /v1/day/today` and `GET /v1/day/{local_date}`
```json
{ "local_date": "2026-10-06", "kind": "weekday", "tz": "Asia/Kolkata", "window": { "start": "06:00", "end": "11:00" },
  "state": "confirmed", "confirmed_at": "...", "delivered_at": null, "fallback_used": false,
  "ticks_sent": 15, "ticks_received": 13, "reel": { "public_id": "01J9...", "status": "ready" },
  "score_timeline": [ { "at": "...", "score": 12 }, { "at": "...", "score": 72 }, { "at": "...", "score": 85 } ],
  "actual_wake_local": null }
```
### `PUT /v1/day/{local_date}/actual-wake` `{ "actual_wake_local": "07:05" }` → annotation for tuning.

## 3. Reels and media

### `GET /v1/reels?limit=20` → `[ReelSummary]` newest first (today + saved).
### `GET /v1/reels/{public_id}`
```json
{ "public_id": "01J9...", "local_date": "2026-10-06", "status": "ready", "duration_sec": 61.4, "saved": false,
  "expires_at": "2026-10-07T00:00:00Z",
  "media_url": "https://chronos.local:8443/v1/media/01J9.../reel.mp4?exp=1759740000&sig=...",
  "thumbnail_url": "https://.../thumb.jpg?exp=...&sig=...",
  "scenes": [ { "index": 0, "type": "cover", "title": "Monday, 6 Oct", "start_sec": 0, "end_sec": 4.2 },
              { "index": 1, "type": "story", "title": "Qwen3-VL open weights", "source": "Hugging Face", "url": "https://...", "start_sec": 4.2, "end_sec": 16.8 } ],
  "captions_url": "https://.../captions.json?exp=...&sig=..." }
```
### `POST /v1/reels/{public_id}/save` / `DELETE /v1/reels/{public_id}/save` → toggles `saved`.
### `DELETE /v1/reels/{public_id}` → deletes files and row (204).
### `GET /v1/media/{public_id}/{file}?exp=&sig=`
Signed with HMAC-SHA256(server secret, `public_id/file|exp`), 1 h expiry, supports `Range`, caches
`private, max-age=0`. 410 `media_expired` after expiry/retention. No bearer required (the player
cannot set headers) — the signature *is* the auth.

## 4. Config and admin

### `GET /v1/config/wake`

Returns the currently active wake-engine rule version.

```json
{
  "version": 3,
  "source": "api",
  "rules": { "...WakeRules..." },
  "sha256": "<hex64>",
  "yaml_sha256": "<hex64>",
  "yaml_stale": false,
  "created_at": "2026-10-19T06:00:00.000000Z",
  "created_by": "api:<device_public_id>"
}
```

- `yaml_sha256` — SHA-256 of `config/wake_rules.yaml` on disk.
- `yaml_stale` — `true` if the YAML file on disk differs from the latest yaml-sourced version in the DB.

### `PUT /v1/config/wake`

Body: `WakeRules` JSON document (full schema, not a patch).
Response `201` — new version created (`{ "version": n+1, "created": true }`).
Response `200` — SHA matches active version; no-op (`{ "version": n, "created": false }`).
Response `422 rules_invalid` (problem+json) with `"errors": [{"loc": [...], "msg": "...", "type": "..."}]` for each validation failure.
### `GET /v1/config` → effective settings with origin (`env` \| `yaml` \| `db`): `render_mode`, `llm_provider`, `pexels_enabled`, `tick_interval_min`, `retention`.
### `PATCH /v1/config` → subset of the above; stored in `settings`.
### `POST /v1/admin/run` `{ "local_date": "2026-10-06", "stages": ["ingest","script","assets","tts","captions","render"], "force": false }` → enqueues the chain; 202 with job ids. Used by the debug panel and `chronos run`.
### `POST /v1/admin/deliver` `{ "local_date": "2026-10-06" }` → force delivery (sets `fallback_used=false`, cause `manual`).
### `POST /v1/admin/push-test` → sends a visible test push.
### `GET /v1/jobs?status=&limit=` → job list for the debug panel.
### `GET /v1/report/{local_date}` → the nightly Markdown report as text.

## 5. Health

- `GET /healthz` → `{ "ok": true, "version": "0.3.1" }` (no auth; liveness).
- `GET /readyz` → checks DB, Ollama reachable, models present, disk ≥ 2 GB, cert expiry > 7 days; 503 with a list of failures otherwise.
- `GET /metrics` → Prometheus text (bound to loopback only by default).

## 6. Push payloads (Expo Push API)

Silent tick (sent by `chronos/scheduler.py` every `tick_interval_min` minutes while `day.state=monitoring`):
```json
{ "to": "ExponentPushToken[...]", "_contentAvailable": true, "priority": "high", "data": { "type": "tick", "local_date": "2026-10-06", "tick_seq": 15, "window_start": "2026-10-06T06:00:00+05:30" } }
```

Expo ticket errors handled by the scheduler:
- `DeviceNotRegistered` — clears `device.expo_push_token`; no further ticks until re-registered.
- `MessageRateExceeded` — skips to the next tick instant (one-interval backoff); recorded in `push_notifications`.
- `MessageTooBig`, `InvalidCredentials` — logged and recorded; does not stop the scheduler.

The `token` field is stripped from `payload_json` before DB storage.  Receipts are checked
~15 min after send via `POST /--/api/v2/push/getReceipts` and recorded in `push_notifications.receipt_status`.
Reel ready:
```json
{ "to": "...", "title": "Good morning ☀️", "body": "Your 60-second morning reel is ready.", "sound": "default", "priority": "high",
  "data": { "type": "reel_ready", "reel_public_id": "01J9...", "local_date": "2026-10-06", "fallback": false } }
```
Text fallback (render failed): `title: "Morning brief (text)"`, `body` = first two headlines, `data.type = "text_fallback"`.

## 7. Shared JSON schemas

`just gen-types` → `scripts/gen-types.sh` is the single entry point for schema distribution:

1. **`chronos openapi export`** — serialises the FastAPI OpenAPI 3.1 schema to
   `services/api/openapi.json` (sorted keys, indent=2, trailing newline, `info.version` from
   `VERSION`).  Stable `<tag>_<route-name>` operationIds via `generate_unique_id_function`.
   Native `HTTPBearer` security scheme via `HTTPBearer(auto_error=False)` in `require_device`.
   Shared `Problem` response component (RFC 9457) declared on authenticated routers via
   `responses=` for 401/403/422/429.

2. **`openapi-typescript 7.13.0`** — generates `apps/mobile/src/api/schema.d.ts` from the
   committed `openapi.json`; compatible with the `openapi-fetch` client (T2.2).

3. **Renderer zod (T3.4)** — when `ReelScript`/`ReelProps` Pydantic models exist the hook in
   `scripts/gen-types.sh` will export `packages/renderer/src/schema.json`; until then the
   hand-written `packages/renderer/src/schema.ts` is not touched.

Both `services/api/openapi.json` and `apps/mobile/src/api/schema.d.ts` are committed to git.

**CI staleness checks:**
- `api` job: `chronos openapi export --out /tmp/openapi.json && diff -u services/api/openapi.json /tmp/openapi.json`
- `mobile` job: `openapi-typescript … --output /tmp/schema.d.ts && diff -u src/api/schema.d.ts /tmp/schema.d.ts`

A PR that modifies a router without running `just gen-types` will fail CI.
