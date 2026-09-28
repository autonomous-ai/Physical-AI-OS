import AppKit
import Foundation

struct CuaObserveExecutor: Executor {
    let action = "cua_observe"
    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard CuaClient.enabled else { throw ExecutorError.actionFailed("Cua is disabled") }
        return try await CuaObservationStore.shared.observe(params: params)
    }
}

struct CuaActionExecutor: Executor {
    let action = "cua_action"
    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard CuaClient.enabled else { throw ExecutorError.actionFailed("Cua is disabled") }
        return try await CuaObservationStore.shared.perform(params: params)
    }
}

// Keep Cua's window-scoped tokens separate from Buddy's native AX references.
// An attempted mutation consumes the snapshot even when its outcome is uncertain.
actor CuaObservationStore {
    typealias Call = (String, [String: Any]) async throws -> [String: Any]
    static let shared = CuaObservationStore { tool, arguments in
        try await CuaClient.shared.call(tool: tool, arguments: arguments)
    }
    private let call: Call
    private var generation = UUID()
    private var snapshot: Snapshot?
    private struct Snapshot {
        let id: String
        let pid: Int
        let window: Int
        let observed: Date
        let tokens: Set<String>
        let actions: [String: Set<String>]
    }

    init(call: @escaping Call) { self.call = call }
    func invalidate() { generation = UUID(); snapshot = nil }

    func observe(params: [String: Any]) async throws -> [String: Any] {
        guard Set(params.keys).isSubset(of: ["app", "window_id", "max_nodes", "max_depth"]) else {
            throw ExecutorError.invalidParam("cua_observe accepts app, window_id, max_nodes, max_depth")
        }
        if let app = params["app"], !(app is String) { throw ExecutorError.invalidParam("app") }
        let requested = params["app"] as? String
        let pid = await MainActor.run { () -> Int? in
            let workspace = NSWorkspace.shared
            let app = requested.map { name in workspace.runningApplications.first {
                $0.bundleIdentifier == name || $0.localizedName == name
            }} ?? workspace.frontmostApplication
            return app.map { Int($0.processIdentifier) }
        }
        guard let pid else { throw ExecutorError.actionFailed("target app is not running") }
        return try await observe(pid: pid, params: params)
    }

    // Separate targeting from transport so stale/cancelled snapshot behavior is testable.
    func observe(pid: Int, params: [String: Any]) async throws -> [String: Any] {
        invalidate()
        let current = generation
        let maxNodes = try ExecutorParameters.integer(params, "max_nodes", default: 150, range: 1...500)
        let maxDepth = try ExecutorParameters.integer(params, "max_depth", default: 12, range: 1...30)
        try Task.checkCancellation()
        let listed = try await call("list_windows", ["pid": pid])
        guard let windows = listed["windows"] as? [[String: Any]] else {
            throw ExecutorError.actionFailed("Cua returned invalid windows")
        }
        let window: Int
        if params["window_id"] != nil {
            window = try ExecutorParameters.integer(params, "window_id", range: 1...Int.max)
            guard windows.contains(where: { ($0["window_id"] as? Int) == window }) else {
                throw ExecutorError.actionFailed("Cua target window no longer exists")
            }
        } else {
            let titled = windows.filter { !(($0["title"] as? String) ?? "").isEmpty }
            let candidates = titled.isEmpty ? windows : titled
            guard candidates.count == 1, let id = candidates[0]["window_id"] as? Int else {
                try Task.checkCancellation()
                return ["backend": "cua", "pid": pid, "windows": windows,
                        "requires_window_selection": true]
            }
            window = id
        }
        try Task.checkCancellation()
        guard generation == current else { throw CancellationError() }
        var result = try await call("get_window_state", ["pid": pid, "window_id": window,
            "include_screenshot": false, "max_elements": maxNodes, "max_depth": maxDepth])
        try Task.checkCancellation()
        guard generation == current, let upstream = result["snapshot_id"] as? String,
              let elements = result["elements"] as? [[String: Any]],
              (result["pid"] as? Int) == pid, (result["window_id"] as? Int) == window else {
            throw ExecutorError.actionFailed("Cua observation invalidated or malformed; observe again")
        }
        let tokens = Set(elements.compactMap { $0["element_token"] as? String })
        guard tokens.allSatisfy({ $0.hasPrefix(upstream + ":") }) else {
            throw ExecutorError.actionFailed("Cua returned inconsistent element tokens")
        }
        let id = "cua-" + UUID().uuidString
        var actions: [String: Set<String>] = [:]
        for element in elements {
            if let token = element["element_token"] as? String {
                actions[token, default: []].formUnion(element["actions"] as? [String] ?? [])
            }
        }
        snapshot = Snapshot(id: id, pid: pid, window: window, observed: Date(), tokens: tokens, actions: actions)
        result["cua_snapshot_id"] = upstream
        result["snapshot_id"] = id
        result["backend"] = "cua"
        result["expires_in_ms"] = 30_000
        result["requires_observation_after_action"] = true
        return result
    }

    func perform(params: [String: Any]) async throws -> [String: Any] {
        guard let id = params["snapshot_id"] as? String,
              let token = params["element_token"] as? String,
              let action = params["ui_action"] as? String else {
            throw ExecutorError.missingParam("snapshot_id, element_token, ui_action")
        }
        guard let saved = snapshot, saved.id == id, saved.tokens.contains(token),
              Date().timeIntervalSince(saved.observed) >= 0,
              Date().timeIntervalSince(saved.observed) < 30 else {
            throw ExecutorError.actionFailed("stale Cua reference; cua_observe again")
        }
        var args: [String: Any] = ["pid": saved.pid, "window_id": saved.window,
                                  "element_token": token, "delivery_mode": "background"]
        let allowed: Set<String>
        switch action {
        case "click":
            allowed = ["snapshot_id", "element_token", "ui_action", "ax_action"]
            if let raw = params["ax_action"] {
                let names = ["press": "AXPress", "show_menu": "AXShowMenu", "pick": "AXPick",
                             "confirm": "AXConfirm", "cancel": "AXCancel", "open": "AXOpen"]
                guard let requested = raw as? String, let axName = names[requested],
                      saved.actions[token]?.contains(axName) == true else {
                    throw ExecutorError.invalidParam("ax_action must name an action advertised by the observed element")
                }
                args["action"] = requested
            }
        case "type_text":
            allowed = ["snapshot_id", "element_token", "ui_action", "text"]
            guard let text = params["text"] as? String, !text.isEmpty, text.count <= 20_000 else {
                throw ExecutorError.invalidParam("text must contain 1–20000 characters")
            }
            args["text"] = text
        case "press_key":
            allowed = ["snapshot_id", "element_token", "ui_action", "key", "modifiers", "delivery_mode"]
            if let raw = params["delivery_mode"] {
                guard let mode = raw as? String, ["background", "foreground"].contains(mode) else {
                    throw ExecutorError.invalidParam("delivery_mode must be background or foreground")
                }
                // Foreground is an explicit single-call escalation. The driver
                // targets this snapshot's exact window and restores prior focus.
                args["delivery_mode"] = mode
            }
            guard let key = params["key"] as? String, !key.isEmpty, key.count <= 32 else {
                throw ExecutorError.invalidParam("key")
            }
            args["key"] = key
            if let raw = params["modifiers"] {
                guard let modifiers = raw as? [String], modifiers.count <= 5,
                      modifiers.allSatisfy({ ["cmd", "shift", "option", "alt", "ctrl", "fn"].contains($0) }) else {
                    throw ExecutorError.invalidParam("modifiers")
                }
                args["modifiers"] = modifiers
            }
        default: throw ExecutorError.invalidParam("ui_action must be click, type_text or press_key")
        }
        guard Set(params.keys).isSubset(of: allowed) else { throw ExecutorError.invalidParam("unexpected Cua action parameter") }
        invalidate()
        try Task.checkCancellation()
        var result = try await call(action, args)
        try Task.checkCancellation()
        result["backend"] = "cua"
        result["requires_observation"] = true
        return result
    }
}
