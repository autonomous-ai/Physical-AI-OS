import Cocoa

final class MenuBarController: NSObject, NSMenuItemValidation {
    private let statusItem: NSStatusItem
    private let menu = NSMenu()

    private let onPair: (String?) -> Void
    private let onUnpair: () -> Void
    private let onTogglePause: (Bool) -> Void
    private let onShowActivity: () -> Void
    private let onAbout: () -> Void
    private let onQuit: () -> Void
    private let onOpenManager: (() -> Void)?
    private let onCheckForUpdates: (() -> Void)?
    private var updateLabel = "Check for Updates…"
    private var updateEnabled = true

    init(
        onPair: @escaping (String?) -> Void,
        onUnpair: @escaping () -> Void,
        onTogglePause: @escaping (Bool) -> Void,
        onShowActivity: @escaping () -> Void,
        onAbout: @escaping () -> Void,
        onQuit: @escaping () -> Void,
        onOpenManager: (() -> Void)? = nil,
        onCheckForUpdates: (() -> Void)? = nil
    ) {
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        self.onPair = onPair
        self.onUnpair = onUnpair
        self.onTogglePause = onTogglePause
        self.onShowActivity = onShowActivity
        self.onAbout = onAbout
        self.onQuit = onQuit
        self.onOpenManager = onOpenManager
        self.onCheckForUpdates = onCheckForUpdates
        super.init()

        statusItem.menu = menu
        AppState.shared.onChange = { [weak self] in self?.rebuild() }
        rebuild()
    }

    private func rebuild() {
        let state = AppState.shared

        if let button = statusItem.button {
            button.image = iconSymbol(for: state)
            button.title = ""
            button.toolTip = headerText(for: state)
        }

        menu.removeAllItems()

        let header = NSMenuItem(title: headerText(for: state), action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(.separator())

        // Temporarily hide the agent workspace entry point; device controls remain available.
        /*
        if onOpenManager != nil {
            let open = NSMenuItem(title: "Open Agent Manager…", action: #selector(openManagerAction), keyEquivalent: "")
            open.target = self
            menu.addItem(open)
            menu.addItem(.separator())
        }
        */

        switch state.pairing {
        case .notPaired:
            addUnpairedItems(devices: state.discoveredDevices)
        case .paired(_, let host):
            addPairedItems(host: host, connection: state.connection, paused: state.paused)
        }

        menu.addItem(.separator())

        let about = NSMenuItem(title: "About Autonomous Buddy", action: #selector(aboutAction), keyEquivalent: "")
        about.target = self
        menu.addItem(about)

        if onCheckForUpdates != nil {
            let update = NSMenuItem(title: updateLabel, action: #selector(checkForUpdatesAction), keyEquivalent: "")
            update.target = self
            update.isEnabled = updateEnabled
            menu.addItem(update)
        }

        let quit = NSMenuItem(title: "Quit Autonomous Buddy", action: #selector(quitAction), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
    }

    // MARK: - header text + icon

    private func iconSymbol(for state: AppState) -> NSImage? {
        // `lit` (paired, connected, not paused) paints a yellow non-template bulb;
        // template mode would make it look identical to the unpaired outline.
        let name: String
        var lit = false
        switch state.pairing {
        case .notPaired:
            name = "lightbulb"
        case .paired:
            switch state.connection {
            case .connected:
                if state.paused {
                    name = "pause.fill"
                } else {
                    name = "lightbulb.fill"
                    lit = true
                }
            case .connecting:
                name = "lightbulb"
            case .error:
                name = "exclamationmark.triangle.fill"
            case .disconnected:
                name = "lightbulb.slash"
            }
        }
        let base = NSImage(systemSymbolName: name, accessibilityDescription: headerText(for: state))
        guard let base else { return nil }
        if lit {
            let config = NSImage.SymbolConfiguration(paletteColors: [NSColor.systemYellow])
            let coloured = base.withSymbolConfiguration(config) ?? base
            coloured.isTemplate = false
            return coloured
        }
        base.isTemplate = true
        return base
    }

    private func headerText(for state: AppState) -> String {
        switch state.pairing {
        case .notPaired:
            return "Autonomous Buddy — Not paired"
        case .paired(_, let host):
            switch state.connection {
            case .connected:
                return state.paused ? "Paused · paired with \(host)" : "Connected to \(host)"
            case .connecting:
                return "Connecting to \(host)…"
            case .error(let msg):
                return "Disconnected: \(msg)"
            case .disconnected:
                return "Disconnected from \(host)"
            }
        }
    }

    // MARK: - menu sections

    private func addUnpairedItems(devices: [DeviceInfo]) {
        if devices.isEmpty {
            let none = NSMenuItem(title: "No devices discovered yet", action: nil, keyEquivalent: "")
            none.isEnabled = false
            menu.addItem(none)
        } else {
            let label = NSMenuItem(title: "Discovered devices:", action: nil, keyEquivalent: "")
            label.isEnabled = false
            menu.addItem(label)
            for device in devices {
                let item = NSMenuItem(title: "  \(device.host)", action: #selector(pairDiscovered(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = device.host
                menu.addItem(item)
            }
        }
        let manual = NSMenuItem(title: "Pair with device…", action: #selector(pairManual), keyEquivalent: "p")
        manual.target = self
        menu.addItem(manual)
    }

    private func addPairedItems(host: String, connection: ConnectionStatus, paused: Bool) {
        let pauseTitle = paused ? "Resume command execution" : "Pause command execution"
        let pause = NSMenuItem(title: pauseTitle, action: #selector(togglePauseAction), keyEquivalent: "")
        pause.target = self
        menu.addItem(pause)

        addActivitySubmenu()

        menu.addItem(.separator())
        let unpair = NSMenuItem(title: "Revoke pairing…", action: #selector(unpairAction), keyEquivalent: "")
        unpair.target = self
        menu.addItem(unpair)
    }

    private func addActivitySubmenu() {
        let recent = AppState.shared.recentCommands
        if let last = recent.first {
            let symbol = last.ok ? "✓" : "✗"
            let summary = NSMenuItem(title: "Last: \(last.action) \(symbol)", action: nil, keyEquivalent: "")
            summary.isEnabled = false
            menu.addItem(summary)
        }
        let activity = NSMenuItem(title: "Show Activity…", action: #selector(showActivityAction), keyEquivalent: "a")
        activity.target = self
        menu.addItem(activity)
    }

    // MARK: - actions

    func setUpdateStatus(label: String, enabled: Bool) {
        updateLabel = label
        updateEnabled = enabled
        rebuild()
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        // AppKit otherwise re-enables actionable menu items when opening the menu.
        if menuItem.action == #selector(checkForUpdatesAction) { return updateEnabled }
        return true
    }

    @objc private func checkForUpdatesAction() {
        guard updateEnabled else { return }
        onCheckForUpdates?()
    }

    @objc private func pairDiscovered(_ sender: NSMenuItem) {
        onPair(sender.representedObject as? String)
    }

    @objc private func pairManual() {
        onPair(nil)
    }

    @objc private func togglePauseAction() {
        onTogglePause(!AppState.shared.paused)
    }

    @objc private func unpairAction() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Revoke pairing?"
        alert.informativeText = "Your device will no longer be able to control this Mac until you pair again."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Revoke")
        alert.addButton(withTitle: "Cancel")
        if alert.runModal() == .alertFirstButtonReturn {
            onUnpair()
        }
    }

    @objc private func aboutAction() {
        onAbout()
    }

    @objc private func showActivityAction() {
        onShowActivity()
    }

    @objc private func openManagerAction() {
        onOpenManager?()
    }

    @objc private func quitAction() {
        onQuit()
    }
}
