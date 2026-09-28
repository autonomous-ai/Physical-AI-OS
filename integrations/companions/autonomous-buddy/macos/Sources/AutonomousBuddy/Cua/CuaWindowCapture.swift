import AppKit
import Foundation
import ImageIO

// Capture only the explicitly identified window; never guess a display on failure.
enum CuaWindowCapture {
    typealias Call = (String, [String: Any]) async throws -> [String: Any]
    struct Capture {
        let image: CGImage
        let bounds: CGRect
        let pid: Int
        let windowID: Int
        let backingScale: Double
    }

    static func targetApp(_ params: [String: Any]) throws -> String? {
        guard let raw = params["app"] else {
            guard params["window_id"] == nil else { throw ExecutorError.invalidParam("window_id requires app") }
            return nil
        }
        guard let app = raw as? String, !app.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              app.count <= 256, params["display_id"] == nil else {
            throw ExecutorError.invalidParam("app must be nonempty and cannot be combined with display_id")
        }
        if params["window_id"] != nil {
            _ = try ExecutorParameters.integer(params, "window_id", range: 1...Int(UInt32.max))
        }
        return app
    }

    static func capture(app: String, params: [String: Any]) async throws -> Capture {
        let pids = await MainActor.run {
            NSWorkspace.shared.runningApplications.filter {
                $0.bundleIdentifier == app || $0.localizedName == app
            }.map { Int($0.processIdentifier) }
        }
        guard pids.count == 1 else {
            throw ExecutorError.actionFailed("target app is not running or is ambiguous")
        }
        return try await capture(pid: pids[0], params: params) {
            try await CuaClient.shared.call(tool: $0, arguments: $1)
        }
    }

    static func capture(pid: Int, params: [String: Any], call: Call) async throws -> Capture {
        try Task.checkCancellation()
        let listed = try await call("list_windows", ["pid": pid])
        guard let windows = listed["windows"] as? [[String: Any]] else {
            throw ExecutorError.actionFailed("Cua returned invalid windows")
        }
        let window: Int
        if params["window_id"] != nil {
            window = try ExecutorParameters.integer(params, "window_id", range: 1...Int(UInt32.max))
            guard windows.contains(where: { ($0["window_id"] as? Int) == window }) else {
                throw ExecutorError.actionFailed("target window does not belong to app or no longer exists")
            }
        } else {
            // Match Cua observation's window policy: ignore untitled menu-bar
            // surfaces when a titled application window is available.
            let titled = windows.filter { !(($0["title"] as? String) ?? "").isEmpty }
            let candidates = titled.isEmpty ? windows : titled
            guard candidates.count == 1 else {
                throw ExecutorError.actionFailed("app has zero or multiple candidate windows; observe and pass window_id")
            }
            window = try ExecutorParameters.integer(candidates[0], "window_id", range: 1...Int(UInt32.max))
        }
        try Task.checkCancellation()
        let response = try await call("get_window_state", ["pid": pid, "window_id": window,
            "include_screenshot": true, "include_accessibility_tree": false])
        try Task.checkCancellation()
        return try decode(response, pid: pid, window: window)
    }

    static func decode(_ response: [String: Any], pid: Int, window: Int) throws -> Capture {
        guard response["pid"] as? Int == pid, response["window_id"] as? Int == window,
              response["screenshot_frame_valid"] as? Bool == true,
              let bounds = response["window_bounds"] as? [String: Any],
              let images = response["_cua_images"] as? [[String: Any]], images.count == 1,
              images[0]["mimeType"] as? String == "image/png",
              let encoded = images[0]["data"] as? String, let data = Data(base64Encoded: encoded),
              let source = CGImageSourceCreateWithData(data as CFData, nil),
              CGImageSourceGetType(source) as String? == "public.png",
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width > 0, height > 0, width <= 16384, height <= 16384, width * height <= 40_000_000,
              response["screenshot_width"] as? Int == width,
              response["screenshot_height"] as? Int == height,
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw ExecutorError.actionFailed("Cua window screenshot missing or frame unverified")
        }
        let x = try ExecutorParameters.number(bounds, "x", range: -1_000_000...1_000_000)
        let y = try ExecutorParameters.number(bounds, "y", range: -1_000_000...1_000_000)
        let w = try ExecutorParameters.number(bounds, "width", range: 0.01...1_000_000)
        let h = try ExecutorParameters.number(bounds, "height", range: 0.01...1_000_000)
        let scale = try ExecutorParameters.number(response, "screenshot_scale", range: 0.000001...4)
        // Cua reports native backing scale even when its configured image ceiling
        // downsizes the PNG. Verify aspect ratio and derive transforms from the
        // actual encoded dimensions rather than using that native scale directly.
        guard Double(width) <= w * scale + 2, Double(height) <= h * scale + 2,
              abs(Double(width) / w - Double(height) / h) <= max(2 / w, 2 / h) else {
            throw ExecutorError.actionFailed("Cua screenshot dimensions disagree with verified frame")
        }
        return Capture(image: image, bounds: CGRect(x: x, y: y, width: w, height: h), pid: pid, windowID: window, backingScale: scale)
    }
}
