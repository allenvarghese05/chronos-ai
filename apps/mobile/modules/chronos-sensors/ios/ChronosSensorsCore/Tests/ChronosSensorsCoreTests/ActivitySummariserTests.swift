import XCTest
@testable import ChronosSensorsCore

final class ActivitySummariserTests: XCTestCase {
    // MARK: - Helpers

    private let epoch = Date(timeIntervalSinceReferenceDate: 0)

    private func date(seconds: TimeInterval) -> Date {
        epoch.addingTimeInterval(seconds)
    }

    // MARK: - Edge case 1: from > to throws

    func test_invalidRange_fromGreaterThanTo_throws() throws {
        let from = date(seconds: 200)
        let to   = date(seconds: 100)
        XCTAssertThrowsError(
            try summarise(samples: [], from: from, to: to)
        ) { error in
            guard case SummariserError.invalidRange = error else {
                return XCTFail("Expected invalidRange, got \(error)")
            }
        }
    }

    // MARK: - Edge case 1b: from == to → zero duration

    func test_fromEqualsTo_allZeros() throws {
        let t = date(seconds: 0)
        let result = try summarise(samples: [], from: t, to: t)
        XCTAssertEqual(result.walkingSec, 0)
        XCTAssertEqual(result.runningSec, 0)
        XCTAssertEqual(result.stationarySec, 0)
        XCTAssertEqual(result.automotiveSec, 0)
        XCTAssertEqual(result.cyclingSec, 0)
        XCTAssertEqual(result.unknownSec, 0)
        XCTAssertEqual(result.walkingConf, "low")
    }

    // MARK: - Edge case 3: empty activity list

    func test_emptyActivityList_allZerosLowConf() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 600)
        let result = try summarise(samples: [], from: from, to: to)
        XCTAssertEqual(result.walkingSec, 0)
        XCTAssertEqual(result.runningSec, 0)
        XCTAssertEqual(result.stationarySec, 0)
        XCTAssertEqual(result.automotiveSec, 0)
        XCTAssertEqual(result.cyclingSec, 0)
        XCTAssertEqual(result.unknownSec, 0)
        XCTAssertEqual(result.walkingConf, "low")
    }

    // MARK: - Basic two-sample change-point

    func test_twoSamples_stationaryThenWalking() throws {
        // [from, from+30s) stationary; [from+30s, to) walking
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            ActivitySample(startDate: date(seconds: 0),  stationary: true,  confidence: 2),
            ActivitySample(startDate: date(seconds: 30), walking: true,     confidence: 1),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.stationarySec, 30)
        XCTAssertEqual(result.walkingSec, 30)
        XCTAssertEqual(result.walkingConf, "medium")
    }

    // MARK: - Edge case 4: first sample starts after `from` (gap before first sample)

    func test_firstSampleStartsAfterFrom_gapIsCorrect() throws {
        // from=0, first sample at +30s (stationary), second at +60s (walking), to=90s
        // [0, 30s) → no sample → unknown? Actually no sample covers this gap.
        // The gap before the first sample has no coverage — it is simply not counted.
        // Only segments [30s,60s) stationary and [60s,90s) walking are included.
        let from = date(seconds: 0)
        let to   = date(seconds: 90)
        let samples = [
            ActivitySample(startDate: date(seconds: 30), stationary: true,  confidence: 0),
            ActivitySample(startDate: date(seconds: 60), walking: true,     confidence: 2),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        // The first sample starts at 30s; segStart = max(30, 0) = 30; segEnd = 60 → 30s stationary
        // The second sample starts at 60s; segStart = max(60, 0) = 60; segEnd = 90 → 30s walking
        XCTAssertEqual(result.stationarySec, 30)
        XCTAssertEqual(result.walkingSec, 30)
        XCTAssertEqual(result.walkingConf, "high")
    }

    // MARK: - Edge case 5: sample starts before `from` (clamped)

    func test_sampleStartsBeforeFrom_durationClampedToFrom() throws {
        // Sample at -10s (walking), second at +60s (stationary), to = 120s
        // First sample: segStart = max(-10, 0) = 0; segEnd = 60 → 60s walking
        // Second sample: segStart = max(60, 0) = 60; segEnd = 120 → 60s stationary
        let from = date(seconds: 0)
        let to   = date(seconds: 120)
        let samples = [
            ActivitySample(startDate: date(seconds: -10), walking: true,    confidence: 2),
            ActivitySample(startDate: date(seconds: 60),  stationary: true, confidence: 0),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.walkingSec, 60)
        XCTAssertEqual(result.stationarySec, 60)
        XCTAssertEqual(result.walkingConf, "high")
    }

    // MARK: - Edge case 6: multiple boolean flags true — precedence applied

    func test_multipleFlagsTrue_precedenceAutomotiveOverWalking() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            // Both automotive and walking are true → automotive wins
            ActivitySample(startDate: date(seconds: 0), walking: true, automotive: true, confidence: 2),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.automotiveSec, 60)
        XCTAssertEqual(result.walkingSec, 0)
        // automotive flag is not walking/running → walkingConf = "low"
        // But walking flag is also true, so we do track walkingConf = high
        XCTAssertEqual(result.walkingConf, "high")
    }

    func test_multipleFlagsTrue_precedenceCyclingOverRunning() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            ActivitySample(startDate: date(seconds: 0), running: true, cycling: true, confidence: 1),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.cyclingSec, 60)
        XCTAssertEqual(result.runningSec, 0)
        // running flag is true, so walkingConf is tracked
        XCTAssertEqual(result.walkingConf, "medium")
    }

    func test_multipleFlagsTrue_precedenceWalkingOverStationary() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            ActivitySample(startDate: date(seconds: 0), stationary: true, walking: true, confidence: 0),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.walkingSec, 60)
        XCTAssertEqual(result.stationarySec, 0)
        XCTAssertEqual(result.walkingConf, "low")
    }

    // MARK: - Edge case 7: walkingConf = max over walking+running samples

    func test_walkingConf_maxOverMixedSamples() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 120)
        let samples = [
            ActivitySample(startDate: date(seconds: 0),  walking: true, confidence: 0),  // low
            ActivitySample(startDate: date(seconds: 60), running: true, confidence: 2),  // high
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.walkingConf, "high")  // max of low and high
        XCTAssertEqual(result.walkingSec, 60)
        XCTAssertEqual(result.runningSec, 60)
    }

    func test_walkingConf_onlyStationarySamples_returnsLow() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            ActivitySample(startDate: date(seconds: 0), stationary: true, confidence: 2),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.walkingConf, "low")
        XCTAssertEqual(result.stationarySec, 60)
    }

    // MARK: - 7-day clamping

    func test_rangeLongerThan7Days_clampedTo7Days() throws {
        let from = date(seconds: 0)
        let tenDays: TimeInterval = 10 * 24 * 3600
        let to   = date(seconds: tenDays)
        let sevenDaysSec = 7 * 24 * 3600
        let samples = [
            ActivitySample(startDate: date(seconds: 0), walking: true, confidence: 2),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        // All duration should be at most 7 days worth
        XCTAssertEqual(result.walkingSec, sevenDaysSec)
    }

    // MARK: - Unsorted samples

    func test_unsortedSamples_sortedBeforeProcessing() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 90)
        let samples = [
            ActivitySample(startDate: date(seconds: 60), walking: true,    confidence: 2),  // out of order
            ActivitySample(startDate: date(seconds: 0),  stationary: true, confidence: 0),
            ActivitySample(startDate: date(seconds: 30), running: true,    confidence: 1),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.stationarySec, 30)
        XCTAssertEqual(result.runningSec, 30)
        XCTAssertEqual(result.walkingSec, 30)
        XCTAssertEqual(result.walkingConf, "high")
    }

    // MARK: - Single sample spanning full window

    func test_singleSample_spansFullWindow() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 600)
        let samples = [
            ActivitySample(startDate: date(seconds: 0), stationary: true, confidence: 0),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.stationarySec, 600)
        XCTAssertEqual(result.walkingConf, "low")
    }

    // MARK: - Sample entirely after window — excluded

    func test_sampleAfterTo_excluded() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            ActivitySample(startDate: date(seconds: 100), walking: true, confidence: 2),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.walkingSec, 0)
        XCTAssertEqual(result.walkingConf, "low")
    }

    // MARK: - All activity types

    func test_allActivityTypes() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 300)
        let samples = [
            ActivitySample(startDate: date(seconds: 0),   automotive: true, confidence: 0),
            ActivitySample(startDate: date(seconds: 60),  cycling: true,    confidence: 0),
            ActivitySample(startDate: date(seconds: 120), running: true,    confidence: 1),
            ActivitySample(startDate: date(seconds: 180), walking: true,    confidence: 2),
            ActivitySample(startDate: date(seconds: 240), stationary: true, confidence: 0),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.automotiveSec, 60)
        XCTAssertEqual(result.cyclingSec, 60)
        XCTAssertEqual(result.runningSec, 60)
        XCTAssertEqual(result.walkingSec, 60)
        XCTAssertEqual(result.stationarySec, 60)
        XCTAssertEqual(result.unknownSec, 0)
        XCTAssertEqual(result.walkingConf, "high")
    }

    // MARK: - Unknown-dominant sample

    func test_unknownDominant_countsAsUnknown() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            ActivitySample(startDate: date(seconds: 0), unknown: true, confidence: 0),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.unknownSec, 60)
        XCTAssertEqual(result.walkingConf, "low")
    }

    // MARK: - Sample with all flags false → unknown

    func test_noFlagsTrue_treatedAsUnknown() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            ActivitySample(startDate: date(seconds: 0)),  // all flags false
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.unknownSec, 60)
    }

    // MARK: - Confidence high = 2, medium = 1, low = 0

    func test_walkingConf_highWhenConfidence2() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            ActivitySample(startDate: date(seconds: 0), walking: true, confidence: 2),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.walkingConf, "high")
    }

    func test_walkingConf_mediumWhenConfidence1() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            ActivitySample(startDate: date(seconds: 0), walking: true, confidence: 1),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.walkingConf, "medium")
    }

    func test_walkingConf_lowWhenConfidence0() throws {
        let from = date(seconds: 0)
        let to   = date(seconds: 60)
        let samples = [
            ActivitySample(startDate: date(seconds: 0), walking: true, confidence: 0),
        ]
        let result = try summarise(samples: samples, from: from, to: to)
        XCTAssertEqual(result.walkingConf, "low")
    }
}
