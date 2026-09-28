import Cocoa

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let embedded = CommandLine.arguments.contains("--embedded-helper")
    private let nativeTestMode = ProcessInfo.processInfo.environment["BUDDY_NATIVE_TEST_MODE"] == "1"
    private let agentRelay = AgentRequestRelay()
    private var helperBridge: EmbeddedHelperBridge?
    private var menuBarController: MenuBarController?
    private var discovery: DeviceDiscovery?
    private var pairingManager: PairingManager?
    private var dispatcher: CommandDispatcher?
    private var connection: DeviceConnection?
    private var auditLog: AuditLog?
    private var pairingWindow: PairingWindowController?
    private var activityWindow: ActivityWindowController?
    private var terminationRequested = false
    private var terminationReady = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let store = PairingStore()
        let audit = AuditLog(url: nativeTestMode ? URL(fileURLWithPath: "/dev/null") : nil)
        let dispatcher = CommandDispatcher(auditLog: audit)
        let pairingManager = PairingManager(store: store)

        self.auditLog = audit
        self.dispatcher = dispatcher
        self.pairingManager = pairingManager

        // Bonjour discovery — best-effort. If the device doesn't advertise `_autonomous._tcp`,
        // user pairs by typing `lamp-xxxx.local` manually.
        let discovery = DeviceDiscovery()
        discovery.onDevicesChanged = { devices in
            AppState.shared.setDiscoveredDevices(devices)
        }
        if !nativeTestMode { discovery.start() }
        self.discovery = discovery

        if !nativeTestMode {
            menuBarController = MenuBarController(
                onPair: { [weak self] host in self?.showPairing(host: host) },
                onUnpair: { [weak self] in self?.unpair() },
                onTogglePause: { paused in
                    AppState.shared.setPaused(paused)
                    if paused { Task { await dispatcher.cancelActive() } }
                },
                onShowActivity: { [weak self] in self?.showActivity() },
                onAbout: { [weak self] in self?.showAbout() },
                onQuit: { [weak self] in
                    if self?.embedded == true { self?.helperBridge?.publishMenuAction("quit") }
                    else { NSApp.terminate(nil) }
                },
                onOpenManager: embedded ? { [weak self] in
                    self?.helperBridge?.publishMenuAction("open-manager")
                } : nil,
                onCheckForUpdates: embedded ? { [weak self] in
                    self?.helperBridge?.publishMenuAction("check-updates")
                } : nil
            )

        }

        if embedded {
            let bridge = EmbeddedHelperBridge { [weak self] id, method, params in
                await self?.handleHelperRequest(id: id, method: method, params: params)
            }
            helperBridge = bridge
            agentRelay.publish = { [weak bridge] id, command in
                bridge?.publishAgentRequest(id: id, command: command)
            }
            bridge.start()
        }

        if nativeTestMode,
           let host = Self.testDeviceHost(ProcessInfo.processInfo.environment["BUDDY_TEST_DEVICE_URL"]) {
            let record = PairingRecord(buddyID: "test-buddy", deviceHost: host, token: "test-token", pairedAt: Date())
            AppState.shared.setPairing(.paired(buddyID: record.buddyID, deviceHost: host))
            startConnection(record: record)
        }

        // Auto-reconnect if a record already exists from a previous run.
        if !nativeTestMode, let record = pairingManager.current() {
            AppState.shared.setPairing(.paired(buddyID: record.buddyID, deviceHost: record.deviceHost))
            startConnection(record: record)
        }
    }

    static func testDeviceHost(_ value: String?) -> String? {
        guard let value, let url = URLComponents(string: value), url.scheme == "ws",
              url.host == "127.0.0.1", let port = url.port, (1...65535).contains(port),
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/api/buddy/ws" else { return nil }
        return "127.0.0.1:\(port)"
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminationReady { return .terminateNow }
        if !terminationRequested {
            terminationRequested = true
            // terminateLater runs a nested AppKit loop which can starve MainActor
            // tasks when termination originates from the bridge's EOF callback.
            Task { @MainActor in
                await dispatcher?.cancelActive()
                await CuaClient.shared.close()
                terminationReady = true
                sender.terminate(nil)
            }
        }
        return .terminateCancel
    }

    func applicationWillTerminate(_ notification: Notification) {
        connection?.disconnect()
        discovery?.stop()
    }

    // MARK: - actions

    private func showPairing(host: String?) {
        guard let pairingManager else { return }
        let controller = PairingWindowController(manager: pairingManager, initialHost: host)
        controller.onSuccess = { [weak self] record in
            AppState.shared.setPairing(.paired(buddyID: record.buddyID, deviceHost: record.deviceHost))
            self?.startConnection(record: record)
        }
        pairingWindow = controller
        NSApp.activate(ignoringOtherApps: true)
        controller.window?.center()
        controller.showWindow(nil)
        controller.window?.makeKeyAndOrderFront(nil)
    }

    private func unpair(completion: (() -> Void)? = nil) {
        // Tell the device first so it drops its pairing record before we forget
        // our token. Fire-and-forget with a 5s timeout inside notifyRevokeSelf;
        // local state always clears on completion regardless of device reachability.
        let snapshot = pairingManager?.current()
        let manager = pairingManager
        Task {
            if let record = snapshot, let manager {
                await manager.notifyRevokeSelf(host: record.deviceHost, token: record.token)
            }
            await MainActor.run { [weak self] in
                self?.connection?.disconnect()
                self?.connection = nil
                try? self?.pairingManager?.unpair()
                AppState.shared.setPairing(.notPaired)
                completion?()
            }
        }
    }

    private func startConnection(record: PairingRecord) {
        connection?.disconnect()
        guard let dispatcher else { return }
        let c = DeviceConnection(host: record.deviceHost, token: record.token, dispatcher: dispatcher)
        if embedded {
            c.agentHandler = { [weak self] command in
                guard let self else {
                    return AgentRequestRelay.failure(id: command["id"] as? String ?? "", message: "Agent manager is unavailable")
                }
                return await self.agentRelay.request(command)
            }
        }
        c.connect()
        connection = c
    }

    private func showActivity() {
        if activityWindow == nil {
            activityWindow = ActivityWindowController()
        }
        activityWindow?.show()
    }

    @MainActor
    private func handleHelperRequest(id: String, method: String, params: [String: Any]) async {
        guard let bridge = helperBridge else { return }
        if nativeTestMode && ["pair", "unpair", "permissions", "activity"].contains(method) {
            bridge.fail(id: id, error: "Native UI and pairing are disabled in test mode")
            return
        }
        switch method {
        case "status", "ping":
            bridge.respond(id: id, result: EmbeddedHelperBridge.snapshot())
        case "update_status":
            guard let label = params["label"] as? String, !label.isEmpty, label.count <= 120,
                  let enabled = params["enabled"] as? Bool else {
                bridge.fail(id: id, error: "Update status requires a label and enabled boolean")
                return
            }
            // Safe in test mode: no menu or windows are created by this method.
            menuBarController?.setUpdateStatus(label: label, enabled: enabled)
            bridge.respond(id: id, result: ["ok": true])
        case "pair":
            showPairing(host: params["host"] as? String)
            bridge.respond(id: id, result: EmbeddedHelperBridge.snapshot())
        case "unpair":
            await withCheckedContinuation { continuation in
                unpair { continuation.resume() }
            }
            bridge.respond(id: id, result: EmbeddedHelperBridge.snapshot())
        case "pause":
            guard let paused = params["paused"] as? Bool else {
                bridge.fail(id: id, error: "paused must be a boolean")
                return
            }
            AppState.shared.setPaused(paused)
            if paused { await dispatcher?.cancelActive() }
            bridge.respond(id: id, result: EmbeddedHelperBridge.snapshot())
        case "permissions":
            // Do not retain a driver's cached TCC answers across a permissions visit.
            await dispatcher?.cancelActive()
            await CuaClient.shared.close()
            AccessibilityCheck.requestPrompt()
            ScreenRecordingCheck.requestPrompt()
            bridge.publishState()
            bridge.respond(id: id, result: EmbeddedHelperBridge.snapshot())
        case "activity":
            showActivity()
            bridge.respond(id: id, result: EmbeddedHelperBridge.snapshot())
        case "command":
            guard let dispatcher else {
                bridge.fail(id: id, error: "Dispatcher is unavailable")
                return
            }
            var command = params
            command["id"] = id
            do {
                let data = try JSONSerialization.data(withJSONObject: command)
                let response = await dispatcher.dispatch(data)
                bridge.respond(id: id, result: try JSONSerialization.jsonObject(with: response))
            } catch {
                bridge.fail(id: id, error: error.localizedDescription)
            }
        case "agent_response":
            guard let relayID = params["id"] as? String,
                  let response = params["response"] as? [String: Any] else {
                bridge.fail(id: id, error: "Invalid agent response")
                return
            }
            bridge.respond(id: id, result: ["accepted": agentRelay.respond(id: relayID, response: response)])
        case "agent_event":
            bridge.respond(id: id, result: ["sent": connection?.sendAgentEvent(params) ?? false])
        case "shutdown":
            bridge.respond(id: id, result: ["ok": true])
            NSApp.terminate(nil)
        default:
            bridge.fail(id: id, error: "Unknown native helper method: \(method)")
        }
    }

    private func showAbout() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Autonomous Buddy"
        alert.informativeText = """
            Native macOS companion that lets your device control this Mac \
            via voice commands processed by OpenClaw.

            MVP build: pairing, persistent WebSocket, command execution. \
            Device-side Go endpoints are required for end-to-end use; see \
            autonomous-buddy/docs/autonomous-buddy-mvp.md.
            """
        alert.alertStyle = .informational
        alert.runModal()
    }
}
