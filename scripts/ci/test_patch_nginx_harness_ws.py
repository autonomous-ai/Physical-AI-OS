"""Exercise the maintenance script with isolated paths and mocked services."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "maintenance/patch-nginx-harness-ws.sh"
ORIGINAL = """server {
  location = /api/buddy/ws {
    proxy_pass http://backend;
  }
}
"""


class HarnessNginxPatchTests(unittest.TestCase):
    def test_backup_restore_and_idempotence(self):
        for layout in ("regular", "relative-link", "absolute-link", "conf.d"):
            for validation_fails in (False, True):
                with self.subTest(layout=layout, validation_fails=validation_fails):
                    self.check_patch(layout, validation_fails)

    def check_patch(self, layout, validation_fails):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nginx = root / "nginx"
            for name in ("sites-enabled", "sites-available", "conf.d"):
                (nginx / name).mkdir(parents=True)
            enabled = nginx / "sites-enabled/default"
            if layout.endswith("link"):
                target = nginx / "sites-available/default"
                link = "../sites-available/default" if layout == "relative-link" else str(target)
                enabled.symlink_to(link)
            else:
                target = nginx / ("conf.d/default.conf" if layout == "conf.d" else "sites-enabled/default")
            target.write_text(ORIGINAL)
            target.chmod(0o640)
            backups = root / "backups"
            reload_log = root / "reload.log"
            binaries = root / "bin"
            binaries.mkdir()
            mocks = {
                "id": "echo 0",
                "nginx": 'exit "$TEST_NGINX_EXIT"',
                "systemctl": 'echo "$*" >> "$TEST_RELOAD_LOG"',
                "hostname": "echo 127.0.0.1",
            }
            for name, body in mocks.items():
                executable = binaries / name
                executable.write_text("#!/bin/sh\n" + body + "\n")
                executable.chmod(0o755)
            # Redirect only filesystem roots; execute the actual script logic.
            script = root / "patch.sh"
            script.write_text(SCRIPT.read_text().replace("/etc/nginx", str(nginx)).replace(
                "/var/backups/nginx-harness-ws", str(backups)))
            env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"],
                       TEST_NGINX_EXIT="1" if validation_fails else "0",
                       TEST_RELOAD_LOG=str(reload_log))

            def run_patch():
                return subprocess.run(["bash", str(script)], env=env,
                                      capture_output=True, text=True, timeout=15)

            result = run_patch()
            self.assertEqual(result.returncode, 1 if validation_fails else 0,
                             result.stdout + result.stderr)
            copies = list(backups.iterdir())
            self.assertEqual(len(copies), 1)
            self.assertFalse(copies[0].is_symlink())
            self.assertEqual(copies[0].read_text(), ORIGINAL)
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            if layout.endswith("link"):
                self.assertTrue(enabled.is_symlink())
                self.assertEqual(os.readlink(enabled), link)
            if validation_fails:
                self.assertEqual(target.read_text(), ORIGINAL)
                self.assertFalse(reload_log.exists())
            else:
                self.assertEqual(target.read_text().count("location = /api/harness/ws"), 1)
                self.assertEqual(reload_log.read_text(), "reload nginx\n")
                result = run_patch()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(list(backups.iterdir()), copies)
                self.assertEqual(reload_log.read_text(), "reload nginx\n")


if __name__ == "__main__":
    unittest.main()
