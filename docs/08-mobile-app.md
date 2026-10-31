# Mobile App (`apps/mobile`) — Expo dev-client + Swift module

## 1. Project setup (T0.4)

- Expo SDK **pinned at task time (T0.4, 2026-10-06)**. All versions are recorded in
  `apps/mobile/package.json` and `README.md`:

  | Package | Pinned version | Note |
  |---|---|---|
  | `expo` | 57.0.28 | SDK pin (bumped T0.2b) |
  | `expo-router` | 57.0.26 | dist-tag `sdk-57` (bumped T0.2b) |
  | `expo-dev-client` | 57.0.20 | dist-tag `sdk-57` (bumped T0.2b) |
  | `expo-notifications` | 57.0.22 | dist-tag `sdk-57` |
  | `expo-secure-store` | 57.0.4 | dist-tag `sdk-57` |
  | `expo-video` | 57.0.5 | dist-tag `sdk-57` |
  | `react` | 19.2.3 | SDK manifest |
  | `react-native` | 0.86.3 | SDK manifest |
  | `typescript` | 6.0.3 | SDK `~6.0.3` |
  | `jest-expo` | 57.0.6 | dist-tag `sdk-57` (bumped T0.2b) |

- **pnpm 12.x + Metro:** `apps/mobile/pnpm-workspace.yaml` sets `nodeLinker: hoisted`
  (pnpm 12.x moved project settings out of `.npmrc`). This is required for Metro.

- `create-expo-app` with the TypeScript template, **Expo SDK pinned** (latest stable at task time;
  record the version in the task plan and `README.md`), `expo-router`, `expo-dev-client`.
- `app.config.ts` (not JSON) so iOS config is typed:
  ```ts
  ios: {
    bundleIdentifier: "dev.allenvarghese.chronos",
    infoPlist: {
      NSMotionUsageDescription: "Chronos reads step and walking history to notice when you're up.",
      UIBackgroundModes: ["remote-notification", "fetch"],
      BGTaskSchedulerPermittedIdentifiers: ["dev.allenvarghese.chronos.refresh"],
      NSAppTransportSecurity: { NSAllowsLocalNetworking: true }   // mkcert cert is trusted; LAN hostnames
    },
    entitlements: { "aps-environment": "development" }
  },
  plugins: ["expo-router", "expo-dev-client", "expo-notifications", "expo-secure-store", "expo-video", "./modules/chronos-sensors/app.plugin.js"]
  ```
- `expo prebuild --platform ios --clean` is reproducible from `app.config.ts`; the `ios/`
  directory is **gitignored** (regenerated), except nothing — the Swift module lives in
  `modules/chronos-sensors/` which Expo autolinks.
- **Simulator (T0.4b):** `just mobile-sim` (or `CHRONOS_SIM="iPhone 15" just mobile-sim`) — runs
  `expo run:ios --device "<name>"` against the named simulator (default "iPhone 16 Pro").
  Minimum Xcode: any version shipping Swift ≥ 6.3.1 (i.e. **Xcode 17 or newer**). Verified
  with Xcode 27.1 (Swift 6.4) / iPhone 16 Pro iOS 18.6 simulator. The app displays
  "Hello from ChronosSensors!" proving the Swift native module is autolinked and working.
  Physical device install is deferred to T2.4 (owner's iPhone port is broken).
- **Device (deferred to T2.4):** `just mobile-build` → `expo run:ios --device` (Xcode 17+, a free
  Personal Team supports limited local testing with seven-day provisioning, but does not supply
  the APNs credentials required by the full Chronos push route. Paid membership is required for
  that capability even without App Store publication; the owner currently has none). EAS Build (`eas build --profile development
  --platform ios`) is a manual GitHub workflow with `EXPO_TOKEN`.
- Push: `expo-notifications` with `getExpoPushTokenAsync({ projectId })` → `PATCH /v1/device/me`.
  APNs key is uploaded to Expo once (`eas credentials`) — required even for local builds because
  Expo Push Service sends through APNs.

## 2. Swift Expo Module `chronos-sensors`

**File layout (T2.1):**

```
modules/chronos-sensors/
  ios/
    ChronosSensors.podspec          # includes ChronosSensorsCore/Sources/**
    ChronosSensorsModule.swift      # Expo wrapper: all functions + Events
    ChronosSensorsAppDelegateSubscriber.swift  # BGTaskScheduler registration at launch
    ChronosSensorsCore/             # SwiftPM package (pure Swift, no Expo import)
      Package.swift
      Sources/ChronosSensorsCore/
        Constants.swift             # BG_REFRESH_IDENTIFIER
        Protocols.swift             # StepQuerying, ActivityQuerying, Sendable result types
        ActivitySample.swift        # Value type mirroring CMMotionActivity
        ActivitySummariser.swift    # Pure duration summation + confidence extraction
        DeviceStateMapper.swift     # UIKit enum->string mapping (pure)
      Tests/ChronosSensorsCoreTests/
        ActivitySummariserTests.swift
        DeviceStateMapperTests.swift
  src/index.ts                      # Full typed TS API + addBackgroundRefreshListener
  expo-module.config.json           # modules + appDelegateSubscribers
```

> **T2.1 acceptance note:** The manual "50 real steps" check is deferred to T2.4 (physical device).
> The Simulator acceptance criterion shows `deviceState()` JSON and the `pedometer_unavailable` path.

`modules/chronos-sensors/ios/ChronosSensorsModule.swift` (Expo Modules API):

```swift
public class ChronosSensorsModule: Module {
  public func definition() -> ModuleDefinition {
    Name("ChronosSensors")

    AsyncFunction("pedometerAuthorization") { () -> String in /* CMPedometer.authorizationStatus() → "authorized" | ... */ }
    AsyncFunction("requestMotionPermission") { (promise: Promise) in /* trigger a short query to prompt */ }

    // Steps between two dates (ms since epoch). Rejects with "pedometer_unavailable" if !isStepCountingAvailable.
    AsyncFunction("querySteps") { (fromMs: Double, toMs: Double, promise: Promise) in
      pedometer.queryPedometerData(from: Date(ms: fromMs), to: Date(ms: toMs)) { data, err in ... promise.resolve(["steps": steps, "distanceM": dist]) }
    }

    // Motion activity summary between two dates.
    AsyncFunction("queryActivity") { (fromMs: Double, toMs: Double, promise: Promise) in
      activityManager.queryActivityStarting(from:to:to: .main) { acts, err in
        // sum durations per class using consecutive activity timestamps; track max confidence for walking/running
        promise.resolve(["walkingSec": w, "runningSec": r, "stationarySec": s, "automotiveSec": a, "unknownSec": u, "walkingConf": "high"])
      }
    }

    Function("deviceState") { () -> [String: Any] in
      UIDevice.current.isBatteryMonitoringEnabled = true
      return [
        "isUnlocked": UIApplication.shared.isProtectedDataAvailable,
        "batteryState": map(UIDevice.current.batteryState),   // unplugged|charging|full|unknown
        "batteryLevel": UIDevice.current.batteryLevel < 0 ? nil : UIDevice.current.batteryLevel,
        "appState": map(UIApplication.shared.applicationState),
        "lowPowerMode": ProcessInfo.processInfo.isLowPowerModeEnabled,
        "iosVersion": UIDevice.current.systemVersion
      ]
    }

    // Background refresh registration (BGTaskScheduler). JS passes the identifier once at app start.
    Function("registerBackgroundRefresh") { (identifier: String) in ... BGTaskScheduler.shared.register(...) { task in self.sendEvent("backgroundRefresh", ["id": identifier]); self.pendingTask = task } }
    Function("scheduleBackgroundRefresh") { (identifier: String, earliestInSec: Double) in ... submit BGAppRefreshTaskRequest }
    Function("completeBackgroundRefresh") { (success: Bool) in pendingTask?.setTaskCompleted(success: success) }

    Events("backgroundRefresh")
  }
}
```

Notes for the implementer:
- `deviceState` must be called on the main thread (`UIApplication` APIs) — use `@MainActor`/`DispatchQueue.main.sync` guard.
- `queryActivityStarting` returns activities as change points; duration of activity *i* =
  `acts[i+1].startDate − acts[i].startDate` (last one until `to`). Clamp to the query range.
- Silent push handling is done by `expo-notifications`'s background handler
  (`Notifications.registerTaskAsync` with `expo-task-manager`) — no Swift needed, but the module
  exports the state getters that the task calls.
- Unit tests for the Swift module use XCTest with injected fake `CMPedometer`/`CMMotionActivityManager`
  protocols (`StepQuerying`, `ActivityQuerying`) so the duration summation is tested without
  hardware. Run via `just mobile-ios-test` on macOS only (not in required CI).

## 3. Wake handling in JS (`src/wake/`, T2.4 staged)

`index.ts` loads `src/wake/bootstrap.ts` before Expo Router. TaskManager defines the silent
notification task at module scope. Native notification registration/import is gated to physical
iOS and an existing `EXPO_PROJECT_ID`; registration itself never requests permission. SDK57's
native-normalized envelope is validated with bounded JSON parsing, visible/action notifications
are rejected, and only tick context/sequence is accepted. Payload URLs and credentials are ignored.

A shared runtime restores the existing Keychain record locally without waiting for a validation
GET, then uses the same singleton telemetry controller/outbox as manual controls. The total
monotonic deadline is 18 seconds, including restore; sensors retain 5 seconds and flush retains 8 seconds.
Each attempt returns sent/queued/skipped/failed independently. Acknowledged work maps to the SDK's
NewData; skipped maps to NoData; queued/failed maps to Failed without losing the durable snapshot.
Foreground validation remains separate. Initial paired active launch and real return-to-active
transitions sample once; React remounts do not register more listeners/controllers.

Native refresh registration still happens at launch. A launch-owned main-actor coordinator retains
pending execution until JS installs its listener and explicitly claims it. UUID-scoped completion,
iOS expiration and a 25-second safety timer complete only the corresponding task, at most once.
Expiration also aborts the matching JS attempt; late native results cannot enqueue/ACK. The
prebuilt Expo AppDelegate starts React Native in didFinishLaunching, including background launch;
actual cold-start BG execution remains a physical-device acceptance item. Refresh requests are
opportunistic, no earlier than 15 minutes, and submission status never promises execution.

Silent server ticks explicitly use Expo `normal` priority (APNs 5); future visible messages retain
the generic high default. Five-minute server cadence is unchanged. iOS can throttle/suppress these
updates; membership does not guarantee the cadence.

## 4. Screens (expo-router)

| Route | Purpose |
|---|---|
| `/pairing` | Enter 8-char code or scan QR from the laptop; stores token in `expo-secure-store`; confirms the API host and links to out-of-band certificate trust instructions (no pinning claim); explains the force-quit caveat |
| `/` (feed) | Vertical pager (`react-native-pager-view`, vertical) of today's reel + saved reels; each page = `expo-video` player with autoplay/mute toggle, caption overlay toggle (uses `captions.json`), source list sheet, save/delete; empty state shows the day's state ("Monitoring · 13 ticks · last score 42") |
| `/reel/[id]` | Deep-link target from the push (`chronos://reel/<id>`) → same player |
| `/settings` | Window/thresholds (reads/writes `/v1/config/wake` with a form generated from the schema), theme, render mode, Pexels toggle, "send test push", re-pair, revoke |
| `/debug` | Live signals (calls `sampleAndSend("manual")` and shows the server's breakdown), last 24 h score timeline, missed ticks, permissions status, outbox size, app/server versions, "mark actual wake time" |

State: a small Zustand store with one versioned SecureStore session envelope (token, device ID,
canonical HTTPS API root). No general-purpose persist middleware stores credentials.
Keychain uses `AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY` per D34. The generated `openapi-typescript`
paths and `openapi-fetch` client join the origin to full `/v1/...` paths. SDK57 `expo/fetch`
receives explicit `redirect: 'error'` options; RN's Request polyfill is not relied upon for that
security setting. All remote errors are sanitized, body size is bounded, and requests time out.
Startup/foreground GET verifies saved sessions; only confirmed 401/403 clears them, and generation
checks reject stale requests after logout/re-pair. Temporary connectivity errors preserve pairing.

## 5. Permissions flow

1. First launch → pairing → explain the three permissions on one screen.
2. Request notifications (`expo-notifications`); request motion by making a 1-minute
   `querySteps` call (iOS prompts on first CoreMotion access).
3. Settings deep link if denied; the debug screen shows `pedometer_auth`; the server reweights.

## 6. Testing

- `jest-expo` unit tests: `buildTelemetry`, outbox queue (fake SQLite), `withTimeout`, caption
  overlay line-breaking, pager state.
- Swift XCTest for the module's duration summation and state mapping (macOS-only job).
- Manual device checklist in `12-testing-and-quality.md` §5 (background behaviour cannot be
  automated).


## T2.2 pairing setup and limits

1. Run `just certs` and transfer only public `data/certs/rootCA.pem` via a trusted channel.
   Verify it separately with `openssl x509 -in data/certs/rootCA.pem -noout -subject
   -fingerprint -sha256`. The cert script prints the leaf fingerprint; the QR contains neither
   fingerprint nor CA. Never transfer private keys or serve the entire cert directory.
2. On iPhone install the public-root profile in Settings → General → VPN & Device Management;
   enable full trust in General → About → Certificate Trust Settings. Native iOS TLS validates
   the server; the JS app cannot inspect the peer certificate and does not implement pinning.
3. Run `chronos pair-code` on the laptop. Enter its exact HTTPS `/v1` URL and code, or explicitly
   request the camera and scan that JSON QR. A scan only fills the host-confirmation form.
   Simulator or camera denial leaves manual entry usable; microphone is never requested.
4. After explicit host confirmation, pairing is stored in Keychain before navigating home.
   If storage fails after server acceptance, revoke on the laptop before retrying. A lost
   response can similarly leave an active server device; the app never auto-revokes another one.
5. Notifications are a separate optional action. Set `EXPO_PROJECT_ID` to an existing Expo/EAS
   project UUID before native prebuild/build. This supplies `extra.eas.projectId`; no Expo
   resources or APNs credentials are created by the app. Missing ID, denied permission, Simulator
   or network failure does not discard pairing. Real token issuance/delivery is unverified until
   the T2.4 signed physical-device run (owner's wired port is broken).
6. “Revoke on laptop and forget” requires backend confirmation; “Forget only this phone” removes
   local credentials only and explains that the laptop still needs revocation before re-pairing.
   Force-quitting iOS can prevent later silent wakes; T2.2 does not claim wake handling.

Native dependency additions require a rebuilt dev-client, not Expo Go or JS export alone.
Physical camera scanning, APNs credentials/push delivery and locked-phone Keychain cold start
remain on the T2.4 checklist. The home screen preserves T2.1’s device-state and step-query
display inside an authenticated boundary; sensor reads mount only after secure session hydration.

### T2.3 sampler and durable outbox

Authenticated Home provides manual Sample and send / Retry queued telemetry controls.
T2.4 now connects automatic wake handlers to the same controller; T2.5 still owns the full debug screen.
Sampling captures one phone timestamp/timezone, reads actual authorization without prompting,
and spends at most five seconds on native history. Unavailable motion gives zero/low values
with local diagnostics; an absent native bridge is an explicit unavailable result. Walking
includes running. Supplied authoritative window context must match the captured timezone/date;
without it window steps are conservatively zero, never based on guessed wake rules.

A private expo-sqlite database persists immutable body/seq/time before sending. Sequence and
row insertion share one short transaction; counters and rows are partitioned by canonical API
origin plus device ID, with no bearer token in SQLite. FIFO flushes use an eight-second
monotonic budget and abort fetch/stream reads. Retry metadata is written before I/O; network
failure/lost responses retain the original row. Bounded Retry-After/backoff never sleeps during
a wake. Confirmed401/403 use central pairing invalidation. Terminal invalid/skew events are
explicitly dropped; the original timestamp is never refreshed. Local retention drops strictly
older than two hours and future-created rows after a backward clock change. Server freshness
is separately300seconds and checked before deduplication, including previously accepted rows.

Current manual acceptance uses an isolated HTTPS API outage instead of physical airplane mode:
pair, stop only test API, sample twice, inspect the native SQLite queue, restart the app process,
reopen the project, restore test API and retry within300seconds. Compare original sequence,
timestamp and body hashes with server records and confirm FIFO/empty queue. Physical sensor,
airplane-mode and background-push validation remains on the T2.4 device checklist.

### T2.4 permission flow and staged acceptance

Authenticated Home shows Motion authorization, Background App Refresh setting, Low Power Mode,
notification alert authorization, push-registration status and refresh-submission status separately.
Only explicit foreground actions request Motion or notification access. Rendering legacy sensor
history first checks authorization, so Home never implicitly triggers a Motion prompt. Returning
from Settings reads actual statuses again. Denied/restricted/not-determined authorization is still
posted by the existing sampler, never replaced with a fabricated grant. Alert permission is distinct
from silent-background execution; the screen does not claim denial prevents every background wake.

The persistent caveat explains force-quit, first unlock after reboot, Low Power Mode, disabled
refresh and best-effort scheduling. Notification native modules are not loaded in this Simulator
setup, avoiding unentitled Keychain auto-registration side effects without suppressing errors.

No paid Apple Developer membership/physical installation route is currently available. Simulator
builds and code tests do not require membership. APNs credentials for the chosen remote-push route
require paid membership even for a private app; App Store publication is not required. No EAS
project, account, profile or credential is automatically created. T2.4's physical hello(), counted
walking/permission checks, genuine BG cold start, locked Keychain behavior and at least three
locked-phone tick→event observations within 60 seconds remain **pending**. See the device checklist
in `docs/plans/T2.4-plan.md`; do not mark full T2.4/M2 acceptance complete from Simulator evidence.

### T2.5 wake diagnostics

Authenticated Home links to **Open wake diagnostics** (`/debug`). Send now uses the same bounded
manual wake runtime, sampler and SQLite queue as the other wake sources. Merely viewing the
screen never queries Motion history or creates a second background/foreground listener.

The screen distinguishes the latest local snapshot, the exact last locally acknowledged
sample/result pair and the latest persisted server pair. Each retains sequence, timestamp and
source. Server details survive process restart; an offline refresh leaves dated values visibly
stale. Unknown battery values and denied/unavailable Motion remain explicit. The unlock field
is labeled as the protected-data proxy rather than a precise screen-unlock event.

Stored breakdown/thresholds/derived facts/penalties are displayed without recomputing scores.
The timeline covers24elapsed hours with original decisions and a fixed0–100scale; no sample
is not a zero score. History is bounded and explicitly paginated; empty/partial states are
labeled. Tick status means correlation, not proof of Apple delivery or reason for a missing wake.

Mark actual wake time is an explicit exact `HH:MM` annotation for the displayed server day and
timezone. It saves only after pressing the button and confirms success by server readback,
including recovery from a lost PUT response. A failed confirmation preserves the draft.
App/iOS/server/active-rule/evaluation-rule versions are labeled separately. Existing permission
controls and force-quit/iOS caveats remain available. Focus/foreground/explicit actions refresh
diagnostics; blur/background/unmount/session-generation changes cancel or discard stale work.

No new dependencies, database tables or credentials are introduced. T2.4's physical-device
acceptance checklist remains pending and cannot be closed by this screen.
