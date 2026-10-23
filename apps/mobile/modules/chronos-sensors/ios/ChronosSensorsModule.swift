import ExpoModulesCore
import CoreMotion
import UIKit
import BackgroundTasks

// MARK: - Shared BG task notification

/// Posted by ChronosSensorsAppDelegateSubscriber when a BGAppRefreshTask fires.
extension Notification.Name {
    static let chronosBGRefreshFired = Notification.Name("ChronosSensorsBGRefreshFired")
}
/// UserInfo key for the BGAppRefreshTask object.
let kChronosBGTaskKey = "task"

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

// Expo owns the module context; our adapters are immutable and Sendable, and all
// mutable background-task state is isolated to MainActor.
public class ChronosSensorsModule: Module, @unchecked Sendable {
    // Adapters are created once; CoreMotion objects are thread-safe.
    private let pedometer = PedometerAdapter()
    private let activityAdapter = ActivityAdapter()
    // Reference to the pending BGAppRefreshTask so JS can call completeBackgroundRefresh.
    // Expo calls arrive on different queues; all mutable lifecycle state is main-isolated.
    @MainActor private var pendingTask: BGAppRefreshTask?
    // Auto-complete timer: fires after 25 s if JS never completes the task.
    @MainActor private var completionTimer: Timer?

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
            Task {
                do {
                    let result = try await self.pedometer.querySteps(from: from, to: to)
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
            Task {
                do {
                    let samples  = try await self.activityAdapter.queryActivity(from: from, to: to)
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

        Function("scheduleBackgroundRefresh") { (identifier: String, earliestInSec: Double) in
            let request = BGAppRefreshTaskRequest(identifier: identifier)
            request.earliestBeginDate = Date(timeIntervalSinceNow: earliestInSec)
            do {
                try BGTaskScheduler.shared.submit(request)
            } catch {
                print("[ChronosSensors] scheduleBackgroundRefresh error: \(error)")
            }
        }

        Function("completeBackgroundRefresh") { (success: Bool) in
            DispatchQueue.main.async {
                self.finishBackgroundRefresh(success: success)
            }
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

    @objc private func handleBGRefreshNotification(_ notification: Notification) {
        guard let task = notification.userInfo?[kChronosBGTaskKey] as? BGAppRefreshTask else { return }
        DispatchQueue.main.async {
            self.finishBackgroundRefresh(success: false)
            self.pendingTask = task
            // Install the timeout before JS can respond to the event.
            // Retain the module until the safety completion even if Expo releases it.
            let timer = Timer(timeInterval: 25, repeats: false) { [self] _ in
                DispatchQueue.main.async {
                    self.finishBackgroundRefresh(success: false)
                }
            }
            RunLoop.main.add(timer, forMode: .default)
            self.completionTimer = timer
            task.expirationHandler = { [weak self] in
                DispatchQueue.main.async {
                    self?.finishBackgroundRefresh(success: false)
                }
            }
            self.sendEvent("backgroundRefresh", ["id": task.identifier])
        }
    }

    /// One main-isolated completion path for JS, expiration, and the safety timeout.
    @MainActor private func finishBackgroundRefresh(success: Bool) {
        completionTimer?.invalidate()
        completionTimer = nil
        pendingTask?.expirationHandler = nil
        pendingTask?.setTaskCompleted(success: success)
        pendingTask = nil
    }
}
