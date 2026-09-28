import Foundation

actor CommandDispatcher {
    private var executors: [String: Executor] = [:]
    private let auditLog: AuditLog
    private var activeCancelled = false
    private var activeID: String?
    private var activeTask: Task<[String: Any], Error>?

    init(auditLog: AuditLog, additionalExecutors: [Executor] = []) {
        self.auditLog = auditLog
        let defaults: [Executor] = [
            OpenAppExecutor(),
            OpenPathExecutor(),
            CloseAppExecutor(),
            OpenURLExecutor(),
            TypeTextExecutor(),
            KeyComboExecutor(),
            NotificationExecutor(),
            PingExecutor(),
            DesktopInfoExecutor(),
            ScreenshotExecutor(),
            ClickAtExecutor(),
            ScrollExecutor(),
            MouseMoveExecutor(),
            DragExecutor(),
            ReadClipboardExecutor(),
            WriteClipboardExecutor(),
            ClickButtonExecutor(),
            CursorPosExecutor(),
            ListDisplaysExecutor(),
            GetUITreeExecutor(),
            PerformUIActionExecutor(),
            CuaObserveExecutor(),
            CuaActionExecutor()
        ]
        for executor in defaults + additionalExecutors {
            executors[executor.action] = executor
        }
    }

    func dispatch(_ data: Data) async -> Data {
        let start = Date()
        let cmd: IncomingCommand
        do {
            cmd = try IncomingCommand.decode(from: data)
        } catch {
            // Preserve correlation even when another field is malformed.
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let resp = CommandResponse(
                id: object?["id"] as? String ?? "unknown",
                ok: false,
                result: nil,
                error: (error as? LocalizedError)?.errorDescription ?? error.localizedDescription,
                durationMs: 0
            )
            return (try? resp.encode()) ?? Data()
        }

        let summary = CommandSummary.describe(action: cmd.action, params: cmd.params)

        if cmd.action == "cancel_command" {
            guard let targetID = cmd.params["id"] as? String, !targetID.isEmpty else {
                return await finish(cmd, summary: summary, start: start, error: "missing param: id")
            }
            let cancelled = activeID == targetID
            if cancelled { await cancelActive() }
            return await finish(cmd, summary: summary, start: start, result: ["cancel_requested": cancelled, "id": targetID])
        }

        // Reserve before the first suspension: actor reentrancy must not allow two
        // input sequences to interleave while permission checks or events await.
        guard activeID == nil else {
            return await finish(cmd, summary: summary, start: start, error: "buddy busy: another command is running; observe again before retrying")
        }
        activeCancelled = false
        activeID = cmd.id
        defer { activeID = nil; activeTask = nil }
        let paused = await readPaused()
        if paused && cmd.action != "desktop_info" {
            return await finish(cmd, summary: summary, start: start, error: "buddy paused by user")
        }

        guard let executor = executors[cmd.action] else {
            let resp = CommandResponse(id: cmd.id, ok: false, result: nil, error: "unknown action: \(cmd.action)", durationMs: 0)
            await auditLog.append(action: cmd.action, summary: summary, ok: false, error: resp.error)
            await recordOnMain(id: cmd.id, action: cmd.action, summary: summary, ok: false, error: resp.error)
            return (try? resp.encode()) ?? Data()
        }

        do {
            let observations: Set<String> = ["get_ui_tree", "screenshot", "list_displays", "cursor_pos", "read_clipboard", "ping", "desktop_info", "perform_ui_action"]
            if !observations.contains(cmd.action) { await UIObservationStore.shared.invalidate() }
            let cuaObservations: Set<String> = ["cua_observe", "cua_action", "desktop_info", "ping"]
            if !cuaObservations.contains(cmd.action) { await CuaObservationStore.shared.invalidate() }
            try Task.checkCancellation()
            if activeCancelled { throw CancellationError() }
            let task = Task { try await self.runWithTimeout(executor: executor, params: cmd.params, timeoutMs: cmd.timeoutMs) }
            activeTask = task
            let result = try await withTaskCancellationHandler(operation: { try await task.value }, onCancel: { task.cancel() })
            try Task.checkCancellation()
            if task.isCancelled { throw CancellationError() }
            let duration = Int(Date().timeIntervalSince(start) * 1000)
            let resp = CommandResponse(id: cmd.id, ok: true, result: result, error: nil, durationMs: duration)
            await auditLog.append(action: cmd.action, summary: summary, ok: true, error: nil)
            await recordOnMain(id: cmd.id, action: cmd.action, summary: summary, ok: true)
            return (try? resp.encode()) ?? Data()
        } catch {
            await CuaObservationStore.shared.invalidate()
            let duration = Int(Date().timeIntervalSince(start) * 1000)
            let msg = error is CancellationError ? "command cancelled" : ((error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
            let resp = CommandResponse(id: cmd.id, ok: false, result: nil, error: msg, durationMs: duration)
            await auditLog.append(action: cmd.action, summary: summary, ok: false, error: msg)
            await recordOnMain(id: cmd.id, action: cmd.action, summary: summary, ok: false, error: msg)
            return (try? resp.encode()) ?? Data()
        }
    }

    private func runWithTimeout(executor: Executor, params: [String: Any], timeoutMs: Int?) async throws -> [String: Any] {
        let timeout = max(500, timeoutMs ?? 5000)
        try Task.checkCancellation()
        return try await withThrowingTaskGroup(of: [String: Any].self) { group in
            defer { group.cancelAll() }
            group.addTask {
                try Task.checkCancellation()
                return try await executor.execute(params: params)
            }
            group.addTask {
                try await Task.sleep(nanoseconds: UInt64(timeout) * 1_000_000)
                throw ExecutorError.actionFailed("timeout after \(timeout)ms")
            }
            let value = try await group.next()!
            group.cancelAll()
            return value
        }
    }

    func cancelActive() async {
        activeCancelled = true
        activeTask?.cancel()
        await CuaObservationStore.shared.invalidate()
    }

    private func finish(_ cmd: IncomingCommand, summary: String, start: Date, result: [String: Any]? = nil, error: String? = nil) async -> Data {
        let response = CommandResponse(id: cmd.id, ok: error == nil, result: result, error: error, durationMs: Int(Date().timeIntervalSince(start) * 1000))
        await auditLog.append(action: cmd.action, summary: summary, ok: response.ok, error: error)
        await recordOnMain(id: cmd.id, action: cmd.action, summary: summary, ok: response.ok, error: error)
        return (try? response.encode()) ?? Data()
    }

    @MainActor
    private func readPaused() -> Bool {
        return AppState.shared.paused
    }

    @MainActor
    private func recordOnMain(id: String, action: String, summary: String, ok: Bool, error: String? = nil) {
        AppState.shared.recordCommand(CommandRecord(id: id, action: action, summary: summary, ok: ok, error: error, timestamp: Date()))
    }
}
