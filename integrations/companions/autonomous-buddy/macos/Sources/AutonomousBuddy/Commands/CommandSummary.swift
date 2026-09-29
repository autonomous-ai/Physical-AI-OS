import Foundation

// One-line human-readable command description for the Activity window and audit log.
enum CommandSummary {
    static func describe(action: String, params: [String: Any]) -> String {
        switch action {
        case "open_url":
            let url = (params["url"] as? String) ?? ""
            if let browser = (params["browser"] as? String)?.lowercased(), !browser.isEmpty {
                return "open_url (\(browser)): \(truncate(url, 80))"
            }
            return "open_url: \(truncate(url, 80))"

        case "open_app":
            let app = (params["app"] as? String) ?? "?"
            return "open_app: \(app)"

        case "close_app":
            let app = (params["app"] as? String) ?? "?"
            let force = (params["force"] as? Bool) == true
            return force ? "close_app (force): \(app)" : "close_app: \(app)"

        case "click_at":
            let x = numberString(params["x"])
            let y = numberString(params["y"])
            let button = (params["button"] as? String) ?? "left"
            let clicks = (params["clicks"] as? Int) ?? 1
            let suffix = clicks > 1 ? " ×\(clicks)" : ""
            return button == "left" ? "click_at: (\(x), \(y))\(suffix)" : "click_at (\(button)): (\(x), \(y))\(suffix)"

        case "click_button":
            let label = (params["label"] as? String) ?? "?"
            if let app = (params["app"] as? String), !app.isEmpty {
                return "click_button [\(app)]: \"\(truncate(label, 60))\""
            }
            return "click_button: \"\(truncate(label, 60))\""

        case "type_text":
            let text = (params["text"] as? String) ?? ""
            return "type_text: \"\(truncate(text, 60))\""

        case "key_combo":
            let keys = (params["keys"] as? [String]) ?? []
            return "key_combo: \(keys.joined(separator: "+"))"

        case "scroll":
            let dy = (params["delta_y"] as? Int) ?? 0
            let dx = (params["delta_x"] as? Int) ?? 0
            if dx != 0 && dy != 0 { return "scroll: dx=\(dx) dy=\(dy)" }
            if dx != 0 { return "scroll: dx=\(dx)" }
            return "scroll: dy=\(dy)"

        case "mouse_move":
            let x = numberString(params["x"])
            let y = numberString(params["y"])
            return "mouse_move: (\(x), \(y))"

        case "drag":
            let from = params["from"] as? [String: Any] ?? [:]
            let to = params["to"] as? [String: Any] ?? [:]
            return "drag: (\(numberString(from["x"])), \(numberString(from["y"]))) → (\(numberString(to["x"])), \(numberString(to["y"])))"

        case "screenshot":
            let scale = (params["scale"] as? Double) ?? 1.0
            let fmt = (params["return_format"] as? String) ?? "path"
            return "screenshot: scale=\(stripTrailingZero(scale)) fmt=\(fmt)"

        case "notification":
            let title = (params["title"] as? String) ?? ""
            return "notification: \"\(truncate(title, 60))\""

        case "write_clipboard":
            let text = (params["text"] as? String) ?? ""
            return "write_clipboard: \"\(truncate(text, 60))\""

        case "ping", "read_clipboard", "cursor_pos", "list_displays":
            return action

        default:
            return action
        }
    }

    private static func truncate(_ s: String, _ max: Int) -> String {
        guard s.count > max else { return s }
        let end = s.index(s.startIndex, offsetBy: max)
        return String(s[..<end]) + "…"
    }

    private static func numberString(_ v: Any?) -> String {
        if let i = v as? Int { return "\(i)" }
        if let d = v as? Double { return stripTrailingZero(d) }
        return "?"
    }

    private static func stripTrailingZero(_ d: Double) -> String {
        guard d.isFinite else { return "invalid" }
        if let integer = Int(exactly: d) { return "\(integer)" }
        return String(format: "%.2f", d)
    }
}
