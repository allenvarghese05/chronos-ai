import Foundation

// MARK: - Step query result

/// Result of a pedometer step-count query.
/// All fields are value types so the struct is automatically Sendable.
public struct StepQueryResult: Sendable {
    public let steps: Int
    public let distanceM: Double

    public init(steps: Int, distanceM: Double) {
        self.steps = steps
        self.distanceM = distanceM
    }
}

// MARK: - Protocols

/// Abstracts CMPedometer so tests can inject a fake implementation.
public protocol StepQuerying: Sendable {
    /// Query step count and distance between two dates.
    /// Throws if the pedometer is unavailable or the query fails.
    func querySteps(from: Date, to: Date) async throws -> StepQueryResult
}

/// Abstracts CMMotionActivityManager so tests can inject a fake implementation.
public protocol ActivityQuerying: Sendable {
    /// Query change-point motion activities between two dates.
    /// Returns an empty array (not an error) when no activities are recorded.
    func queryActivity(from: Date, to: Date) async throws -> [ActivitySample]
}
