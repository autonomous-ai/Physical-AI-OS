"""Local contract tests: no Hermes installation or external inference required."""

import contextvars
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("jev_router", Path(__file__).with_name("router.py"), submodule_search_locations=[str(Path(__file__).parent)])
router = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = router
SPEC.loader.exec_module(router)


def decision(payload):
    ids = [c["id"] for c in payload["state"]["candidates"]]
    return {"answers": {"skill": {"type": "choice", "choice": ids[0], "probabilities": {
        **{i: (.98 if index == 0 else 0) for index, i in enumerate(ids)}, "none": .02}},
        **{"fit_" + i: {"type": "noul", "noul": .99} for i in ids}}}


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pointer = self.root / "os-config-path.json"
        self.config = self.root / "config.json"
        self.pointer.write_text(json.dumps({"config_path": str(self.config)}))
        self.calls = []
        enabled_patch = patch.object(router, "ENABLED", True)
        enabled_patch.start()
        self.addCleanup(enabled_patch.stop)
        self.settings()

    def settings(self):
        self.config.write_text(json.dumps({"llm_base_url": "https://proxy.test/v1/", "llm_api_key": "secret"}))

    def make(self, request=None):
        def capture(endpoint, key, timeout, payload):
            self.calls.append((endpoint, key, timeout, payload))
            return decision(payload)
        return router.Router(self.pointer, lambda: [{"name": "connectors", "description": "Read email", "category": "openclaw-imports"}], request or capture, load=lambda name, task_id: "Loaded skill " + name + "; mandatory connectors")

    def test_native_preload_and_fallbacks(self):
        plugin = self.make()
        native = {"success": True, "content": "Use this music skill.",
                  "skill_dir": "/skills/music", "linked_files": {"references": ["play.md"]}}
        calls = []
        def view(**kwargs):
            calls.append(kwargs)
            return json.dumps(native)
        plugin.load = router.load_skill_context
        with patch.dict(sys.modules, {"tools.skills_tool": SimpleNamespace(skill_view=view)}):
            with self.assertLogs(router.LOG, level="INFO") as logs:
                context = plugin.before_turn(user_message="play music", task_id="task-A")["context"]
            self.assertIn("Use this music skill.", context)
            self.assertIn("/skills/music", context)
            self.assertIn("play.md", context)
            self.assertIn("outcome=preloaded", "\n".join(logs.output))
            self.assertEqual(calls, [{"name": "connectors", "task_id": "task-A", "preprocess": False}])
            for bad in ({"success": False, "error": "disabled"},
                        {"success": True, "content": ""},
                        {"success": True, "content": "!`echo unsafe`"},
                        {"success": True, "content": "x" * (128 * 1024)}):
                native = bad
                plugin.cooldown_until = 0
                self.assertIsNone(plugin.before_turn(user_message="play music"))

    def test_preload_respects_actual_core_spill_budget(self):
        native = {"success": True, "content": "skill instructions " * 1000}
        modules = {
            "tools.skills_tool": SimpleNamespace(skill_view=lambda **kw: json.dumps(native)),
            "tools.hook_output_spill": SimpleNamespace(get_spill_config=lambda: {"enabled": True, "max_chars": 10000}),
        }
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(ValueError, "inline hook budget"):
                router.load_skill_context("openclaw-imports/computer-use")

            modules["tools.hook_output_spill"].get_spill_config = lambda: {"enabled": True, "max_chars": 131072}
            context = router.load_skill_context("openclaw-imports/computer-use")
            self.assertIn(native["content"], context)
            self.assertLess(len(context), 131072)
            modules["tools.hook_output_spill"].get_spill_config = lambda: {"enabled": True, "max_chars": 500}
            with self.assertRaisesRegex(ValueError, "inline hook budget"):
                router.load_skill_context("openclaw-imports/computer-use")

    def test_preload_diagnostics_correlate_turn_without_content(self):
        plugin = self.make()
        with self.assertLogs(router.LOG, level="INFO") as logs:
            plugin.before_turn(user_message="private request", session_id="session-1",
                               turn_id="turn-2", task_id="task-3")
        message = "\n".join(logs.output)
        for field in ("session_id=session-1", "turn_id=turn-2", "task_id=task-3", "context_chars="):
            self.assertIn(field, message)
        self.assertNotIn("private request", message)
        self.assertNotIn("Loaded skill", message)
        def fail_load(*args):
            raise router.PreloadError("skill_inline_budget", "sensitive native detail")
        plugin.load = fail_load
        with self.assertLogs(router.LOG, level="INFO") as logs:
            self.assertIsNone(plugin.before_turn(user_message="private request", turn_id="unsafe\ninjection"))
        message = "\n".join(logs.output)
        self.assertIn("reason=skill_inline_budget", message)
        self.assertNotIn("sensitive", message)
        self.assertNotIn("injection", message)

    def test_shipped_computer_skill_fits_default_inline_budget(self):
        # Guard the real fast-path instructions against growing back beyond the
        # default Hermes hook spill cap. Native metadata has additional headroom.
        skill_dir = Path(__file__).resolve().parents[4] / "skills" / "computer-use"
        native = {"success": True, "name": "computer-use",
                  "skill_dir": "/root/.hermes/skills/openclaw-imports/computer-use",
                  "content": (skill_dir / "SKILL.md").read_text(),
                  "linked_files": {"scripts": [str(p.relative_to(skill_dir))
                                                 for p in (skill_dir / "scripts").glob("*.py")],
                                   "references": [str(p.relative_to(skill_dir))
                                                  for p in (skill_dir / "references").glob("*.md")]}}
        modules = {
            "tools.skills_tool": SimpleNamespace(skill_view=lambda **kw: json.dumps(native)),
            "tools.hook_output_spill": SimpleNamespace(get_spill_config=lambda: {"enabled": True, "max_chars": 10000}),
        }
        with patch.dict(sys.modules, modules):
            context = router.load_skill_context("openclaw-imports/computer-use")
        self.assertLess(len(context), 9500)
        self.assertIn("--inspect-after", context)
        self.assertIn("using lookup_name exactly as skill_view name", context)
        self.assertIn('"lookup_name": "openclaw-imports/computer-use"', context)

    def test_slow_preload_is_discarded_and_context_is_copied(self):
        plugin = self.make()
        entered, release = threading.Event(), threading.Event()
        scope = contextvars.ContextVar("loader_scope", default="wrong")
        seen = []
        def load(name, task_id):
            seen.append((scope.get(), task_id))
            entered.set()
            release.wait(2)
            return "late skill body from A"
        plugin.load = load
        token = scope.set("session-A")
        try:
            with patch.object(router, "TIMEOUT_SECONDS", .03):
                self.assertIsNone(plugin.before_turn(user_message="A", task_id="task-A"))
            self.assertTrue(entered.is_set())
        finally:
            release.set()
            scope.reset(token)
        with plugin.busy:
            pass
        self.assertEqual(seen, [("session-A", "task-A")])
        plugin.cooldown_until = 0
        plugin.load = lambda name, task_id: "current skill body from B"
        result = plugin.before_turn(user_message="B", task_id="task-B")
        self.assertEqual(result["context"], "current skill body from B")

    def test_removed_skill_and_429_never_load(self):
        plugin = self.make()
        first_catalog = plugin.catalog
        def request(*args):
            plugin.catalog = lambda: []
            return decision(args[-1])
        plugin.request = request
        with patch.object(plugin, "load", side_effect=AssertionError("must not load")):
            self.assertIsNone(plugin.before_turn(user_message="play music"))
        plugin.cooldown_until = 0
        plugin.catalog = first_catalog
        def limited(*args):
            raise router.DecisionError("http_error", 429)
        plugin.request = limited
        with patch.object(plugin, "load", side_effect=AssertionError("must not load")):
            with self.assertLogs(router.LOG, level="INFO") as logs:
                self.assertIsNone(plugin.before_turn(user_message="play music"))
            self.assertIn("http_status=429", "\n".join(logs.output))
            self.assertGreater(plugin.cooldown_until, time.monotonic())

    def test_disabled_and_explicit_whitelist_never_request(self):
        plugin = self.make()
        with patch.object(router, "ENABLED", False), \
             patch.object(router, "read_config", side_effect=AssertionError("must not read")), \
             patch.object(plugin, "catalog", side_effect=AssertionError("must not scan")):
            self.assertIsNone(plugin.before_turn(user_message="read email"))
        self.assertIsNone(plugin.before_turn(user_message="[skills: other] read email"))
        self.assertIsNone(plugin.before_turn(user_message="  /connectors read email"))
        self.assertEqual(self.calls, [])

    def test_catalog_keeps_current_session_context_in_worker(self):
        platform = contextvars.ContextVar("test_hermes_platform", default="unknown")
        plugin = self.make()
        observed = []

        def catalog():
            current = platform.get()
            observed.append(current)
            if current == "restricted":
                return []
            return [{"name": "connectors", "description": "Read email", "category": "openclaw-imports"}]

        plugin.catalog = catalog
        for current in ("restricted", "allowed", "restricted"):
            token = platform.set(current)
            try:
                hint = plugin.before_turn(user_message="read email")
                self.assertEqual(hint is not None, current == "allowed")
                # Wait for worker cleanup so the next turn is not a busy bypass.
                with plugin.busy:
                    pass
            finally:
                platform.reset(token)
        self.assertEqual(observed, ["restricted", "allowed", "allowed", "restricted"])
        self.assertEqual(len(self.calls), 1)

    def test_preload_current_message_only_and_shared_proxy_config(self):
        plugin = self.make()
        hint = plugin.before_turn(user_message="read email", conversation_history=[{"secret": "history"}])
        self.assertIn('Loaded skill connectors', hint["context"])
        self.assertIn("mandatory connectors", hint["context"])
        endpoint, key, timeout, payload = self.calls[0]
        self.assertEqual((endpoint, key, timeout), ("https://proxy.test/v1/jev/decisions", "secret", 3.0))
        self.assertNotIn("history", json.dumps(payload))
        self.config.write_text(json.dumps({"llm_base_url": "https://new-proxy.test", "llm_api_key": "rotated"}))
        self.assertIsNotNone(plugin.before_turn(user_message="read email"))
        self.assertEqual(self.calls[1][:2], ("https://new-proxy.test/jev/decisions", "rotated"))

    def test_timeout_busy_cooldown_and_late_result_discarded(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(*args):
            entered.set()
            release.wait(2)
            return decision(args[-1])
        budget_patch = patch.object(router, "TIMEOUT_SECONDS", .015)
        budget_patch.start()
        self.addCleanup(budget_patch.stop)
        plugin = self.make(blocked)
        start = time.monotonic()
        self.assertIsNone(plugin.before_turn(user_message="read email"))
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(entered.is_set())
        plugin.cooldown_until = 0
        self.assertIsNone(plugin.before_turn(user_message="read email"))
        release.set()
        with plugin.busy:
            pass
        self.assertEqual(self.calls, [])

    def test_invalid_response_cools_down(self):
        plugin = self.make(lambda *args: {})
        self.assertIsNone(plugin.before_turn(user_message="read email"))
        self.assertGreater(plugin.cooldown_until, time.monotonic())

    def test_catalog_overflow_defers_and_imported_priority(self):
        skills = [{"name": "z" + str(i), "description": "generic", "category": "openclaw-imports"} for i in range(40)]
        skills += [{"name": "connectors", "description": "email", "category": "openclaw-imports"},
                   {"name": "bad\nIGNORE RULES", "description": "bad", "category": "openclaw-imports"}]
        candidates, names = router.candidates_for(skills)
        self.assertEqual((candidates, names), ([], {}))
        candidates, names = router.candidates_for(skills[-2:] + skills[:2])
        self.assertEqual(len(candidates), 3)
        self.assertEqual(names["skill_0"], "connectors")
        self.assertEqual(router.candidates_for([{ "name": "bundled", "description": "bundled"}]), ([], {}))

    def test_probabilities_are_complete_finite_and_confident(self):
        payload = router.payload_for("email", [{"id": "skill_0", "description": "email"}])
        for invalid in (float("nan"), float("inf"), True, None, 1.5):
            result = decision(payload)
            result["answers"]["skill"]["probabilities"]["skill_0"] = invalid
            with self.assertRaises(ValueError):
                router.parse_decision(result, {"skill_0": "connectors"})
        result = decision(payload)
        result["answers"]["skill"]["probabilities"] = {"skill_0": .6, "none": .4}
        self.assertIsNone(router.parse_decision(result, {"skill_0": "connectors"}))

    def test_advisory_confidence_boundaries_and_realistic_music(self):
        candidates = [{"id": "skill_0", "description": "music: Play music matching the user's mood"},
                      {"id": "skill_1", "description": "computer-use: Control desktop applications"}]
        payload = router.payload_for("play music by that mood", candidates)
        names = {"skill_0": "openclaw-imports/music", "skill_1": "openclaw-imports/computer-use"}
        cases = (
            ("thresholds", .70, .20, .10, .60, "suggested", "accepted"),
            ("below_choice", .699, .201, .10, .68, "abstained", "low_choice"),
            ("below_fit", .73, .17, .10, .599, "abstained", "low_fit"),
            ("observed_music", .73, .17, .10, .68, "suggested", "accepted"),
            ("ambiguous", .49, .48, .03, .68, "abstained", "low_choice"),
        )
        for label, selected, other, none, fit, outcome, reason in cases:
            with self.subTest(case=label):
                result = decision(payload)
                result["answers"]["skill"]["probabilities"] = {
                    "skill_0": selected, "skill_1": other, "none": none}
                result["answers"]["fit_skill_0"]["noul"] = fit
                evaluation = router.evaluate_decision(result, names)
                self.assertEqual((evaluation["outcome"], evaluation["reason"]), (outcome, reason))
                self.assertEqual(evaluation["selected"], names["skill_0"] if outcome == "suggested" else None)
        result = decision(payload)
        result["answers"]["skill"].update(
            choice="none", probabilities={"skill_0": .20, "skill_1": .07, "none": .73})
        evaluation = router.evaluate_decision(result, names)
        self.assertEqual((evaluation["outcome"], evaluation["reason"], evaluation["selected"]),
                         ("abstained", "none", None))

    def test_margin_remains_an_independent_guard(self):
        payload = router.payload_for("play music", [{"id": "skill_0", "description": "music"}])
        result = decision(payload)
        result["answers"]["skill"]["probabilities"] = {"skill_0": .55, "none": .45}
        with patch.object(router, "MIN_CHOICE_PROBABILITY", .55):
            evaluation = router.evaluate_decision(result, {"skill_0": "openclaw-imports/music"})
        self.assertEqual((evaluation["outcome"], evaluation["reason"], evaluation["selected"]),
                         ("abstained", "low_margin", None))

    def test_valid_abstentions_are_distinct_and_do_not_cool_down(self):
        for reason in ("none", "low_choice", "low_fit"):
            with self.subTest(reason=reason):
                def abstain(*args):
                    result = decision(args[-1])
                    choice = result["answers"]["skill"]
                    if reason == "none":
                        choice.update(choice="none", probabilities={"skill_0": .02, "none": .98})
                    elif reason == "low_choice":
                        choice["probabilities"] = {"skill_0": .6, "none": .4}
                    else:
                        result["answers"]["fit_skill_0"]["noul"] = .59
                    return result
                plugin = self.make(abstain)
                with self.assertLogs(router.LOG, level="INFO") as captured:
                    self.assertIsNone(plugin.before_turn(user_message="private user prompt"))
                log = "\n".join(captured.output)
                self.assertIn("outcome=abstained reason=" + reason, log)
                self.assertIn("choice_probability=", log)
                self.assertIn("request_ms=", log)
                self.assertNotIn("private user prompt", log)
                self.assertEqual(plugin.cooldown_until, 0)

    def test_errors_are_distinct_sanitized_and_cool_down(self):
        for reason in ("http_error", "invalid_schema", "catalog_error", "network_error"):
            with self.subTest(reason=reason):
                plugin = self.make()
                if reason == "http_error":
                    def rejected(*args):
                        raise router.DecisionError("http_error", 403)
                    plugin.request = rejected
                elif reason == "invalid_schema":
                    plugin.request = lambda *args: {"raw_body": "private response body"}
                elif reason == "catalog_error":
                    plugin.catalog = lambda: (_ for _ in ()).throw(RuntimeError("private catalog exception"))
                else:
                    plugin.request = lambda *args: (_ for _ in ()).throw(RuntimeError("private network exception"))
                with self.assertLogs(router.LOG, level="INFO") as captured:
                    self.assertIsNone(plugin.before_turn(user_message="private user prompt"))
                log = "\n".join(captured.output)
                self.assertIn("outcome=error reason=" + reason, log)
                if reason == "http_error":
                    self.assertIn("http_status=403", log)
                for private in ("secret", "private user prompt", "private response body",
                                "private catalog exception", "private network exception"):
                    self.assertNotIn(private, log)
                self.assertGreater(plugin.cooldown_until, time.monotonic())
                with self.assertLogs(router.LOG, level="INFO") as captured:
                    self.assertIsNone(plugin.before_turn(user_message="read email"))
                self.assertIn("outcome=skipped reason=cooldown", "\n".join(captured.output))

    def test_empty_catalog_skips_without_request_or_cooldown(self):
        plugin = self.make()
        plugin.catalog = lambda: []
        with self.assertLogs(router.LOG, level="INFO") as captured:
            self.assertIsNone(plugin.before_turn(user_message="play music"))
        self.assertIn("outcome=skipped reason=no_candidates", "\n".join(captured.output))
        self.assertEqual(self.calls, [])
        self.assertEqual(plugin.cooldown_until, 0)

    def test_choice_must_agree_with_probability_argmax(self):
        payload = router.payload_for("email", [{"id": "skill_0", "description": "email"}])
        result = decision(payload)
        result["answers"]["skill"]["choice"] = "none"
        with self.assertRaises(router.DecisionError) as error:
            router.evaluate_decision(result, {"skill_0": "connectors"})
        self.assertEqual(error.exception.reason, "invalid_schema")

    def test_live_os_catalog_preserves_namespace_and_native_filters(self):
        os_root = self.root / "skills" / "openclaw-imports"
        metadata = {
            "computer-use": {"name": "computer-use", "description": "Control the connected Mac", "platform": "allowed"},
            "disabled-name": {"name": "disabled-name", "description": "Disabled by name"},
            "disabled-path": {"name": "disabled-path", "description": "Disabled by qualified path"},
            "wrong-platform": {"name": "wrong-platform", "description": "Wrong platform", "platform": "other"},
            "missing-env": {"name": "missing-env", "description": "Missing environment", "env": False},
        }
        index_paths = []
        for folder, value in metadata.items():
            path = os_root / folder / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(value))
            index_paths.append(path)
        # A bundled namesake must not win before the OS category is considered.
        bundled = self.root / "skills" / "desktop" / "computer-use" / "SKILL.md"
        bundled.parent.mkdir(parents=True)
        bundled.write_text(json.dumps({"name": "computer-use", "description": "Bundled desktop"}))
        support = os_root / "computer-use" / "references" / "example.md"
        support.parent.mkdir()
        support.write_text("Support document, not a skill")
        platform = contextvars.ContextVar("native_catalog_platform", default="restricted")
        seen_platforms = []

        def matches_platform(frontmatter):
            current = platform.get()
            seen_platforms.append(current)
            return frontmatter.get("platform", current) == current

        def iterate(root, filename):
            self.assertEqual((root, filename), (os_root, "SKILL.md"))
            return iter(index_paths)

        modules = {
            "hermes_constants": SimpleNamespace(get_hermes_home=lambda: self.root),
            "agent.skill_utils": SimpleNamespace(
                get_disabled_skill_names=lambda: {"disabled-name", "openclaw-imports/disabled-path"},
                iter_skill_index_files=iterate,
                parse_frontmatter=lambda text: (json.loads(text), ""),
                skill_matches_platform=matches_platform,
                skill_matches_environment=lambda frontmatter: frontmatter.get("env", True)),
        }
        with patch.dict(sys.modules, modules):
            token = platform.set("allowed")
            try:
                skills = router.live_skills()
                self.assertEqual([skill["name"] for skill in skills], ["computer-use"])
                self.assertEqual(skills[0]["lookup_name"], "openclaw-imports/computer-use")
                self.assertEqual(skills[0]["description"], "Control the connected Mac")
                candidates, names = router.candidates_for(skills)
                self.assertEqual(names, {"skill_0": "openclaw-imports/computer-use"})
                plugin = self.make()
                plugin.catalog = router.live_skills
                hint = plugin.before_turn(user_message="open Calculator on my Mac")
                self.assertIn('Loaded skill openclaw-imports/computer-use', hint["context"])
                self.assertTrue(seen_platforms)
                self.assertEqual(set(seen_platforms), {"allowed"})
            finally:
                platform.reset(token)
            self.assertEqual(router.live_skills(), [])

    def test_config_timeout_and_endpoint_validation(self):
        self.assertEqual(router.read_config(self.pointer)[2], 3.0)
        for base in ("http://remote.test", "https://u:p@proxy.test", "https://proxy.test?x=1", "https://proxy.test#"):
            config = json.loads(self.config.read_text())
            config["llm_base_url"] = base
            self.config.write_text(json.dumps(config))
            self.assertIsNone(router.read_config(self.pointer))

    def test_http_headers_and_redirect_rejection(self):
        with patch.object(router.urllib.request, "build_opener") as build:
            response = build.return_value.open.return_value
            response.status = 200
            response.read.return_value = b'{"answers":{}}'
            router.request_decision("https://proxy.test/jev/decisions", "secret", .35, {})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
            self.assertEqual(request.method, "POST")
            self.assertIsInstance(build.call_args.args[0], router.NoRedirect)
        self.assertIsNone(router.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))

    def test_plugin_package_registers_public_hook(self):
        spec = importlib.util.spec_from_file_location("jev_plugin_test", Path(__file__).with_name("__init__.py"),
                                                     submodule_search_locations=[str(Path(__file__).parent)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        self.addCleanup(lambda: sys.modules.pop(spec.name, None))
        self.addCleanup(lambda: sys.modules.pop(spec.name + ".router", None))
        spec.loader.exec_module(package)
        hooks = {}
        class Context:
            def register_hook(self, hook_name, callback):
                hooks[hook_name] = callback
        self.assertTrue(package.Router.before_turn.__globals__["ENABLED"])
        package.register(Context())
        self.assertEqual(list(hooks), ["pre_llm_call"])
        self.assertIsNone(hooks["pre_llm_call"](session_id="s", user_message="hello", conversation_history=[],
                                              is_first_turn=True, model="test", platform="cli"))

    def test_real_loopback_proxy_and_redirect_do_not_follow(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.path)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.headers.get("User-Agent") != "AutonomousOS-Jev/0.1":
                    self.send_response(403)
                    self.end_headers()
                    return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-follow")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps(decision(body)).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            endpoint = "http://127.0.0.1:" + str(server.server_port)
            payload = router.payload_for("email", [{"id": "skill_0", "description": "email"}])
            result = router.request_decision(endpoint + "/jev/decisions", "test-key", .5, payload)
            self.assertEqual(router.parse_decision(result, {"skill_0": "connectors"}), "connectors")
            with self.assertRaises(Exception):
                router.request_decision(endpoint + "/redirect", "test-key", .5, payload)
            self.assertEqual(seen, ["/jev/decisions", "/redirect"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
