import Foundation

// MARK: - Result type

/// Duration summary for a motion-activity query window.
public struct ActivitySummary: Sendable {
    public let walkingSec: Int
    public let runningSec: Int
    public let stationarySec: Int
    public let automotiveSec: Int
    public let cyclingSec: Int
    public let unknownSec: Int
    /// Maximum confidence over all walking or running samples ("low" | "medium" | "high").
    /// Returns "low" when no walking/running samples are present.
    public let walkingConf: String
}

// MARK: - Error

/// Errors thrown by summarise(samples:from:to:).
public enum SummariserError: Error, Sendable {
    case invalidRange(String)
}

// MARK: - Pure summarisation function

/// Compute per-activity-type durations from a CoreMotion change-point array.
///
/// Algorithm:
///  1. Reject from > to (throws SummariserError.invalidRange).
///  2. Clamp effectiveTo = min(to, from + 7 days) (CoreMotion 7-day limit).
///  3. Sort samples ascending by startDate; drop samples whose startDate >= effectiveTo.
///  4. For each sample i, duration = max(0, min(nextStart, effectiveTo) − max(sample.startDate, from)).
///  5. The dominant activity type (automotive > cycling > running > walking > stationary > unknown)
///     receives the full duration.
///  6. walkingConf = max confidence over samples where walking == true OR running == true.
///
/// - Parameters:
///   - samples: Unsorted; may start before `from` (start is clamped per-segment).
///   - from: Inclusive start of the query window.
///   - to: End of the query window (may exceed 7 days; clamped internally).
/// - Returns: ActivitySummary with per-type durations in whole seconds.
/// - Throws: SummariserError.invalidRange when from > to.
public func summarise(samples: [ActivitySample], from: Date, to: Date) throws -> ActivitySummary {
    guard from <= to else {
        throw SummariserError.invalidRange("from (\(from)) must be ≤ to (\(to))")
    }

    // CoreMotion refuses queries longer than 7 days — clamp effectiveTo.
    let sevenDays: TimeInterval = 7 * 24 * 3600
    let effectiveTo = to.timeIntervalSince(from) > sevenDays
        ? from.addingTimeInterval(sevenDays)
        : to

    // Sort ascending; exclude samples that start at or after effectiveTo
    // (they contribute zero duration and would only complicate boundary maths).
    let sorted = samples
        .filter { $0.startDate < effectiveTo }
        .sorted { $0.startDate < $1.startDate }

    var walkingSec    = 0
    var runningSec    = 0
    var stationarySec = 0
    var automotiveSec = 0
    var cyclingSec    = 0
    var unknownSec    = 0
    var maxWalkingConf = -1   // -1 means no walking/running sample seen yet

    for (i, sample) in sorted.enumerated() {
        // Clamp the segment start to the query window's from.
        let segStart = max(sample.startDate.timeIntervalSinceReferenceDate,
                          from.timeIntervalSinceReferenceDate)

        // The segment ends at the next sample's startDate (or effectiveTo for the last sample).
        let rawSegEnd: TimeInterval
        if i + 1 < sorted.count {
            rawSegEnd = sorted[i + 1].startDate.timeIntervalSinceReferenceDate
        } else {
            rawSegEnd = effectiveTo.timeIntervalSinceReferenceDate
        }
        let segEnd = min(rawSegEnd, effectiveTo.timeIntervalSinceReferenceDate)

        let duration = max(0.0, segEnd - segStart)
        let secs = Int(duration.rounded())

        switch sample.dominantType {
        case .walking:    walkingSec    += secs
        case .running:    runningSec    += secs
        case .stationary: stationarySec += secs
        case .automotive: automotiveSec += secs
        case .cycling:    cyclingSec    += secs
        case .unknown:    unknownSec    += secs
        }

        // Track max confidence for walking and running samples.
        if sample.walking || sample.running {
            maxWalkingConf = max(maxWalkingConf, sample.confidence)
        }
    }

    let walkingConf: String
    switch maxWalkingConf {
    case 2:  walkingConf = "high"
    case 1:  walkingConf = "medium"
    case 0:  walkingConf = "low"
    default: walkingConf = "low"   // no walking/running sample
    }

    return ActivitySummary(
        walkingSec: walkingSec,
        runningSec: runningSec,
        stationarySec: stationarySec,
        automotiveSec: automotiveSec,
        cyclingSec: cyclingSec,
        unknownSec: unknownSec,
        walkingConf: walkingConf
    )
}
