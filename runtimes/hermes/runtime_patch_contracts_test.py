"""Do not silently ship an embedded runtime patch without fixture tests.

The companion suites must exercise the resulting upstream source (including
compilation, repeated application, and unsupported anchors), not just compile
the patch script. They run under the same CI unittest discovery command.
"""

import ast
from pathlib import Path
import shlex
import unittest


ROOT = Path(__file__).resolve().parent


def embedded_patches(root):
    patches = set()
    for go_file in root.rglob("*.go"):
        for line in go_file.read_text().splitlines():
            if not line.startswith("//go:embed "):
                continue
            for pattern in shlex.split(line.removeprefix("//go:embed ")):
                for path in go_file.parent.glob(pattern.removeprefix("all:")):
                    if path.suffix == ".py" and (
                        path.name.endswith("_patch.py") or "patches" in path.relative_to(root).parts
                    ):
                        patches.add(path)
    return patches


class RuntimePatchContractCoverage(unittest.TestCase):
    def test_every_embedded_patch_has_discoverable_fixture_tests(self):
        patches = embedded_patches(ROOT)
        self.assertTrue(patches, "No embedded patches found; check embed discovery")
        for patch in sorted(patches):
            with self.subTest(patch=str(patch.relative_to(ROOT))):
                ast.parse(patch.read_text(), filename=str(patch))
                # Keep suites at this root so unittest discovery always runs
                # them even when the patch lives in a non-package directory.
                suite_path = ROOT / (patch.stem + "_test.py")
                self.assertTrue(
                    suite_path.is_file(),
                    f"{patch.relative_to(ROOT)} needs {suite_path.name}: test patched "
                    "source compilation, idempotence, and unsupported anchors",
                )
                suite = unittest.defaultTestLoader.discover(
                    str(ROOT), pattern=suite_path.name
                )
                self.assertGreater(suite.countTestCases(), 0, f"Empty contract suite: {suite_path.name}")


if __name__ == "__main__":
    unittest.main()
