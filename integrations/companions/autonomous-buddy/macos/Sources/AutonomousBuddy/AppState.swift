import Foundation

extension Notification.Name {
    // Posted on the main queue whenever any AppState field changes.
    static let autonomousBuddyAppStateChanged = Notification.Name("autonomousBuddyAppStateChanged")
}

enum PairingStatus: Equatable {
    case notPaired
    case paired(buddyID: String, deviceHost: String)
}

enum ConnectionStatus: Equatable {
    case disconnected
    case connecting
    case connected
    case error(String)
}

struct CommandRecord {
    let id: String
    let action: String
    let summary: String
    let ok: Bool
    let error: String?
    let timestamp: Date
}

final class AppState {
    static let shared = AppState()

    // In-memory ring buffer cap; the full audit trail lives on disk (AuditLog).
    static let recentCommandsCap = 100

    private(set) var pairing: PairingStatus = .notPaired { didSet { notify() } }
    private(set) var connection: ConnectionStatus = .disconnected { didSet { notify() } }
    private(set) var discoveredDevices: [DeviceInfo] = [] { didSet { notify() } }
    private(set) var paused: Bool = false { didSet { notify() } }
    private(set) var recentCommands: [CommandRecord] = [] { didSet { notify() } }

    var lastCommand: CommandRecord? { recentCommands.first }

    var onChange: (() -> Void)?

    private init() {}

    func setPairing(_ status: PairingStatus) { onMain { self.pairing = status } }
    func setConnection(_ status: ConnectionStatus) { onMain { self.connection = status } }
    func setDiscoveredDevices(_ devices: [DeviceInfo]) { onMain { self.discoveredDevices = devices } }
    func setPaused(_ paused: Bool) { onMain { self.paused = paused } }
    func recordCommand(_ record: CommandRecord) {
        onMain {
            var list = self.recentCommands
            list.insert(record, at: 0)
            if list.count > Self.recentCommandsCap {
                list.removeLast(list.count - Self.recentCommandsCap)
            }
            self.recentCommands = list
        }
    }

    private func notify() {
        // Setters hop to main first, so onChange always fires on main.
        onChange?()
        NotificationCenter.default.post(name: .autonomousBuddyAppStateChanged, object: nil)
    }

    private func onMain(_ block: @escaping () -> Void) {
        if Thread.isMainThread { block() }
        else { DispatchQueue.main.async(execute: block) }
    }
}
