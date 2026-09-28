"""Mock-transport tests: never send commands to a real Buddy or desktop."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("buddy", Path(__file__).parents[1] / "scripts" / "buddy.py")
buddy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(buddy)


class MockBuddy(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(payload)
        body = self.server.reply(payload)
        self.send_response(self.server.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def log_message(self, *args):
        pass


class MockServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockBuddy)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/api/buddy/command"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.requests = []
        self.server.status_code = 200
        self.server.reply = lambda payload: {
            "status": 1, "data": {"id": payload["id"], "ok": True, "result": {"read": "Observed"}},
        }


class TransportTests(MockServerCase):
    def send(self, action="get_ui_tree", params=None, **kwargs):
        return buddy.command(action, {} if params is None else params, endpoint=self.endpoint, **kwargs)

    def test_observation_bounds_and_unknown_keys_fail_before_network(self):
        for action in ("get_ui_tree", "cua_observe"):
            for params in ({"max_elements": 800}, {"max_nodes": 800}, {"max_nodes": True},
                           {"max_nodes": 0}, {"max_depth": 31}, {"max_depth": 0}, {"max_depth": 1.5}):
                with self.assertRaisesRegex(buddy.BuddyError, "inspect mode navigation"):
                    self.send(action, params)
        self.assertEqual(self.server.requests, [])
        for action in ("get_ui_tree", "cua_observe"):
            self.send(action, {"app": "Calendar", "max_nodes": 500, "max_depth": 30})
        self.assertEqual(len(self.server.requests), 2)

    def test_cua_validation_rejects_trace_mistakes_without_network(self):
        base = {"snapshot_id": "cua-observed", "element_token": "s1:0", "ui_action": "press_key", "key": "t"}
        bad = [dict(base, window_id=61920), dict(base, ui_action="hotkey"),
               dict(base, key=""), dict(base, modifiers="cmd"), dict(base, modifiers=[{}]),
               dict(base, delivery_mode="automatic"), dict(base, pid=123),
               {"snapshot_id": "cua-observed", "window_id": 61920, "ui_action": "press_key", "key": "t"},
               {"snapshot_id": "cua-observed", "element_token": "s1:0", "ui_action": "type_text", "text": ""},
               {"snapshot_id": "cua-observed", "element_token": "s1:0", "ui_action": "click", "ax_action": "unknown"}]
        for params in bad:
            with self.subTest(params=params), self.assertRaises(buddy.BuddyValidationError):
                self.send("cua_action", params)
        self.assertEqual(self.server.requests, [])

    def test_valid_cua_actions_forward_exactly_once_without_rewriting(self):
        base = {"snapshot_id": "cua-observed", "element_token": "s1:0"}
        for extra in ({"ui_action": "click", "ax_action": "open"},
                      {"ui_action": "press_key", "key": "t", "modifiers": ["cmd", "shift"], "delivery_mode": "foreground"},
                      {"ui_action": "type_text", "text": "Ngày 31/12/2026"}):
            params = dict(base, **extra)
            self.send("cua_action", params)
            self.assertEqual(self.server.requests[-1]["params"], params)
        self.assertEqual(len(self.server.requests), 3)

    def test_command_preserves_unicode_nested_params_and_cancellation_id(self):
        params = {"from": {"x": -20, "y": 7}, "text": "Tiếng Việt `$(private)`\n"}
        response = self.send("drag", params, command_id="known-unique-id", timeout_ms=60000)
        self.assertTrue(response["ok"])
        self.assertEqual(self.server.requests[0], {
            "id": "known-unique-id", "action": "drag", "params": params, "timeout_ms": 60000,
        })

    def test_unique_command_ids(self):
        self.send()
        self.send()
        self.assertNotEqual(self.server.requests[0]["id"], self.server.requests[1]["id"])

    def test_rejected_command_is_not_retried(self):
        self.server.reply = lambda payload: {
            "status": 1, "data": {"id": payload["id"], "ok": False, "error": "buddy paused by user"},
        }
        with self.assertRaisesRegex(buddy.BuddyError, "paused"):
            self.send("type_text", {"text": "do not repeat"})
        self.assertEqual(len(self.server.requests), 1)

    def test_device_http_error_keeps_reason(self):
        self.server.status_code = 502
        self.server.reply = lambda _: {"status": 0, "message": "no buddy connected"}
        with self.assertRaisesRegex(buddy.BuddyError, "no buddy connected"):
            self.send()

    def test_response_id_must_match(self):
        self.server.reply = lambda _: {"status": 1, "data": {"id": "wrong", "ok": True, "result": {}}}
        with self.assertRaisesRegex(buddy.BuddyError, "ID mismatch"):
            self.send()

    def test_malformed_envelopes_fail(self):
        for body in [b"not json", [], {"status": True}, {"status": 1, "data": []}]:
            with self.subTest(body=body):
                self.server.reply = lambda _, body=body: body
                with self.assertRaises(buddy.BuddyError):
                    self.send()

    def test_invalid_inputs_do_not_reach_network(self):
        for timeout in [0, 499, 60001, True, 1.5]:
            with self.subTest(timeout=timeout), self.assertRaises(buddy.BuddyError):
                self.send(timeout_ms=timeout)
        with self.assertRaises(buddy.BuddyError):
            self.send(params=[])
        with self.assertRaises(buddy.BuddyError):
            self.send(action="a" * 65)
        with self.assertRaises(buddy.BuddyError):
            self.send(command_id="é" * 65)
        self.assertEqual(self.server.requests, [])

    def test_response_size_is_bounded(self):
        with patch.object(buddy, "MAX_RESPONSE_BYTES", 8), self.assertRaisesRegex(buddy.BuddyError, "exceeds"):
            self.send()


class ScreenshotTests(unittest.TestCase):
    def response(self):
        # A framing fixture, not a screenshot of the user's desktop.
        content = b"\xff\xd8\xff\xe0test\xff\xd9"
        return {"id": "shot", "ok": True, "result": {
            "path": "/Users/mac/remote.jpg", "image_b64": base64.b64encode(content).decode(),
            "bytes": len(content), "mime": "image/jpeg", "width": 1200, "height": 800,
            "image_to_global_points": {"origin_x": -1920, "origin_y": 100, "scale_x": 1.6, "scale_y": 1.6},
        }}

    def test_decode_on_device_preserve_geometry_without_base64(self):
        with tempfile.TemporaryDirectory() as folder:
            first = buddy.save_screenshot(self.response(), folder)
            second = buddy.save_screenshot(self.response(), folder)
            result = first["result"]
            path = Path(result["local_image_path"])
            self.assertNotEqual(path, Path(second["result"]["local_image_path"]))
            self.assertTrue(path.read_bytes().startswith(b"\xff\xd8\xff"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            metadata = Path(result["metadata_path"])
            self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)
            self.assertEqual(json.loads(metadata.read_text()), first)
            self.assertNotIn("image_b64", json.dumps(first))
            self.assertEqual(result["mac_image_path"], "/Users/mac/remote.jpg")
            self.assertEqual(result["image_to_global_points"]["origin_x"], -1920)

    def test_bad_images_leave_no_files(self):
        for change in [{"image_b64": "bad$"}, {"mime": "image/png"}, {"bytes": 1}, {"width": 0},
                       {"image_b64": base64.b64encode(b"not JPEG").decode()}]:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                data = self.response()
                data["result"].update(change)
                with self.assertRaises(buddy.BuddyError):
                    buddy.save_screenshot(data, folder)
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_cli_forces_image_transport_and_prints_local_file(self):
        with tempfile.TemporaryDirectory() as folder:
            params = Path(folder) / "params.json"
            params.write_text('{"return_format":"path","scale":0.5}')
            output = io.StringIO()
            with patch.object(buddy, "command", return_value=self.response()) as send, patch("sys.stdout", output):
                status = buddy.main(["screenshot", "--params-file", str(params), "--output-dir", folder])
            self.assertEqual(status, 0)
            self.assertEqual(send.call_args.args[1], {"return_format": "base64", "scale": 0.5})
            self.assertTrue(Path(json.loads(output.getvalue())["result"]["local_image_path"]).exists())
            self.assertNotIn("image_b64", output.getvalue())

    def test_window_capture_rejects_old_companion_display_response(self):
        with patch.object(buddy, "command", return_value=self.response()), patch.object(buddy, "save_screenshot") as save, patch("sys.stderr", io.StringIO()):
            self.assertEqual(buddy.main(["screenshot", "--params", '{"app":"Calendar"}']), 1)
            save.assert_not_called()

    def test_window_capture_keeps_window_metadata(self):
        data = self.response()
        data["result"].update(capture_scope="window", window_id=42, pid=123)
        with tempfile.TemporaryDirectory() as folder, patch.object(buddy, "command", return_value=data), patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(buddy.main(["screenshot", "--params", '{"app":"Calendar","window_id":42}', "--output-dir", folder]), 0)
            capture = json.loads(output.getvalue())["result"]
            self.assertEqual(capture["capture_scope"], "window")
            self.assertEqual(capture["window_id"], 42)

    def test_cli_failure_exits_nonzero(self):
        output = io.StringIO()
        with patch.object(buddy, "command", side_effect=buddy.BuddyError("paused")), patch("sys.stderr", output):
            self.assertEqual(buddy.main(["get_ui_tree"]), 1)
        self.assertEqual(json.loads(output.getvalue()), {"ok": False, "error": "paused"})

    def test_retention_keeps_recent_pairs_and_ignores_unrelated_or_symlinked_files(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as other:
            unrelated = Path(folder) / "unrelated.jpg"
            unrelated.write_bytes(b"keep")
            external = Path(other) / "screen-external.jpg"
            external.write_bytes(b"private")
            link = Path(folder) / "screen-link.jpg"
            link.symlink_to(external)
            with patch.object(buddy, "MAX_CAPTURES", 2):
                old = buddy.save_screenshot(self.response(), folder)
                recent = buddy.save_screenshot(self.response(), folder)
                latest = buddy.save_screenshot(self.response(), folder)
            self.assertFalse(Path(old["result"]["local_image_path"]).exists())
            self.assertFalse(Path(old["result"]["metadata_path"]).exists())
            self.assertTrue(Path(recent["result"]["local_image_path"]).exists())
            self.assertTrue(Path(latest["result"]["metadata_path"]).exists())
            self.assertEqual(unrelated.read_bytes(), b"keep")
            self.assertTrue(link.is_symlink())
            self.assertEqual(external.read_bytes(), b"private")

    def test_capture_directory_must_be_private_and_not_a_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "target"
            target.mkdir(mode=0o700)
            link = Path(folder) / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(buddy.BuddyError, "private"):
                buddy.save_screenshot(self.response(), link)
            target.chmod(0o755)
            with self.assertRaisesRegex(buddy.BuddyError, "private"):
                buddy.save_screenshot(self.response(), target)


class SuggestTests(MockServerCase):
    def setUp(self):
        super().setUp()
        self.choice = {"snapshot_id": "fresh-snapshot", "ref": "button-1", "ui_action": "press"}
        self.target = {"role": "AXButton", "title": "Tìm kiếm", "description": "Search the page"}
        self.server.reply = lambda _: {"status": 1, "data": {"suggestion": self.choice, "target": self.target, "reason": "selected"}}

    def send_suggest(self, goal="Find search", params=None):
        return buddy.suggest(goal, {} if params is None else params, endpoint=self.endpoint)

    def test_suggestion_is_only_advice_and_preserves_unicode(self):
        with patch.object(buddy, "command") as command:
            result = self.send_suggest("Tìm ô tìm kiếm", {"app": "Safari"})
        self.assertEqual(result, {"ok": True, "suggestion": self.choice, "target": self.target, "reason": "selected"})
        self.assertEqual(self.server.requests, [{"goal": "Tìm ô tìm kiếm", "app": "Safari"}])
        command.assert_not_called()

    def test_null_suggestion_is_normal_fallback(self):
        for reason in ["disabled", "timeout", "uncertain"]:
            with self.subTest(reason=reason):
                self.server.reply = lambda _, reason=reason: {"status": 1, "data": {"suggestion": None, "reason": reason}}
                self.assertEqual(self.send_suggest(), {"ok": True, "suggestion": None, "reason": reason})

    def test_invalid_inputs_do_not_reach_network(self):
        for goal in [None, True, " ", "x" * 2001]:
            with self.subTest(goal=goal), self.assertRaises(buddy.BuddyError):
                self.send_suggest(goal)
        for params in [[], {"app": None}, {"app": ""}, {"app": " "}, {"app": "x" * 257},
                       {"snapshot_id": "stale"}, {"ref": "button"}, {"ui_action": "press"}]:
            with self.subTest(params=params), self.assertRaises(buddy.BuddyError):
                self.send_suggest(params=params)
        self.assertEqual(self.server.requests, [])

    def test_unicode_limits_count_characters(self):
        self.send_suggest("ệ" * 2000, {"app": "ệ" * 256})
        self.assertEqual(len(self.server.requests), 1)

    def test_invalid_results_are_rejected(self):
        invalid_data = [{}, {"suggestion": None}, {"suggestion": None, "reason": 1}]
        for choice in [[], {}, dict(self.choice, snapshot_id=" "), dict(self.choice, ref=1),
                       dict(self.choice, ui_action="type_text"), dict(self.choice, text="injected")]:
            invalid_data.append({"suggestion": choice, "target": self.target, "reason": "selected"})
        for target in [None, {}, [], dict(self.target, title=1), dict(self.target, role="r" * 129),
                       dict(self.target, title="t" * 2001), dict(self.target, description="d" * 2001),
                       dict(self.target, text="injected")]:
            invalid_data.append({"suggestion": self.choice, "target": target, "reason": "selected"})
        invalid_data.append({"suggestion": self.choice, "reason": "selected"})
        invalid_data.append({"suggestion": None, "target": self.target, "reason": "disabled"})
        for data in invalid_data:
            with self.subTest(data=data):
                self.server.reply = lambda _, data=data: {"status": 1, "data": data}
                with self.assertRaises(buddy.BuddyError):
                    self.send_suggest()
        for body in [b"not json", [], {"status": True}, {"status": 0, "message": "failed"}]:
            with self.subTest(body=body):
                self.server.reply = lambda _, body=body: body
                with self.assertRaises(buddy.BuddyError):
                    self.send_suggest()

    def test_http_failure_does_not_retry_or_execute(self):
        self.server.status_code = 502
        self.server.reply = lambda _: {"status": 0, "message": "no buddy connected"}
        with patch.object(buddy, "command") as command, self.assertRaisesRegex(buddy.BuddyError, "no buddy connected"):
            self.send_suggest()
        command.assert_not_called()
        self.assertEqual(len(self.server.requests), 1)

    def test_response_size_is_bounded(self):
        with patch.object(buddy, "MAX_SUGGEST_RESPONSE_BYTES", 8), self.assertRaisesRegex(buddy.BuddyError, "exceeds"):
            self.send_suggest()

    def test_timeout_uses_fixed_budget_without_proxy_retry_or_mutation(self):
        with patch.object(buddy.urllib.request, "build_opener") as build, patch.object(buddy, "command") as command:
            build.return_value.open.side_effect = TimeoutError("timeout")
            with self.assertRaisesRegex(buddy.BuddyError, "no action was executed"):
                buddy.suggest("Find search", {})
            build.return_value.open.assert_called_once()
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, buddy.SUGGEST_ENDPOINT)
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 15)
            self.assertEqual(build.call_args.args[0].proxies, {})
            command.assert_not_called()

    def test_cli_with_mock_http_returns_advice_or_fallback_without_command(self):
        original = buddy.suggest
        for choice in [None, self.choice]:
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as folder:
                params = Path(folder) / "params.json"
                params.write_text('{"app":"Safari"}')
                self.server.reply = lambda _, choice=choice: {"status": 1, "data": {
                    "suggestion": choice, "target": self.target if choice else None, "reason": "test"}}
                output = io.StringIO()
                with patch.object(buddy, "suggest", side_effect=lambda goal, params: original(goal, params, endpoint=self.endpoint)), \
                     patch.object(buddy, "command") as command, patch("sys.stdout", output):
                    self.assertEqual(buddy.main(["suggest", "--goal", "Find search", "--params-file", str(params)]), 0)
                expected = {"ok": True, "suggestion": choice, "reason": "test"}
                if choice:
                    expected["target"] = self.target
                self.assertEqual(json.loads(output.getvalue()), expected)
                command.assert_not_called()

    def test_cli_missing_goal_and_incompatible_arguments_fail_without_network(self):
        for args in [["suggest"], ["suggest", "--goal", "Find", "--id", "id"],
                     ["suggest", "--goal", "Find", "--question", "other"],
                     ["suggest", "--goal", "Find", "--timeout-ms", "1000"],
                     ["get_ui_tree", "--goal", "Find"]]:
            with self.subTest(args=args), patch("sys.stderr", io.StringIO()), patch.object(buddy, "command") as command:
                self.assertEqual(buddy.main(args), 1)
                command.assert_not_called()
        self.assertEqual(self.server.requests, [])


class ObserveTests(MockServerCase):
    def setUp(self):
        super().setUp()
        self.server.reply = lambda _: {"status": 1, "data": {
            "description": "The search box is visible at screenshot pixel (320, 80).",
            "screenshot": {"width": 1200, "height": 800,
                           "image_to_global_points": {"origin_x": -1920, "origin_y": 0, "scale_x": 1.6, "scale_y": 1.6}},
        }}

    def send_observe(self, question="Find the search box", params=None):
        return buddy.observe(question, {} if params is None else params, endpoint=self.endpoint)

    def test_observe_preserves_description_and_geometry(self):
        result = self.send_observe("Tìm ô tìm kiếm", {"display_id": 123})
        self.assertTrue(result["ok"])
        self.assertIn("search box", result["description"])
        self.assertEqual(result["screenshot"]["image_to_global_points"]["origin_x"], -1920)
        self.assertEqual(self.server.requests, [{"question": "Tìm ô tìm kiếm", "display_id": 123, "scale": 0.5}])

    def test_observe_forwards_window_target(self):
        self.send_observe(params={"app": "com.apple.iCal", "window_id": 42})
        self.assertEqual(self.server.requests[0]["app"], "com.apple.iCal")
        self.assertEqual(self.server.requests[0]["window_id"], 42)
        self.assertEqual(self.server.requests[0]["scale"], 1)
        self.assertNotIn("display_id", self.server.requests[0])

    def test_observe_missing_description_and_raw_base64_rejected(self):
        for data in [{"screenshot": {}}, {"description": "", "screenshot": {}},
                     {"description": "visible", "screenshot": {"image_b64": "unexpected"}}]:
            with self.subTest(data=data):
                self.server.reply = lambda _, data=data: {"status": 1, "data": data}
                with self.assertRaises(buddy.BuddyError):
                    self.send_observe()

    def test_observe_validation_prevents_invalid_network_requests(self):
        for question in [None, " ", "x" * 2001]:
            with self.subTest(question=question), self.assertRaises(buddy.BuddyError):
                self.send_observe(question)
        for params in [{"display_id": 0}, {"display_id": -1}, {"display_id": True}, {"display_id": 4294967296},
                       {"app": " "}, {"app": True}, {"app": "a" * 257}, {"window_id": 42},
                       {"app": "Calendar", "window_id": True}, {"app": "Calendar", "window_id": 0},
                       {"app": "Calendar", "window_id": 4294967296}, {"app": "Calendar", "display_id": 1},
                       {"scale": 0}, {"scale": 1.1}, {"scale": float("nan")}, {"scale": True}, {"path": "/private/file"}]:
            with self.subTest(params=params), self.assertRaises(buddy.BuddyError):
                self.send_observe(params=params)
        self.assertEqual(self.server.requests, [])

    def test_observe_error_preserves_upstream_reason_and_does_not_retry(self):
        self.server.status_code = 502
        self.server.reply = lambda _: {"status": 0, "message": "vision model not configured"}
        with self.assertRaisesRegex(buddy.BuddyError, "vision model not configured"):
            self.send_observe()
        self.assertEqual(len(self.server.requests), 1)

    def test_observe_cli_uses_dedicated_endpoint_not_command(self):
        output = io.StringIO()
        with patch.object(buddy, "observe", return_value={"ok": True, "description": "visible", "screenshot": {}}) as observe, \
             patch.object(buddy, "command") as command, patch("sys.stdout", output):
            self.assertEqual(buddy.main(["observe", "--question", "Find search", "--params", '{"scale":0.25}']), 0)
        observe.assert_called_once_with("Find search", {"scale": 0.25})
        command.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["description"], "visible")


if __name__ == "__main__":
    unittest.main()
