"""Focused regression tests; run with python3 -m unittest discover here."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from cache_usage_patch import LEGACY_USAGE, UnsupportedSource, patch_file, patched_source


LEGACY = '''# Unrelated Unicode: tiếng Việt
_USAGE_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")
def _responses_usage_payload(usage):
    """Serialize a response."""
    return {key: usage.get(key, 0) for key in _USAGE_TOKEN_KEYS}
class Gateway:
    def _finish_turn_result(self, agent, result):
        usage = LEGACY_USAGE
        result["preserved"] = True
        return result, usage
'''.replace("LEGACY_USAGE", LEGACY_USAGE)


class CacheUsagePatchTests(unittest.TestCase):
    def test_counts_survive_both_boundaries(self):
        patched = patched_source(LEGACY)
        namespace = {}
        exec(compile(patched, "fixture", "exec"), namespace)
        agent = SimpleNamespace(session_prompt_tokens=14097,
                                session_completion_tokens=66, session_total_tokens=14163,
                                session_cache_read_tokens=13824, session_cache_write_tokens=12)
        result, usage = namespace["Gateway"]()._finish_turn_result(agent, {})
        payload = json.loads(json.dumps(namespace["_responses_usage_payload"](usage)))
        self.assertTrue(result["preserved"])
        self.assertEqual(payload, {"input_tokens": 14097, "output_tokens": 66,
                                  "total_tokens": 14163,
                                  "input_tokens_details": {"cached_tokens": 13824,
                                                           "cache_write_tokens": 12}})
        _, usage = namespace["Gateway"]()._finish_turn_result(SimpleNamespace(), {})
        self.assertEqual(namespace["_responses_usage_payload"](usage)["input_tokens_details"],
                         {"cached_tokens": 0, "cache_write_tokens": 0})
        self.assertIn("# Unrelated Unicode: tiếng Việt", patched)
        self.assertEqual(patched_source(patched), patched)

    def test_atomic_idempotence_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_server.py"
            path.write_text(LEGACY)
            path.chmod(0o640)
            self.assertTrue(patch_file(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            before = path.stat().st_mtime_ns
            self.assertFalse(patch_file(path))
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_drift_never_writes_even_if_other_target_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_server.py"
            drift = LEGACY.replace('usage.get(key, 0)', 'usage.get(key, -1)')
            path.write_text(drift)
            with self.assertRaises(UnsupportedSource):
                patch_file(path)
            self.assertEqual(path.read_text(), drift)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_missing_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_server.py"
            self.assertFalse(patch_file(path))
            target = Path(directory) / "target.py"
            target.write_text(LEGACY)
            path.symlink_to(target)
            with self.assertRaises(UnsupportedSource):
                patch_file(path)
            self.assertEqual(target.read_text(), LEGACY)


if __name__ == "__main__":
    unittest.main()
