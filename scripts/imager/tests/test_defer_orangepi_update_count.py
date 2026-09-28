"""Only the vendor's boot-time package-count job should be deferred."""

import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    'defer_count', Path(__file__).resolve().parents[1] / 'lib/defer_orangepi_update_count.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_patch_preserves_daily_job_and_is_idempotent(tmp_path):
    path = tmp_path / 'etc/cron.d/orangepi-updates'
    path.parent.mkdir(parents=True)
    original = ('# vendor tasks\n@reboot root /usr/lib/orangepi/orangepi-apt-updates\n'
                '@daily root /usr/lib/orangepi/orangepi-apt-updates\n')
    path.write_text(original)
    path.chmod(0o640)
    assert module.apply(tmp_path)
    assert path.read_text() == original.replace('@reboot root ', '@reboot root /bin/sleep 120 && ')
    assert path.stat().st_mode & 0o777 == 0o640
    backup = tmp_path / 'var/backups/autonomous/orangepi-updates.before-boot-delay'
    assert backup.read_text() == original
    assert not module.apply(tmp_path)
    assert backup.read_text() == original


def test_missing_vendor_job_is_a_noop(tmp_path):
    assert not module.apply(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_custom_commands_are_untouched(tmp_path):
    path = tmp_path / 'etc/cron.d/orangepi-updates'
    path.parent.mkdir(parents=True)
    original = ('@reboot root /usr/lib/orangepi/orangepi-apt-updates --custom\n'
                '@reboot root /bin/echo untouched\n')
    path.write_text(original)
    assert not module.apply(tmp_path)
    assert path.read_text() == original
