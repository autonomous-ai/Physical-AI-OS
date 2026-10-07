"""UV activity is observational: preserve installer output and exit status."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "software-update"


def function(name):
    return re.search(rf"^{name}\(\) \{{\n.*?^\}}$", SCRIPT.read_text(), re.M | re.S)[0]


class UVActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = '\n'.join(function(n) for n in (
            'progress_report', 'uv_activity_message', 'run_uv_with_activity'))
        self.env = {**os.environ, 'APP': 'hal', 'PROGRESS_RUN_ID': 'test',
                    'PROGRESS_DIR': str(self.root / 'progress')}

    def parse(self, line):
        result = subprocess.run(['/bin/bash', '-c', self.source + '\nuv_activity_message "$LINE"; printf "%s" "$UV_ACTIVITY"'],
                                env={**self.env, 'LINE': line}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_recognized_activity_and_ansi(self):
        for line, expected in [
            ('Downloading numpy (18.2MiB)', 'Downloading numpy'),
            ('  Building package-name==1.2.3', 'Building package-name'),
            ('\x1b[32mBuilt package_name==1.2\x1b[0m\r', 'Built package_name'),
            ('Prepared 12 packages in 3s', 'Prepared 12 packages'),
            ('Installed 1 package in 50ms', 'Installed 1 package'),
            (' + numpy==2.0.0', 'Installed numpy'),
        ]:
            with self.subTest(line=line):
                self.assertEqual(self.parse(line), expected)

    def test_unknown_private_logs_are_not_forwarded_to_ui(self):
        for line in ('Using index https://user:password@private.example/simple',
                     'Downloading https://user:password@example/pkg',
                     'Building /private/worktree', 'a future uv output format'):
            self.assertEqual(self.parse(line), '')

    def run_uv(self, code, failed_writer=False):
        command = self.root / 'uv'
        command.write_text('#!/bin/bash\nprintf "Downloading numpy (18MiB)\\n"\nprintf "private log\\n" >&2\nexit ' + str(code))
        command.chmod(0o755)
        source = self.source
        if failed_writer:
            source += '\nprogress_report() { return 1; }\n'
        result = subprocess.run(['/bin/bash', '-e', '-c', source + '\nrun_uv_with_activity "$UV" || exit $?'],
                                env={**self.env, 'UV': str(command)}, capture_output=True, text=True)
        return result

    def test_output_and_failure_status_preserved(self):
        for code in (0, 7):
            with self.subTest(code=code):
                result = self.run_uv(code)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn('private log', result.stdout)
                record = json.loads((self.root / 'progress/hal.json').read_text())
                self.assertEqual(record['message'], 'Downloading numpy')
                self.assertGreater(record['activity_at'], 0)
                self.assertEqual(record['phase'], 'installing')
                self.assertEqual(record['total_bytes'], 0)

    def test_telemetry_failure_never_masks_uv_status(self):
        for code in (0, 9):
            result = self.run_uv(code, failed_writer=True)
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertIn('private log', result.stdout)


if __name__ == '__main__':
    unittest.main()
