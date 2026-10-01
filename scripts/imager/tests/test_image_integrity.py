"""Exercise corrupted/truncated readback and the builder's publication gate."""

import importlib.util
import lzma
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_flash', ROOT / 'lib/verify_flash.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('compressed', [False, True])
def test_readback_matches_only_image_length(tmp_path, compressed):
    data = b'golden-image' * 1000
    image = tmp_path / ('image.xz' if compressed else 'image.img')
    image.write_bytes(lzma.compress(data) if compressed else data)
    card = tmp_path / 'card'
    card.write_bytes(data + b'unused capacity')
    module.verify(image, card)


@pytest.mark.parametrize('damage', ['mismatch', 'short', 'empty'])
def test_readback_rejects_damage(tmp_path, damage):
    image, card = tmp_path / 'image.img', tmp_path / 'card'
    image.write_bytes(b'' if damage == 'empty' else b'abcdefgh')
    card.write_bytes(b'abXdefgh' if damage == 'mismatch' else b'abc')
    with pytest.raises(ValueError):
        module.verify(image, card)


def test_broken_xz_is_not_accepted(tmp_path):
    image, card = tmp_path / 'image.xz', tmp_path / 'card'
    image.write_bytes(lzma.compress(b'image')[:-10])
    card.write_bytes(b'image')
    with pytest.raises((EOFError, lzma.LZMAError)):
        module.verify(image, card)


@pytest.mark.parametrize('fsck_exit', [0, 1, 2, 4, 8, 16, 32, 128])
def test_resize_precheck_accepts_only_clean_or_corrected(fsck_exit):
    result = subprocess.run(['bash', '-c', f'''set -euo pipefail
source "{ROOT}/lib/check_ext4.sh"
e2fsck() {{ [ "$1" = -fp ] || exit 99; return {fsck_exit}; }}
prepare_ext4_for_resize fake-part
'''], capture_output=True, text=True)
    assert (result.returncode == 0) == (fsck_exit in (0, 1)), result.stderr


@pytest.mark.parametrize('fsck_exit', [0, 1, 2, 4, 8, 16, 32, 128])
def test_final_fsck_prevents_publication_on_failure(tmp_path, fsck_exit):
    # Execute the real finalization fragment with stubbed privileged operations.
    script = (ROOT / 'build-orangepi.sh').read_text()
    fragment = script.split('# Flush + unmount before xz', 1)[1]
    fragment = fragment[fragment.index('\nsync'):].split('# COMPRESS=0', 1)[0]
    output, final = tmp_path / 'image.building', tmp_path / 'image.img'
    output.write_text('new image')
    final.write_text('previous valid image')
    env = dict(os.environ, OUT_IMG=str(output), FINAL_IMG=str(final),
               MNT=str(tmp_path), PART='fake-part', PART_LOOP='fake-part', LOOP_DEV='fake-disk')
    result = subprocess.run(['bash', '-c', f'''set -euo pipefail
source "{ROOT}/lib/check_ext4.sh"
sync() {{ :; }}
umount() {{ :; }}
losetup() {{ :; }}
log() {{ :; }}
e2fsck() {{ return {fsck_exit}; }}
{fragment}
'''], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (fsck_exit == 0), result.stderr
    assert final.read_text() == ('new image' if fsck_exit == 0 else 'previous valid image')


@pytest.mark.parametrize('raw,xz_exit,verify_exit', [
    (True, 0, 0), (True, 0, 1), (False, 0, 0), (False, 0, 1), (False, 2, 0),
])
def test_flash_target_only_succeeds_after_verification(tmp_path, raw, xz_exit, verify_exit):
    # All device-facing commands are replaced; never touch a real disk or sudo.
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'calls'
    commands = {
        'sudo': 'exec "$@"',
        'diskutil': 'echo "diskutil $*" >> "$CALL_LOG"',
        'sync': ':',
        'dd': 'if [ "$RAW_TEST" = 0 ]; then cat >/dev/null; fi',
        'xz': f'printf partial; exit {xz_exit}',
        'python3': f'echo verified >> "$CALL_LOG"; exit {verify_exit}',
    }
    for name, body in commands.items():
        path = bindir / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
    output = tmp_path / 'output/lamp'
    output.mkdir(parents=True)
    (output / 'golden-opi.img').write_bytes(b'image')
    (output / 'golden-opi-lamp.img.xz').write_bytes(b'compressed')
    result = subprocess.run([
        'make', '-f', str(ROOT / 'Makefile'),
        'sd-card-flash-raw' if raw else 'sd-card-flash', 'DEVICE_TYPE=lamp', 'DISK=99',
    ], cwd=tmp_path, env=dict(os.environ, PATH=f'{bindir}:{os.environ["PATH"]}',
                             CALL_LOG=str(log), RAW_TEST=str(int(raw))),
        capture_output=True, text=True)
    success = xz_exit == 0 and verify_exit == 0
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    assert ('Flash complete' in result.stdout) == success
    calls = log.read_text()
    assert ('diskutil eject' in calls) == success
    assert ('verified' in calls) == (xz_exit == 0)
