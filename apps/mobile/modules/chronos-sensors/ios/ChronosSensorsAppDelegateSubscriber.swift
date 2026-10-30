import ExpoModulesCore
import BackgroundTasks
import UIKit

extension Notification.Name {
    static let chronosBGRefreshFired = Notification.Name("ChronosSensorsBGRefreshFired")
}

/// Launch-owned retention and timeout exist even when the Expo module or JS does not yet exist.
@MainActor final class NativeRefreshOwner {
    static let shared = NativeRefreshOwner()
    private let coordinator = RefreshCoordinator()
    private var timer: Timer?

    func begin(_ task: BGAppRefreshTask) {
        let id = UUID().uuidString
        coordinator.begin(id: id) { [weak self, task] success in
            self?.timer?.invalidate()
            self?.timer = nil
            task.expirationHandler = nil
            task.setTaskCompleted(success: success)
            NotificationCenter.default.post(name: .chronosBGRefreshFired, object: nil,
                userInfo: ["endedExecutionId": id])
        }
        task.expirationHandler = { [weak self] in
            DispatchQueue.main.async { self?.coordinator.finish(id: id, success: false) }
        }
        let safety = Timer(timeInterval: 25, repeats: false) { [weak self] _ in
            DispatchQueue.main.async { self?.coordinator.finish(id: id, success: false) }
        }
        timer = safety
        RunLoop.main.add(safety, forMode: .common)
        NotificationCenter.default.post(name: .chronosBGRefreshFired, object: nil)
    }

    func claim() -> [String: String]? {
        guard let id = coordinator.claim() else { return nil }
        return ["id": BG_REFRESH_IDENTIFIER, "executionId": id]
    }

    func finish(id: String, success: Bool) {
        coordinator.finish(id: id, success: success)
    }
}

/// Register before didFinishLaunching returns. Generated Expo AppDelegate starts React Native
/// at launch, including background launch; pending work waits for JS's explicit claim handshake.
public class ChronosSensorsAppDelegateSubscriber: ExpoAppDelegateSubscriber {
    public func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: BG_REFRESH_IDENTIFIER,
            using: DispatchQueue.main
        ) { task in
            guard let refresh = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            MainActor.assumeIsolated { NativeRefreshOwner.shared.begin(refresh) }
        }
        return true
    }
}
