import Foundation

/// The supported direct MCP host owns its runtime until stdin closes. It never
/// connects to, starts, or stops the user's standalone CuaDriver daemon.
enum CuaRuntime {
    static let arguments = ["mcp", "--direct", "--embedded"]
    static let driverPath = "Contents/Helpers/CuaDriver.app/Contents/MacOS/cua-driver"

    /// Electron launches the native bridge from Contents/Resources/native,
    /// where Bundle.main is not necessarily the outer application bundle.
    static func applicationURL(executable: URL) -> URL? {
        var ancestor = executable.deletingLastPathComponent()
        while ancestor.path != "/" {
            if ancestor.pathExtension == "app" { return ancestor }
            ancestor.deleteLastPathComponent()
        }
        return nil
    }

    static func executableURL(
        hostExecutable: URL? = Bundle.main.executableURL,
        standalone: URL = URL(fileURLWithPath: "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"),
        isExecutable: (String) -> Bool = FileManager.default.isExecutableFile(atPath:)
    ) -> URL? {
        if let hostExecutable, let app = applicationURL(executable: hostExecutable) {
            // A packaged application must use its pinned payload; silently using
            // a different system install would defeat release compatibility.
            let bundled = app.appendingPathComponent(driverPath)
            return isExecutable(bundled.path) ? bundled : nil
        }
        // Swift development builds may use a manually installed driver.
        return isExecutable(standalone.path) ? standalone : nil
    }

    static func environment(base: [String: String] = ProcessInfo.processInfo.environment) -> [String: String] {
        var environment = base
        environment["CUA_DRIVER_MCP_ENVELOPES"] = "1"
        environment["CUA_DRIVER_EMBEDDED"] = "1"
        if let executable = Bundle.main.executableURL,
           let app = applicationURL(executable: executable),
           let identifier = Bundle(url: app)?.bundleIdentifier {
            environment["CUA_DRIVER_HOST_BUNDLE_ID"] = identifier
        } else {
            environment["CUA_DRIVER_HOST_BUNDLE_ID"] = "network.autonomous.ai.buddy.manager"
        }
        environment["CUA_DRIVER_PERMISSION_MODE"] = "standard"
        environment.removeValue(forKey: "CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS")
        environment["CUA_DRIVER_RS_UPDATE_CHECK"] = "false"
        environment["DO_NOT_TRACK"] = "1"
        return environment
    }
}
