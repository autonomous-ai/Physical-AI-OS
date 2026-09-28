"""Verify the CI gate includes new routers without importing their decorators."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from check_python_syntax import check


class PythonSyntaxTests(unittest.TestCase):
    def test_all_tracked_sources_including_router_decorators(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            router = root / "hal" / "routes" / "new_route.py"
            router.parent.mkdir(parents=True)
            # No FastAPI/hardware imports are executed by the syntax gate.
            router.write_text('@router.get("/ready")\ndef ready():\n    return True\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            self.assertEqual(check(root), 0)
            router.write_text('@router.get("/ready"\ndef ready():\n    return True\n')
            self.assertEqual(check(root), 1)

    def test_compile_catches_invalid_context_beyond_ast_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "bad.py").write_text("return 1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            self.assertEqual(check(root), 1)


if __name__ == "__main__":
    unittest.main()
