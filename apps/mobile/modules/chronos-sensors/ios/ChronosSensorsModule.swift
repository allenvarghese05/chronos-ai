import ExpoModulesCore
import CoreMotion
import UIKit
import BackgroundTasks

// MARK: - CoreMotion Adapters

/// Wraps CMPedometer to satisfy StepQuerying.
final class PedometerAdapter: StepQuerying, @unchecked Sendable {
    private let pedometer = CMPedometer()

    func querySteps(from: Date, to: Date) async throws -> StepQueryResult {
        try await withCheckedThrowingContinuation { continuation in
            pedometer.queryPedometerData(from: from, to: to) { data, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if let data {
                    let steps = data.numberOfSteps.intValue
                    let dist  = data.distance?.doubleValue ?? 0
                    continuation.resume(returning: StepQueryResult(steps: steps, distanceM: dist))
                } else {
                    continuation.resume(returning: StepQueryResult(steps: 0, distanceM: 0))
                }
            }
        }
    }
}

/// Wraps CMMotionActivityManager to satisfy ActivityQuerying.
final class ActivityAdapter: ActivityQuerying, @unchecked Sendable {
    private let manager = CMMotionActivityManager()

    func queryActivity(from: Date, to: Date) async throws -> [ActivitySample] {
        try await withCheckedThrowingContinuation { continuation in
            manager.queryActivityStarting(from: from, to: to, to: .main) { activities, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    let samples = (activities ?? []).map { a in
                        ActivitySample(
                            startDate:  a.startDate,
                            stationary: a.stationary,
                            walking:    a.walking,
                            running:    a.running,
                            automotive: a.automotive,
                            cycling:    a.cycling,
                            unknown:    a.unknown,
                            confidence: a.confidence.rawValue
                        )
                    }
                    continuation.resume(returning: samples)
                }
            }
        }
    }
}

// MARK: - Module

public class ChronosSensorsModule: Module {
    // Adapters are created once; CoreMotion objects are thread-safe.
    private let pedometer = PedometerAdapter()
    private let activityAdapter = ActivityAdapter()

    public func definition() -> ModuleDefinition {
        Name("ChronosSensors")

        // ── Greeting (kept for T0.4b compatibility) ──────────────────────────
        Function("hello") { () -> String in
            "Hello from ChronosSensors!"
        }

        // ── Pedometer authorization ───────────────────────────────────────────
        AsyncFunction("pedometerAuthorization") { () -> String in
            switch CMPedometer.authorizationStatus() {
            case .authorized:     return "authorized"
            case .denied:         return "denied"
            case .restricted:     return "restricted"
            case .notDetermined:  return "not_determined"
            @unknown default:     return "not_determined"
            }
        }

        // ── Request motion permission ─────────────────────────────────────────
        // Triggers a 1-minute pedometer query which causes iOS to show the
        // NSMotionUsageDescription prompt if not yet determined.
        AsyncFunction("requestMotionPermission") { (promise: Promise) in
            guard CMPedometer.isStepCountingAvailable() else {
                // On Simulator: unavailable but not denied; resolve silently.
                promise.resolve(nil)
                return
            }
            let to   = Date()
            let from = to.addingTimeInterval(-60)
            let ped  = CMPedometer()
            ped.queryPedometerData(from: from, to: to) { _, _ in
                promise.resolve(nil)
            }
        }

        // ── Query steps ───────────────────────────────────────────────────────
        AsyncFunction("querySteps") { (fromMs: Double, toMs: Double, promise: Promise) in
            guard CMPedometer.isStepCountingAvailable() else {
                promise.reject("pedometer_unavailable", "Step counting is not available on this device")
                return
            }
            let from = Date(timeIntervalSince1970: fromMs / 1000)
            let to   = Date(timeIntervalSince1970: toMs   / 1000)
            Task { [pedometer] in
                do {
                    let result = try await pedometer.querySteps(from: from, to: to)
                    promise.resolve(["steps": result.steps, "distanceM": result.distanceM])
                } catch {
                    promise.reject("pedometer_error", error.localizedDescription)
                }
            }
        }

        // ── Query activity ────────────────────────────────────────────────────
        AsyncFunction("queryActivity") { (fromMs: Double, toMs: Double, promise: Promise) in
            let from = Date(timeIntervalSince1970: fromMs / 1000)
            let to   = Date(timeIntervalSince1970: toMs   / 1000)
            Task { [activityAdapter] in
                do {
                    let samples  = try await activityAdapter.queryActivity(from: from, to: to)
                    let summary  = try summarise(samples: samples, from: from, to: to)
                    promise.resolve([
                        "walkingSec":    summary.walkingSec,
                        "runningSec":    summary.runningSec,
                        "stationarySec": summary.stationarySec,
                        "automotiveSec": summary.automotiveSec,
                        "cyclingSec":    summary.cyclingSec,
                        "unknownSec":    summary.unknownSec,
                        "walkingConf":   summary.walkingConf,
                    ])
                } catch let err as SummariserError {
                    switch err {
                    case .invalidRange(let msg):
                        promise.reject("invalid_range", msg)
                    }
                } catch {
                    promise.reject("activity_error", error.localizedDescription)
                }
            }
        }

        // ── Device state (synchronous, main thread) ───────────────────────────
        Function("deviceState") { () -> [String: Any] in
            let inputs: DeviceStateInputs
            // UIKit reads require the main thread.
            if Thread.isMainThread {
                inputs = MainActor.assumeIsolated { Self.readDeviceStateInputs() }
            } else {
                inputs = DispatchQueue.main.sync {
                    MainActor.assumeIsolated { Self.readDeviceStateInputs() }
                }
            }
            return mapDeviceState(inputs)
        }

        // ── Background refresh ────────────────────────────────────────────────
        Function("registerBackgroundRefresh") { (identifier: String) in
            // Log a warning if the identifier doesn't match the Swift constant.
            if identifier != BG_REFRESH_IDENTIFIER {
                print("[ChronosSensors] WARNING: registerBackgroundRefresh called with " +
                      "'\(identifier)' but expected '\(BG_REFRESH_IDENTIFIER)'")
            }
            // Registration is handled by ChronosSensorsAppDelegateSubscriber at launch.
            // This JS call is a no-op (the identifier is already registered).
        }

        Function("backgroundRefreshStatus") { () -> String in
            Self.onMain {
                switch UIApplication.shared.backgroundRefreshStatus {
                case .available: return "available"
                case .denied: return "denied"
                case .restricted: return "restricted"
                @unknown default: return "unknown"
                }
            }
        }

        Function("scheduleBackgroundRefresh") { (identifier: String, earliestInSec: Double) -> Bool in
            guard identifier == BG_REFRESH_IDENTIFIER, earliestInSec.isFinite,
                  earliestInSec >= 60, earliestInSec <= 86400 else { return false }
            return Self.onMain {
                let request = BGAppRefreshTaskRequest(identifier: identifier)
                request.earliestBeginDate = Date(timeIntervalSinceNow: earliestInSec)
                BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: identifier)
                do {
                    try BGTaskScheduler.shared.submit(request)
                    return true
                } catch { return false }
            }
        }

        Function("takePendingBackgroundRefresh") { () -> [String: String]? in
            Self.onMain { NativeRefreshOwner.shared.claim() }
        }

        Function("completeBackgroundRefresh") { (executionId: String, success: Bool) in
            Self.onMain { NativeRefreshOwner.shared.finish(id: executionId, success: success) }
        }

        Events("backgroundRefresh")
    }

    // MARK: - Private helpers

    /// Read device state from UIKit. Must be called on the main thread.
    @MainActor private static func readDeviceStateInputs() -> DeviceStateInputs {
        UIDevice.current.isBatteryMonitoringEnabled = true
        return DeviceStateInputs(
            isProtectedDataAvailable: UIApplication.shared.isProtectedDataAvailable,
            batteryStateRaw:          UIDevice.current.batteryState.rawValue,
            batteryLevel:             UIDevice.current.batteryLevel,
            appStateRaw:              UIApplication.shared.applicationState.rawValue,
            isLowPowerModeEnabled:    ProcessInfo.processInfo.isLowPowerModeEnabled,
            iosVersion:               UIDevice.current.systemVersion
        )
    }

    // MARK: - Lifecycle

    public required init(appContext: AppContext) {
        super.init(appContext: appContext)
        // Listen for BGAppRefreshTask notifications from the AppDelegateSubscriber.
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleBGRefreshNotification(_:)),
            name: .chronosBGRefreshFired,
            object: nil
        )
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    @MainActor @objc private func handleBGRefreshNotification(_ notification: Notification) {
        // Availability only: JS claims after it has installed its listener, also replaying cold starts.
        var payload = ["id": BG_REFRESH_IDENTIFIER]
        if let ended = notification.userInfo?["endedExecutionId"] as? String {
            payload["endedExecutionId"] = ended
        }
        self.sendEvent("backgroundRefresh", payload)
    }

    private static func onMain<T: Sendable>(_ operation: @MainActor () -> T) -> T {
        if Thread.isMainThread { return MainActor.assumeIsolated { operation() } }
        return DispatchQueue.main.sync { MainActor.assumeIsolated { operation() } }
    }
}
