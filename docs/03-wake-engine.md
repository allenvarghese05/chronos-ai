# Wake Engine — iOS signals, the score, rules, state machine, tuning

## 1. What the "score" is (plain English)

Every time the app gets a chance to run in the morning window, it takes a snapshot of a handful of
facts about you and the phone and sends them to the laptop. The laptop converts each fact into
**evidence points**: more points the more the fact looks like "a person who got out of bed", fewer
or zero when it looks like "a phone lying on the nightstand". The points add up to a **score from
0 to 100**. If the score is high enough (the **threshold**) on two snapshots in a row, or very high
on a single snapshot, the laptop decides you are awake and sends the reel.

It is a weighted checklist, not machine learning. Each line of the checklist is a *rule* with a
*weight* (how many points it is worth) and a *condition*. You can change weights, conditions and
the threshold in a YAML file or from the API; the laptop stores every snapshot with the exact
points each rule gave, so you can replay last week's mornings against new weights and see what
would have happened.

Example of one snapshot's breakdown:

```
07:12 local, weekday window open ........... gate: OPEN
steps in last 10 min = 48 .................. +30  (rule steps_10m: ≥35 → 30)
walking seconds in last 10 min = 95, conf hi +20  (rule walking_10m: ≥60 s → 20)
device unlocked now = yes .................. +15  (rule unlocked)
charger disconnected since last tick = yes . +15  (rule charger_disconnect)
app foregrounded by user = no ..............  +0
steps since window start = 112 .............  +5  (rule steps_window: ≥100 → 5)
                                             ----
score 85 ≥ threshold 70 → "high"; previous tick was 72 → CONFIRMED
```
Note: score 85 ≥ strong 85 (one-tick rule) AND consecutive_high 2 (two-tick rule) both fire;
``confirm_reason = "both"``.
```
```

## 2. Why these signals (and not the ones in the original brief)

| Original idea | iOS reality | Replacement |
|---|---|---|
| Wi-Fi RSSI spike near hallway extender | iOS exposes no RSSI; BSSID requires precise location + entitlement and is not proximity | **Pedometer + motion-activity history**: walking 10–20 m to the bathroom/kitchen is the same physical event, read directly from the motion coprocessor |
| Screen unlocked > 2 min | No unlock event or duration API | **`UIApplication.isProtectedDataAvailable`** sampled at each tick = "phone is unlocked right now"; two consecutive unlocked ticks ≈ "unlocked for a while" |
| (none) | — | **Charger disconnect**: most people unplug when they get up; `UIDevice.batteryState` is readable in background. Transition is computed server-side from the previous tick |
| App polls sensors continuously | iOS suspends the app | **Server-driven silent push ticks** + BGAppRefresh; history queries make continuous running unnecessary |

## 3. Signals (the telemetry payload)

Sampled by the Swift module `chronos-sensors` whenever the app is woken. All fields are required
unless marked optional; the server rejects unknown fields.

| Field | Type | Source | Meaning |
|---|---|---|---|
| `device_ts` | ISO-8601 with offset | `Date()` | When the snapshot was taken |
| `tz` | IANA string | `TimeZone.current.identifier` | Used to compute the local day and window |
| `wake_source` | enum `silent_push` \| `bg_refresh` \| `foreground` \| `manual` | app | How the app got to run |
| `steps_10m` | int ≥ 0 | `CMPedometer.queryPedometerData(from: now-10m, to: now)` | Steps in the last 10 minutes |
| `steps_since_window` | int ≥ 0 | same, from window start (server sends `window_start` in the tick payload; app falls back to local computation) | Cumulative morning steps |
| `walking_sec_10m` | int ≥ 0 | `CMMotionActivityManager.queryActivityStarting(from: now-10m)` summed `walking`/`running` durations | Seconds spent walking |
| `walking_conf` | enum `low` \| `medium` \| `high` | max confidence of walking segments | CoreMotion's confidence |
| `stationary_sec_10m` | int ≥ 0 | same query | Seconds stationary (sanity) |
| `is_unlocked` | bool | `UIApplication.shared.isProtectedDataAvailable` | Phone currently unlocked |
| `battery_state` | enum `unplugged` \| `charging` \| `full` \| `unknown` | `UIDevice.current.batteryState` | Charger state |
| `battery_level` | float 0–1 or null | `UIDevice.current.batteryLevel` | Sanity / debugging only |
| `app_state` | enum `active` \| `background` \| `inactive` | `UIApplication.applicationState` | Was the user looking at the app |
| `low_power_mode` | bool | `ProcessInfo.isLowPowerModeEnabled` | Explains missing ticks |
| `pedometer_auth` | enum `authorized` \| `denied` \| `restricted` \| `not_determined` | `CMPedometer.authorizationStatus()` | Explains zeros |
| `seq` | int | app counter | Dedup / ordering |
| `app_version`, `ios_version` | string | — | Debugging |

**Server-derived facts** (not sent by the phone): `charger_disconnected` = previous tick's
`battery_state ∈ {charging, full}` and this tick's is `unplugged`, within the window;
`consecutive_unlocked_ticks`; `minutes_since_window_start`; `ticks_received_today`.

## 4. Rules (`services/api/config/wake_rules.yaml`, version 1)

```yaml
version: 1
window:
  weekday: { start: "06:00", end: "11:00" }
  weekend: { start: "08:00", end: "14:00" }
  day_boundary: "03:00"          # local time at which a new "day" begins
  holidays_as_weekend: []        # ISO dates, optional
  tick_interval_min: 5
thresholds:
  confirm: 70                    # score ≥ confirm on 2 consecutive ticks (≤ 12 min apart) → confirmed
  strong: 85                     # score ≥ strong on 1 tick → confirmed
  dedupe_min: 2                  # ignore a second snapshot < 2 min after the last (same wake)
  confirm_max_gap_min: 12        # two consecutive high ticks must be ≤ 12 min apart (allows one missed tick at 5-min interval)
rules:
  - id: steps_10m
    signal: steps_10m
    points: [[35, 30], [20, 18], [10, 8]]     # first threshold met wins: ≥35 → 30, ≥20 → 18, ≥10 → 8
    cap: 30
  - id: walking_10m
    signal: walking_sec_10m
    points: [[60, 20], [30, 12]]
    requires: { walking_conf: [medium, high] }
    cap: 20
  - id: unlocked
    signal: is_unlocked
    points: [[true, 15]]
    bonus_consecutive: { ticks: 2, points: 10 }   # unlocked on this and previous tick → +10
    cap: 25
  - id: charger_disconnect
    signal: charger_disconnected         # server-derived
    points: [[true, 15]]
    cap: 15
  - id: steps_window
    signal: steps_since_window
    points: [[100, 5]]
    cap: 5
  - id: foreground
    signal: wake_source
    points: [["foreground", 40]]         # the owner opened the app on purpose
    cap: 40
penalties:
  - id: stationary_dominant
    when: { stationary_sec_10m: ">= 540", steps_10m: "< 10" }
    points: -20                          # phone awake but person not moving (in bed)
  - id: pedometer_denied
    when: { pedometer_auth: ["denied", "restricted"] }
    points: 0                            # no penalty; instead unlocked+charger weights are scaled (see below)
reweight_without_pedometer:              # if pedometer is unavailable, redistribute to keep max 100
  unlocked: 35       # base row = 35 − bonus(10) = 25 pts; with consecutive bonus = 35 pts; cap = 35
  charger_disconnect: 25               # bool rule: pts = 25, cap = 25 (35+25+40=100)
  foreground: 40     # enum rule: pts = 40, cap = 40
```

`reweight_without_pedometer` semantics (applies when `pedometer_auth ∈ {denied, restricted, not_determined}`):

- Each value sets **both the cap and the effective points** for that rule.
- **Bool / enum rules** (no `bonus_consecutive`): the single row's points become the reweight
  value (e.g. `charger_disconnect`: original pts 15 → reweighted pts 25).
- **Rules with `bonus_consecutive`**: `base_pts = reweight_value − bonus.points`; the rule
  awards `base_pts` without the bonus and `reweight_value` when the bonus fires
  (e.g. `unlocked`: base 25, with bonus 35; requires `reweight_value > bonus.points`).
- **Numeric rules**: rows scaled proportionally so the top row equals the reweight value
  (none in the default YAML today; documented for future use).
- The breakdown note for a reweighted rule states the effective pts.
- Validator ensures `sum(reweight_values) ≤ 100` and `reweight_value > bonus.points`
  for any rule that has `bonus_consecutive`.

With these values: `unlocked (35) + charger_disconnect (25) + foreground (40) = 100`.
First in-window tick with all three = 25+25+40 = 90 ≥ strong 85 → **confirm on first tick**
(even without foreground the max is 35+25 = 60 < 70, so the day never auto-confirms).

Other semantics:

- The **window gate** is evaluated first. Outside the window the result is `decision=outside_window`,
  score 0, nothing else runs, nothing is confirmed.
- `points` lists are ordered thresholds; the first satisfied row awards its points; rows are
  `[threshold, points]`, compared with `>=` for numbers and `==` for booleans/enums.
- Scores are clamped to `[0, 100]` after penalties.
- The maximum achievable without foreground is 30+20+25+15+5 = 95; with foreground 100 (caps).
- `confirm` needs two consecutive ticks ≥ 70 with ≤ 12 min between them (allows one missed tick).
- The reviewer must check that the YAML is schema-validated (`WakeRules` pydantic model) on
  startup and on `PUT /v1/config/wake`; an invalid rule set is rejected, never partially applied.

## 5. Day state machine

```
                 window opens                 score high ×2 (or strong ×1)
 ┌────────┐ ───────────────▶ ┌────────────┐ ──────────────────────────▶ ┌───────────┐
 │ idle   │                  │ monitoring │                             │ confirmed │
 └────────┘ ◀─────────────── └────────────┘ ──────────┐                 └─────┬─────┘
    ▲        day boundary 03:00       │                │ window end           │ deliver job ok
    │                                 │                ▼                      ▼
    │                                 │          ┌───────────┐          ┌───────────┐
    │                                 └─────────▶│ fallback  │─────────▶│ delivered │
    │                                   (ready reel, no confirm)        └─────┬─────┘
    └─────────────────────────────────────────────────────────────────────────┘
```

| State | Entered when | Behaviour |
|---|---|---|
| `idle` | day boundary (03:00 local) | No ticks. Prewarm job scheduled at window start − 30 min |
| `monitoring` | window start | Scheduler sends a silent push every `tick_interval_min`; evaluations stored; state persists across missed ticks |
| `confirmed` | evaluation decision `confirm` | Ticks stop; `deliver` job enqueued (reel ready → visible push; else wait for reel then push) |
| `fallback` | window end reached while `monitoring` | `deliver` job enqueued with `fallback=true` |
| `delivered` | push accepted by Expo and receipt not `error` | Further telemetry is accepted and stored (useful for tuning) but has no effect |

Rows in `days` are keyed by `local_date`; transitions are recorded in `day_transitions` with the
evaluation id that caused them.

## 6. Sampling on the phone

| Wake source | When | What the app does (≤ 25 s budget) |
|---|---|---|
| `silent_push` | Server tick (every 5 min in `monitoring`) | Parse payload `{type:"tick", local_date, window_start}`; sample; POST; call completion handler |
| `bg_refresh` | iOS-chosen (`BGAppRefreshTask`, earliest = window start, re-scheduled after each run) | Same as above; the app re-registers the task for `+15 min` |
| `foreground` | User opens app or returns to it | Sample immediately with `wake_source=foreground`; also used for the debug panel |
| `manual` | Debug panel "send now" button | Same payload, flagged so it can be excluded from tuning |

Permissions required: Motion & Fitness (`NSMotionUsageDescription`), notifications (for the
visible push; silent pushes work without alert permission but the app must be installed and not
force-quit). Background modes: `remote-notification`, `fetch`. The debug panel shows the last
snapshot, the server's score breakdown and the list of missed ticks (server sends `tick_seq`;
app records which it saw).

Known iOS limits the app must handle: if the user **force-quits** the app, silent pushes are not
delivered until the app is launched again (the app shows a banner explaining this after pairing,
and the server's nightly report flags days with zero `silent_push` events). Low Power Mode
suppresses background refresh and delays silent pushes.

## 7. Tuning and replay

- `chronos wake replay --from 2026-10-01 --to 2026-10-14 --rules config/wake_rules.yaml`
  re-scores every stored snapshot under the given rules and prints, per day: first confirm time
  under old vs new rules, ticks ≥ threshold, false confirms (confirmed before the owner's marked
  true wake time, if annotated).
- `chronos wake annotate 2026-10-06 --actual 07:05` records ground truth in `days.actual_wake_local`
  (the owner can also set it from the app's debug panel the same morning).
- `PUT /v1/config/wake` stores a new `rule_versions` row; `GET /v1/config/wake` returns the
  effective rules and version; the nightly report shows the score timeline of the day as a sparkline.
- Target metrics (G1): median confirm latency ≤ 10 min after `actual_wake_local`; zero confirms
  more than 30 min before it.

## 8. Edge cases the implementation must cover (with tests)

1. Tick arrives at 05:59:50 with `device_ts` 06:00:10 → window computed from `device_ts` in `tz`;
   gate open.
2. Phone timezone changes mid-day (travel) → the `days` row keeps the tz it was created with;
   the new tz applies from the next day boundary.
3. Two snapshots 20 s apart (silent push + foreground) → the second is deduped for the
   consecutive-tick rule but still stored.
4. Pedometer permission denied → `reweight_without_pedometer` applies; the debug panel shows a
   permission prompt.
5. Weekend/holiday → weekend window; `holidays_as_weekend` list honoured.
6. Fallback at window end while `deliver` of a *confirmed* day is still running → no double push
   (`deliver` job unique per `local_date`).
7. Device clock skew > 5 min → 422; app displays the error and retries after NTP sync.
8. Daylight-saving transition day → window computed with `zoneinfo`; test both spring-forward and
   fall-back days.
9. App force-quit all morning → zero `silent_push` events; fallback delivers; report flags it.
10. Two devices paired (future) → each device has its own tick stream; the day confirms on the
    first device to cross; v1 allows exactly one active device.
