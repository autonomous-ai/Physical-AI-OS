"""Run real e2fsprogs against a disposable sparse file, without root or disks."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ('mkfs.ext4', 'debugfs', 'e2fsck', 'resize2fs')


@unittest.skipUnless(
    sys.platform == 'linux' and all(shutil.which(tool) for tool in TOOLS),
    'Requires Linux e2fsprogs tools; runs without privileged devices',
)
class Ext4ResizeIntegrationTest(unittest.TestCase):
    def test_writable_precheck_makes_clean_but_unfinalized_ext4_resizable(self):
        with tempfile.TemporaryDirectory() as directory:
            self.check_resize(Path(directory))

    def check_resize(self, tmp_path):
        image = tmp_path / 'filesystem.img'
        env = dict(os.environ, LC_ALL='C')

        def run(*args, check=True):
            return subprocess.run(args, check=check, capture_output=True, text=True, env=env)

        with image.open('wb') as stream:
            stream.truncate(32 * 1024 * 1024)
        run('mkfs.ext4', '-q', '-F', str(image))
        # Model an otherwise consistent filesystem whose clean state was not saved.
        run('debugfs', '-w', '-R', 'set_super_value state 0', str(image))
        with image.open('r+b') as stream:
            stream.truncate(64 * 1024 * 1024)

        # Read-only validation succeeds but cannot update the state resize2fs needs.
        run('e2fsck', '-fn', str(image))
        refused = run('resize2fs', str(image), check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('Please run', refused.stderr)
        self.assertIn('e2fsck -f', refused.stderr)

        run('bash', '-c', 'source "$1"; prepare_ext4_for_resize "$2"',
            'test', str(ROOT / 'lib/check_ext4.sh'), str(image))
        run('resize2fs', str(image))
        run('e2fsck', '-fn', str(image))


if __name__ == "__main__":
    unittest.main()
