import AppKit
import ApplicationServices
import Foundation

struct GetUITreeExecutor: Executor {
    let action = "get_ui_tree"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        try await UIObservationStore.shared.observe(params: params)
    }
}

struct PerformUIActionExecutor: Executor {
    let action = "perform_ui_action"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        try await UIObservationStore.shared.perform(params: params)
    }
}

// AX references never leave this process. A new observation or attempted mutation
// invalidates the previous snapshot, preventing accidental reuse across UI changes.
actor UIObservationStore {
    static let shared = UIObservationStore()

    private struct Entry {
        let element: AXUIElement
        let role: String
        let title: String
        let secure: Bool
    }

    private var entries: [String: Entry] = [:]
    private var snapshotID = ""
    private var observedAt = Date.distantPast
    private var processID: pid_t = 0

    func invalidate() {
        entries.removeAll()
        snapshotID = ""
        observedAt = .distantPast
        processID = 0
    }

    private func attribute(_ element: AXUIElement, _ name: String) -> AnyObject? {
        var value: AnyObject?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
        return value
    }

    private func string(_ element: AXUIElement, _ name: String) -> String {
        (attribute(element, name) as? String) ?? ""
    }

    private func isSecure(_ element: AXUIElement) -> Bool {
        string(element, kAXSubroleAttribute) == "AXSecureTextField"
            || string(element, kAXRoleAttribute) == "AXSecureTextField"
            || (attribute(element, "AXProtectedContent") as? Bool) == true
    }

    private func settable(_ element: AXUIElement, _ name: String) -> Bool {
        var result = DarwinBoolean(false)
        return AXUIElementIsAttributeSettable(element, name as CFString, &result) == .success && result.boolValue
    }

    private func actionNames(_ element: AXUIElement) -> [String] {
        var result: CFArray?
        guard AXUIElementCopyActionNames(element, &result) == .success else { return [] }
        return result as? [String] ?? []
    }

    private func bounds(_ element: AXUIElement) -> [String: Double]? {
        guard let position = attribute(element, kAXPositionAttribute),
              let size = attribute(element, kAXSizeAttribute),
              CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
        let positionValue = position as! AXValue
        let sizeValue = size as! AXValue
        guard AXValueGetType(positionValue) == .cgPoint, AXValueGetType(sizeValue) == .cgSize else { return nil }
        var point = CGPoint.zero
        var extent = CGSize.zero
        guard AXValueGetValue(positionValue, .cgPoint, &point), AXValueGetValue(sizeValue, .cgSize, &extent),
              point.x.isFinite, point.y.isFinite, extent.width.isFinite, extent.height.isFinite else { return nil }
        return ["x": point.x, "y": point.y, "width": extent.width, "height": extent.height]
    }

    func observe(params: [String: Any]) async throws -> [String: Any] {
        guard AccessibilityCheck.isTrusted() else {
            throw ExecutorError.permissionDenied("Enable Accessibility for Autonomous Buddy in macOS System Settings")
        }
        let maxNodes = try ExecutorParameters.integer(params, "max_nodes", default: 150, range: 1...500)
        let maxDepth = try ExecutorParameters.integer(params, "max_depth", default: 12, range: 1...30)
        if let value = params["app"], !(value is String) { throw ExecutorError.invalidParam("app") }
        let requestedApp = params["app"] as? String
        let (app, frontmostPID) = await MainActor.run { () -> (NSRunningApplication?, pid_t?) in
            let frontmost = NSWorkspace.shared.frontmostApplication
            if let requestedApp {
                return (NSWorkspace.shared.runningApplications.first {
                    $0.localizedName == requestedApp || $0.bundleIdentifier == requestedApp
                }, frontmost?.processIdentifier)
            }
            return (frontmost, frontmost?.processIdentifier)
        }
        guard let app, !app.isTerminated else { throw ExecutorError.actionFailed("app is not running or no frontmost app") }
        try Task.checkCancellation()
        entries.removeAll()
        snapshotID = UUID().uuidString
        processID = app.processIdentifier
        observedAt = Date()
        let root = AXUIElementCreateApplication(processID)
        AXUIElementSetMessagingTimeout(root, 0.2)
        guard !string(root, kAXRoleAttribute).isEmpty else {
            throw ExecutorError.actionFailed("app does not expose an accessible UI; use screenshot")
        }
        var nodes: [[String: Any]] = []
        var truncated = false
        var visited: [AXUIElement] = []
        let deadline = Date().addingTimeInterval(3)

        func visit(_ element: AXUIElement, parent: String?, depth: Int, protected: Bool) throws {
            try Task.checkCancellation()
            guard nodes.count < maxNodes, Date() < deadline else { truncated = true; return }
            guard !visited.contains(where: { CFEqual($0, element) }) else { return }
            visited.append(element)
            AXUIElementSetMessagingTimeout(element, 0.2)
            let role = string(element, kAXRoleAttribute)
            guard !role.isEmpty else { return }
            let secure = protected || isSecure(element)
            let title = secure ? "" : string(element, kAXTitleAttribute)
            let ref = "e\(nodes.count + 1)"
            entries[ref] = Entry(element: element, role: role, title: title, secure: secure)
            var actions: [String] = []
            if actionNames(element).contains(kAXPressAction) { actions.append("press") }
            if settable(element, kAXFocusedAttribute) { actions.append("focus") }
            if !secure && settable(element, kAXValueAttribute) { actions.append("set_value") }
            var node: [String: Any] = ["ref": ref, "role": role, "actions": actions, "secure": secure]
            if let parent { node["parent_ref"] = parent }
            if !secure {
                for (key, outputKey) in [(kAXTitleAttribute, "title"), (kAXDescriptionAttribute, "description"),
                                         (kAXValueAttribute, "value"), (kAXHelpAttribute, "help")] {
                    let rawValue = attribute(element, key)
                    if let value = rawValue as? String {
                        node[outputKey] = String(value.prefix(500))
                    } else if let value = rawValue as? NSNumber {
                        node[outputKey] = value
                    }
                }
            }
            if let bounds = bounds(element) { node["bounds_global_points"] = bounds }
            if let enabled = attribute(element, kAXEnabledAttribute) as? Bool { node["enabled"] = enabled }
            if let focused = attribute(element, kAXFocusedAttribute) as? Bool { node["focused"] = focused }
            nodes.append(node)
            var count: CFIndex = 0
            guard AXUIElementGetAttributeValueCount(element, kAXChildrenAttribute as CFString, &count) == .success, count > 0 else { return }
            guard depth < maxDepth, nodes.count < maxNodes, Date() < deadline else { truncated = true; return }
            let allowed = min(count, maxNodes - nodes.count)
            if allowed < count { truncated = true }
            var children: CFArray?
            guard AXUIElementCopyAttributeValues(element, kAXChildrenAttribute as CFString, 0, allowed, &children) == .success,
                  let children = children as? [AXUIElement] else { return }
            for child in children {
                if nodes.count >= maxNodes || Date() >= deadline { truncated = true; break }
                try visit(child, parent: ref, depth: depth + 1, protected: secure)
            }
        }
        try visit(root, parent: nil, depth: 0, protected: false)
        return ["snapshot_id": snapshotID, "app": app.localizedName ?? "", "bundle_id": app.bundleIdentifier ?? "",
                "pid": Int(processID), "frontmost": frontmostPID == processID, "nodes": nodes,
                "truncated": truncated, "expires_in_ms": max(0, 30_000 - Int(Date().timeIntervalSince(observedAt) * 1000)),
                "hint": "References expire after 30 seconds, a new observation, or an attempted action. Observe again after acting. Use screenshot for missing UI."]
    }

    static func referenceIsCurrent(requested: String, snapshot: String, age: TimeInterval,
                                   observedPID: pid_t, frontmostPID: pid_t?) -> Bool {
        !snapshot.isEmpty && requested == snapshot && age >= 0 && age < 30
            && observedPID > 0 && frontmostPID == observedPID
    }

    func perform(params: [String: Any]) async throws -> [String: Any] {
        guard let requestedSnapshot = params["snapshot_id"] as? String else { throw ExecutorError.missingParam("snapshot_id") }
        guard let ref = params["ref"] as? String else { throw ExecutorError.missingParam("ref") }
        guard let action = params["ui_action"] as? String else { throw ExecutorError.missingParam("ui_action") }
        guard ["press", "focus", "set_value"].contains(action) else { throw ExecutorError.invalidParam("ui_action") }
        guard AccessibilityCheck.isTrusted() else { throw ExecutorError.permissionDenied("Accessibility access required") }
        let frontmostPID = await MainActor.run { NSWorkspace.shared.frontmostApplication?.processIdentifier }
        try Task.checkCancellation()
        guard Self.referenceIsCurrent(requested: requestedSnapshot, snapshot: snapshotID,
                                      age: Date().timeIntervalSince(observedAt), observedPID: processID, frontmostPID: frontmostPID),
              let entry = entries[ref] else {
            throw ExecutorError.actionFailed("stale UI reference or app is not frontmost; get_ui_tree again after activating the intended app")
        }
        entries.removeAll()
        snapshotID = ""
        try Task.checkCancellation()
        var currentPID: pid_t = 0
        guard AXUIElementGetPid(entry.element, &currentPID) == .success, currentPID == processID,
              string(entry.element, kAXRoleAttribute) == entry.role,
              (entry.secure || string(entry.element, kAXTitleAttribute) == entry.title) else {
            throw ExecutorError.actionFailed("UI element changed; get_ui_tree again")
        }
        if (attribute(entry.element, kAXEnabledAttribute) as? Bool) == false {
            throw ExecutorError.actionFailed("UI element is disabled")
        }
        let status: AXError
        switch action {
        case "press":
            guard actionNames(entry.element).contains(kAXPressAction) else { throw ExecutorError.actionFailed("element does not support press") }
            try Task.checkCancellation()
            status = AXUIElementPerformAction(entry.element, kAXPressAction as CFString)
        case "focus":
            guard settable(entry.element, kAXFocusedAttribute) else { throw ExecutorError.actionFailed("element does not support focus") }
            try Task.checkCancellation()
            status = AXUIElementSetAttributeValue(entry.element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        default:
            guard !entry.secure, !isSecure(entry.element) else { throw ExecutorError.permissionDenied("secure field values cannot be set through UI references") }
            guard let value = params["value"] as? String, value.count <= 20_000 else { throw ExecutorError.invalidParam("value must be a string of at most 20000 characters") }
            guard settable(entry.element, kAXValueAttribute) else { throw ExecutorError.actionFailed("element does not support set_value") }
            try Task.checkCancellation()
            status = AXUIElementSetAttributeValue(entry.element, kAXValueAttribute as CFString, value as CFString)
        }
        guard status == .success else { throw ExecutorError.actionFailed("Accessibility action failed (status=\(status.rawValue)); observe again") }
        return ["performed": true, "ui_action": action, "ref": ref, "requires_observation": true]
    }
}
