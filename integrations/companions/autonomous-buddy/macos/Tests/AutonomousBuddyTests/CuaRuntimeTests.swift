import XCTest
@testable import AutonomousBuddy

final class CuaRuntimeTests: XCTestCase {
    func testFindsDriverInOuterElectronBundle() {
        let bridge = URL(fileURLWithPath: "/Applications/Autonomous Buddy.app/Contents/Resources/native/AutonomousBuddy")
        let expected = "/Applications/Autonomous Buddy.app/" + CuaRuntime.driverPath
        XCTAssertEqual(CuaRuntime.executableURL(hostExecutable: bridge, isExecutable: { $0 == expected })?.path, expected)
    }

    func testPackagedAppNeverUsesUnpinnedStandaloneDriver() {
        let bridge = URL(fileURLWithPath: "/Applications/Buddy.app/Contents/MacOS/AutonomousBuddy")
        XCTAssertNil(CuaRuntime.executableURL(hostExecutable: bridge, isExecutable: { $0.hasPrefix("/Applications/CuaDriver.app/") }))
        let development = URL(fileURLWithPath: "/tmp/build/AutonomousBuddy")
        XCTAssertNotNil(CuaRuntime.executableURL(hostExecutable: development, isExecutable: { _ in true }))
    }

    func testPrivateRuntimeOwnsPermissionsAndDoesNotUpdate() {
        XCTAssertEqual(CuaRuntime.arguments, ["mcp", "--direct", "--embedded"])
        let base = ["HOME": "/Users/example", "CUA_DRIVER_PERMISSION_MODE": "unrestricted", "CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS": "1"]
        let environment = CuaRuntime.environment(base: base)
        XCTAssertEqual(environment["HOME"], base["HOME"])
        XCTAssertEqual(environment["CUA_DRIVER_PERMISSION_MODE"], "standard")
        XCTAssertNil(environment["CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS"])
        XCTAssertEqual(environment["CUA_DRIVER_RS_UPDATE_CHECK"], "false")
        XCTAssertEqual(environment["CUA_DRIVER_MCP_ENVELOPES"], "1")
        XCTAssertEqual(environment["DO_NOT_TRACK"], "1")
    }
}
