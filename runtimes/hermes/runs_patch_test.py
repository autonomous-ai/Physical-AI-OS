"""Native Runs enrichment contract tests without importing Hermes dependencies."""
import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from runs_patch import (FIXED_CAPS, FIXED_USAGE, LEGACY_CAPS, LEGACY_CREATE, LEGACY_USAGE, MODERN_CREATE,
                        UnsupportedSource, patch_file, patched_source)


LEGACY = '''# Keep unrelated Unicode: tiếng Việt
from contextlib import suppress
_USAGE_FIELDS = LEGACY_USAGE

def _idempotency_capabilities(self, *, store_type):
    return LEGACY_CAPS

def _run_event(run_id, event, **fields):
    return {"run_id": run_id, "event": event, **fields}

async def _execute_run(self, run):
    run_id, loop = run.run_id, asyncio.get_running_loop()
    def _text_cb(delta):
        return delta
    agent = LEGACY_CREATE
    return agent
'''.replace("LEGACY_USAGE", LEGACY_USAGE).replace("LEGACY_CAPS", LEGACY_CAPS).replace("LEGACY_CREATE", LEGACY_CREATE)
MODERN = LEGACY.replace(LEGACY_USAGE, FIXED_USAGE).replace(LEGACY_CREATE, MODERN_CREATE).replace(
    "    agent =", "    def _interim_cb(text, already_streamed=False):\n"
    "        return text, already_streamed\n    agent =")


class RunsPatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_modern_callbacks_preserve_interim_output_and_full_evidence(self):
        namespace = {"asyncio": asyncio}
        patched = patched_source(MODERN)
        exec(compile(patched, "fixture", "exec"), namespace)
        events = []
        progress = object()
        owner = SimpleNamespace(_make_run_event_callback=lambda *_: progress,
                                _create_agent=lambda **kwargs: kwargs)
        run = SimpleNamespace(run_id="run-modern", put_event=events.append,
                              agent_kwargs={"session_id": "session-modern"})
        callbacks = await namespace["_execute_run"](owner, run)
        self.assertIs(callbacks["tool_progress_callback"], progress)
        self.assertEqual(callbacks["session_id"], "session-modern")
        self.assertEqual(callbacks["interim_assistant_callback"]("Working", True), ("Working", True))
        self.assertEqual(callbacks["stream_delta_callback"]("Done"), "Done")
        args = {"command": "harness task", "nested": {"flag": True}}
        result = {"output": "x" * 2000, "task_id": "task-real"}
        callbacks["tool_start_callback"]("call-real", "terminal", args)
        callbacks["tool_complete_callback"]("call-real", "terminal", args, result)
        await asyncio.sleep(0)
        self.assertEqual(events, [
            {"run_id": "run-modern", "event": "tool.call.started", "tool_call_id": "call-real",
             "tool": "terminal", "arguments": args},
            {"run_id": "run-modern", "event": "tool.call.completed", "tool_call_id": "call-real",
             "tool": "terminal", "arguments": args, "result": result}])
        self.assertEqual(patched_source(patched), patched)
        self.assertEqual(dict(namespace["_USAGE_FIELDS"])["cache_read_tokens"], "session_cache_read_tokens")

    async def test_real_callbacks_preserve_tool_ids_args_results_and_progress(self):
        namespace = {"asyncio": asyncio}
        patched = patched_source(LEGACY)
        exec(compile(patched, "fixture", "exec"), namespace)
        events = []
        progress = object()
        owner = SimpleNamespace(_make_run_event_callback=lambda *_: progress,
                                _create_agent=lambda **kwargs: kwargs)
        run = SimpleNamespace(run_id="run-real", put_event=events.append,
                              agent_kwargs={"session_id": "session-real", "model": "unchanged"})
        callbacks = await namespace["_execute_run"](owner, run)
        self.assertIs(callbacks["tool_progress_callback"], progress)
        self.assertEqual(callbacks["session_id"], "session-real")
        self.assertEqual(callbacks["model"], "unchanged")
        args = {"command": "hal-tool speak hello", "nested": {"flag": True}}
        result = {"stdout": "spoken", "exit_code": 0}
        callbacks["tool_start_callback"]("call-actual", "terminal", args)
        callbacks["tool_complete_callback"]("call-actual", "terminal", args, result)
        await asyncio.sleep(0)
        self.assertEqual(events, [
            {"run_id": "run-real", "event": "tool.call.started", "tool_call_id": "call-actual",
             "tool": "terminal", "arguments": args},
            {"run_id": "run-real", "event": "tool.call.completed", "tool_call_id": "call-actual",
             "tool": "terminal", "arguments": args, "result": result}])
        self.assertEqual(patched_source(patched), patched)
        self.assertIn("# Keep unrelated Unicode: tiếng Việt", patched)

    async def test_observation_error_cannot_break_tool_execution(self):
        namespace = {"asyncio": asyncio}
        exec(compile(patched_source(LEGACY), "fixture", "exec"), namespace)
        class ClosedLoop:
            def call_soon_threadsafe(self, *_):
                raise RuntimeError("loop closed")
        run = SimpleNamespace(run_id="r", put_event=lambda _: None)
        namespace["_autonomous_run_tool_event"](run, ClosedLoop(), "tool.call.completed", "c", "tool", {}, "result")

    def test_cache_and_capability_are_truthful(self):
        namespace = {"asyncio": asyncio}
        exec(compile(patched_source(LEGACY), "fixture", "exec"), namespace)
        agent = SimpleNamespace(session_prompt_tokens=42, session_completion_tokens=2,
                                session_total_tokens=44, session_cache_read_tokens=37,
                                session_cache_write_tokens=3)
        usage = {key: getattr(agent, attr, 0) or 0 for key, attr in namespace["_USAGE_FIELDS"]}
        self.assertEqual(usage, {"input_tokens": 42, "output_tokens": 2, "total_tokens": 44,
                                 "cache_read_tokens": 37, "cache_write_tokens": 3})
        caps = namespace["_idempotency_capabilities"](
            SimpleNamespace(_run_idempotency_store=SimpleNamespace(durable=True)),
            store_type=SimpleNamespace(RETENTION_SECONDS=99))
        self.assertEqual(caps, {"supported": True, "durable": True, "retention_seconds": 99,
                                "autonomous_run_events_v1": True})

    def test_atomic_backup_idempotence_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_server_runs.py"
            path.write_text(LEGACY)
            path.chmod(0o640)
            self.assertTrue(patch_file(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(path.with_name(path.name + ".autonomous-runs-v1.bak").read_text(), LEGACY)
            before = path.stat().st_mtime_ns
            self.assertFalse(patch_file(path))
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertEqual(len(list(Path(directory).iterdir())), 2)

    def test_drift_and_partial_patch_leave_original_untouched(self):
        sources = [LEGACY.replace("stream_delta_callback=_text_cb", "stream_delta_callback=other"),
                   LEGACY.replace(LEGACY_CAPS, FIXED_CAPS),
                   LEGACY.replace(LEGACY_USAGE, FIXED_USAGE),
                   MODERN.replace(FIXED_USAGE, LEGACY_USAGE),
                   MODERN.replace("interim_assistant_callback=_interim_cb", "interim_assistant_callback=other"),
                   MODERN.replace(LEGACY_CAPS, FIXED_CAPS),
                   patched_source(LEGACY).replace('"arguments": arguments', '"arguments": {}')]
        for source in sources:
            with self.subTest(source=source[-70:]), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "api_server_runs.py"
                path.write_text(source)
                with self.assertRaises(UnsupportedSource):
                    patch_file(path)
                self.assertEqual(path.read_text(), source)
                self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_missing_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_server_runs.py"
            self.assertFalse(patch_file(path))
            target = Path(directory) / "target.py"
            target.write_text(LEGACY)
            path.symlink_to(target)
            with self.assertRaises(UnsupportedSource):
                patch_file(path)
            self.assertEqual(target.read_text(), LEGACY)


if __name__ == "__main__":
    unittest.main()
