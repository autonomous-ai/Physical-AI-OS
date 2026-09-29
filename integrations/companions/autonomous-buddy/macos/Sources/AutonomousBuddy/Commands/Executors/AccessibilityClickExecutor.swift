import AppKit
import ApplicationServices
import Foundation

// click_button: find a UI element by label/role via Accessibility and AXPress it.
// Reliable for native apps; browser web-content coverage is inconsistent.

struct ClickButtonExecutor: Executor {
    let action = "click_button"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let label = (params["label"] as? String)?.trimmingCharacters(in: .whitespaces),
              !label.isEmpty else {
            throw ExecutorError.missingParam("label")
        }
        let appName = params["app"] as? String
        let role = (params["role"] as? String) ?? (kAXButtonRole as String)
        let maxDepth = try ExecutorParameters.integer(params, "max_depth", default: 12, range: 1...30)
        let maxNodes = try ExecutorParameters.integer(params, "max_nodes", default: 500, range: 1...500)
        try Task.checkCancellation()

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for click_button")
        }

        let apps = await MainActor.run { () -> [NSRunningApplication] in
            let workspace = NSWorkspace.shared
            if let appName {
                return workspace.runningApplications.filter { app in
                    app.localizedName == appName
                        || app.bundleIdentifier == appName
                        || (app.bundleURL?.lastPathComponent == "\(appName).app")
                }
            }
            return workspace.frontmostApplication.map { [$0] } ?? []
        }
        guard !apps.isEmpty else {
            throw ExecutorError.actionFailed(appName.map { "app not running: \($0)" } ?? "no frontmost app — specify `app`")
        }
        let deadline = Date().addingTimeInterval(3)
        var remainingNodes = maxNodes

        for app in apps {
            try Task.checkCancellation()
            let axApp = AXUIElementCreateApplication(app.processIdentifier)
            if let element = try findElement(in: axApp, label: label, role: role, maxDepth: maxDepth,
                                             remainingNodes: &remainingNodes, deadline: deadline) {
                try Task.checkCancellation()
                guard Date() < deadline else { throw ExecutorError.actionFailed("UI search budget exhausted; observe again") }
                let result = AXUIElementPerformAction(element, kAXPressAction as CFString)
                if result == .success {
                    return [
                        "clicked": true,
                        "app": app.localizedName ?? app.bundleIdentifier ?? "?",
                        "label": label,
                        "role": role,
                    ]
                }
                throw ExecutorError.actionFailed("AXPress failed (status=\(result.rawValue))")
            }
        }
        throw ExecutorError.actionFailed("element not found: label=\(label) role=\(role)")
    }

    private func findElement(in root: AXUIElement, label: String, role: String, maxDepth: Int,
                             remainingNodes: inout Int, deadline: Date) throws -> AXUIElement? {
        var stack: [(AXUIElement, Int)] = [(root, 0)]
        var visited: [AXUIElement] = []
        while let (element, depth) = stack.popLast() {
            try Task.checkCancellation()
            guard remainingNodes > 0, Date() < deadline else {
                throw ExecutorError.actionFailed("UI search budget exhausted; use get_ui_tree or screenshot")
            }
            guard !visited.contains(where: { CFEqual($0, element) }) else { continue }
            visited.append(element)
            remainingNodes -= 1
            AXUIElementSetMessagingTimeout(element, 0.2)
            if depth > 0 {
                var roleRef: AnyObject?
                AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &roleRef)
                if let elementRole = roleRef as? String, elementRole == role,
                   try matchLabel(element: element, target: label, deadline: deadline) {
                    return element
                }
            }
            guard depth < maxDepth else { continue }
            try Task.checkCancellation()
            guard Date() < deadline else { throw ExecutorError.actionFailed("UI search budget exhausted") }
            var count: CFIndex = 0
            guard AXUIElementGetAttributeValueCount(element, kAXChildrenAttribute as CFString, &count) == .success,
                  count > 0 else { continue }
            let allowed = min(count, max(0, remainingNodes - stack.count))
            guard allowed > 0 else { continue }
            var childrenRef: CFArray?
            guard AXUIElementCopyAttributeValues(element, kAXChildrenAttribute as CFString, 0, allowed, &childrenRef) == .success,
                  let children = childrenRef as? [AXUIElement] else { continue }
            stack.append(contentsOf: children.reversed().map { ($0, depth + 1) })
        }
        return nil
    }

    private func matchLabel(element: AXUIElement, target: String, deadline: Date) throws -> Bool {
        let attrs: [CFString] = [
            kAXTitleAttribute as CFString,
            kAXDescriptionAttribute as CFString,
            kAXValueAttribute as CFString,
            "AXLabel" as CFString,
        ]
        for attr in attrs {
            try Task.checkCancellation()
            guard Date() < deadline else { throw ExecutorError.actionFailed("UI search budget exhausted") }
            var ref: AnyObject?
            AXUIElementCopyAttributeValue(element, attr, &ref)
            if let str = ref as? String,
               str.localizedCaseInsensitiveCompare(target) == .orderedSame {
                return true
            }
        }
        return false
    }
}
