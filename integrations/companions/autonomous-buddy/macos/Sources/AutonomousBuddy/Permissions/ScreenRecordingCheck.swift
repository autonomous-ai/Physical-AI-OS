import CoreGraphics
import Foundation

enum ScreenRecordingCheck {
    static func isTrusted() -> Bool {
        return CGPreflightScreenCaptureAccess()
    }

    /// Triggers the system prompt the first time it's called.
    /// After one denial, only a manual grant in System Settings works.
    @discardableResult
    static func requestPrompt() -> Bool {
        return CGRequestScreenCaptureAccess()
    }
}
