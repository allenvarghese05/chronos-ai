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
- **Device (deferred to T2.4):** `just mobile-build` → `expo run:ios --device` (Xcode 17+, free
  personal team is enough for a 7-day cert; a paid developer account gives a 1-year profile and
  is recommended — see `13-open-questions.md`). EAS Build (`eas build --profile development
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

## 3. Wake handling in JS (`src/wake/`)

```ts
// registerWake.ts — called once from the root layout
export async function registerWake() {
  await Notifications.registerTaskAsync(SILENT_PUSH_TASK);        // expo-task-manager task
  ChronosSensors.registerBackgroundRefresh(BG_REFRESH_ID);
  ChronosSensors.addListener("backgroundRefresh", () => void sampleAndSend("bg_refresh").finally(() => ChronosSensors.completeBackgroundRefresh(true)));
  AppState.addEventListener("change", (s) => { if (s === "active") void sampleAndSend("foreground"); });
}

TaskManager.defineTask(SILENT_PUSH_TASK, async ({ data }) => {
  const payload = (data as any)?.notification?.request?.content?.data;
  if (payload?.type !== "tick") return;
  await sampleAndSend("silent_push", { tickSeq: payload.tick_seq, windowStart: payload.window_start });
  ChronosSensors.scheduleBackgroundRefresh(BG_REFRESH_ID, 15 * 60);
});

// sampleAndSend.ts — must finish in < 20 s; everything is time-boxed
export async function sampleAndSend(source: WakeSource, ctx: TickCtx = {}) {
  const now = Date.now();
  const [steps10, stepsWin, act, state] = await Promise.all([
    withTimeout(ChronosSensors.querySteps(now - 10 * 60_000, now), 5_000, { steps: 0 }),
    withTimeout(ChronosSensors.querySteps(windowStartMs(ctx), now), 5_000, { steps: 0 }),
    withTimeout(ChronosSensors.queryActivity(now - 10 * 60_000, now), 5_000, EMPTY_ACTIVITY),
    Promise.resolve(ChronosSensors.deviceState()),
  ]);
  const event = buildTelemetry({ source, ctx, steps10, stepsWin, act, state, seq: await nextSeq() });
  await telemetryQueue.enqueue(event);
  await telemetryQueue.flush({ budgetMs: 8_000 });    // POST with bearer; leaves the rest queued
}
```

Telemetry queue: `expo-sqlite` table `outbox(seq PK, json, created_at, attempts)`; flush sends
oldest first, stops on 401 (go to pairing) or when the budget is spent; events older than 2 h are
dropped (they would be outside the window anyway). The server's `(device, seq)` uniqueness makes
retries safe.

## 4. Screens (expo-router)

| Route | Purpose |
|---|---|
| `/pairing` | Enter 8-char code or scan QR from the laptop; stores token in `expo-secure-store`; shows the API host and cert fingerprint; explains the force-quit caveat |
| `/` (feed) | Vertical pager (`react-native-pager-view`, vertical) of today's reel + saved reels; each page = `expo-video` player with autoplay/mute toggle, caption overlay toggle (uses `captions.json`), source list sheet, save/delete; empty state shows the day's state ("Monitoring · 13 ticks · last score 42") |
| `/reel/[id]` | Deep-link target from the push (`chronos://reel/<id>`) → same player |
| `/settings` | Window/thresholds (reads/writes `/v1/config/wake` with a form generated from the schema), theme, render mode, Pexels toggle, "send test push", re-pair, revoke |
| `/debug` | Live signals (calls `sampleAndSend("manual")` and shows the server's breakdown), last 24 h score timeline, missed ticks, permissions status, outbox size, app/server versions, "mark actual wake time" |

State: `zustand` with `persist` (secure storage for the token only). API client generated with
`openapi-typescript` + `openapi-fetch`, base URL from the pairing response.

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
