"""Deterministic overlapping-hook tests; no device or external inference needed."""

import contextvars
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "jev_isolation_plugin", Path(__file__).with_name("__init__.py"),
    submodule_search_locations=[str(Path(__file__).parent)],
)
PACKAGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PACKAGE
SPEC.loader.exec_module(PACKAGE)
router = sys.modules[SPEC.name + ".router"]
SESSION_SKILL = contextvars.ContextVar("isolation_skill", default="unexpected")


def decision(payload):
    ids = [item["id"] for item in payload["state"]["candidates"]]
    return {"answers": {
        "skill": {"type": "choice", "choice": ids[0],
                  "probabilities": {**{key: (.99 if i == 0 else 0) for i, key in enumerate(ids)}, "none": .01}},
        **{"fit_" + key: {"type": "noul", "noul": .99} for key in ids},
    }}


class TurnIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(json.dumps({"llm_base_url": "https://proxy.test", "llm_api_key": "test"}))
        self.pointer = root / "os-config-path.json"
        self.pointer.write_text(json.dumps({"config_path": str(config)}))
        enabled = patch.object(router, "ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.workers = []
        self.releases = []
        self.addCleanup(self.finish_workers)

    def finish_workers(self):
        for release in self.releases:
            release.set()
        for worker in self.workers:
            worker.join(3)
            self.assertFalse(worker.is_alive(), "hook did not finish")

    def gate(self):
        gate = threading.Event()
        self.releases.append(gate)
        return gate

    def make_hook(self, request):
        def catalog():
            return [{"name": SESSION_SKILL.get(), "description": "Test skill", "category": "openclaw-imports"}]
        instance = router.Router(self.pointer, catalog, request, load=lambda name, task_id: "Loaded skill " + name)
        hooks = {}

        class Context:
            def register_hook(self, name, callback):
                hooks[name] = callback

        with patch.object(PACKAGE, "Router", return_value=instance):
            PACKAGE.register(Context())
        return instance, hooks["pre_llm_call"]

    def invoke(self, hook, turn, skill, session="shared-session"):
        token = SESSION_SKILL.set(skill)
        try:
            return hook(user_message="request-" + turn, turn_id=turn, session_id=session,
                        conversation_history=[{"role": "user", "content": "unrelated-history"}])
        finally:
            SESSION_SKILL.reset(token)

    def launch(self, hook, turn, skill, results, session="shared-session"):
        def call():
            try:
                results[turn] = self.invoke(hook, turn, skill, session)
            except BaseException as error:
                results[turn] = error
        worker = threading.Thread(target=call)
        self.workers.append(worker)
        worker.start()
        return worker

    def assert_hint(self, result, expected, forbidden):
        self.assertIsInstance(result, dict)
        self.assertIn('Loaded skill ' + expected, result["context"])
        self.assertNotIn(forbidden, result["context"])

    def test_system_skill_reload_never_reads_catalog_or_calls_proxy(self):
        instance, hook = self.make_hook(lambda *args: self.fail("must not call Jev"))
        message = ("[system] The following skills have been updated. Re-read them now — "
                   "files on disk have changed. Follow the updated instructions strictly. "
                   "After re-reading, output exactly NO_REPLY and nothing else; "
                   "do not announce or acknowledge this maintenance update to the user."
                   "\n- skills/openclaw-imports/computer-use/SKILL.md")
        with patch.object(instance, "catalog", side_effect=AssertionError("must not scan")), \
             patch.object(router, "read_config", side_effect=AssertionError("must not read")):
            for value in (message, "  " + message, message.replace("[system]", "[SYSTEM]")):
                with self.assertLogs(router.LOG, level="INFO") as logs:
                    self.assertIsNone(hook(user_message=value, turn_id="reload", session_id="test"))
                self.assertIn("reason=system_message", " ".join(logs.output))

    def test_overlapping_turns_never_receive_other_turn_hint(self):
        # Exercise both same-session turns and independent sessions sharing a router.
        for session in ("shared-session", "other-session"):
            with self.subTest(session=session), patch.object(router, "TIMEOUT_SECONDS", 2):
                entered, release = threading.Event(), self.gate()
                seen, results = [], {}

                def request(*args):
                    payload = args[-1]
                    seen.append(payload)
                    if payload["state"]["prompt"] == "request-A":
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError("test gate timed out")
                    return decision(payload)

                instance, hook = self.make_hook(request)
                worker = self.launch(hook, "A", "music_A", results)
                self.assertTrue(entered.wait(1))
                self.assertIsNone(self.invoke(hook, "B", "calculator_B", session))
                self.assertEqual(len(seen), 1, "busy turn must not start another request")
                release.set()
                worker.join(2)
                self.assert_hint(results["A"], "music_A", "calculator_B")
                with instance.busy:
                    pass
                result_b = self.invoke(hook, "B", "calculator_B", session)
                self.assert_hint(result_b, "calculator_B", "music_A")
                self.assertEqual([p["state"]["prompt"] for p in seen], ["request-A", "request-B"])
                # Both responses choose skill_0; the local catalog must resolve it separately.
                self.assertEqual([p["state"]["candidates"][0]["id"] for p in seen], ["skill_0", "skill_0"])
                self.assertNotIn("unrelated-history", json.dumps(seen))

    def test_timed_out_A_cannot_supply_hint_to_B_after_late_completion(self):
        entered, release = threading.Event(), self.gate()
        seen = []

        def request(*args):
            payload = args[-1]
            seen.append(payload["state"]["prompt"])
            if payload["state"]["prompt"] == "request-A":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test gate timed out")
            return decision(payload)

        instance, hook = self.make_hook(request)
        with patch.object(router, "TIMEOUT_SECONDS", .03):
            result_a = self.invoke(hook, "A", "music_A")
        self.assertTrue(entered.is_set())
        self.assertIsNone(result_a)
        self.assertIsNone(self.invoke(hook, "B", "calculator_B"))  # Cooldown.
        instance.cooldown_until = 0  # Simulate cooldown expiry while A is still stuck.
        self.assertIsNone(self.invoke(hook, "B", "calculator_B"))  # Still busy.
        self.assertEqual(seen, ["request-A"])
        release.set()
        with instance.busy:
            pass  # Worker has written A's late result and released the lock.
        instance.cooldown_until = 0
        with patch.object(router, "TIMEOUT_SECONDS", 2):
            result_b = self.invoke(hook, "B", "calculator_B")
        self.assert_hint(result_b, "calculator_B", "music_A")
        self.assertIsNone(result_a)
        self.assertEqual(seen, ["request-A", "request-B"])

    def test_separate_router_instances_finish_in_reverse_order(self):
        entered, release = threading.Event(), self.gate()
        results = {}

        def delayed(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test gate timed out")
            return decision(args[-1])

        _, hook_a = self.make_hook(delayed)
        _, hook_b = self.make_hook(lambda *args: decision(args[-1]))
        with patch.object(router, "TIMEOUT_SECONDS", 2):
            worker = self.launch(hook_a, "A", "music_A", results)
            self.assertTrue(entered.wait(1))
            result_b = self.invoke(hook_b, "B", "calculator_B", "other-session")
            self.assert_hint(result_b, "calculator_B", "music_A")
            self.assertNotIn("A", results)
            release.set()
            worker.join(2)
        self.assert_hint(results["A"], "music_A", "calculator_B")
        self.assert_hint(result_b, "calculator_B", "music_A")


if __name__ == "__main__":
    unittest.main()
