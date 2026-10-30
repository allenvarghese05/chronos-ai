import XCTest
@testable import ChronosSensorsCore

final class RefreshCoordinatorTests: XCTestCase {
    func testPendingSurvivesBeforeListenerAndClaimsOnlyOnce() async {
        await MainActor.run {
            let owner = RefreshCoordinator()
            var results: [Bool] = []
            owner.begin(id: "A") { results.append($0) }
            XCTAssertEqual(owner.pendingID, "A")
            XCTAssertEqual(owner.claim(), "A")
            XCTAssertNil(owner.claim())
            owner.finish(id: "A", success: true)
            owner.finish(id: "A", success: false)
            XCTAssertEqual(results, [true])
        }
    }
    func testExpiryBeforeReadinessDoesNotReplay() async {
        await MainActor.run {
            let owner = RefreshCoordinator()
            var results: [Bool] = []
            owner.begin(id: "A") { results.append($0) }
            owner.finish(id: "A", success: false)
            XCTAssertNil(owner.claim())
            XCTAssertEqual(results, [false])
        }
    }
    func testStaleCompletionCannotFinishReplacement() async {
        await MainActor.run {
            let owner = RefreshCoordinator()
            var a: [Bool] = []; var b: [Bool] = []
            owner.begin(id: "A") { a.append($0) }
            owner.begin(id: "B") { b.append($0) }
            owner.finish(id: "A", success: true)
            XCTAssertEqual(owner.claim(), "B")
            XCTAssertEqual(a, [false]); XCTAssertEqual(b, [])
            owner.finish(id: "B", success: true)
            XCTAssertEqual(b, [true])
        }
    }
    func testCompletionClearsBeforeReentrantNewExecution() async {
        await MainActor.run {
            let owner = RefreshCoordinator()
            owner.begin(id: "A") { _ in owner.begin(id: "B") { _ in } }
            owner.finish(id: "A", success: true)
            XCTAssertEqual(owner.claim(), "B")
        }
    }
}
