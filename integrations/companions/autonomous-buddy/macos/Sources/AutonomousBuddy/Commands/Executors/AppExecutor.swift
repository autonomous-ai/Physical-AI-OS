import AppKit
import Foundation

struct OpenAppExecutor: Executor {
    let action = "open_app"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let appName = params["app"] as? String, !appName.isEmpty else {
            throw ExecutorError.missingParam("app")
        }
        try Task.checkCancellation()
        return try await openApplication(named: appName)
    }

    @MainActor
    private func openApplication(named appName: String) async throws -> [String: Any] {
        try Task.checkCancellation()
        guard let url = Self.resolveAppURL(named: appName) else {
            throw ExecutorError.actionFailed("app not found: \(appName)")
        }
        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        try Task.checkCancellation()
        let runningApp = try await NSWorkspace.shared.openApplication(at: url, configuration: config)
        try Task.checkCancellation()
        return Self.activationResult(runningApp)
    }

    // Activation is requested once. Report current focus without stealing it
    // back if the user switches applications while a launch is completing.
    @MainActor
    static func activationResult(_ app: NSRunningApplication?) -> [String: Any] {
        var result: [String: Any] = ["activation_requested": true, "requires_observation": true]
        if let app {
            result["pid"] = Int(app.processIdentifier)
            result["bundle_id"] = app.bundleIdentifier ?? ""
            result["app"] = app.localizedName ?? ""
            result["frontmost"] = NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier
        }
        return result
    }

    static func resolveAppURL(named name: String) -> URL? {
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: name) {
            return url
        }
        let suffix = name.hasSuffix(".app") ? name : "\(name).app"
        let bases = ["/Applications", "/System/Applications", "/Applications/Utilities", "/System/Applications/Utilities"]
        for base in bases {
            let path = "\(base)/\(suffix)"
            if FileManager.default.fileExists(atPath: path) {
                return URL(fileURLWithPath: path)
            }
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        let homeApps = home.appendingPathComponent("Applications").appendingPathComponent(suffix)
        if FileManager.default.fileExists(atPath: homeApps.path) {
            return homeApps
        }
        return nil
    }
}

struct CloseAppExecutor: Executor {
    let action = "close_app"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let appName = params["app"] as? String, !appName.isEmpty else {
            throw ExecutorError.missingParam("app")
        }
        let force = (params["force"] as? Bool) ?? false
        let script: String
        if force {
            script = "tell application \"\(appName)\" to quit saving no"
        } else {
            script = "tell application \"\(appName)\" to quit"
        }
        guard let appleScript = NSAppleScript(source: script) else {
            throw ExecutorError.actionFailed("could not build AppleScript")
        }
        var errorDict: NSDictionary?
        appleScript.executeAndReturnError(&errorDict)
        if let errorDict {
            let msg = (errorDict[NSAppleScript.errorMessage] as? String) ?? "AppleScript failed"
            throw ExecutorError.actionFailed(msg)
        }
        return ["closed": true]
    }
}
