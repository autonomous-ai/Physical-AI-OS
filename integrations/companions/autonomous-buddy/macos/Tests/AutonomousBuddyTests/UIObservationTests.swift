import XCTest
@testable import AutonomousBuddy

final class UIObservationTests: XCTestCase {
    func testObservationLimitsRejectUnboundedOrNonIntegerInputs() throws {
        for value in [true, false, 0, -1, 501, 2.5, "20", Double.infinity] as [Any] {
            XCTAssertThrowsError(try ExecutorParameters.integer(["max_nodes": value], "max_nodes", default: 150, range: 1...500))
        }
        XCTAssertEqual(try ExecutorParameters.integer([:], "max_nodes", default: 150, range: 1...500), 150)
        XCTAssertEqual(try ExecutorParameters.integer(["max_nodes": 500], "max_nodes", default: 150, range: 1...500), 500)
    }

    func testReferencesRequireSameSnapshotUnexpiredAndSameForegroundProcess() {
        func valid(_ requested: String = "snapshot", _ snapshot: String = "snapshot", _ age: TimeInterval = 1,
                   _ pid: pid_t = 123, _ frontmost: pid_t? = 123) -> Bool {
            UIObservationStore.referenceIsCurrent(requested: requested, snapshot: snapshot, age: age,
                                                   observedPID: pid, frontmostPID: frontmost)
        }
        XCTAssertTrue(valid())
        XCTAssertFalse(valid("older"))
        XCTAssertFalse(valid("", ""))
        XCTAssertFalse(valid("snapshot", "snapshot", 30))
        XCTAssertFalse(valid("snapshot", "snapshot", -1))
        XCTAssertFalse(valid("snapshot", "snapshot", 1, 123, 456))
        XCTAssertFalse(valid("snapshot", "snapshot", 1, 123, nil))
        XCTAssertFalse(valid("snapshot", "snapshot", 1, 0, 0))
    }
}
