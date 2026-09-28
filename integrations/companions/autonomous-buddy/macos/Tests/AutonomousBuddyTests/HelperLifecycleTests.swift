import XCTest
@testable import AutonomousBuddy

final class HelperLifecycleTests: XCTestCase {
    func testEmbeddedHelperExitsWhenParentClosesStdin() async throws {
        let products = Bundle(for: Self.self).bundleURL.deletingLastPathComponent()
        let executable = products.appendingPathComponent("AutonomousBuddy")
        XCTAssertTrue(FileManager.default.isExecutableFile(atPath: executable.path))
        let child = Process()
        child.executableURL = executable
        child.arguments = ["--embedded-helper"]
        var environment = ProcessInfo.processInfo.environment
        environment["BUDDY_NATIVE_TEST_MODE"] = "1"
        child.environment = environment
        let input = Pipe(), output = Pipe()
        child.standardInput = input
        child.standardOutput = output
        child.standardError = FileHandle.nullDevice
        let ready = expectation(description: "Native helper publishes its first state")
        let state = FirstState()
        output.fileHandleForReading.readabilityHandler = { handle in
            if state.receive(handle.availableData) { ready.fulfill() }
        }
        defer {
            if child.isRunning { kill(child.processIdentifier, SIGKILL) }
            output.fileHandleForReading.readabilityHandler = nil
            try? input.fileHandleForWriting.close()
            try? output.fileHandleForReading.close()
        }
        try child.run()
        await fulfillment(of: [ready], timeout: 5)
        // This exercises the AppKit termination path, not only bridge parsing.
        // Returning terminateLater from a main-queue EOF handler used to block
        // the MainActor cleanup task forever in AppKit's nested run loop.
        try input.fileHandleForWriting.close()
        let deadline = Date().addingTimeInterval(2)
        while child.isRunning && Date() < deadline {
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertFalse(child.isRunning, "Helper outlived its parent's input pipe")
        if !child.isRunning { XCTAssertEqual(child.terminationStatus, 0) }
    }

    private final class FirstState: @unchecked Sendable {
        private let lock = NSLock()
        private var bytes = Data()
        private var received = false

        func receive(_ data: Data) -> Bool {
            lock.lock()
            defer { lock.unlock() }
            guard !received else { return false }
            bytes.append(data)
            while let newline = bytes.firstIndex(of: 10) {
                let line = bytes.prefix(upTo: newline)
                bytes.removeSubrange(...newline)
                if let value = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                   value["event"] as? String == "state" {
                    received = true
                    return true
                }
            }
            return false
        }
    }
}
