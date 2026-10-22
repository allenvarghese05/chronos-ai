import ExpoModulesCore
import BackgroundTasks

/// Registers the BGTaskScheduler handler before the app finishes launching.
///
/// iOS requires `BGTaskScheduler.shared.register(forTaskWithIdentifier:...)` to be called
/// **before** `applicationDidFinishLaunching` returns.  The Expo AppDelegateSubscriber hook
/// runs at the right time and avoids modifying the generated AppDelegate.
///
/// When the task fires it posts a `chronosBGRefreshFired` notification; the live
/// `ChronosSensorsModule` instance observes it and emits the JS `backgroundRefresh` event.
/// The subscriber also sets a 25-second expiration handler so iOS does not kill the app if
/// neither the module nor JS ever completes the task.
public class ChronosSensorsAppDelegateSubscriber: ExpoAppDelegateSubscriber {
    public func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: BG_REFRESH_IDENTIFIER,
            using: nil
        ) { task in
            guard let refreshTask = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }

            // Expiration handler: if neither the module nor JS completes the task within
            // iOS's allowed budget (~25-30 s), mark it failed so iOS doesn't kill the app.
            refreshTask.expirationHandler = {
                refreshTask.setTaskCompleted(success: false)
            }

            // Post a notification; ChronosSensorsModule picks it up via NotificationCenter.
            NotificationCenter.default.post(
                name: .chronosBGRefreshFired,
                object: nil,
                userInfo: [kChronosBGTaskKey: refreshTask]
            )
        }
        return true
    }
}
