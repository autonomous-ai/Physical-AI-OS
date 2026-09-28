"""Observation compaction and preflight tests; no desktop/network access."""
import importlib.util
import json
import copy
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("buddy_inspect", Path(__file__).parents[1] / "scripts/buddy_inspect.py")
inspect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inspect)


def tree(nodes, **extra):
    return dict(snapshot_id="snapshot", expires_in_ms=30000, frontmost=True,
                truncated=False, nodes=nodes, **extra)


def node(ref, **extra):
    return dict(ref=ref, role="AXStaticText", secure=False, **extra)


class InspectTest(unittest.TestCase):
    def test_preserves_evidence_and_marks_missing_content(self):
        original = tree([node("root"), node("value", parent_ref="root", value="4"),
                         node("button", title="2", actions=["press"], enabled=True),
                         dict(ref="menu", role="AXMenu", secure=False, title="Menu"),
                         node("entry", parent_ref="menu", title="Delete")])
        result = inspect.compact_tree(original)
        self.assertEqual([n["ref"] for n in result["items"]], ["value", "button"])
        self.assertEqual(result["items"][0]["value"], "4")
        self.assertEqual(result["snapshot_id"], "snapshot")
        self.assertIn("compact_output_omits_content", result["issues"])
        self.assertEqual(len(original["nodes"]), 5)

    def test_secure_and_unknown_ancestors_hide_descendants(self):
        for privacy in (True, None):
            parent = dict(ref="parent", role="AXGroup", secure=privacy, value="secret")
            result = inspect.compact_tree(tree([parent, node("child", parent_ref="parent", value="password")]))
            self.assertEqual(result["items"], [])

    def test_invalid_ancestry_rejected(self):
        for nodes in ([node("a", parent_ref="missing")], [node("a", parent_ref="b"), node("b", parent_ref="a")], [node("a"), node("a")]):
            with self.assertRaises(ValueError):
                inspect.compact_tree(tree(nodes))

    def test_bounded_output_never_claims_completeness(self):
        original = tree([node(str(i), value="x" * 500) for i in range(150)])
        original.update(frontmost=False, truncated=True)
        result = inspect.compact_tree(original)
        self.assertEqual(len(result["items"]), 120)
        self.assertTrue(result["text_clipped"])
        self.assertEqual(len(result["items"][0]["value"]), 240)
        self.assertIn("app_not_frontmost", result["issues"])
        self.assertIn("tree_incomplete", result["issues"])

    def test_permission_and_pause_block_tree(self):
        for desktop in ({"paused": True, "accessibility": True}, {"paused": False, "accessibility": False}, {}):
            calls = []
            def command(action, params):
                calls.append(action)
                return {"result": desktop}
            self.assertFalse(inspect.inspect_ui({}, command)["ok"])
            self.assertEqual(calls, ["desktop_info"])

    def test_missing_screen_permission_still_allows_accessibility(self):
        calls = []
        def command(action, params):
            calls.append((action, params))
            if action == "desktop_info":
                return {"result": {"paused": False, "accessibility": True, "screen_recording": False}}
            return {"result": tree([node("a", value="Today")])}
        result = inspect.inspect_ui({"app": "Calendar"}, command)
        self.assertTrue(result["ok"])
        self.assertIn("screen_recording_unavailable", result["observation"]["issues"])
        self.assertEqual([c[0] for c in calls], ["desktop_info", "get_ui_tree"])
        self.assertEqual(calls[1][1], {"app": "Calendar", "max_nodes": 500, "max_depth": 12})

    def test_bad_params_make_no_call(self):
        for params in ({"app": ""}, {"goal": "invent a filter"},
                       {"window_id": True}, {"window_id": 0}, {"window_id": -1},
                       {"window_id": "123"}, {"window_id": 1.5}):
            with self.assertRaises(ValueError):
                inspect.inspect_ui(params, lambda *args: self.fail("unexpected native call"))

    def test_cua_preserves_upstream_observation_without_native_permission_or_jev(self):
        observation = dict(backend="cua", pid=123, window_id=456,
                           snapshot_id="local-token", cua_snapshot_id="upstream-token",
                           elements=[{"element_index": 1, "label": "Equals"}],
                           tree_markdown="AXStaticText: 4", elements_complete=True,
                           expires_in_ms=30000)
        calls = []
        def command(action, params):
            calls.append((action, params))
            if action == "desktop_info":
                return {"result": {"paused": False, "accessibility": False,
                                   "screen_recording": False,
                                   "cua": {"installed": True, "enabled": True}}}
            return {"result": observation}
        result = inspect.inspect_ui({"app": "Calculator", "window_id": 456}, command)
        self.assertTrue(result["ok"])
        self.assertEqual(result["observation"].get("tree_markdown"), observation.get("tree_markdown"))
        self.assertEqual(calls, [("desktop_info", {}),
                                 ("cua_observe", {"app": "Calculator", "window_id": 456})])

    def test_cua_window_selection_is_preserved_without_retry(self):
        observation = dict(backend="cua", pid=123, requires_window_selection=True,
                           windows=[{"window_id": 456}, {"window_id": 789}])
        calls = []
        def command(action, params):
            calls.append(action)
            if action == "desktop_info":
                return {"result": {"paused": False, "cua": {"installed": True, "enabled": True}}}
            return {"result": observation}
        result = inspect.inspect_ui({}, command)
        self.assertEqual(result["observation"].get("tree_markdown"), observation.get("tree_markdown"))
        self.assertNotIn("snapshot_id", result["observation"])
        self.assertEqual(calls, ["desktop_info", "cua_observe"])

    def test_cua_failure_never_falls_back(self):
        for error in (TimeoutError("timeout"), PermissionError("permission denied"), RuntimeError("driver stopped")):
            calls = []
            def command(action, params):
                calls.append(action)
                if action == "desktop_info":
                    return {"result": {"paused": False, "accessibility": True,
                                       "cua": {"installed": True, "enabled": True}}}
                raise error
            with self.assertRaises(type(error)):
                inspect.inspect_ui({}, command)
            self.assertEqual(calls, ["desktop_info", "cua_observe"])

    def test_native_fallback_only_when_cua_disabled_or_absent(self):
        for cua in ({"installed": True, "enabled": False},
                    {"installed": False, "enabled": True}, None):
            calls = []
            def command(action, params):
                calls.append(action)
                if action == "desktop_info":
                    return {"result": {"paused": False, "accessibility": True, "cua": cua}}
                return {"result": tree([node("value", value="4")])}
            self.assertTrue(inspect.inspect_ui({}, command)["ok"])
            self.assertEqual(calls, ["desktop_info", "get_ui_tree"])
            calls.clear()
            with self.assertRaisesRegex(ValueError, "window_id requires Cua"):
                inspect.inspect_ui({"window_id": 456}, command)
            self.assertEqual(calls, ["desktop_info"])

    def test_modes_and_incomplete_auto_replace_snapshot_once_on_each_route(self):
        for backend in ("cua", "native"):
            for known in (None, backend):
                for mode in ("auto", "navigation", "detail"):
                    calls = []
                    observations = []
                    def command(action, params):
                        calls.append((action, params))
                        if action == "desktop_info":
                            return {"result": {"paused": False, "accessibility": True,
                                               "cua": {"installed": backend == "cua", "enabled": True}}}
                        token = "fresh" if params.get("max_depth") in (2, 4) else "old"
                        value = ({"snapshot_id": token, "elements_complete": False, "tree_truncated": True, "elements": [{"element_token": token}]}
                                 if backend == "cua" else dict(tree([node(token, value=token)]), truncated=True))
                        value["snapshot_id"] = token
                        observations.append(value)
                        return {"id": str(len(calls)), "result": value}
                    target = {"app": "Calendar", "mode": mode}
                    if backend == "cua":
                        target["window_id"] = 7
                    result = inspect.inspect_ui(target, command, known_backend=known)
                    expected_reads = 2 if mode == "auto" else 1
                    self.assertEqual(len(observations), expected_reads)
                    self.assertEqual(result["observation"]["snapshot_id"], "old" if mode == "detail" else "fresh")
                    self.assertEqual(result["navigation_only"], mode != "detail")
                    if mode != "detail":
                        self.assertFalse(result["content_complete"])
                        self.assertNotIn('"old"', json.dumps(result["observation"]))
                    for action, params in calls:
                        self.assertNotIn("mode", params)
                        if action != "desktop_info":
                            self.assertEqual(params["app"], "Calendar")
                            if backend == "cua":
                                self.assertEqual(params["window_id"], 7)
                    self.assertEqual(len(result["timing"]["requests"]), len(calls))

    def test_incomplete_auto_second_read_error_never_falls_back(self):
        for backend in ("cua", "native"):
            for known in (None, backend):
                calls = []
                def command(action, params):
                    calls.append(action)
                    if action == "desktop_info":
                        return {"result": {"paused": False, "accessibility": True,
                                           "cua": {"installed": backend == "cua", "enabled": True}}}
                    if params.get("max_depth") in (2, 4):
                        raise PermissionError("paused or permission revoked")
                    return {"result": {"elements_complete": False, "tree_truncated": True} if backend == "cua" else dict(tree([]), truncated=True)}
                with self.assertRaises(PermissionError):
                    inspect.inspect_ui({"app": "Calendar"}, command, known_backend=known)
                self.assertEqual(calls.count("cua_observe" if backend == "cua" else "get_ui_tree"), 2)
                self.assertNotIn("get_ui_tree" if backend == "cua" else "cua_observe", calls)

    def test_navigation_menu_refs_preserved_but_secure_ancestors_always_hidden(self):
        nodes = [dict(node("menu", title="Go"), role="AXMenu"),
                 dict(node("go-date", parent_ref="menu", title="Go to Date", actions=["press"], enabled=True), role="AXMenuItem"),
                 dict(node("secure-menu", title="Private"), role="AXMenu", secure=True),
                 dict(node("secret", parent_ref="secure-menu", title="Hidden", actions=["press"], enabled=True), role="AXMenuItem"),
                 dict(node("unknown-menu", title="Unknown"), role="AXMenu", secure=None),
                 node("unknown-child", parent_ref="unknown-menu", title="Hidden too")]
        for mode in ("navigation", "detail"):
            def command(action, params):
                return {"result": tree(nodes)}
            result = inspect.inspect_ui({"mode": mode}, command, known_backend="native")
            refs = [item["ref"] for item in result["observation"]["items"]]
            self.assertNotIn("secret", refs)
            self.assertNotIn("unknown-child", refs)
            if mode == "navigation":
                self.assertIn("go-date", refs)
                self.assertEqual(result["observation"]["items"][0]["parent_ref"], "menu")
                self.assertFalse(result["content_complete"])
            else:
                self.assertNotIn("go-date", refs)

    def test_navigation_prioritizes_enabled_controls_under_output_limit(self):
        original = tree([node(str(i), value="day") for i in range(140)] +
                        [node("navigate", title="Go to Date", actions=["press"], enabled=True)])
        result = inspect.compact_tree(original, navigation=True)
        self.assertEqual(result["items"][0]["ref"], "navigate")
        self.assertEqual(len(result["items"]), 120)
        self.assertEqual(result["omitted_nodes"], 21)
        self.assertIn("compact_output_omits_content", result["issues"])

    def test_cua_compaction_removes_driver_advice_preserves_unique_tree_evidence(self):
        raw = {"snapshot_id": "snapshot", "cua_snapshot_id": "upstream", "elements_complete": False,
               "elements": [{"element_token": "upstream:1", "role": "AXButton", "actions": ["AXPress"],
                             "frame": {"x": 1, "y": 2, "width": 10, "height": 10}, "secure": False}],
               "tree_markdown": "AXStaticText: unique appointment at 10:30", "truncated": True,
               "_note": "Increase max_elements to see more"}
        result = inspect.compact_cua(raw)
        self.assertNotIn("_note", result)
        self.assertIn("_note", raw)
        for key, value in raw.items():
            if key not in ("_note", "elements"):
                self.assertEqual(result[key], value)
        self.assertEqual({key: value for key, value in result["elements"][0].items() if key != "window_geometry"}, raw["elements"][0])
        self.assertEqual(result["tree_markdown"], raw["tree_markdown"])

    def test_cua_note_removed_in_full_navigation_and_auto_outputs(self):
        for mode in ("auto", "detail", "navigation"):
            def command(action, params):
                return {"result": {"snapshot_id": "fresh", "elements_complete": False,
                                   "elements": [], "tree_markdown": "tree-only text",
                                   "_note": "max_elements"}}
            result = inspect.inspect_ui({"mode": mode}, command, known_backend="cua")
            self.assertNotIn("_note", result["observation"])
            self.assertEqual(result["observation"]["tree_markdown"], "tree-only text")
            self.assertFalse(result["observation"]["elements_complete"])

    def test_cua_geometry_annotations_keep_evidence_and_input_immutable(self):
        elements = [{"element_index": 0, "role": "AXWindow", "frame": {"x": 10, "y": 20, "w": 100, "h": 100}}]
        for index, (x, y, w, h) in enumerate(((20, 30, 10, 10), (5, 25, 10, 10), (0, 0, 10, 20)), 1):
            elements.append({"element_index": index, "element_token": f"s:{index}", "role": "AXButton",
                             "parent_index": 0, "actions": ["AXPress"], "secure": False,
                             "frame": {"x": x, "y": y, "w": w, "h": h}})
        raw = {"elements": elements, "tree_markdown": "2026", "elements_complete": False}
        before = copy.deepcopy(raw)
        result = inspect.compact_cua(raw)
        self.assertEqual([item["window_geometry"] for item in result["elements"]],
                         ["inside_window", "inside_window", "partially_visible", "outside_window"])
        for original, annotated in zip(elements, result["elements"]):
            self.assertEqual({key: value for key, value in annotated.items() if key != "window_geometry"}, original)
        self.assertEqual(raw, before)
        self.assertEqual(result["tree_markdown"], "2026")
        self.assertFalse(result["elements_complete"])
        self.assertNotIn("interaction_hint", result)

    def test_cua_menu_descendants_exempt_from_window_geometry(self):
        frame = {"x": 500, "y": 500, "w": 10, "h": 10}
        raw = {"elements": [
            {"element_index": 0, "role": "AXWindow", "frame": {"x": 0, "y": 0, "w": 100, "h": 100}},
            {"element_index": 1, "role": "AXMenuBar", "frame": frame},
            {"element_index": 2, "role": "AXGroup", "parent_index": 1, "frame": frame},
            {"element_index": 3, "role": "AXButton", "parent_index": 2, "frame": frame}]}
        result = inspect.compact_cua(raw)
        for element in result["elements"][1:]:
            self.assertEqual(element["window_geometry"], "unknown")
            self.assertEqual(element["window_geometry_exemption"], "menu")

    def test_cua_bad_geometry_or_ancestry_never_guesses(self):
        window = {"element_index": 0, "role": "AXWindow", "frame": {"x": 0, "y": 0, "w": 100, "h": 100}}
        valid = {"x": 5, "y": 5, "w": 10, "h": 10}
        invalid = [None, {}, {"x": 5, "y": 5, "w": 0, "h": 10},
                   {"x": True, "y": 5, "w": 10, "h": 10},
                   {"x": float("nan"), "y": 5, "w": 10, "h": 10},
                   {"x": float("inf"), "y": 5, "w": 10, "h": 10}]
        for frame in invalid:
            result = inspect.compact_cua({"elements": [window, {"frame": frame}]})
            self.assertEqual(result["elements"][1]["window_geometry"], "unknown")
        for ancestors in ([{"element_index": 1, "parent_index": 8, "frame": valid}],
                          [{"element_index": 1, "parent_index": 1, "frame": valid}],
                          [{"element_index": 1, "parent_index": [], "frame": valid}]):
            result = inspect.compact_cua({"elements": [window, *ancestors]})
            self.assertEqual(result["elements"][-1]["window_geometry"], "unknown")
        for windows in ([], [window, dict(window, element_index=2)], [dict(window, frame={})]):
            result = inspect.compact_cua({"elements": windows + [{"frame": valid}]})
            self.assertEqual(result["elements"][-1]["window_geometry"], "unknown")

    def test_cua_sheet_and_explicit_modal_hint_only_from_observation(self):
        for modal in ({"role": "AXSheet"}, {"role": "AXWindow", "modal": True}):
            result = inspect.compact_cua({"elements": [modal]})
            self.assertIn("dialog", result["interaction_hint"])
        self.assertNotIn("interaction_hint", inspect.compact_cua({"elements": [{"role": "AXWindow", "modal": "true"}]}))

    def test_cua_structured_incomplete_does_not_discard_complete_markdown(self):
        for known in (None, "cua"):
            calls = []
            raw = {"snapshot_id": "full", "elements_complete": False, "total_element_count": 151,
                   "returned_element_count": 151, "elements": [],
                   "tree_markdown": '- AXStaticText "December 31, 2026: meeting"'}
            def command(action, params):
                calls.append((action, params))
                if action == "desktop_info":
                    return {"result": {"paused": False, "cua": {"installed": True, "enabled": True}}}
                return {"result": raw}
            result = inspect.inspect_ui({"app": "Calendar"}, command, known_backend=known)
            self.assertEqual([action for action, _ in calls].count("cua_observe"), 1)
            self.assertEqual(calls[-1][1], {"app": "Calendar"})
            self.assertEqual(result["observation"]["tree_markdown"], raw["tree_markdown"])
            self.assertFalse(result["observation"]["elements_complete"])
            self.assertFalse(result["navigation_only"])

    def test_cua_explicit_truncation_sources_trigger_only_one_overview(self):
        for marker in ({"truncated": True}, {"tree_truncated": True},
                       {"tree_markdown": "- AXStaticText value\nAX tree truncated at 150 elements"},
                       {"tree_markdown": "- AXStaticText value\n\n⚠️  AX tree truncated at 150 nodes (app has a very large accessibility tree — Arc, Electron, or similar). Element indices above are still valid. Use pixel clicks for elements not visible in this partial tree.\n"}):
            calls = []
            def command(action, params):
                calls.append(params)
                return {"result": dict(marker, snapshot_id=str(len(calls)), elements=[])}
            result = inspect.inspect_ui({}, command, known_backend="cua")
            self.assertEqual(calls, [{}, {"max_nodes": 500, "max_depth": 2}])
            self.assertEqual(result["observation"]["snapshot_id"], "2")
            self.assertTrue(result["navigation_only"])

    def test_cua_detail_uses_full_bounds_and_never_auto_overviews(self):
        calls = []
        def command(action, params):
            calls.append(params)
            return {"result": {"elements": [], "tree_truncated": True}}
        inspect.inspect_ui({"app": "Calendar", "mode": "detail", "window_id": 7}, command, known_backend="cua")
        self.assertEqual(calls, [{"app": "Calendar", "window_id": 7, "max_nodes": 500, "max_depth": 12}])

    def test_cua_truncation_words_inside_ui_text_do_not_trigger_overview(self):
        calls = []
        def command(action, params):
            calls.append(params)
            return {"result": {"elements": [], "tree_markdown": '- AXStaticText "AX tree truncated at 150 elements"'}}
        inspect.inspect_ui({}, command, known_backend="cua")
        self.assertEqual(len(calls), 1)

    def test_invalid_mode_is_rejected_before_any_command(self):
        for mode in (None, "other", {}, [], 1):
            with self.assertRaisesRegex(ValueError, "mode"):
                inspect.inspect_ui({"mode": mode}, lambda *args: self.fail("unexpected call"))

    def test_pause_blocks_cua_and_invalid_cua_result_is_rejected(self):
        for paused in (True, False):
            calls = []
            def command(action, params):
                calls.append(action)
                if action == "desktop_info":
                    return {"result": {"paused": paused,
                                       "cua": {"installed": True, "enabled": True}}}
                return {"result": None}
            if paused:
                self.assertFalse(inspect.inspect_ui({}, command)["ok"])
                self.assertEqual(calls, ["desktop_info"])
            else:
                with self.assertRaisesRegex(ValueError, "invalid Cua observation"):
                    inspect.inspect_ui({}, command)
                self.assertEqual(calls, ["desktop_info", "cua_observe"])


if __name__ == "__main__":
    unittest.main()
