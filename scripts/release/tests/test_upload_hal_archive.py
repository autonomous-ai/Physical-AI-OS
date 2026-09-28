"""Exercise the release ZIP command without publishing or touching real secrets."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'upload-hal.sh'


class HALArchiveTests(unittest.TestCase):
    def test_archive_includes_lock_and_excludes_local_runtime(self):
        source = SCRIPT.read_text()
        start = source.index('(cd "$HAL_DIR" && zip')
        command = source[start:source.index('\n\n', start)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hal = root / 'hal'
            hal.mkdir()
            for name in ('uv.lock', 'pyproject.toml', '.env', '.python-version',
                         '.venv/secret', 'test/test_example.py', 'cache/__pycache__/x.pyc'):
                path = hal / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            archive = root / 'hal.zip'
            subprocess.run(['bash', '-c', command], check=True, capture_output=True,
                           env={**os.environ, 'HAL_DIR': str(hal), 'ZIP_PATH': str(archive)})
            with zipfile.ZipFile(archive) as zipped:
                files = {name for name in zipped.namelist() if not name.endswith('/')}
            self.assertEqual(files, {'uv.lock', 'pyproject.toml'})


if __name__ == '__main__':
    unittest.main()
