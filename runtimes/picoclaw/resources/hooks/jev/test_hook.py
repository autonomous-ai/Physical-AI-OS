import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[4]
package = types.ModuleType("jev")
package.__path__ = [str(BASE / "hermes/plugins/jev")]
sys.modules["jev"] = package
spec = importlib.util.spec_from_file_location("pico_hook", Path(__file__).with_name("hook.py"))
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class FakeRouter:
    def __init__(self):
        self.busy = threading.Lock()
        self.prompts = []
    def before_turn(self, user_message=None, **kwargs):
        self.prompts.append(user_message)
        if user_message == "hello":
            return None
        return {"context": self.load("a")}


class HookTest(unittest.TestCase):
    def setUp(self):
        self.loader_patch = patch.object(hook, "load_context", return_value="PRELOADED")
        self.loader_patch.start()
        self.addCleanup(self.loader_patch.stop)

    def request(self, turn="one", prompt="do work", iteration=1):
        return {"meta": {"TurnID": turn, "Iteration": iteration}, "messages": [
            {"role": "system", "content": "native system"},
            {"role": "user", "content": "private old message"},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": prompt}], "tools": [{"name": "read_file"}]}

    def test_native_channels_and_turn_isolation(self):
        router = FakeRouter(); adapter = hook.Hook(router)
        for channel in ("pico", "telegram", "discord"):
            original = self.request(channel)
            original["context"] = {"inbound": {"channel": channel}}
            before = copy.deepcopy(original)
            result = adapter.handle("hook.before_llm", original)
            self.assertEqual(result["action"], "modify")
            self.assertEqual(original, before)
            self.assertEqual(result["request"]["messages"][0], original["messages"][0])
            self.assertEqual(result["request"]["tools"], original["tools"])
        self.assertEqual(router.prompts, ["do work"] * 3)
        self.assertEqual(adapter.handle("hook.before_llm", self.request("next", "hello")), {"action": "continue"})

    def test_tool_loop_reuses_only_same_turn(self):
        router = FakeRouter(); adapter = hook.Hook(router)
        adapter.handle("hook.before_llm", self.request())
        request = self.request(iteration=2)
        request["messages"].append({"role": "tool", "content": "result"})
        result = adapter.handle("hook.before_llm", request)
        self.assertEqual(result["action"], "modify")
        self.assertEqual(len(router.prompts), 1)
        self.assertEqual(adapter.handle("hook.before_llm", self.request(prompt="new steering", iteration=2))["action"], "continue")
        self.assertEqual(adapter.handle("hook.before_llm", self.request("unseen", iteration=2))["action"], "continue")

    def test_identical_turn_id_in_other_session_does_not_leak(self):
        router = FakeRouter(); adapter = hook.Hook(router)
        original = self.request(); original["meta"]["SessionKey"] = "session-a"
        adapter.handle("hook.before_llm", original)
        other = self.request(prompt="hello"); other["meta"]["SessionKey"] = "session-b"
        self.assertEqual(adapter.handle("hook.before_llm", other)["action"], "continue")
        self.assertEqual(router.prompts, ["do work", "hello"])

    def test_busy_and_unknown_contract_fail_open(self):
        router = FakeRouter(); adapter = hook.Hook(router)
        router.busy.acquire()
        self.assertEqual(adapter.handle("hook.before_llm", self.request())["action"], "continue")
        self.assertEqual(router.prompts, [])
        self.assertEqual(adapter.handle("hook.after_llm", {})["action"], "continue")
        self.assertEqual(adapter.handle("hook.before_llm", {})["action"], "continue")

    def test_shared_selector_preloads_with_mock_provider(self):
        self.loader_patch.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "a/SKILL.md"; path.parent.mkdir(); path.write_text("complete skill")
            request = self.request()
            request["messages"][0]["content"] = (f"<skills><skill><name>a</name><description>Action</description>"
                                                  f"<location>{path}</location></skill></skills>")
            payloads = []
            def provider(endpoint, key, timeout, payload):
                payloads.append(payload)
                return {"answers": {"skill": {"type": "choice", "choice": "skill_0",
                                               "probabilities": {"none": .01, "skill_0": .99}},
                                    "fit_skill_0": {"type": "noul", "noul": .99}}}
            router = hook.Router(request=provider)
            adapter = hook.Hook(router)
            native_catalog = hook.catalog_for
            with patch("jev.router.read_config", return_value=("http://localhost/jev", "mock", 3)), \
                 patch.object(hook, "catalog_for", side_effect=lambda messages: native_catalog(messages, root)):
                result = adapter.handle("hook.before_llm", request)
            self.assertEqual(result["action"], "modify")
            self.assertIn("complete skill", result["request"]["messages"][-1]["content"])
            self.assertEqual(payloads[0]["state"]["prompt"], "do work")
            self.assertNotIn("private old message", json.dumps(payloads))

    def test_attachments_and_ambient_do_not_call_jev(self):
        router = FakeRouter(); adapter = hook.Hook(router)
        for field in ("media", "attachments", "content_parts", "parts"):
            request = self.request(field)
            request["messages"][-1][field] = ["image"]
            self.assertEqual(adapter.handle("hook.before_llm", request)["action"], "continue")
        for prompt in ("[sensing:presence] hello", "[HANDLED] already done", "[system] wake"):
            self.assertEqual(adapter.handle("hook.before_llm", self.request(prompt, prompt))["action"], "continue")
        request = self.request("cron")
        request["context"] = {"inbound": {"sender_id": "cron"}}
        self.assertEqual(adapter.handle("hook.before_llm", request)["action"], "continue")
        self.assertEqual(router.prompts, [])

    def test_context_followups_preserve_main_routing(self):
        router = FakeRouter(); adapter = hook.Hook(router)
        for prompt in ("brighter", "Make it brighter.", "continue", "do it", "yes", "try again",
                       "[user] [voice-instruction] brighter [transcript] brighter"):
            self.assertEqual(adapter.handle("hook.before_llm", self.request(prompt, prompt))["action"], "continue")
        self.assertEqual(router.prompts, [])
        for prompt in ("Make the lamp brighter", "Create an image of a fox"):
            self.assertEqual(adapter.handle("hook.before_llm", self.request(prompt, prompt))["action"], "modify")
        self.assertEqual(router.prompts, ["Make the lamp brighter", "Create an image of a fox"])

    def test_voice_instruction_is_only_selector_input(self):
        router = FakeRouter(); adapter = hook.Hook(router)
        prompt = "[user] [voice-instruction] Create an image of a fox. [transcript] Turn off the lights."
        request = self.request(prompt=prompt)
        result = adapter.handle("hook.before_llm", request)
        self.assertEqual(router.prompts, ["Create an image of a fox."])
        self.assertTrue(result["request"]["messages"][-1]["content"].startswith(prompt))
        for index, malformed in enumerate((
            "[voice-instruction] [transcript] Turn off the lights.",
            "quoted [voice-instruction] Turn off the lights.",
            "[voice-instruction] hello [voice-instruction] turn off lights",
            "[voice-instruction] hello [transcript] x [transcript] y",
            "[transcript] Turn off the lights.",
        )):
            self.assertEqual(adapter.handle("hook.before_llm", self.request(str(index), malformed))["action"], "continue")
        self.assertEqual(router.prompts, ["Create an image of a fox."])

    def test_replay_rechecks_eligibility(self):
        router = FakeRouter(); adapter = hook.Hook(router)
        adapter.handle("hook.before_llm", self.request())
        with patch.object(hook, "load_context", side_effect=ValueError("removed")):
            self.assertEqual(adapter.handle("hook.before_llm", self.request(iteration=2))["action"], "continue")

    def test_roster_path_bounds_and_safe_full_load(self):
        self.loader_patch.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "a/SKILL.md"; path.parent.mkdir()
            path.write_text("---\nname: a\n---\nFull instructions, references/example.md")
            xml = f"<skills><skill><name>a</name><description>Action</description><location>{path}</location></skill></skills>"
            messages = [{"role": "system", "content": xml}]
            catalog = lambda: hook.catalog_for(messages, root)
            self.assertEqual(len(catalog()), 1)
            loaded = hook.load_context("a", catalog)
            self.assertIn(str(path.parent), loaded)
            self.assertIn("Full instructions", loaded)
            self.assertEqual(hook.catalog_for([{"role": "user", "content": xml}], root), [])
            self.assertEqual(hook.catalog_for(messages, root / "other"), [])
            path.write_text("!`echo no`")
            with self.assertRaises(ValueError): hook.load_context("a", catalog)
            path.write_bytes(b"x" * (hook.MAX_SKILL + 1))
            with self.assertRaises(ValueError): hook.load_context("a", catalog)
            path.write_text('"' * (hook.MAX_SKILL // 2))
            with self.assertRaises(ValueError): hook.load_context("a", catalog)
            path.unlink()
            target = root / "outside"; target.write_text("not a skill")
            path.symlink_to(target)
            self.assertEqual(catalog(), [])
            with self.assertRaises(OSError): hook.read_skill(path, root)
            path.unlink()
            import os
            os.mkfifo(path)
            with self.assertRaises(ValueError): hook.read_skill(path, root)
            path.unlink()
            self.assertEqual(catalog(), [])

if __name__ == "__main__":
    unittest.main()
