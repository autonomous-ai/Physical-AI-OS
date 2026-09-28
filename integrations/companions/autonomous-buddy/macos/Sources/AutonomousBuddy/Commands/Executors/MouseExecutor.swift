import AppKit
import CoreGraphics
import Foundation

// All x/y are CGEvent global display points (top-left origin), not pixels.
// Use screenshot.image_to_global_points to convert from screenshot pixels.

struct ClickAtExecutor: Executor {
    let action = "click_at"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        let x = try requireCGFloat(params, key: "x")
        let y = try requireCGFloat(params, key: "y")
        let buttonName = (params["button"] as? String) ?? "left"
        let clicks = try ExecutorParameters.integer(params, "clicks", default: 1, range: 1...3)
        guard ["left", "right", "middle"].contains(buttonName.lowercased()) else {
            throw ExecutorError.invalidParam("button")
        }
        try Task.checkCancellation()

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for mouse click")
        }

        let (button, downType, upType): (CGMouseButton, CGEventType, CGEventType)
        switch buttonName.lowercased() {
        case "right":  (button, downType, upType) = (.right,  .rightMouseDown, .rightMouseUp)
        case "middle": (button, downType, upType) = (.center, .otherMouseDown, .otherMouseUp)
        default:       (button, downType, upType) = (.left,   .leftMouseDown,  .leftMouseUp)
        }

        let pt = CGPoint(x: x, y: y)
        for n in 0..<clicks {
            try Task.checkCancellation()
            guard let down = CGEvent(mouseEventSource: nil, mouseType: downType, mouseCursorPosition: pt, mouseButton: button),
                  let up = CGEvent(mouseEventSource: nil, mouseType: upType, mouseCursorPosition: pt, mouseButton: button) else {
                throw ExecutorError.actionFailed("could not create mouse event")
            }
            down.setIntegerValueField(.mouseEventClickState, value: Int64(n + 1))
            up.setIntegerValueField(.mouseEventClickState, value: Int64(n + 1))
            down.post(tap: .cghidEventTap)
            up.post(tap: .cghidEventTap)
            if n < clicks - 1 {
                try await Task.sleep(nanoseconds: 60_000_000)
            }
        }
        return ["clicked": true, "x": Int(x), "y": Int(y), "button": buttonName, "clicks": clicks]
    }
}

struct ScrollExecutor: Executor {
    let action = "scroll"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        let dy = try ExecutorParameters.integer(params, "delta_y", default: 0, range: -100_000...100_000)
        let dx = try ExecutorParameters.integer(params, "delta_x", default: 0, range: -100_000...100_000)
        let point: CGPoint?
        if params["x"] != nil || params["y"] != nil {
            point = CGPoint(x: try requireCGFloat(params, key: "x"), y: try requireCGFloat(params, key: "y"))
        } else {
            point = nil
        }
        try Task.checkCancellation()

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for scroll")
        }

        if let point {
            guard let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
                                     mouseCursorPosition: point, mouseButton: .left) else {
                throw ExecutorError.actionFailed("could not create mouse event")
            }
            move.post(tap: .cghidEventTap)
        }

        guard let scroll = CGEvent(
            scrollWheelEvent2Source: nil,
            units: .pixel,
            wheelCount: 2,
            wheel1: Int32(dy),
            wheel2: Int32(dx),
            wheel3: 0
        ) else {
            throw ExecutorError.actionFailed("could not create scroll event")
        }
        scroll.post(tap: .cghidEventTap)
        return ["scrolled": true, "delta_y": dy, "delta_x": dx]
    }
}

struct MouseMoveExecutor: Executor {
    let action = "mouse_move"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        let x = try requireCGFloat(params, key: "x")
        let y = try requireCGFloat(params, key: "y")
        let smooth = (params["smooth"] as? Bool) ?? false
        try Task.checkCancellation()

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for mouse move")
        }

        if smooth {
            guard let cursor = CGEvent(source: nil) else {
                throw ExecutorError.actionFailed("could not read cursor position")
            }
            let from = cursor.location
            let to = CGPoint(x: x, y: y)
            let steps = 24
            for i in 1...steps {
                try Task.checkCancellation()
                let t = CGFloat(i) / CGFloat(steps)
                let pt = CGPoint(x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t)
                guard let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
                                         mouseCursorPosition: pt, mouseButton: .left) else {
                    throw ExecutorError.actionFailed("could not create mouse event")
                }
                move.post(tap: .cghidEventTap)
                try await Task.sleep(nanoseconds: 8_000_000)
            }
        } else {
            guard let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
                                     mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left) else {
                throw ExecutorError.actionFailed("could not create mouse event")
            }
            move.post(tap: .cghidEventTap)
        }
        return ["moved": true, "x": Int(x), "y": Int(y), "smooth": smooth]
    }
}

struct DragExecutor: Executor {
    let action = "drag"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let from = params["from"] as? [String: Any],
              let to = params["to"] as? [String: Any] else {
            throw ExecutorError.missingParam("from/to")
        }
        let x1 = try requireCGFloat(from, key: "x")
        let y1 = try requireCGFloat(from, key: "y")
        let x2 = try requireCGFloat(to, key: "x")
        let y2 = try requireCGFloat(to, key: "y")
        let durationMs = try ExecutorParameters.integer(params, "duration_ms", default: 300, range: 50...10_000)
        try Task.checkCancellation()

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for drag")
        }

        let start = CGPoint(x: x1, y: y1)
        let end = CGPoint(x: x2, y: y2)

        guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: start, mouseButton: .left) else {
            throw ExecutorError.actionFailed("could not create mouse down")
        }
        guard let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                               mouseCursorPosition: start, mouseButton: .left) else {
            throw ExecutorError.actionFailed("could not create mouse up")
        }
        var lastPoint = start
        down.post(tap: .cghidEventTap)
        // Release at the last delivered position even when cancellation interrupts a sleep.
        defer {
            up.location = lastPoint
            up.post(tap: .cghidEventTap)
        }

        let steps = max(1, durationMs / 16)
        let stepNs = UInt64(durationMs * 1_000_000 / steps)
        for i in 1...steps {
            try Task.checkCancellation()
            let t = CGFloat(i) / CGFloat(steps)
            let pt = CGPoint(x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t)
            guard let drag = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged, mouseCursorPosition: pt, mouseButton: .left) else {
                throw ExecutorError.actionFailed("could not create drag event")
            }
            drag.post(tap: .cghidEventTap)
            lastPoint = pt
            try await Task.sleep(nanoseconds: stepNs)
        }

        return ["dragged": true, "from": ["x": Int(x1), "y": Int(y1)], "to": ["x": Int(x2), "y": Int(y2)]]
    }
}

struct CursorPosExecutor: Executor {
    let action = "cursor_pos"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        // NSEvent is bottom-left origin, CGEvent top-left: flip y around the primary
        // (menu-bar) screen height, not NSScreen.main, which follows focus.
        return await MainActor.run {
            let pt = NSEvent.mouseLocation
            let screens = NSScreen.screens
            let primary = screens.first(where: { $0.frame.origin == .zero }) ?? screens.first
            let screenH = primary?.frame.size.height ?? 0
            let scale = primary?.backingScaleFactor ?? 1.0
            let cgY = screenH - pt.y
            return [
                "x": Int(pt.x.rounded()),
                "y": Int(cgY.rounded()),
                "screen_height": Int(screenH),
                "backing_scale": Double(scale),
            ]
        }
    }
}

// MARK: - helpers

private func requireCGFloat(_ params: [String: Any], key: String) throws -> CGFloat {
    CGFloat(try ExecutorParameters.number(params, key, range: -1_000_000...1_000_000))
}
