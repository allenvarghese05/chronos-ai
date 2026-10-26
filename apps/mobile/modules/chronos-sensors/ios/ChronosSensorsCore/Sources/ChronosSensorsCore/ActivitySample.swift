import Foundation

/// Mirrors the fields of CMMotionActivity that we consume.
/// Pure value type — constructible in tests without CoreMotion.
public struct ActivitySample: Sendable {
    public let startDate: Date
    public let stationary: Bool
    public let walking: Bool
    public let running: Bool
    public let automotive: Bool
    public let cycling: Bool
    public let unknown: Bool
    /// Raw confidence: 0 = low, 1 = medium, 2 = high (matches CMMotionActivityConfidence).
    public let confidence: Int

    public init(
        startDate: Date,
        stationary: Bool = false,
        walking: Bool = false,
        running: Bool = false,
        automotive: Bool = false,
        cycling: Bool = false,
        unknown: Bool = false,
        confidence: Int = 0
    ) {
        self.startDate = startDate
        self.stationary = stationary
        self.walking = walking
        self.running = running
        self.automotive = automotive
        self.cycling = cycling
        self.unknown = unknown
        self.confidence = confidence
    }

    // MARK: - Activity type

    public enum ActivityType: Sendable {
        case automotive, cycling, running, walking, stationary, unknown
    }

    /// Dominant activity type applying the canonical precedence rule:
    /// automotive > cycling > running > walking > stationary > unknown.
    public var dominantType: ActivityType {
        if automotive { return .automotive }
        if cycling    { return .cycling }
        if running    { return .running }
        if walking    { return .walking }
        if stationary { return .stationary }
        return .unknown
    }
}
