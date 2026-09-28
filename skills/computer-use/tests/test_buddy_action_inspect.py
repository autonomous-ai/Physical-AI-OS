"""One-shot action/observation contract; no desktop or network access."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("buddy_action_client", SCRIPTS / "buddy.py")
buddy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(buddy)
INSPECT_SPEC = importlib.util.spec_from_file_location("buddy_inspect", SCRIPTS / "buddy_inspect.py")
inspection = importlib.util.module_from_spec(INSPECT_SPEC)
INSPECT_SPEC.loader.exec_module(inspection)


class ActionInspectTests(unittest.TestCase):
    def setUp(self):
        self.modules = patch.dict(sys.modules, {"buddy_inspect": inspection})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.calls = []
        self.action_response = {"ok": True, "id": "action-id", "result": {"dispatched": True}}
        self.desktop = {"paused": False, "accessibility": True,
                        "capabilities": ["target_app_input"], "protocol_version": 2}
        self.tree = {"snapshot_id": "fresh-token", "frontmost": True, "truncated": False,
                     "nodes": [{"ref": "new", "role": "AXStaticText", "secure": False, "value": "new state"}]}

    def command(self, action, params, *args, **kwargs):
        self.calls.append((action, params, kwargs))
        if action == "desktop_info":
            return {"id": "desktop-id", "result": self.desktop}
        if action == "get_ui_tree":
            return {"id": "tree-id", "result": self.tree}
        return self.action_response

    def run_action(self, **kwargs):
        return buddy.action_and_inspect("key_combo", {"app": "Calendar", "keys": ["cmd", "t"]},
                                        {"app": "Calendar"}, **kwargs)

    def test_one_action_then_fresh_observation_with_request_timings(self):
        with patch.object(buddy, "command", side_effect=self.command):
            result = self.run_action(command_id="action-id")
        self.assertEqual([x[0] for x in self.calls], ["key_combo", "desktop_info", "get_ui_tree"])
        self.assertIs(result["action"], self.action_response)
        self.assertTrue(result["ok"])
        self.assertFalse(result["retry_action"])
        self.assertEqual(result["inspection"]["observation"]["snapshot_id"], "fresh-token")
        self.assertEqual([x["id"] for x in result["inspection"]["timing"]["requests"]], ["desktop-id", "tree-id"])
        self.assertEqual(result["inspection"]["desktop"]["capabilities"], ["target_app_input"])
        self.assertEqual(result["inspection"]["desktop"]["protocol_version"], 2)
        self.assertGreaterEqual(result["timing"]["elapsed_ms"], 0)
        self.assertEqual(self.calls[0][2]["command_id"], "action-id")

    def test_unverified_dispatch_points_to_inspection_without_retry(self):
        for effect in ("suspected_noop", "unverifiable"):
            self.calls = []
            self.action_response["result"] = {"effect": effect}
            with patch.object(buddy, "command", side_effect=self.command):
                result = self.run_action()
            self.assertTrue(result["ok"])
            self.assertEqual(result["action"]["result"]["effect"], effect)
            self.assertIn("do not repeat", result["next_step"])
            self.assertFalse(result["retry_action"])
            self.assertEqual([x[0] for x in self.calls], ["key_combo", "desktop_info", "get_ui_tree"])

    def test_paused_after_action_preserves_outcome_and_stops(self):
        self.desktop["paused"] = True
        with patch.object(buddy, "command", side_effect=self.command):
            result = self.run_action()
        self.assertFalse(result["ok"])
        self.assertIs(result["action"], self.action_response)
        self.assertEqual(result["inspection"]["reason"], "desktop_paused_or_unknown")
        self.assertEqual([x[0] for x in self.calls], ["key_combo", "desktop_info"])

    def test_observation_error_retains_action_without_replay(self):
        def command(action, *args, **kwargs):
            if action == "desktop_info":
                self.calls.append((action, {}, {}))
                raise buddy.BuddyError("disconnected")
            return self.command(action, *args, **kwargs)
        with patch.object(buddy, "command", side_effect=command):
            result = self.run_action()
        self.assertFalse(result["ok"])
        self.assertIs(result["action"], self.action_response)
        self.assertEqual(result["inspection"]["error"], "disconnected")
        self.assertEqual([x[0] for x in self.calls], ["key_combo", "desktop_info"])

    def test_action_errors_never_trigger_observation_or_replay(self):
        for error in (buddy.BuddyError("paused"), buddy.BuddyError("permission denied"),
                      TimeoutError("unknown outcome")):
            with patch.object(buddy, "command", side_effect=error) as command:
                result = self.run_action(command_id="retained-id")
            self.assertEqual(command.call_count, 1)
            self.assertIsNone(result["inspection"])
            self.assertEqual(result["action"]["outcome"], "unconfirmed")
            self.assertEqual(result["action"]["id"], "retained-id")
            self.assertFalse(result["retry_action"])

    def test_local_cua_validation_distinguishes_unsent_from_unknown_outcome(self):
        with patch.object(buddy.urllib.request, "build_opener") as opener:
            result = buddy.action_and_inspect("cua_action", {
                "snapshot_id": "cua-observed", "window_id": 61920, "ui_action": "press_key", "key": "t"
            }, {"app": "Calendar"})
        opener.assert_not_called()
        self.assertEqual(result["action"]["outcome"], "not_sent")
        self.assertIn("element_token", result["action"]["error"])
        self.assertIsNone(result["inspection"])
        self.assertFalse(result["retry_action"])

    def test_invalid_observation_and_unsupported_actions_rejected_before_write(self):
        targets = [None, [], {}, {"app": ""}, {"app": "Calendar", "window_id": True},
                   {"app": "Calendar", "window_id": -1}, {"app": "Calendar", "scale": 1},
                   {"app": "Notes"}, {"app": "Calendar", "mode": "wrong"}]
        with patch.object(buddy, "command") as command:
            for target in targets:
                with self.assertRaises((ValueError, buddy.BuddyError)):
                    buddy.action_and_inspect("open_app", {"app": "Calendar"}, target)
            with self.assertRaises(buddy.BuddyError):
                buddy.action_and_inspect("read_clipboard", {}, {"app": "Calendar"})
        command.assert_not_called()

    def test_cli_output_preserves_failed_inspection_action_result(self):
        self.desktop["paused"] = True
        output = io.StringIO()
        with patch.object(buddy, "command", side_effect=self.command), contextlib.redirect_stdout(output):
            status = buddy.main(["open_app", "--params", '{"app":"Calendar"}',
                                 "--inspect-after", '{"app":"Calendar"}'])
        self.assertEqual(status, 1)
        self.assertTrue(json.loads(output.getvalue())["action"]["ok"])

    def test_cua_action_keeps_snapshot_and_returns_fresh_tokens(self):
        self.desktop["cua"] = {"installed": True, "enabled": True}
        fresh = {"snapshot_id": "fresh-cua", "elements": [{"element_token": "fresh-element"}]}
        def command(action, params, *args, **kwargs):
            if action == "cua_observe":
                self.calls.append((action, params, kwargs))
                return {"id": "observe-id", "result": fresh}
            return self.command(action, params, *args, **kwargs)
        action = {"snapshot_id": "old-cua", "element_token": "old-element", "ui_action": "click"}
        with patch.object(buddy, "command", side_effect=command):
            result = buddy.action_and_inspect("cua_action", action, {"app": "Calendar", "window_id": 7})
        self.assertEqual([x[0] for x in self.calls], ["cua_action", "cua_observe"])
        self.assertIs(self.calls[0][1], action)
        self.assertEqual(self.calls[-1][1], {"app": "Calendar", "window_id": 7})
        self.assertEqual(result["inspection"]["observation"]["snapshot_id"], fresh["snapshot_id"])

    def test_native_typed_action_observes_without_preflight(self):
        with patch.object(buddy, "command", side_effect=self.command):
            result = buddy.action_and_inspect("perform_ui_action", {"snapshot_id": "old", "ref": "r", "ui_action": "press"},
                                              {"app": "Calendar"})
        self.assertEqual([x[0] for x in self.calls], ["perform_ui_action", "get_ui_tree"])
        self.assertIsNone(result["inspection"]["desktop"])
        self.assertEqual(result["inspection"]["backend"], "native")
        self.assertEqual(result["inspection"]["backend_source"], "successful_action")

    def test_typed_observation_rejection_preserves_action_without_fallback(self):
        for action, observe in (("cua_action", "cua_observe"), ("perform_ui_action", "get_ui_tree")):
            for reason in ("buddy paused by user", "permission denied", "Cua disabled", "timeout"):
                self.calls.clear()
                def command(name, params, *args, **kwargs):
                    response = self.command(name, params, *args, **kwargs)
                    if name == observe:
                        raise buddy.BuddyError(reason)
                    return response
                with patch.object(buddy, "command", side_effect=command):
                    result = buddy.action_and_inspect(action, {}, {"app": "Calendar"})
                self.assertFalse(result["ok"])
                self.assertIs(result["action"], self.action_response)
                self.assertEqual(result["inspection"]["error"], reason)
                self.assertEqual([x[0] for x in self.calls], [action, observe])

    def test_native_window_target_rejected_before_action(self):
        with patch.object(buddy, "command") as command:
            with self.assertRaisesRegex(buddy.BuddyError, "window_id requires Cua"):
                buddy.action_and_inspect("perform_ui_action", {}, {"app": "Calendar", "window_id": 7})
        command.assert_not_called()

    def test_cli_invalid_followup_never_dispatches_action(self):
        for extra in (["--inspect-after", "{"], ["--inspect-after", "[]"],
                      ["--inspect-after", '{"app":"Calendar"}', "--question", "what?"],
                      ["--inspect-after", '{"app":"Calendar"}', "--output-dir", "/tmp"]):
            with patch.object(buddy, "command") as command, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(buddy.main(["open_app", "--params", '{"app":"Calendar"}', *extra]), 1)
            command.assert_not_called()

    def test_open_url_and_path_use_explicit_followup_target(self):
        for action, params, target in (
            ("open_url", {"url": "https://example.com", "browser": "safari"}, "Safari"),
            ("open_path", {"path": "~/Downloads", "mode": "reveal"}, "Finder"),
        ):
            self.calls.clear()
            with patch.object(buddy, "command", side_effect=self.command):
                result = buddy.action_and_inspect(action, params, {"app": target})
            self.assertTrue(result["ok"])
            self.assertEqual([x[0] for x in self.calls], [action, "desktop_info", "get_ui_tree"])
            self.assertEqual(self.calls[-1][1]["app"], target)

    def test_existing_cli_action_still_sends_only_one_command(self):
        with patch.object(buddy, "command", side_effect=self.command), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(buddy.main(["open_app", "--params", '{"app":"Calendar"}']), 0)
        self.assertEqual([x[0] for x in self.calls], ["open_app"])


if __name__ == "__main__":
    unittest.main()
