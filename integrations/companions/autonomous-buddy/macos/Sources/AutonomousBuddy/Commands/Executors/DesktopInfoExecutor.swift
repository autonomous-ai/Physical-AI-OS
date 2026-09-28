import AppKit
import Foundation

// A read-only preflight never prompts for permissions or changes focus.
struct DesktopInfoExecutor: Executor {
    let action = "desktop_info"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        try Task.checkCancellation()
        return await MainActor.run {
            let workspace = NSWorkspace.shared
            func info(_ app: NSRunningApplication) -> [String: Any] {
                ["pid": Int(app.processIdentifier), "name": app.localizedName ?? "",
                 "bundle_id": app.bundleIdentifier ?? "", "active": app.isActive]
            }
            let apps = workspace.runningApplications.filter { $0.activationPolicy == .regular }
            var result: [String: Any] = [
                "protocol_version": 2,
                "cua": ["installed": CuaClient.available, "enabled": CuaClient.enabled,
                        "required_version": CuaClient.supportedVersion],
                "paused": AppState.shared.paused,
                "accessibility": AccessibilityCheck.isTrusted(),
                "screen_recording": ScreenRecordingCheck.isTrusted(),
                "apps": Array(apps.prefix(100)).map(info),
                "apps_truncated": apps.count > 100,
                "capabilities": ["get_ui_tree", "perform_ui_action", "screenshot", "cancel_command", "open_path", "target_app_input"],
            ]
            if CuaClient.available && CuaClient.enabled {
                result["capabilities"] = (result["capabilities"] as? [String] ?? []) + ["cua_observe", "cua_action"]
            }
            if let frontmost = workspace.frontmostApplication { result["frontmost_app"] = info(frontmost) }
            return result
        }
    }
}
