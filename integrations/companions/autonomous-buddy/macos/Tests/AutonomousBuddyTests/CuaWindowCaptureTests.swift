import XCTest
import AppKit
import ImageIO
@testable import AutonomousBuddy

final class CuaWindowCaptureTests: XCTestCase {
    private func fixture(width: Int = 40, height: Int = 20) -> [String: Any] {
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        let png = bitmap.representation(using: .png, properties: [:])!
        return ["pid": 42, "window_id": 7, "screenshot_frame_valid": true,
                "window_bounds": ["x": -300.0, "y": 80.0, "width": 80.0, "height": 40.0],
                "screenshot_scale": 2.0, "screenshot_width": width, "screenshot_height": height,
                "_cua_images": [["type": "image", "mimeType": "image/png", "data": png.base64EncodedString()]]]
    }

    func testScaleUsesNativePixelsWithoutDoubleDownsampling() throws {
        let reduced = try ScreenshotExecutor.captureOutputSize(width: 1000, height: 500,
            nativeWidth: 2000, scale: 0.5)
        XCTAssertEqual(reduced.0, 1000)
        XCTAssertEqual(reduced.1, 500)
        let quarter = try ScreenshotExecutor.captureOutputSize(width: 1000, height: 500,
            nativeWidth: 2000, scale: 0.25)
        XCTAssertEqual(quarter.0, 500)
        let full = try ScreenshotExecutor.captureOutputSize(width: 1000, height: 500,
            nativeWidth: 2000, scale: 1)
        XCTAssertEqual(full.0, 1000)
    }

    func testTargetValidation() throws {
        XCTAssertNil(try CuaWindowCapture.targetApp([:]))
        XCTAssertEqual(try CuaWindowCapture.targetApp(["app": "Calendar", "window_id": 7]), "Calendar")
        for params: [String: Any] in [["window_id": 7], ["app": " "], ["app": 1],
            ["app": "Calendar", "display_id": 1], ["app": "Calendar", "window_id": true],
            ["app": "Calendar", "window_id": 2.5]] {
            XCTAssertThrowsError(try CuaWindowCapture.targetApp(params))
        }
    }

    func testCaptureUsesWindowOnlyWithoutAXWalk() async throws {
        let response = fixture()
        var calls: [String] = []
        let capture = try await CuaWindowCapture.capture(pid: 42, params: ["window_id": 7]) { tool, arguments in
            calls.append(tool)
            XCTAssertEqual(arguments["pid"] as? Int, 42)
            if tool == "list_windows" { return ["windows": [["window_id": 7], ["window_id": 8]]] }
            XCTAssertEqual(arguments["window_id"] as? Int, 7)
            XCTAssertEqual(arguments["include_screenshot"] as? Bool, true)
            XCTAssertEqual(arguments["include_accessibility_tree"] as? Bool, false)
            return response
        }
        XCTAssertEqual(calls, ["list_windows", "get_window_state"])
        XCTAssertEqual(capture.bounds.origin.x, -300)
        let transform = ScreenshotExecutor.imageTransform(originX: capture.bounds.minX, originY: capture.bounds.minY,
            pointWidth: capture.bounds.width, pointHeight: capture.bounds.height,
            imageWidth: capture.image.width, imageHeight: capture.image.height)
        XCTAssertEqual(transform["origin_x"], -300)
        XCTAssertEqual(transform["scale_x"], 2)
        XCTAssertEqual(transform["scale_y"], 2)
    }

    func testAppOnlyPrefersSingleTitledWindowOverAuxiliarySurfaces() async throws {
        let response = fixture()
        let capture = try await CuaWindowCapture.capture(pid: 42, params: [:]) { tool, arguments in
            if tool == "list_windows" {
                return ["windows": [["window_id": 8, "title": ""],
                                    ["window_id": 7, "title": "Calendar"], ["window_id": 9]]]
            }
            XCTAssertEqual(arguments["window_id"] as? Int, 7)
            return response
        }
        XCTAssertEqual(capture.windowID, 7)
    }

    func testAppOnlyRejectsMultipleTitledWindows() async throws {
        do {
            _ = try await CuaWindowCapture.capture(pid: 42, params: [:]) { tool, _ in
                XCTAssertEqual(tool, "list_windows")
                return ["windows": [["window_id": 7, "title": "Calendar"],
                                    ["window_id": 8, "title": "Settings"], ["window_id": 9]]]
            }
            XCTFail("Ambiguous titled window captured")
        } catch {}
    }

    func testAmbiguousOrMissingWindowNeverCaptures() async throws {
        for params: [String: Any] in [[:], ["window_id": 9]] {
            do {
                _ = try await CuaWindowCapture.capture(pid: 42, params: params) { tool, _ in
                    XCTAssertEqual(tool, "list_windows")
                    return ["windows": [["window_id": 7], ["window_id": 8]]]
                }
                XCTFail("Unproven window captured")
            } catch {}
        }
    }

    func testRejectsMalformedImageOrFrame() {
        for (key, value): (String, Any) in [("pid", 9), ("window_id", 8), ("screenshot_frame_valid", false),
            ("screenshot_width", 999), ("_cua_images", []),
            ("window_bounds", ["x": 0, "y": 0, "width": 80, "height": 80])] {
            var response = fixture(); response[key] = value
            XCTAssertThrowsError(try CuaWindowCapture.decode(response, pid: 42, window: 7))
        }
    }

    func testEnvelopePreservesImageAlongsideStructuredFrame() throws {
        var response = fixture()
        let images = response.removeValue(forKey: "_cua_images")!
        let envelope: [String: Any] = ["request_id": "test", "envelope_version": 1, "ok": true,
            "completion_known": true, "result": ["structuredContent": response, "content": images]]
        let decoded = try CuaClient.decodeEnvelope(envelope, requestID: "test")
        XCTAssertEqual(try CuaWindowCapture.decode(decoded, pid: 42, window: 7).image.width, 40)
    }

    func testLiveWindowCapture() async throws {
        guard let app = ProcessInfo.processInfo.environment["CUA_WINDOW_APP"],
              let executable = ProcessInfo.processInfo.environment["CUA_DRIVER_PATH"] else {
            throw XCTSkip("Set CUA_WINDOW_APP and CUA_DRIVER_PATH to exercise installed driver")
        }
        let pid = await MainActor.run {
            NSWorkspace.shared.runningApplications.first {
                $0.bundleIdentifier == app || $0.localizedName == app
            }.map { Int($0.processIdentifier) }
        }
        let targetPID = try XCTUnwrap(pid)
        var params: [String: Any] = [:]
        if let id = ProcessInfo.processInfo.environment["CUA_WINDOW_ID"].flatMap(Int.init) { params["window_id"] = id }
        let client = CuaClient(executable: URL(fileURLWithPath: executable))
        do {
            let capture = try await CuaWindowCapture.capture(pid: targetPID, params: params) {
                try await client.call(tool: $0, arguments: $1)
            }
            XCTAssertGreaterThan(capture.image.width, 0)
            print("LIVE_WINDOW_CAPTURE width=\(capture.image.width) height=\(capture.image.height) bounds=\(capture.bounds)")
            await client.close()
        } catch {
            await client.close()
            throw error
        }
    }
}
