import Foundation

/// A single, persistent, cancellable Cua Driver session. No tool is replayed.
actor CuaClient {
    static let shared = CuaClient()
    static let supportedVersion = "0.28.2"
    static var executableURL: URL? {
        CuaRuntime.executableURL()
    }
    static var available: Bool { executableURL != nil }
    static var enabled: Bool { !UserDefaults.standard.bool(forKey: "disableCuaDriver") }

    enum Failure: LocalizedError {
        case unavailable, busy, invalidResponse, oversized, closed, timeout, incompatible, remote(String)
        var errorDescription: String? {
            switch self {
            case .unavailable: return "Cua Driver is not installed"
            case .busy: return "Cua Driver already has an active command"
            case .invalidResponse: return "Invalid Cua Driver response"
            case .oversized: return "Cua Driver response exceeds the size limit"
            case .closed: return "Cua Driver connection closed; action completion may be unknown"
            case .timeout: return "Cua Driver timed out; action completion may be unknown"
            case .incompatible: return "Cua Driver 0.28.2 with acknowledged MCP envelope cancellation is required"
            case .remote(let message): return "Cua Driver: \(message)"
            }
        }
    }

    private var process: Process?
    private var transportGeneration: UUID?
    private var input: FileHandle?
    private var output: FileHandle?
    private var errors: FileHandle?
    private var reader: Task<Void, Never>?
    private var buffer = Data()
    private var sequence = 0
    private var pending: [Int: CheckedContinuation<[String: Any], Error>] = [:]
    private var binding: [String: Any]?
    private var active: UUID?
    private var aborting = false
    private let executable: URL?
    private let timeoutSeconds: Double
    private static let prefix = "cua/driver/v1/"
    private static let maximumBytes = 8 * 1024 * 1024

    init(executable: URL? = CuaClient.executableURL, timeoutSeconds: Double = 30) {
        self.executable = executable
        self.timeoutSeconds = timeoutSeconds
    }

    /// Closing the typed transport invalidates its receiver sessions upstream.
    func close() { shutdown(Failure.closed) }

    func call(tool: String, arguments: [String: Any]) async throws -> [String: Any] {
        try Task.checkCancellation()
        guard active == nil, !aborting else { throw Failure.busy }
        let token = UUID()
        active = token
        defer { if active == token { active = nil } }
        let timer = DispatchWorkItem {
            Task { await self.abort(token, error: Failure.timeout) }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeoutSeconds, execute: timer)
        defer { timer.cancel() }
        return try await withTaskCancellationHandler {
            try await connect()
            try Task.checkCancellation()
            guard active == token, !aborting, var parameters = binding else { throw CancellationError() }
            parameters["envelope"] = [
                "envelope_version": 1, "request_id": token.uuidString,
                "operation": "call", "name": tool, "arguments": arguments,
                "deadline_unix_ms": Int64(Date().timeIntervalSince1970 * 1000 + timeoutSeconds * 1000)
            ] as [String: Any]
            do {
                let envelope = try await rpc(Self.prefix + "exchange", parameters)
                try Task.checkCancellation()
                return try Self.decodeEnvelope(envelope, requestID: token.uuidString)
            }
            catch { shutdown(error); throw error }
        } onCancel: {
            Task { await self.abort(token, error: CancellationError()) }
        }
    }

    static func decodeEnvelope(_ envelope: [String: Any], requestID: String) throws -> [String: Any] {
        guard envelope["request_id"] as? String == requestID,
              envelope["envelope_version"] as? Int == 1 else { throw Failure.invalidResponse }
        guard envelope["completion_known"] as? Bool == true else { throw Failure.closed }
        guard envelope["ok"] as? Bool == true else {
            throw Failure.remote(String((envelope["error"] as? String ?? envelope["error_code"] as? String ?? "tool_failed").prefix(500)))
        }
        guard let result = envelope["result"] as? [String: Any] else { throw Failure.invalidResponse }
        if result["isError"] as? Bool == true {
            let content = result["content"] as? [[String: Any]]
            let message = content?.compactMap { $0["text"] as? String }.first ?? "tool_failed"
            throw Failure.remote(String(message.prefix(500)))
        }
        // SDK envelopes may contain the raw structured result or the MCP wrapper.
        guard var structured = result["structuredContent"] as? [String: Any] else { return result }
        // Images are protocol content, not fields in structuredContent. Preserve them
        // separately for capture-only consumers without replacing native metadata.
        let images = (result["content"] as? [[String: Any]])?.filter { $0["type"] as? String == "image" } ?? []
        if !images.isEmpty { structured["_cua_images"] = images }
        return structured
    }

    private func connect() async throws {
        if binding != nil { return }
        guard let executable else { throw Failure.unavailable }
        let child = Process()
        child.executableURL = executable
        child.arguments = CuaRuntime.arguments
        child.environment = CuaRuntime.environment()
        let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
        child.standardInput = stdin
        child.standardOutput = stdout
        child.standardError = stderr
        let stream = AsyncStream<Data>(bufferingPolicy: .bufferingOldest(128)) { continuation in
            stdout.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                if data.isEmpty { continuation.finish() }
                else if case .dropped = continuation.yield(data) { continuation.finish() }
            }
        }
        // Drain diagnostics without storing private UI contents or blocking the child.
        stderr.fileHandleForReading.readabilityHandler = { handle in _ = handle.availableData }
        process = child
        transportGeneration = UUID()
        input = stdin.fileHandleForWriting
        output = stdout.fileHandleForReading
        errors = stderr.fileHandleForReading
        do { try child.run() } catch { shutdown(error); throw error }
        reader = Task {
            for await bytes in stream {
                if Task.isCancelled { break }
                self.receive(bytes)
            }
            if !Task.isCancelled { self.shutdown(Failure.closed) }
        }
        do {
            let initialized = try await rpc("initialize", [
                "protocolVersion": "2025-06-18", "capabilities": [:],
                "clientInfo": ["name": "buddy", "version": "1"]
            ])
            let info = initialized["serverInfo"] as? [String: Any]
            let capabilities = initialized["capabilities"] as? [String: Any]
            let experimental = capabilities?["experimental"] as? [String: Any]
            let envelopes = experimental?["ai.cua.driver.envelopes"] as? [String: Any]
            guard info?["version"] as? String == Self.supportedVersion,
                  envelopes?["version"] as? Int == 1 else { throw Failure.incompatible }
            let opened = try await rpc(Self.prefix + "open", [:])
            let negotiated = opened["capabilities"] as? [String: Any]
            guard negotiated?["supports_cancellation"] as? Bool == true,
                  negotiated?["minimum_envelope_version"] as? Int == 1,
                  negotiated?["maximum_envelope_version"] as? Int == 1 else { throw Failure.incompatible }
            guard let id = opened["connection_id"] as? String,
                  let generation = opened["generation"] as? String,
                  UUID(uuidString: id) != nil, UUID(uuidString: generation) != nil else { throw Failure.invalidResponse }
            binding = ["connection_id": id, "generation": generation]
        } catch { shutdown(error); throw error }
    }

    private func rpc(_ method: String, _ parameters: [String: Any]) async throws -> [String: Any] {
        guard let input else { throw Failure.closed }
        sequence += 1
        let id = sequence
        var bytes = try JSONSerialization.data(withJSONObject: [
            "jsonrpc": "2.0", "id": id, "method": method, "params": parameters
        ])
        guard bytes.count < 1024 * 1024 else { throw Failure.oversized }
        bytes.append(10)
        return try await withCheckedThrowingContinuation { continuation in
            pending[id] = continuation
            do { try input.write(contentsOf: bytes) }
            catch { shutdown(error) }
        }
    }

    private func receive(_ data: Data) {
        buffer.append(data)
        guard buffer.count <= Self.maximumBytes else { shutdown(Failure.oversized); return }
        while let newline = buffer.firstIndex(of: 10) {
            let line = buffer.prefix(upTo: newline)
            buffer.removeSubrange(...newline)
            guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
                shutdown(Failure.invalidResponse); return
            }
            guard let id = object["id"] as? Int else { continue }
            guard let continuation = pending.removeValue(forKey: id) else {
                shutdown(Failure.invalidResponse); return
            }
            if let result = object["result"] as? [String: Any] {
                continuation.resume(returning: result)
            } else {
                let error = object["error"] as? [String: Any]
                continuation.resume(throwing: Failure.remote(String((error?["message"] as? String ?? "rpc_failed").prefix(500))))
            }
        }
    }

    private func abort(_ token: UUID, error: Error) async {
        guard active == token, !aborting else { return }
        aborting = true
        let cancelledGeneration = transportGeneration
        // Ordinary notifications/cancelled are ignored by upstream. Use its
        // acknowledged receiver cancellation, then close this session on EOF.
        let watchdog = DispatchWorkItem {
            Task { self.forceAbort(cancelledGeneration, error: error) }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + 2, execute: watchdog)
        if var parameters = binding {
            parameters["request_id"] = token.uuidString
            _ = try? await rpc(Self.prefix + "cancel", parameters)
        }
        watchdog.cancel()
        if transportGeneration == cancelledGeneration { shutdown(error) }
    }

    private func forceAbort(_ cancelledGeneration: UUID?, error: Error) {
        if transportGeneration == cancelledGeneration { shutdown(error) }
    }

    private func shutdown(_ error: Error) {
        input?.closeFile()
        output?.readabilityHandler = nil
        errors?.readabilityHandler = nil
        output?.closeFile()
        errors?.closeFile()
        input = nil; output = nil; errors = nil
        if let child = process, child.isRunning {
            child.terminate()
            DispatchQueue.global().asyncAfter(deadline: .now() + 1) {
                if child.isRunning { kill(child.processIdentifier, SIGKILL) }
            }
        }
        process = nil
        transportGeneration = nil
        reader?.cancel(); reader = nil
        binding = nil
        buffer.removeAll(keepingCapacity: false)
        let waiting = pending.values
        pending.removeAll()
        for continuation in waiting { continuation.resume(throwing: error) }
        aborting = false
    }
}
