import XCTest
@testable import AutonomousBuddy

final class CuaClientTests: XCTestCase {
    func testInstalledDriverReadOnly() async throws {
        guard ProcessInfo.processInfo.environment["CUA_DRIVER_TEST"] == "1" else {
            throw XCTSkip("Set CUA_DRIVER_TEST=1 to exercise the installed driver directly")
        }
        let client = CuaClient(executable: URL(fileURLWithPath: "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"))
        let result = try await client.call(tool: "get_screen_size", arguments: [:])
        XCTAssertFalse(result.isEmpty)
        await client.close()
    }

    func testRejectsStaleResponseAndUnknownCompletion() {
        let good: [String: Any] = ["envelope_version": 1, "request_id": "current", "ok": true,
                                   "completion_known": true, "result": ["tree_markdown": "Calculator"]]
        XCTAssertThrowsError(try CuaClient.decodeEnvelope(good, requestID: "previous"))
        var uncertain = good
        uncertain["completion_known"] = false
        XCTAssertThrowsError(try CuaClient.decodeEnvelope(uncertain, requestID: "current"))
        XCTAssertEqual(try CuaClient.decodeEnvelope(good, requestID: "current")["tree_markdown"] as? String, "Calculator")
    }

    func testRPCFailureReconnectsOnNextCallWithoutReplayingAction() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let executable = directory.appendingPathComponent("driver")
        let log = directory.appendingPathComponent("requests")
        // The first receiver loses its binding while its transport stays alive.
        // Later calls must reconnect; successful calls should reuse the new receiver.
        let script = """
        #!/usr/bin/python3
        import json,sys,pathlib
        base=pathlib.Path(__file__).parent
        first=not (base/'launched').exists()
        (base/'launched').touch()
        def record(value):
            with (base/'requests').open('a') as f:
                f.write(value+'\\n')
        record('launch')
        for line in sys.stdin:
            r=json.loads(line);m=r['method'];p=r['params']
            record(m)
            if m=='initialize':
                value={'serverInfo':{'version':'0.28.2'},'capabilities':{'experimental':{'ai.cua.driver.envelopes':{'version':1}}}}
            elif m.endswith('/open'):
                value={'connection_id':'00000000-0000-4000-8000-000000000001','generation':'00000000-0000-4000-8000-000000000002','capabilities':{'minimum_envelope_version':1,'maximum_envelope_version':1,'supports_cancellation':True}}
            elif m.endswith('/exchange'):
                if first:
                    print(json.dumps({'jsonrpc':'2.0','id':r['id'],'error':{'code':-32000,'message':'connection_not_found'}}),flush=True)
                    continue
                value={'envelope_version':1,'request_id':p['envelope']['request_id'],'completion_known':True,'ok':True,'result':{'accepted':True}}
            else:raise Exception('unexpected method')
            print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':value}),flush=True)
        """
        try script.write(to: executable, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        let client = CuaClient(executable: executable, timeoutSeconds: 5)
        do {
            do {
                _ = try await client.call(tool: "click", arguments: [:])
                XCTFail("Lost receiver unexpectedly returned success")
            } catch {
                XCTAssertEqual(error.localizedDescription, "Cua Driver: connection_not_found")
            }
            let afterFailure = try String(contentsOf: log).split(separator: "\n")
            XCTAssertEqual(afterFailure.filter { $0 == "launch" }.count, 1)
            XCTAssertEqual(afterFailure.filter { $0.hasSuffix("/exchange") }.count, 1)

            let recovered = try await client.call(tool: "get_screen_size", arguments: [:])
            XCTAssertEqual(recovered["accepted"] as? Bool, true)
            let reused = try await client.call(tool: "get_screen_size", arguments: [:])
            XCTAssertEqual(reused["accepted"] as? Bool, true)
            let requests = try String(contentsOf: log).split(separator: "\n")
            XCTAssertEqual(requests.filter { $0 == "launch" }.count, 2)
            XCTAssertEqual(requests.filter { $0 == "initialize" }.count, 2)
            XCTAssertEqual(requests.filter { $0.hasSuffix("/open") }.count, 2)
            XCTAssertEqual(requests.filter { $0.hasSuffix("/exchange") }.count, 3)
        } catch {
            await client.close()
            throw error
        }
        await client.close()
    }

    func testCancellationReachesAcknowledgedReceiver() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let executable = directory.appendingPathComponent("driver")
        let marker = directory.appendingPathComponent("cancelled")
        let started = directory.appendingPathComponent("started")
        // A mock server intentionally leaves exchange pending until acknowledged cancel.
        let script = """
        #!/usr/bin/python3
        import json,sys,pathlib
        base=pathlib.Path(__file__).parent
        for line in sys.stdin:
            r=json.loads(line);m=r['method'];p=r['params']
            if m=='initialize':
                value={'serverInfo':{'version':'0.28.2'},'capabilities':{'experimental':{'ai.cua.driver.envelopes':{'version':1}}}}
            elif m.endswith('/open'):
                value={'connection_id':'00000000-0000-4000-8000-000000000001','generation':'00000000-0000-4000-8000-000000000002','capabilities':{'minimum_envelope_version':1,'maximum_envelope_version':1,'supports_cancellation':True}}
            elif m.endswith('/exchange'):
                (base/'started').write_text(p['envelope']['request_id']);continue
            elif m.endswith('/cancel'):
                (base/'cancelled').write_text(p['request_id']);value={'ok':True}
            else:raise Exception('unexpected method')
            print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':value}),flush=True)
        """
        try script.write(to: executable, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        let client = CuaClient(executable: executable, timeoutSeconds: 5)
        let task = Task { try await client.call(tool: "click", arguments: [:]) }
        for _ in 0..<200 {
            if FileManager.default.fileExists(atPath: started.path) { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertTrue(FileManager.default.fileExists(atPath: started.path))
        task.cancel()
        do { _ = try await task.value; XCTFail("Cancelled action returned success") }
        catch { XCTAssertTrue(error is CancellationError) }
        XCTAssertEqual(try String(contentsOf: marker), try String(contentsOf: started))
    }
}
