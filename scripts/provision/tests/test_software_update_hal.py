"""Exercise HAL updater discovery without touching the host installation."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "software-update"


class HALUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.hal = self.root / "hal"
        self.hal.mkdir()
        for name in ("grep", "cut", "jq"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable, f"Test prerequisite missing: {name}")
            (self.bin / name).symlink_to(executable)

    def run_function(self, name):
        match = re.search(
            rf"^{name}\(\) \{{\n.*?^\}}$", SCRIPT.read_text(),
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match, f"Missing {name}")
        function = match.group(0)
        for path in ("/root/.local/bin/uv", "/home/pollen/.local/bin/uv",
                     "/root/config/config.json"):
            function = function.replace(path, str(self.root / path.lstrip("/")))
        return subprocess.run(
            ["/bin/bash", "-c", "set -e\n" + function + "\n" + name],
            env={**os.environ, "PATH": str(self.bin), "HAL_DIR": str(self.hal)},
            capture_output=True, text=True,
        )

    def make_uv(self, path, executable=True):
        candidate = self.root / path
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("#!/bin/sh\nexit 0\n")
        candidate.chmod(0o755 if executable else 0o644)
        return candidate

    def test_uv_prefers_path_then_root_then_pollen(self):
        pollen = self.make_uv("home/pollen/.local/bin/uv")
        root = self.make_uv("root/.local/bin/uv")
        path = self.make_uv("bin/uv")
        for expected in (path, root, pollen):
            result = self.run_function("find_hal_uv")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(expected))
            expected.unlink()

    def test_missing_or_nonexecutable_uv_fails(self):
        self.make_uv("home/pollen/.local/bin/uv", executable=False)
        result = self.run_function("find_hal_uv")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uv not found", result.stderr)

    def test_device_env_selects_reachy_and_preserves_other_devices(self):
        for device, expected in (("reachy-mini", "reachy"), ("lamp", "aec"),
                                 ("intern-v2", "aec")):
            with self.subTest(device=device):
                (self.hal / ".env").write_text(f"DEVICE_TYPE={device}\n")
                result = self.run_function("hal_dependency_extra")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_config_fallback_for_reinstall_and_env_precedence(self):
        config = self.root / "root/config/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"device_type":"reachy-mini"}')
        result = self.run_function("hal_dependency_extra")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "reachy")
        (self.hal / ".env").write_text("DEVICE_TYPE=lamp\n")
        self.assertEqual(self.run_function("hal_dependency_extra").stdout.strip(), "aec")

    def test_missing_device_metadata_keeps_legacy_aec(self):
        result = self.run_function("hal_dependency_extra")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "aec")

    def test_ota_installs_smart_turn_except_on_reachy(self):
        script = SCRIPT.read_text()
        start = script.index('  HAL_EXTRA_ARGS=(--extra "$HAL_EXTRA")')
        end = script.index('  save_hal_service_state', start)
        for extra, expected in (("aec", ["--extra", "aec", "--extra", "pipecat"]),
                                ("reachy", ["--extra", "reachy"])):
            with self.subTest(extra=extra):
                result = subprocess.run(
                    ["/bin/bash", "-c", script[start:end] + '\nprintf "%s\\n" "${HAL_EXTRA_ARGS[@]}"'],
                    env={**os.environ, "HAL_EXTRA": extra}, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)
        self.assertIn('sync --python 3.12 --extra hardware "${HAL_EXTRA_ARGS[@]}"', script)


if __name__ == "__main__":
    unittest.main()
