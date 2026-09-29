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
            path = root / "global/a/SKILL.md"; path.parent.mkdir(parents=True); path.write_text("complete skill")
            request = self.request()
            request["messages"][0]["content"] = (f"<skills><skill><name>a</name><description>Action</description>"
                                                  f"<location>{path}</location><source>global</source></skill></skills>")
            payloads = []
            def provider(endpoint, key, timeout, payload):
                payloads.append(payload)
                return {"answers": {"skill": {"type": "choice", "choice": "skill_0",
                                               "probabilities": {"none": .01, "skill_0": .99}},
                                    "fit_skill_0": {"type": "noul", "noul": .99}}}
            router = hook.Router(request=provider)
            adapter = hook.Hook(router)
            with patch("jev.router.read_config", return_value=("http://localhost/jev", "mock", 3)):
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

    def test_native_roster_includes_global_and_builtin_but_not_unlisted_skills(self):
        self.loader_patch.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            rows = []
            for source, name in (("workspace", "lamp"), ("global", "session-recall"),
                                 ("builtin", "weather")):
                path = root / source / name / "SKILL.md"
                path.parent.mkdir(parents=True)
                path.write_text(f"Complete {name} instructions")
                rows.append(f"<skill><name>{name}</name><description>{name}</description>"
                            f"<location>{path}</location><source>{source}</source></skill>")
            unlisted = root / "global/filtered/SKILL.md"
            unlisted.parent.mkdir(); unlisted.write_text("Do not preload")
            xml = "<skills>" + "".join(rows) + "</skills>"
            # Native providers may expose the same roster in content and system_parts.
            messages = [{"role": "system", "content": xml,
                         "system_parts": [{"text": xml}]}]
            catalog = lambda: hook.catalog_for(messages)
            self.assertEqual([s["name"] for s in catalog()], ["lamp", "session-recall", "weather"])
            self.assertIn("Complete session-recall instructions", hook.load_context("session-recall", catalog))
            self.assertIn("Complete weather instructions", hook.load_context("weather", catalog))
            with self.assertRaises(ValueError): hook.load_context("filtered", catalog)
            messages[0]["content"] = "<skills>" + rows[0] + "</skills>"
            messages[0]["system_parts"] = []
            with self.assertRaises(ValueError): hook.load_context("session-recall", catalog)

    def test_native_roster_rejects_unsafe_paths_and_ambiguous_names(self):
        self.loader_patch.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "global/recall/SKILL.md"
            path.parent.mkdir(parents=True); path.write_text("Recall instructions")
            link = root / "linked"
            link.symlink_to(path.parent, target_is_directory=True)
            def roster(paths):
                return [{"role": "system", "content": "<skills>" + "".join(
                    f"<skill><name>recall</name><location>{p}</location></skill>"
                    for p in paths) + "</skills>"}]
            for unsafe in ("relative/SKILL.md", path.parent / "../recall/SKILL.md",
                           link / "SKILL.md", path.parent / "missing/SKILL.md", path.parent):
                self.assertEqual(hook.catalog_for(roster([unsafe])), [])
            other = root / "builtin/recall/SKILL.md"
            other.parent.mkdir(parents=True); other.write_text("Other instructions")
            with self.assertRaises(ValueError):
                hook.load_context("recall", lambda: hook.catalog_for(roster([path, other])))
            # Replacing an ancestor after discovery must not escape through a symlink.
            original = path.parent
            original.rename(original.with_name("moved"))
            original.symlink_to(original.with_name("moved"), target_is_directory=True)
            with self.assertRaises(OSError): hook.read_skill(path, Path(path.anchor))

    def test_roster_path_bounds_and_safe_full_load(self):
        self.loader_patch.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "a/SKILL.md"; path.parent.mkdir()
            path.write_text("---\nname: a\n---\nFull instructions, references/example.md")
            xml = f"<skills><skill><name>a</name><description>Action</description><location>{path}</location></skill></skills>"
            messages = [{"role": "system", "content": xml}]
            catalog = lambda: hook.catalog_for(messages)
            self.assertEqual(len(catalog()), 1)
            loaded = hook.load_context("a", catalog)
            self.assertIn(str(path.parent), loaded)
            self.assertIn("Full instructions", loaded)
            self.assertEqual(hook.catalog_for([{"role": "user", "content": xml}]), [])
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
