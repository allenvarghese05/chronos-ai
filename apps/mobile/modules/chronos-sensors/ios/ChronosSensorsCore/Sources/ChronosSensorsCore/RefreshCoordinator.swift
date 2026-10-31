/// Main-actor execution ownership independent of BackgroundTasks/Expo for deterministic tests.
@MainActor public final class RefreshCoordinator {
    public private(set) var pendingID: String?
    private var claimed = false
    private var completion: ((Bool) -> Void)?

    public init() {}

    /// Replacing a pending execution fails it exactly once before accepting the next.
    public func begin(id: String, completion: @escaping (Bool) -> Void) {
        if let pendingID { finish(id: pendingID, success: false) }
        self.pendingID = id
        self.completion = completion
        claimed = false
    }

    /// Readiness handshake: a launch-time task survives until a JS listener claims it.
    public func claim() -> String? {
        guard let pendingID, !claimed else { return nil }
        claimed = true
        return pendingID
    }

    /// Clear state before invoking completion; stale callbacks cannot complete a newer job.
    public func finish(id: String, success: Bool) {
        guard pendingID == id else { return }
        let callback = completion
        pendingID = nil
        completion = nil
        claimed = false
        callback?(success)
    }
}
