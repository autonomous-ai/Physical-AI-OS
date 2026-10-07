"""Exercise updater telemetry with a fake curl, never touching host services."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "software-update"


def function(name):
    return re.search(rf"^{name}\(\) \{{\n.*?^\}}$", SCRIPT.read_text(), re.M | re.S)[0]


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "progress" / "hal.json"
        self.source = "\n".join(function(n) for n in (
            "progress_report", "progress_download_sample", "download_verified", "on_exit"))
        self.env = {**os.environ, "PROGRESS_DIR": str(self.root / "progress"),
                    "PROGRESS_RUN_ID": "test-run", "APP": "hal", "PROGRESS_BOOT_ID": "test-boot",
                    "ROLLBACK_DIR": str(self.root / "rollback"), "DEST": str(self.root / "artifact")}
        self.fake_curl = self.root / "curl.py"
        self.fake_curl.write_text('''import os, pathlib, sys, time
args = sys.argv[1:]
headers = pathlib.Path(args[args.index("-D") + 1])
path = pathlib.Path(args[args.index("-o") + 1])
mode = os.environ.get("MODE", "known")
headers.write_text("HTTP/1.1 302 Found\\r\\nContent-Length: 9999\\r\\n\\r\\nHTTP/1.1 200 OK\\r\\n" +
    ("Content-Length: 8\\r\\n" if mode not in ("unknown", "chunked") else "") +
    ("Transfer-Encoding: chunked\\r\\n" if mode == "chunked" else "") + "\\r\\n")
with path.open("wb") as f:
    f.write(b"abcd"); f.flush(); time.sleep(1.4)
    if mode == "failed": sys.exit(22)
    f.write(b"efgh")
''')

    def run_download(self, mode="known", digest=""):
        code = self.source + '\ncurl() { python3 "$FAKE_CURL" "$@"; }\n' + \
            'trap on_exit EXIT\nprogress_report preparing\ndownload_verified test://artifact "$DEST" "$DIGEST"\n'
        process = subprocess.Popen(["/bin/bash", "-e", "-c", code], env={**self.env,
            "MODE": mode, "DIGEST": digest, "FAKE_CURL": str(self.fake_curl)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        samples = []
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            if self.snapshot.exists():
                # Atomic rename must never expose partial JSON to concurrent readers.
                samples.append(json.loads(self.snapshot.read_text()))
            time.sleep(0.05)
        if process.poll() is None:
            process.kill()
            self.fail("updater did not finish")
        _, stderr = process.communicate()
        return process.returncode, samples, json.loads(self.snapshot.read_text()), stderr

    def test_known_length_uses_final_response_and_real_bytes(self):
        code, samples, last, error = self.run_download(digest=hashlib.sha256(b"abcdefgh").hexdigest())
        self.assertEqual(code, 0, error)
        self.assertTrue(any(p["downloaded_bytes"] == 4 and p["total_bytes"] == 8 for p in samples))
        self.assertFalse(any(p["total_bytes"] == 9999 for p in samples))
        self.assertEqual(last["phase"], "completed")
        self.assertEqual(last["boot_id"], "test-boot")

    def test_unknown_and_chunked_length_never_invent_percent(self):
        for mode in ("unknown", "chunked"):
            with self.subTest(mode=mode):
                code, samples, last, error = self.run_download(mode)
                self.assertEqual(code, 0, error)
                downloading = [p for p in samples if p["phase"] == "downloading"]
                self.assertTrue(any(p["downloaded_bytes"] == 4 for p in downloading))
                self.assertTrue(all(p["total_bytes"] == 0 for p in downloading))
                self.assertEqual(last["phase"], "completed")

    def test_download_and_checksum_errors_are_terminal_failures(self):
        for mode, digest in (("failed", ""), ("known", "0" * 64)):
            with self.subTest(mode=mode):
                code, _, last, _ = self.run_download(mode, digest)
                self.assertNotEqual(code, 0)
                self.assertEqual(last["phase"], "failed")
                self.assertIn("failed during", last["message"])

    def test_unwritable_telemetry_does_not_fail_installer(self):
        blocked = self.root / "not-a-directory"
        blocked.write_text("file")
        result = subprocess.run(["/bin/bash", "-e", "-c", self.source + "\nprogress_report installing\n"],
            env={**self.env, "PROGRESS_DIR": str(blocked)}, capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_rollback_does_not_claim_update_succeeded(self):
        self.root.joinpath("rollback").mkdir()
        self.root.joinpath("rollback/pending-update.json").write_text("{}")
        code = self.source + '\nrecover_pending_update() { progress_report restarting; return 0; }\n' + \
            'trap on_exit EXIT\nprogress_report installing\nexit 1\n'
        result = subprocess.run(["/bin/bash", "-e", "-c", code], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(self.snapshot.read_text())["phase"], "failed")


if __name__ == "__main__":
    unittest.main()
