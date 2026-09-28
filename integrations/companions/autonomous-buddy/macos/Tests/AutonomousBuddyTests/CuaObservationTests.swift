import XCTest
@testable import AutonomousBuddy

final class CuaObservationTests: XCTestCase {
    func testInstalledCalculatorRoundTrip() async throws {
        guard ProcessInfo.processInfo.environment["CUA_BUDDY_TEST"] == "1" else {
            throw XCTSkip("Set CUA_BUDDY_TEST=1 for the authorized local Calculator experiment")
        }
        let client = CuaClient()
        let store = CuaObservationStore { try await client.call(tool: $0, arguments: $1) }
        do {
            for label in ["All Clear", "2", "Add", "2", "Equals"] {
                let observation = try await store.observe(params: ["app": "com.apple.calculator"])
                let elements = try XCTUnwrap(observation["elements"] as? [[String: Any]])
                let matches = elements.filter { $0["label"] as? String == label && $0["role"] as? String == "AXButton" }
                XCTAssertEqual(matches.count, 1)
                let target = try XCTUnwrap(matches.first)
                _ = try await store.perform(params: ["snapshot_id": observation["snapshot_id"]!,
                    "element_token": target["element_token"]!, "ui_action": "click"])
            }
            let final = try await store.observe(params: ["app": "com.apple.calculator"])
            let tree = try XCTUnwrap(final["tree_markdown"] as? String)
            XCTAssertTrue(tree.contains("AXStaticText = \"\u{200E}4\""), "Calculator must expose the final answer")
            await client.close()
        } catch {
            await client.close()
            throw error
        }
    }

    actor Driver {
        var mutations = 0
        var failMutation = false
        var lastArguments: [String: Any] = [:]
        var windows: [[String: Any]] = [["window_id": 11, "title": "Calculator"]]
        func fail() { failMutation = true }
        func multipleWindows() { windows.append(["window_id": 12, "title": "Another document"]) }
        func call(_ tool: String, _ args: [String: Any]) throws -> [String: Any] {
            if tool == "list_windows" { return ["windows": windows] }
            if tool == "get_window_state" {
                return ["pid": 123, "window_id": 11, "snapshot_id": "s00000001",
                        "elements": [["element_token": "s00000001:2", "label": "2", "actions": ["AXOpen"]]],
                        "tree_markdown": "AXStaticText = 4", "elements_complete": true]
            }
            mutations += 1
            lastArguments = args
            XCTAssertEqual(args["pid"] as? Int, 123)
            XCTAssertEqual(args["window_id"] as? Int, 11)
            if failMutation { throw ExecutorError.actionFailed("uncertain") }
            return ["effect": "unverifiable"]
        }
    }

    func testSnapshotIsConsumedAndEffectIsNotOverstated() async throws {
        let driver = Driver()
        let store = CuaObservationStore { try await driver.call($0, $1) }
        let observed = try await store.observe(pid: 123, params: [:])
        XCTAssertEqual(observed["tree_markdown"] as? String, "AXStaticText = 4")
        let params: [String: Any] = ["snapshot_id": observed["snapshot_id"]!, "element_token": "s00000001:2", "ui_action": "click"]
        let result = try await store.perform(params: params)
        XCTAssertEqual(result["effect"] as? String, "unverifiable")
        do { _ = try await store.perform(params: params); XCTFail("replayed consumed snapshot") } catch {}
        let count = await driver.mutations
        XCTAssertEqual(count, 1)
        let arguments = await driver.lastArguments
        XCTAssertEqual(arguments["delivery_mode"] as? String, "background")
        XCTAssertNil(arguments["action"])
    }

    func testExplicitAXOpenAndForegroundKeyAreBoundToObservedWindow() async throws {
        let driver = Driver()
        let store = CuaObservationStore { try await driver.call($0, $1) }
        for extra: [String: Any] in [["ui_action": "click", "ax_action": "open"],
                                    ["ui_action": "press_key", "key": "l", "modifiers": ["cmd"], "delivery_mode": "foreground"]] {
            let observed = try await store.observe(pid: 123, params: [:])
            var params: [String: Any] = ["snapshot_id": observed["snapshot_id"]!, "element_token": "s00000001:2"]
            params.merge(extra) { _, new in new }
            _ = try await store.perform(params: params)
            let arguments = await driver.lastArguments
            XCTAssertEqual(arguments["pid"] as? Int, 123)
            XCTAssertEqual(arguments["window_id"] as? Int, 11)
            if extra["ui_action"] as? String == "click" {
                XCTAssertEqual(arguments["action"] as? String, "open")
                XCTAssertEqual(arguments["delivery_mode"] as? String, "background")
            } else {
                XCTAssertEqual(arguments["delivery_mode"] as? String, "foreground")
                XCTAssertEqual(arguments["key"] as? String, "l")
                XCTAssertEqual(arguments["modifiers"] as? [String], ["cmd"])
            }
            do { _ = try await store.perform(params: params); XCTFail("replayed explicit action") } catch {}
        }
        let count = await driver.mutations
        XCTAssertEqual(count, 2)
    }

    func testExplicitActionOptionsRejectInvalidOrUnadvertisedValues() async throws {
        let driver = Driver()
        let store = CuaObservationStore { try await driver.call($0, $1) }
        let observed = try await store.observe(pid: 123, params: [:])
        let invalid: [[String: Any]] = [
            ["ui_action": "click", "ax_action": "press"],
            ["ui_action": "click", "ax_action": "arbitrary"],
            ["ui_action": "click", "ax_action": 1],
            ["ui_action": "click", "delivery_mode": "foreground"],
            ["ui_action": "type_text", "text": "x", "ax_action": "open"],
            ["ui_action": "press_key", "key": "l", "delivery_mode": "global"],
            ["ui_action": "press_key", "key": "l", "delivery_mode": true],
            ["ui_action": "press_key", "key": "l", "delivery_mode": "foreground", "window_id": 99],
        ]
        for extra in invalid {
            var params: [String: Any] = ["snapshot_id": observed["snapshot_id"]!, "element_token": "s00000001:2"]
            params.merge(extra) { _, new in new }
            do { _ = try await store.perform(params: params); XCTFail("accepted invalid options: \(extra)") } catch {}
        }
        let count = await driver.mutations
        XCTAssertEqual(count, 0)
    }

    func testNewObservationAndExplicitInvalidationRejectOldReferences() async throws {
        let driver = Driver()
        let store = CuaObservationStore { try await driver.call($0, $1) }
        let first = try await store.observe(pid: 123, params: [:])
        let second = try await store.observe(pid: 123, params: [:])
        for observation in [first, second] {
            if observation["snapshot_id"] as? String == second["snapshot_id"] as? String { await store.invalidate() }
            do {
                _ = try await store.perform(params: ["snapshot_id": observation["snapshot_id"]!, "element_token": "s00000001:2", "ui_action": "click"])
                XCTFail("accepted stale reference")
            } catch {}
        }
        let count = await driver.mutations
        XCTAssertEqual(count, 0)
    }

    func testFailureDoesNotRetryOrRetainReference() async throws {
        let driver = Driver()
        await driver.fail()
        let store = CuaObservationStore { try await driver.call($0, $1) }
        let observed = try await store.observe(pid: 123, params: [:])
        for _ in 0..<2 {
            do {
                _ = try await store.perform(params: ["snapshot_id": observed["snapshot_id"]!, "element_token": "s00000001:2", "ui_action": "click"])
                XCTFail("expected failure")
            } catch {}
        }
        let count = await driver.mutations
        XCTAssertEqual(count, 1)
    }

    func testAmbiguousWindowsRequireSelection() async throws {
        let driver = Driver()
        await driver.multipleWindows()
        let store = CuaObservationStore { try await driver.call($0, $1) }
        let result = try await store.observe(pid: 123, params: [:])
        XCTAssertEqual(result["requires_window_selection"] as? Bool, true)
        XCTAssertNil(result["snapshot_id"])
    }

    func testCannotOverrideTargetOrInjectArbitraryTools() async throws {
        let driver = Driver()
        let store = CuaObservationStore { try await driver.call($0, $1) }
        let observation = try await store.observe(pid: 123, params: [:])
        for extra: [String: Any] in [["pid": 456], ["delivery_mode": "foreground"], ["ui_action": "shell"]] {
            var params: [String: Any] = ["snapshot_id": observation["snapshot_id"]!, "element_token": "s00000001:2", "ui_action": "click"]
            params.merge(extra) { _, new in new }
            do { _ = try await store.perform(params: params); XCTFail("unsafe arguments accepted") } catch {}
        }
        let count = await driver.mutations
        XCTAssertEqual(count, 0)
    }
}
